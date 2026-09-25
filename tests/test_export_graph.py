"""Eval §14.1 — the canonical RSF graph export (`arch export --graph`).

What is locked down here:

  * **the determinism contract** — same facts → byte-identical `graph-<g>.rsf` /
    `clusters-<g>.rsf` / `graph-meta-<g>.json` (the artifact is the single input handed
    to every SAR baseline, eval §6.4 — if it isn't stable, the fairness control isn't);
  * **first-party-only by default** — external/package targets and any edge touching
    them are dropped *and counted* in the meta sidecar, never silently;
  * **the fairness asymmetry** — the dependency graph comes from EXTRACTED facts
    (pre-curation), the clusters file from CURATED facts (`container_id` grouping);
  * **file granularity** (eval §4.1, the primary RQ2 run) — merged from the per-file
    sidecars the C++ extractors emit; pipeline clusters induced file → target →
    container; unattributed/uncurated files counted as unclustered, never guessed;
  * **honest abstention** — file granularity without sidecars raises
    `ExportGraphError`; a missing fact model returns None, never an empty artifact.

Unit tests run on a hand-built minimal fact model; one end-to-end test runs the CLI
over the shared toy pipeline.
"""
from __future__ import annotations

import pytest

from conftest import run_toy_pipeline

from anon import cli, export_graph, filedeps
from anon.jsonio import dump_json, load_json
from anon.paths import resolve_workspace


# ------------------------------------------------------------------ minimal fixture

def _facts(targets, relationships):
    return {
        "schema_version": "1.0",
        "provenance": {"repo": "t", "commit": "c", "generated_at": "now", "extractors": []},
        "targets": targets,
        "relationships": relationships,
    }


def _rel(src, tgt):
    return {
        "id": f"rel:{src}->{tgt}",
        "source": src,
        "target": tgt,
        "evidence": [{"type": "project_ref"}],
    }


A = "csharp:csproj:src/A/A.csproj"
B = "csharp:csproj:src/B/B.csproj"
C = "csharp:csproj:src/C/C.csproj"
PKG = "csharp:package:Newtonsoft.Json"


@pytest.fixture()
def ws(tmp_path):
    w = resolve_workspace(tmp_path / "repo", tmp_path / "arch")
    targets = [
        {"id": A, "name": "A", "type": "csproj", "language": "csharp"},
        {"id": B, "name": "B", "type": "csproj", "language": "csharp"},
        {"id": C, "name": "C", "type": "csproj", "language": "csharp"},
        {"id": PKG, "name": "Newtonsoft.Json", "type": "package", "language": "csharp",
         "external": True},
    ]
    rels = [
        _rel(B, A),
        _rel(C, A),
        _rel(B, PKG),               # external endpoint — dropped by default
        _rel(A, A),                 # self-loop — dropped
        _rel(C, "csharp:csproj:gone.csproj"),  # unknown endpoint — dropped
    ]
    dump_json(_facts(targets, rels), w.extracted_facts)
    curated = _facts(
        [
            {"id": A, "name": "A", "type": "csproj", "language": "csharp",
             "container_id": "container:core", "container_name": "Core"},
            {"id": B, "name": "B", "type": "csproj", "language": "csharp",
             "container_id": "container:core", "container_name": "Core"},
            # C has no container_id — must fall back to a singleton cluster
            {"id": C, "name": "C", "type": "csproj", "language": "csharp"},
        ],
        [_rel(B, A)],
    )
    dump_json(curated, w.curated_facts)
    return w


# ----------------------------------------------------------- target-granularity core

def test_depend_tuples_first_party_only(ws):
    res = export_graph.run(ws)
    lines = ws.graph_rsf("target").read_text(encoding="utf-8").splitlines()
    assert lines == sorted(lines)
    assert lines == [f"depend {B} {A}", f"depend {C} {A}"]
    g = res["meta"]["graph"]
    assert g["nodes"] == 3            # A, B, C — the package is out
    assert g["edges"] == 2
    assert g["isolated_nodes"] == 0
    # the three bad edges are counted, never silently dropped
    assert g["dropped"] == {"excluded_endpoint": 2, "self_loop": 1}


