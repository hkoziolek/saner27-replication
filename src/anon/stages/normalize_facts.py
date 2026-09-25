"""Stage 2 — normalize/merge fact fragments into ``extracted-facts.json`` (plan §6.4).

Multiple extractors emit fragments for the same element/edge; this merges them
**deterministically** so the §18.4 byte-identical / idempotent-regeneration guarantee
holds. Merge rules (plan §6.4):

  (a) targets merge on ``id``; relationships on ``(source, target)`` — ``kind`` is NOT
      in identity (it is curated later).
  (b) evidence concatenated, de-duped by ``(type, detail)``, ``count`` summed; weight /
      is_declared / confidence / evidence_strength **recomputed** from merged evidence
      (never first-wins).
  (c) scalar conflicts (``type`` / ``path`` / ``language`` / ``external``) resolved by
      extractor priority (build-graph outranks include/symbol); ``namespaces`` unioned;
      residual genuine conflicts recorded in ``conflicts[]`` and surfaced, never dropped.
  (d) deterministic output ordering — any fragment order -> byte-identical JSON.

Child-tier merging (§6.0 / §6.3): the Roslyn helper (Agent A) emits a ``components[]``
namespace tier (and optional ``code[]`` class tier) split across fragments. These are
merged by child id (union), and the per-target per-layer ``provenance.coverage`` map is
unioned per layer (for partial "n/m files" descriptors the max-coverage one wins, §6.4 /
§11). Children and conflicts are sorted here because :func:`model.canonicalize` only
orders the top-level target/relationship/evidence lists.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..confidence import _LAYER_OF
from ..ids import rel_id
from ..jsonio import dump_json, load_json
from ..model import (canonicalize, compute_confidence, compute_weight,
                     evidence_strength_str, is_declared)
from ..paths import Workspace

# Evidence-layer rank for the `--max-evidence-layer` ceiling (eval §7 A1). Mirrors the
# confidence._LAYER_OF strata: interop is a declared cross-language seam (L0 stratum), runtime the
# weakest (L3). The ceiling answers "what does the model look like with only the build graph (L0),
# or +declared refs (L1), or +symbol/include evidence (L2)?" — the build-graph-first ablation.
_LAYER_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "interop": 0, "runtime": 3}
EVIDENCE_LAYERS = ("L0", "L1", "L2", "L3")

# Extractor-priority for scalar-conflict resolution (§6.4c). Higher wins. The plan's
# rule is: the **build-graph extractor outranks** the include/symbol (refine-layer)
# extractor for ``type`` / ``path`` / ``language`` / ``external``. We pin known
# extractor names here so the resolution is reproducible and reviewable rather than
# implicit; any unknown extractor defaults to ``_DEFAULT_PRIORITY`` (below the build
# graph but able to seed a field no one else supplied).
#   build-graph tier (100): the authoritative target graph (§4.1 CMake / §4.2 .sln/csproj)
#   refine tier      (50) : L2/L3 symbol/include extractors that refine, never replace (§4)
_PRIORITY = {
    "csharp-build-graph": 100,  # §4.2 .sln + .csproj L0 graph (extract_build_graph.py)
    "cmake-graph": 100,         # §4.1 CMake File-API C++ L0 graph (extract_cmake.py)
    "msbuild-cpp": 100,         # §4.1/§16#1 MSBuild/VS C++ .vcxproj L0 graph (extract_msbuild_cpp.py)
    "roslyn-csharp": 50,        # §4.2 L2 Roslyn semantic model (extract_csharp_facts.py)
    "doxygen-xml": 50,          # §4.1 L2/L3 Doxygen XML C++ refine (extract_cpp_facts.py)
    "clang-scan-deps": 50,      # §4.1 L2 clang include graph C++ refine (extract_clang_deps.py)
    "runtime-topology": 40,     # §5 runtime evidence (extract_runtime.py) — deployment-facet only
    "shared-contracts": 30,     # §16#14 contract elements/edges (extract_contracts.py)
}
_DEFAULT_PRIORITY = 10

# Scalar fields resolved by extractor priority (§6.4c). ``namespaces`` is unioned, not
# priority-resolved, and is handled separately below.
_SCALAR_FIELDS = ("type", "path", "language", "external")
# List fields whose values are unioned across fragments (set-like merge, §6.4c).
# ``com_provides`` (§16#15 / 5b) is a set-like list of captured COM identities; unioning it
# means the msbuild-cpp fragment's capture survives even when an interop fragment (which knows
# no identities) created the cpp:vcxproj stub first.
_UNION_LIST_FIELDS = ("namespaces", "tags", "depends_on", "responsibilities", "com_provides",
                      "package_refs")


def _frag_priority(fragment: dict[str, Any]) -> int:
    names = [e.get("name", "") for e in fragment.get("provenance", {}).get("extractors", [])]
    return max((_PRIORITY.get(n, _DEFAULT_PRIORITY) for n in names), default=_DEFAULT_PRIORITY)


def load_fragments(ws: Workspace) -> list[dict[str, Any]]:
    if not ws.fragments.exists():
        return []
    return [load_json(p) for p in sorted(ws.fragments.glob("*.json"))]


def _merge_evidence(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Concatenate + de-dupe by (type, detail); sum count (§6.4b).

    Order-independent: keyed by (type, detail), so the merged set is identical no matter
    which fragment arrives first. ``canonicalize`` later sorts the result for output.
    """
    by_key: dict[tuple, dict] = {}
    for ev in [*existing, *incoming]:
        key = (ev.get("type"), ev.get("detail"))
        if key in by_key:
            cur = by_key[key]
            if "count" in ev or "count" in cur:
                cur["count"] = (cur.get("count", 0) or 0) + (ev.get("count", 0) or 0)
            # keep visibility if any fragment supplied it (don't let a later None clobber)
            if "visibility" in ev and "visibility" not in cur:
                cur["visibility"] = ev["visibility"]
        else:
            by_key[key] = dict(ev)
    return list(by_key.values())


