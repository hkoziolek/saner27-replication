"""Eval-plan §14.1 — the canonical dependency-graph export (``arch export --graph``).

The empirical evaluation's fairness principle (eval plan §6.4) is that every
clustering/recovery technique — the pipeline itself, the SAR baselines (ACDC/Bunch/
ARC/WCA via ARCADE), the naïve and the controlled-LLM baselines — consumes ONE
canonical exported dependency graph, so the input is held constant and only the
technique varies. This module emits that artifact in **Rigi Standard Format** (RSF),
the lingua franca of the SAR tooling, at both eval §4.1 granularities::

    generated/graph/graph-target.rsf      depend  <source-id> <target-id>
    generated/graph/clusters-target.rsf   contain <container-id> <member-target-id>
    generated/graph/graph-file.rsf        depend  <src-file> <dst-file>
    generated/graph/clusters-file.rsf     contain <container-id> <file>
    generated/graph/graph-meta-<g>.json   provenance sidecar (hashes, counts, drops)

**Target granularity** is exported from ``extracted-facts.json`` (pre-curation — the
generator-independent contract, P§6.1): baselines must see the same evidence the
pipeline's curation consumes, so *curation* stays the technique under test. The
clusters file (the pipeline's own decomposition — eval §14.2 ``clusters.rsf``) comes
from ``curated-facts.json``, whose ``container_id`` grouping IS the pipeline's answer;
an ungrouped curated target clusters as its own singleton (matching
``build_containers``'s shape-independent grouping). External targets are excluded by
default (``include_external=False``): SAR ground truths partition first-party code
only, and on a raw C# extraction the package_ref edges to NuGet boundary targets
outnumber first-party edges ~20:1. Edges with an excluded/unknown endpoint and
self-loops are dropped and *counted* in the meta sidecar, never silently.

**File granularity** (the eval's *primary* RQ2 run — SAR ground truths map source
files → components) is merged from the per-file sidecars the C++ extractors emit
(:mod:`anon.filedeps`: Doxygen include graph, clang-scan-deps). The pipeline's
per-file cluster is *induced* via ``file → build-target → container`` (eval §4.1)
from the curated ``container_id``; files whose target attribution failed or whose
target curation excluded are counted as unclustered, never guessed. No sidecars (no
Doxygen/clang ran, or a C#-only repo — the Roslyn helper does not yet emit
per-document references) → :class:`ExportGraphError` naming the gap instead of a
plausible-but-empty file.

Determinism: tuple lines are de-duplicated and sorted, files written UTF-8/LF — same
facts → byte-identical RSF (the same committed-artifact contract as everything else;
locked by ``tests/test_export_graph.py``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import filedeps
from .jsonio import dump_json, load_json
from .model import content_hash
from .paths import Workspace

GRAPH_SCHEMA = "graph-export/1"


class ExportGraphError(Exception):
    """A graph-export request this workspace cannot honestly satisfy."""


def _rsf_entity(entity: str) -> str:
    """RSF tuples are whitespace-delimited; quote the rare id with embedded whitespace."""
    return f'"{entity}"' if any(c.isspace() for c in entity) else entity


def _included_ids(facts: dict[str, Any], include_external: bool) -> set[str]:
    return {
        t["id"]
        for t in facts.get("targets", [])
        if include_external or not t.get("external", False)
    }


def _graph_stats(nodes: set[str], edges: set[tuple[str, str]],
                 dropped: dict[str, int]) -> dict[str, Any]:
    connected = {n for e in edges for n in e}
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "isolated_nodes": len(nodes - connected),
        "dropped": dropped,
    }


def _depend_lines(edges: set[tuple[str, str]]) -> list[str]:
    return sorted(f"depend {_rsf_entity(a)} {_rsf_entity(b)}" for a, b in edges)


def _contain_lines(members: set[tuple[str, str]]) -> list[str]:
    return sorted(f"contain {_rsf_entity(c)} {_rsf_entity(m)}" for c, m in members)


def depend_lines(facts: dict[str, Any], include_external: bool = False
                 ) -> tuple[list[str], dict[str, Any]]:
    """``depend a b`` tuples for the target-granularity dependency graph + count stats."""
    nodes = _included_ids(facts, include_external)
    edges: set[tuple[str, str]] = set()
    dropped = {"excluded_endpoint": 0, "self_loop": 0}
    for rel in facts.get("relationships", []):
        src, tgt = rel["source"], rel["target"]
        if src not in nodes or tgt not in nodes:
            # external (when excluded) or a non-target endpoint (component/code tier)
            dropped["excluded_endpoint"] += 1
            continue
        if src == tgt:
            dropped["self_loop"] += 1
            continue
        edges.add((src, tgt))
    return _depend_lines(edges), _graph_stats(nodes, edges, dropped)


def contain_lines(curated: dict[str, Any], include_external: bool = False
                  ) -> tuple[list[str], dict[str, Any]]:
    """``contain cluster member`` tuples — the pipeline's curated decomposition."""
    members: set[tuple[str, str]] = set()
    singletons = 0
    for t in curated.get("targets", []):
        if t.get("external", False) and not include_external:
            continue
        cluster = t.get("container_id")
        if not cluster:
            cluster = t["id"]  # ungrouped target = its own singleton cluster
            singletons += 1
        members.add((cluster, t["id"]))
    stats = {
        "clusters": len({c for c, _ in members}),
        "members": len(members),
        "singleton_fallbacks": singletons,
    }
    return _contain_lines(members), stats


def _container_of(curated: dict[str, Any], include_external: bool) -> dict[str, str]:
    """target id → cluster id under the same rules as :func:`contain_lines`."""
    out: dict[str, str] = {}
    for t in curated.get("targets", []):
        if t.get("external", False) and not include_external:
            continue
        out[t["id"]] = t.get("container_id") or t["id"]
    return out