def test_clusters_from_curated_container_id(ws):
    res = export_graph.run(ws)
    lines = ws.clusters_rsf("target").read_text(encoding="utf-8").splitlines()
    assert f"contain container:core {A}" in lines
    assert f"contain container:core {B}" in lines
    assert f"contain {C} {C}" in lines        # singleton fallback for the ungrouped target
    c = res["meta"]["clusters"]
    assert c == {"clusters": 2, "members": 3, "singleton_fallbacks": 1,
                 "unclustered_graph_nodes": 0}


def test_include_external_keeps_package_edges(ws):
    res = export_graph.run(ws, include_external=True)
    lines = ws.graph_rsf("target").read_text(encoding="utf-8").splitlines()
    assert f"depend {B} {PKG}" in lines
    assert res["meta"]["graph"]["nodes"] == 4
    # the unknown endpoint + self-loop still drop
    assert res["meta"]["graph"]["dropped"] == {"excluded_endpoint": 1, "self_loop": 1}


def test_two_runs_are_byte_identical(ws):
    export_graph.run(ws)
    paths = (ws.graph_rsf("target"), ws.clusters_rsf("target"), ws.graph_meta("target"))
    first = {p: p.read_bytes() for p in paths}
    export_graph.run(ws)
    for p, content in first.items():
        assert p.read_bytes() == content


def test_meta_records_source_hashes(ws):
    from anon.model import content_hash
    res = export_graph.run(ws)
    meta = load_json(ws.graph_meta("target"))
    assert meta == res["meta"]
    assert meta["schema"] == export_graph.GRAPH_SCHEMA
    assert meta["derived_from"]["extracted_facts_hash"] == \
        content_hash(load_json(ws.extracted_facts))
    assert meta["derived_from"]["curated_facts_hash"] == \
        content_hash(load_json(ws.curated_facts))


def test_entity_with_whitespace_is_quoted():
    spacey = "cpp:target:My Lib"
    facts = _facts(
        [{"id": A, "name": "A", "type": "csproj", "language": "csharp"},
         {"id": spacey, "name": "My Lib", "type": "static_lib", "language": "cpp"}],
        [_rel(A, spacey)],
    )
    lines, _stats = export_graph.depend_lines(facts)
    assert lines == [f'depend {A} "{spacey}"']


# -------------------------------------------------------------- file granularity

F_A_CPP = "src/A/a.cpp"
F_A_H = "src/A/a.h"
F_B_H = "src/B/b.h"
F_ORPHAN = "src/misc/orphan.h"     # target attribution failed
F_PKG = "src/pkg/p.cs"             # attributed to a curation-excluded target


@pytest.fixture()
def ws_file(ws):
    sidecar = filedeps.make_sidecar(
        {"name": "doxygen-xml", "version": "0.1.0"}, "cpp",
        files={F_A_CPP: A, F_A_H: A, F_B_H: B, F_ORPHAN: None, F_PKG: PKG},
        edges={(F_A_CPP, F_A_H), (F_A_CPP, F_B_H)},
    )
    filedeps.write_sidecar(ws, sidecar)
    return ws


def test_file_graph_from_sidecar(ws_file):
    res = export_graph.run(ws_file, granularity="file")
    lines = ws_file.graph_rsf("file").read_text(encoding="utf-8").splitlines()
    assert lines == [f"depend {F_A_CPP} {F_A_H}", f"depend {F_A_CPP} {F_B_H}"]
    g = res["meta"]["graph"]
    assert g["nodes"] == 5
    assert g["edges"] == 2
    assert g["isolated_nodes"] == 2   # orphan + pkg file
    assert g["attribution_conflicts"] == 0
    assert g["sidecars"] == ["doxygen-xml"]


def test_file_clusters_induced_via_target(ws_file):
    """Eval §4.1: the pipeline's per-file cluster is file → build-target → container."""
    res = export_graph.run(ws_file, granularity="file")
    lines = ws_file.clusters_rsf("file").read_text(encoding="utf-8").splitlines()
    assert f"contain container:core {F_A_CPP}" in lines
    assert f"contain container:core {F_A_H}" in lines
    assert f"contain container:core {F_B_H}" in lines
    assert len(lines) == 3
    c = res["meta"]["clusters"]
    # the orphan (no target) and the package file (target excluded by curation)
    # are REPORTED unclustered, never guessed into a cluster
    assert c["unclustered"] == {"unattributed_file": 1, "target_not_in_curated": 1}