def _record_conflict(target: dict[str, Any], field: str, kept: Any, dropped: Any,
                     winner_prio: int, loser_prio: int) -> None:
    """Record a residual genuine scalar conflict (§6.4c) — never silently dropped."""
    target.setdefault("conflicts", []).append({
        "field": field, "kept": kept, "dropped": dropped,
        "winner_priority": winner_prio, "loser_priority": loser_prio,
    })


def _coverage_rank(value: Any) -> tuple[int, int]:
    """Rank a single per-layer coverage descriptor so the *most* coverage wins on merge.

    Layers may be ``True``/``False`` or a partial string like ``"18/20 files"`` (§6.2).
    Returns a comparable ``(tier, numerator)`` key: a fully-true layer beats any partial,
    a partial beats false/absent, and among partials the larger numerator wins (keep the
    max-coverage descriptor, per the task). Unparseable strings rank just above false.
    """
    if value is True:
        return (3, 0)
    if isinstance(value, str):
        head = value.strip().split("/", 1)[0].strip()
        try:
            return (2, int(head))
        except ValueError:
            return (1, 0)
    return (0, 0)  # False / None / anything else


def _merge_coverage(into: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Union per-target, per-layer coverage; keep the max-coverage descriptor per layer."""
    for tid, layers in (incoming or {}).items():
        dest = into.setdefault(tid, {})
        for layer, value in (layers or {}).items():
            if layer not in dest or _coverage_rank(value) > _coverage_rank(dest[layer]):
                dest[layer] = value


def _merge_code(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Merge ``code[]`` (class tier) by child id; union tags. Sorted by id (§6.4d)."""
    by_id: dict[str, dict] = {}
    for code in [*(existing or []), *(incoming or [])]:
        cid = code.get("id")
        if cid not in by_id:
            by_id[cid] = dict(code)
        else:
            cur = by_id[cid]
            for k, v in code.items():
                if k == "tags":
                    cur["tags"] = sorted({*cur.get("tags", []), *(v or [])})
                elif k not in cur or cur.get(k) is None:
                    cur[k] = v
    return sorted(by_id.values(), key=lambda c: c.get("id", ""))


def _merge_components(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Merge ``components[]`` (namespace tier) by child id, recursing into ``code[]``.

    Union semantics: a component present in either fragment survives; its ``code[]``
    children are merged by code id. Sorted by id so output is fragment-order-independent.
    """
    by_id: dict[str, dict] = {}
    for comp in [*(existing or []), *(incoming or [])]:
        cid = comp.get("id")
        if cid not in by_id:
            merged = dict(comp)
            if merged.get("code"):
                merged["code"] = _merge_code([], merged["code"])
            by_id[cid] = merged
            continue
        cur = by_id[cid]
        for k, v in comp.items():
            if k == "code":
                cur["code"] = _merge_code(cur.get("code", []), v or [])
            elif k == "tags":
                cur["tags"] = sorted({*cur.get("tags", []), *(v or [])})
            elif k not in cur or cur.get(k) is None:
                cur[k] = v
    return sorted(by_id.values(), key=lambda c: c.get("id", ""))


def _merge_target(cur: dict[str, Any], t: dict[str, Any], prio: int,
                  cur_prio: int) -> None:
    """Merge incoming target *t* (priority *prio*) into existing *cur* (priority *cur_prio*)."""
    # scalar conflicts: higher-priority fragment wins; residual genuine conflicts recorded
    for field in _SCALAR_FIELDS:
        if field in t and t[field] != cur.get(field):
            if prio > cur_prio:
                if field in cur:
                    _record_conflict(cur, field, t[field], cur[field], prio, cur_prio)
                cur[field] = t[field]
            elif cur.get(field) is not None and t[field] is not None:
                if prio == cur_prio and field == "external":
                    # PRINCIPLED equal-priority policy for `external` (§6.4c): when two
                    # equal-authority build-graph extractors disagree on whether a target is
                    # external, resolve to external=False (internal) ON PURPOSE — first-party
                    # evidence wins. If anyone in the build graph actually *builds* this target,
                    # it is part of the system, not a third-party dependency; the alternative
                    # (lexicographic "False" < "True") only landed on False by accident, and
                    # `external` gates the drop_external curation step that decides whether the
                    # element survives, so the resolution must be intentional, not arbitrary.
                    # Output value stays False (== prior lexicographic result), keeping goldens
                    # byte-identical; the conflict is still recorded, never silently dropped.
                    if cur[field] is not False:
                        _record_conflict(cur, field, False, cur[field], prio, cur_prio)
                        cur[field] = False
                    else:
                        _record_conflict(cur, field, cur[field], t[field], cur_prio, prio)
                elif prio == cur_prio and str(t[field]) < str(cur[field]):
                    # equal-priority conflict (type/path/language): pick a stable winner
                    # (lexicographically smaller) so any fragment order yields byte-identical
                    # output (§6.4d). Only `external` gets the principled policy above.
                    _record_conflict(cur, field, t[field], cur[field], prio, cur_prio)
                    cur[field] = t[field]
                else:
                    _record_conflict(cur, field, cur[field], t[field], cur_prio, prio)
    # namespaces / tags / depends_on / responsibilities unioned (§6.4c)
    for field in _UNION_LIST_FIELDS:
        if t.get(field):
            cur[field] = sorted({*cur.get(field, []), *t[field]})
    # metrics: fill in any field the existing target lacks (build graph rarely has them)
    if t.get("metrics"):
        merged_metrics = dict(t["metrics"])
        merged_metrics.update(cur.get("metrics", {}))  # existing wins on conflict
        cur["metrics"] = merged_metrics
    # child tiers (§6.0): merge components[] (and their code[]) by child id
    if t.get("components") or cur.get("components"):
        merged_components = _merge_components(cur.get("components", []), t.get("components", []))
        if merged_components:
            cur["components"] = merged_components


def _merge_deployment(into: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Union the §4.4 deployment facet across fragments (1.1).

    Nodes merge by id (fill-null + tag-union, like targets); edges merge by
    ``(source, target, kind)`` with evidence concatenated/de-duped (like build edges).
    Order-independent so the §18.4 byte-identical guarantee holds for deployment too.
    """
    nodes: dict[str, dict] = {n.get("id"): dict(n) for n in into.get("nodes", [])}
    for n in incoming.get("nodes", []) or []:
        nid = n.get("id")
        if nid not in nodes:
            nodes[nid] = dict(n)
            continue
        cur = nodes[nid]
        for k, v in n.items():
            if k == "tags":
                cur["tags"] = sorted({*cur.get("tags", []), *(v or [])})
            elif k == "ports":
                cur["ports"] = sorted({*cur.get("ports", []), *(v or [])})
            elif k not in cur or cur.get(k) is None:
                cur[k] = v
    edges: dict[tuple, dict] = {}
    for e in [*into.get("edges", []), *(incoming.get("edges", []) or [])]:
        key = (e.get("source"), e.get("target"), e.get("kind"))
        if key not in edges:
            edges[key] = dict(e)
            edges[key]["evidence"] = list(e.get("evidence", []))
        else:
            edges[key]["evidence"] = _merge_evidence(edges[key]["evidence"], e.get("evidence", []))
    into["nodes"] = list(nodes.values())
    into["edges"] = list(edges.values())


def merge(fragments: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge fragments into one fact model (without runner-stamped provenance)."""
    targets: dict[str, dict[str, Any]] = {}
    target_prio: dict[str, int] = {}
    rels: dict[tuple[str, str], dict[str, Any]] = {}
    extractors: list[dict] = []
    coverage: dict[str, Any] = {}
    deployment: dict[str, Any] = {"nodes": [], "edges": []}

    for frag in fragments:
        prio = _frag_priority(frag)
        prov = frag.get("provenance", {})
        for ex in prov.get("extractors", []):
            if ex not in extractors:
                extractors.append(ex)
        _merge_coverage(coverage, prov.get("coverage", {}) or {})
        if frag.get("deployment"):
            _merge_deployment(deployment, frag["deployment"])

        for t in frag.get("targets", []):
            tid = t["id"]
            if tid not in targets:
                merged = dict(t)
                if merged.get("components"):
                    merged["components"] = _merge_components([], merged["components"])
                targets[tid] = merged
                target_prio[tid] = prio
                continue
            _merge_target(targets[tid], t, prio, target_prio[tid])
            if prio > target_prio[tid]:
                target_prio[tid] = prio

        for r in frag.get("relationships", []):
            key = (r["source"], r["target"])
            if key not in rels:
                entry: dict[str, Any] = {
                    "id": r["id"], "source": r["source"], "target": r["target"],
                    "evidence": list(r.get("evidence", [])),
                }
                if r.get("kind"):
                    entry["kind"] = r["kind"]
                # carry curation tags (e.g. suspicious-declared) if any fragment supplies them
                if r.get("tags"):
                    entry["tags"] = sorted(set(r["tags"]))
                rels[key] = entry
            else:
                rels[key]["evidence"] = _merge_evidence(rels[key]["evidence"], r.get("evidence", []))
                if r.get("kind") and "kind" not in rels[key]:  # kind NOT in identity (§6.4a)
                    rels[key]["kind"] = r["kind"]
                if r.get("tags"):
                    rels[key]["tags"] = sorted({*rels[key].get("tags", []), *r["tags"]})

    # recompute derived fields from MERGED evidence (§6.4b) — never first-fragment-wins
    for r in rels.values():
        ev = r["evidence"]
        r["weight"] = compute_weight(ev)
        r["is_declared_dependency"] = is_declared(ev)
        r["confidence"] = compute_confidence(ev)
        r["evidence_strength"] = evidence_strength_str(ev)

    # sort the conflict ledger deterministically (canonicalize doesn't reach into targets)
    for t in targets.values():
        if t.get("conflicts"):
            t["conflicts"] = sorted(
                t["conflicts"],
                key=lambda c: (c.get("field", ""), str(c.get("kept", "")), str(c.get("dropped", ""))),
            )

    # sort extractors deterministically so fragment-arrival order can't leak into output
    # (§6.4d byte-identical guarantee). Keyed by (name, version).
    extractors_sorted = sorted(extractors, key=lambda e: (e.get("name", ""), e.get("version", "")))

    facts = {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": extractors_sorted, "coverage": coverage},
        "targets": list(targets.values()),
        "relationships": list(rels.values()),
    }
    # attach the deployment facet only when a fragment supplied one (§4.4 — absent on a
    # build-only run, so single-language/no-artifact runs stay byte-identical to 1.0 shape).
    if deployment["nodes"] or deployment["edges"]:
        deployment["nodes"].sort(key=lambda n: n.get("id", ""))
        deployment["edges"].sort(key=lambda e: (e.get("source", ""), e.get("target", ""), e.get("kind", "")))
        for e in deployment["edges"]:
            if e.get("evidence"):
                e["evidence"] = sorted(e["evidence"], key=lambda v: (v.get("type", ""), v.get("detail", "")))
        facts["deployment"] = deployment
    return canonicalize(facts)


def rebind_contract_dirs(facts: dict[str, Any]) -> dict[str, Any]:
    """Post-merge: rebind contract ``dir:<path>`` placeholder endpoints to real target ids.

    The contract extractor (``extract_contracts.py``) cannot resolve the authoritative
    build-graph id of a referencing source file when no enclosing project marker is found,
    so it emits a ``dir:<repo-relative-dir>`` **placeholder** endpoint (§16#14). Those
    placeholders are not declared targets, so the extract-stage referential-integrity gate
    (``validate_facts.check_referential_integrity``, run by ``cmd_extract`` *before* curate)
    would reject the whole model and abort ``arch run``.

    This runs **post-merge** — once every fragment's targets are visible — and mirrors how
    :mod:`interop_resolve` rebinds ``native:lib:*`` to real ``cpp:target:*`` after merge. It
    uses the *same* matching rule as curate's ``apply_mapping_rules._rebind`` (most specific /
    longest target ``path`` wins; ``dir:D`` rebinds to target id ``tid`` iff ``D == path`` or
    ``D.startswith(path + "/")``). An endpoint that still cannot be resolved cannot join the
    real graph, so its relationship is **dropped** (counted, never silently lost) and the count
    surfaced in ``provenance.contract_dirs_dropped``.

    Deterministic and order-independent: the path index is sorted, and the rebuilt
    relationship list preserves the original order minus drops.

    After this pass there are NO ``dir:`` endpoints left, so curate's own ``_rebind`` becomes
    a no-op on already-resolved ids.
    """
    rels = facts.get("relationships")
    if not rels:
        return facts
    # Path index: most specific (longest) target path wins, mirroring curate's _rebind.
    path_index = sorted(
        ((t["path"], t["id"]) for t in facts.get("targets", []) if t.get("path")),
        key=lambda pi: len(pi[0]), reverse=True,
    )

    def _resolve(endpoint: str) -> str | None:
        if not endpoint.startswith("dir:"):
            return endpoint
        d = endpoint[4:]
        for path, tid in path_index:
            if d == path or d.startswith(path + "/"):
                return tid
        return None  # unresolvable dir: placeholder -> drop the edge

    # Dedup by (source, target): two distinct ``dir:`` placeholders can rebind to the SAME real
    # edge. Mirror interop_resolve/combine — recompute ``id`` when endpoints change (§6.4a: the
    # id must track source/target, not the stale ``rel:dir:…``) and merge a collapsed edge's
    # evidence into the first, keeping its derived fields so the §21.3 contract confidence pin
    # survives. canonicalize() (run after this pass) re-sorts evidence + relationships.
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    dropped = 0
    for r in rels:
        src = _resolve(r["source"])
        tgt = _resolve(r["target"])
        if src is None or tgt is None:
            dropped += 1
            continue
        if src != r["source"] or tgt != r["target"]:
            r = dict(r)
            r["source"], r["target"] = src, tgt
            r["id"] = rel_id(src, tgt)
        key = (src, tgt)
        if key in merged:
            prev = merged[key]
            prev["evidence"] = _merge_evidence(prev.get("evidence", []), r.get("evidence", []))
            if r.get("tags"):
                prev["tags"] = sorted({*prev.get("tags", []), *r["tags"]})
        else:
            merged[key] = r
            order.append(key)

    facts["relationships"] = [merged[k] for k in order]
    # Record only when > 0 so contract-less repos stay byte-identical (no new key on zero).
    if dropped:
        facts.setdefault("provenance", {})["contract_dirs_dropped"] = dropped
    return facts


def _metrics_exclude(ws: Workspace) -> list[str]:
    """The `metrics.exclude` knob in mapping-rules.yaml (datasheet plan §3.2) — extra
    fnmatch globs ADDED to the built-in defaults (never replacing them)."""
    from ..config import load_yaml
    raw = (load_yaml(ws.mapping_rules).get("metrics", {}) or {}).get("exclude", []) or []
    return [str(g) for g in raw if g]


def apply_evidence_ceiling(facts: dict[str, Any], max_layer: str | None) -> dict[str, Any]:
    """Return *facts* as if only evidence up to *max_layer* (one of ``EVIDENCE_LAYERS``) existed —
    the evaluation-plan §7 **A1** build-graph-first ablation, as a faithful engine knob rather than
    the post-hoc ``baselines/rq3_ablation.py`` approximation.

    Each relationship's evidence is filtered to the ceiling; a relationship left with no surviving
    evidence is dropped (the edge would not exist at that layer); the survivors' derived fields
    (``weight`` / ``is_declared_dependency`` / ``confidence`` / ``evidence_strength``) are
    recomputed from the kept evidence — single-sourced through ``model`` so a layer-restricted run
    is identical to an extraction that never saw the higher layers. Targets are NOT dropped (build
    targets are L0 entities by construction); their L2+ symbol refinements (namespaces/components/
    code) are left intact — a documented non-goal, since A1 is scored on container *edges*, where
    only the relationship set moves.

    ``max_layer=None`` (the default) is a strict no-op, so a normal run stays byte-identical and the
    determinism goldens are untouched.
    """
    if not max_layer:
        return facts
    ceiling = _LAYER_RANK.get(max_layer)
    if ceiling is None:
        return facts
    kept = []
    for r in facts.get("relationships", []):
        ev = [e for e in r.get("evidence", [])
              if _LAYER_RANK.get(_LAYER_OF.get(e.get("type")), 99) <= ceiling]
        if not ev:
            continue
        r = dict(r)
        r["evidence"] = ev
        r["weight"] = compute_weight(ev)
        r["is_declared_dependency"] = is_declared(ev)
        r["confidence"] = compute_confidence(ev)
        r["evidence_strength"] = evidence_strength_str(ev)
        kept.append(r)
    facts = dict(facts)
    facts["relationships"] = kept
    prov = dict(facts.get("provenance", {}))
    prov["max_evidence_layer"] = max_layer
    facts["provenance"] = prov
    return canonicalize(facts)


def run(ws: Workspace, repo: str = "", commit: str = "", generated_at: str = "",
        max_evidence_layer: str | None = None) -> dict[str, Any]:
    """Merge all fragments, resolve interop, stamp runner provenance, write the facts.

    *max_evidence_layer* (``"L0".."L3"`` or None) applies the §7-A1 evidence ceiling post-merge —
    None (default) is a no-op.
    """
    from .. import interop_resolve, metrics as source_metrics

    facts = merge(load_fragments(ws))
    # §7-A1 evidence ceiling (no-op unless --max-evidence-layer set). Applied right after merge so
    # interop resolution, metrics, and curation all see the layer-restricted edge set.
    facts = apply_evidence_ceiling(facts, max_evidence_layer)
    # Source metrics (datasheet-enrichment plan §3): annotate targets[].metrics with the
    # deterministic metrics/1 line counts. Post-merge ON PURPOSE — the deepest-owner file
    # attribution needs every fragment's target paths (same placement rationale as
    # interop_resolve below). Class-F facts: deterministic per commit, in the hashed core.
    source_metrics.annotate(facts, ws.repo, exclude=_metrics_exclude(ws))
    # Rebind shared-contract dir:<path> placeholder endpoints to real build-target ids now that
    # every fragment's targets are merged+visible (§16#14). Unresolvable placeholders can't join
    # the real graph, so the edge is pruned here — BEFORE cmd_extract's referential-integrity
    # gate — instead of crashing the run. Runs pre-canonicalize since it rewrites/drops edges.
    facts = rebind_contract_dirs(facts)
    # Resolve P/Invoke native:lib:* boundaries to real cpp:target:* libs when both sides are
    # present (§16#15). Deterministic, conservative (unambiguous single match only), no-op on a
    # single-language model. Re-canonicalize since it may drop/rewrite targets+edges.
    interop = interop_resolve.resolve(facts)
    facts = canonicalize(facts)
    facts["provenance"].update({
        "repo": repo or ws.repo.name,
        "commit": commit or "unknown",
        "generated_at": generated_at or "1970-01-01T00:00:00Z",
    })
    if interop.get("resolved"):
        facts["provenance"]["interop_resolved"] = interop["resolved"]
    dump_json(facts, ws.extracted_facts)
    return facts


if __name__ == "__main__":  # pragma: no cover
    import argparse

    from ..paths import resolve_workspace

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir)
    facts = run(w, repo=Path(args.repo).name)
    print(f"{len(facts['targets'])} targets, {len(facts['relationships'])} relationships")