def file_contain_lines(files: dict[str, str | None], curated: dict[str, Any],
                       include_external: bool = False
                       ) -> tuple[list[str], dict[str, Any]]:
    """Induce the pipeline's per-file decomposition: file → build-target → container
    (eval §4.1). Unattributed files and files of curation-excluded targets are counted
    as unclustered — reported, never guessed into a cluster."""
    target_cluster = _container_of(curated, include_external)
    members: set[tuple[str, str]] = set()
    unattributed = 0
    target_not_curated = 0
    for path, tid in files.items():
        if tid is None:
            unattributed += 1
            continue
        cluster = target_cluster.get(tid)
        if cluster is None:
            target_not_curated += 1
            continue
        members.add((cluster, path))
    stats = {
        "clusters": len({c for c, _ in members}),
        "members": len(members),
        "unclustered": {"unattributed_file": unattributed,
                        "target_not_in_curated": target_not_curated},
    }
    return _contain_lines(members), stats


def _run_target(ws: Workspace, include_external: bool) -> dict[str, Any] | None:
    if not ws.extracted_facts.exists():
        return None
    extracted = load_json(ws.extracted_facts)
    graph, graph_stats = depend_lines(extracted, include_external)
    _write_rsf(graph, ws.graph_rsf("target"))

    clusters_path: Path | None = None
    clusters_stats: dict[str, Any] | None = None
    curated_hash: str | None = None
    if ws.curated_facts.exists():
        curated = load_json(ws.curated_facts)
        clusters, clusters_stats = contain_lines(curated, include_external)
        clusters_path = ws.clusters_rsf("target")
        _write_rsf(clusters, clusters_path)
        curated_hash = content_hash(curated)
        # Curation may legitimately exclude graph nodes (test projects, exclusions) —
        # the fidelity comparison must know how many entities the decomposition does
        # not cover, so the gap is recorded here, never discovered downstream.
        clustered = _included_ids(curated, include_external)
        graph_nodes = _included_ids(extracted, include_external)
        clusters_stats["unclustered_graph_nodes"] = len(graph_nodes - clustered)

    return {
        "graph_lines_path": ws.graph_rsf("target"),
        "clusters_path": clusters_path,
        "graph_stats": graph_stats,
        "clusters_stats": clusters_stats,
        "extracted_hash": content_hash(extracted),
        "curated_hash": curated_hash,
    }


def _run_file(ws: Workspace, include_external: bool) -> dict[str, Any] | None:
    if not ws.extracted_facts.exists():
        return None
    sidecars = filedeps.load_sidecars(ws)
    if not sidecars:
        raise ExportGraphError(
            "no per-file dependency sidecars under "
            f"{ws.fragments} (file-deps-*.json). The C++ extractors emit them when "
            "Doxygen or clang-scan-deps runs (re-run `arch extract`/`arch run`); the "
            "C# Roslyn helper does not emit per-document references yet (open eval "
            "work item) — a C#-only workspace has no file-granularity graph."
        )
    files, edges, conflicts = filedeps.merge(sidecars)
    # an edge endpoint is always in `files` by sidecar construction; guard anyway so a
    # hand-edited sidecar can't smuggle in unknown nodes
    dropped = {"unknown_endpoint": 0}
    kept: set[tuple[str, str]] = set()
    for a, b in edges:
        if a in files and b in files:
            kept.add((a, b))
        else:
            dropped["unknown_endpoint"] += 1
    graph_stats = _graph_stats(set(files), kept, dropped)
    graph_stats["attribution_conflicts"] = conflicts
    graph_stats["sidecars"] = [sc["extractor"]["name"] for sc in sidecars]
    _write_rsf(_depend_lines(kept), ws.graph_rsf("file"))

    clusters_path: Path | None = None
    clusters_stats: dict[str, Any] | None = None
    curated_hash: str | None = None
    if ws.curated_facts.exists():
        curated = load_json(ws.curated_facts)
        lines, clusters_stats = file_contain_lines(files, curated, include_external)
        clusters_path = ws.clusters_rsf("file")
        _write_rsf(lines, clusters_path)
        curated_hash = content_hash(curated)

    return {
        "clusters_path": clusters_path,
        "graph_stats": graph_stats,
        "clusters_stats": clusters_stats,
        "extracted_hash": content_hash(load_json(ws.extracted_facts)),
        "curated_hash": curated_hash,
    }


def _write_rsf(lines: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the artifact is byte-identical on Windows and Linux (plan §22.3)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))


def run(ws: Workspace, granularity: str = "target", include_external: bool = False
        ) -> dict[str, Any] | None:
    """Export the canonical RSF graph at *granularity* (+ the pipeline clusters file,
    when curated facts exist) and the provenance sidecar. Returns ``None`` when there
    is no fact model; raises :class:`ExportGraphError` for an unsatisfiable request."""
    if granularity == "target":
        res = _run_target(ws, include_external)
    elif granularity == "file":
        res = _run_file(ws, include_external)
    else:
        raise ExportGraphError(f"unknown granularity '{granularity}' (target|file)")
    if res is None:
        return None

    meta = {
        "schema": GRAPH_SCHEMA,
        "granularity": granularity,
        "include_external": include_external,
        "derived_from": {
            "extracted_facts_hash": res["extracted_hash"],
            "curated_facts_hash": res["curated_hash"],
        },
        "graph": res["graph_stats"],
        "clusters": res["clusters_stats"],
    }
    dump_json(meta, ws.graph_meta(granularity))
    return {"graph": ws.graph_rsf(granularity), "clusters": res["clusters_path"],
            "meta": meta}