def test_file_export_is_byte_identical(ws_file):
    export_graph.run(ws_file, granularity="file")
    paths = (ws_file.graph_rsf("file"), ws_file.clusters_rsf("file"),
             ws_file.graph_meta("file"))
    first = {p: p.read_bytes() for p in paths}
    export_graph.run(ws_file, granularity="file")
    for p, content in first.items():
        assert p.read_bytes() == content


def test_sidecar_merge_counts_attribution_conflicts(ws_file):
    # a second sidecar disagrees on F_A_H's owner and fills in the orphan's owner;
    # sorted-filename precedence: clang-scan-deps < doxygen-xml, so clang wins F_A_H
    clang = filedeps.make_sidecar(
        {"name": "clang-scan-deps", "version": "0.1.0"}, "cpp",
        files={F_A_H: B, F_ORPHAN: C},
        edges={(F_A_CPP, F_A_H)},   # duplicate edge — must not double
    )
    filedeps.write_sidecar(ws_file, clang)
    res = export_graph.run(ws_file, granularity="file")
    g = res["meta"]["graph"]
    assert g["sidecars"] == ["clang-scan-deps", "doxygen-xml"]
    assert g["attribution_conflicts"] == 1
    assert g["edges"] == 2            # duplicate edge de-duplicated
    # orphan now attributed (filled from clang) → clusters as C's singleton
    lines = ws_file.clusters_rsf("file").read_text(encoding="utf-8").splitlines()
    assert f"contain {C} {F_ORPHAN}" in lines
    # F_A_H follows clang's claim (B) — still container:core, so line unchanged
    assert f"contain container:core {F_A_H}" in lines


# ----------------------------------------------------------------- honest abstention

def test_file_granularity_without_sidecars_fails_honestly(ws):
    with pytest.raises(export_graph.ExportGraphError,
                       match="no per-file dependency sidecars"):
        export_graph.run(ws, granularity="file")


def test_unknown_granularity_is_an_error(ws):
    with pytest.raises(export_graph.ExportGraphError, match="unknown granularity"):
        export_graph.run(ws, granularity="class")


def test_missing_fact_model_returns_none(tmp_path):
    w = resolve_workspace(tmp_path / "repo", tmp_path / "empty-arch")
    assert export_graph.run(w) is None
    assert export_graph.run(w, granularity="file") is None


def test_missing_curated_skips_clusters(ws):
    ws.curated_facts.unlink()
    res = export_graph.run(ws)
    assert res["clusters"] is None
    assert res["meta"]["clusters"] is None
    assert res["meta"]["derived_from"]["curated_facts_hash"] is None
    assert ws.graph_rsf("target").exists()
    assert not ws.clusters_rsf("target").exists()


# ------------------------------------------------------------------------ end-to-end

def test_cli_export_graph_on_toy(tmp_path_factory, capsys):
    tr = run_toy_pipeline(tmp_path_factory.mktemp("graphrun") / "architecture")
    ws = tr.ws
    rc = cli.main(["export", "--repo", str(ws.repo), "--arch-dir", str(ws.arch_dir),
                   "--rules-dir", str(ws.rules_dir), "--graph"])
    assert rc == 0
    out = capsys.readouterr().out
    # the [anon …] prefix carries a wall-clock timestamp — match around it
    assert "] graph (target):" in out and "[anon " in out
    assert "] clusters:" in out
    graph = ws.graph_rsf("target").read_text(encoding="utf-8").splitlines()
    assert graph and all(line.startswith("depend ") for line in graph)
    clusters = ws.clusters_rsf("target").read_text(encoding="utf-8").splitlines()
    assert clusters and all(line.startswith("contain ") for line in clusters)
    # The graph is pre-curation, the decomposition post-curation: curation may
    # legitimately exclude nodes (the toy's test project), and that coverage gap is
    # REPORTED in the sidecar, never discovered downstream (eval §14.1 honesty).
    members = {line.split()[2] for line in clusters}
    nodes = {tok for line in graph for tok in line.split()[1:]}
    meta = load_json(ws.graph_meta("target"))
    assert meta["clusters"]["unclustered_graph_nodes"] == len(nodes - members)
