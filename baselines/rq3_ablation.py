"""Eval §7 RQ3 — the load-bearing design-choice ablations A1 / A3 / A4 / A5.

A2 (curation heuristics) and A8 (stability/churn) already have their own runners
(``a2_ablation.py`` / ``stability_churn.py``). This is the **scaffold for the four
ablations the §18 MPE still needs** — the ones that test the central build-graph-first
thesis (notes ``2026-06-13-four-systems-fidelity.md`` / ``2026-06-14-a2-curation-ablation.md``):

  * **A1** — evidence layers L0 only → L0+L1 → +L2 → +L3 (P§2 #2 build-graph-first, P§5
    multi-evidence corroboration). Expected: more evidence → ≥ fidelity / richer edge set.
  * **A3** — significance threshold ``min_relationship_weight`` swept {0,1,3,5,8}
    (P§7.4). Expected: an inverted-U that identifies the operating point.
  * **A4** — visibility down-weighting (PUBLIC/PRIVATE) on/off (P§6.2/P§7.4
    ``down_weight_private``). Expected: on → cleaner container edges.
  * **A5** — the **declared-deps-never-dropped** rule on/off (P§2 #2 / P§7.4 — the
    load-bearing inversion). Expected: off should visibly drop strong declared
    ``link`` / ``<ProjectReference>`` edges a scalar threshold would otherwise cut.

WHAT THIS MEASURES, AND WHY IT IS AN EDGE ABLATION
--------------------------------------------------
A3/A4/A5 change which **edges survive curation**, not which entities land in which
container — so MoJoFM/a2a (partition metrics) barely move; the right metric is the
**container→container edge set** (P§1.1 #4 "container-edge precision/recall/F1"). We have
no committed *reference* edge set yet (the references are partitions), so the primary,
fully-computable RQ3 number here is **edge-set sensitivity vs the full pipeline config**
(the plan's own framing: "Δ versus the full configuration"): for each toggle, how many
container edges appear/disappear relative to the shipping defaults
(``min_relationship_weight=3``, ``down_weight_private=True``, declared-never-dropped on, all
layers). A1 additionally reports per-layer **coverage** (entities/edges retained), the
build-graph-first story.

The edge-survival predicate is replicated **verbatim** from the engine
(``stages/apply_mapping_rules.py`` lines ~675–703) so the toggles match a real ``arch run``;
weight/declared recomputation for A1 reuses ``model.compute_weight`` / ``model.is_declared``
(single-sourced, never re-implemented).

HONEST SCAFFOLD CAVEATS (read before quoting numbers)
-----------------------------------------------------
1. **A1 here is the cheap post-hoc approximation; the faithful path now exists in the engine.**
   This runner filters the already-merged ``extracted-facts.json`` by evidence layer — it
   reproduces the edge/weight effect from committed facts with no re-extraction, but at the fact
   model's altitude (build targets are L0 entities, so they never disappear; only the relationship
   set moves). The FAITHFUL A1 is now ``arch run --max-evidence-layer {L0,L1,L2,L3}``
   (``normalize_facts.apply_evidence_ceiling``, landed 2026-06-20) — re-run per layer + export +
   re-cluster for numbers that also reflect any entity/refinement loss. The two agree on the
   container-edge set at target granularity (verified on the toy fixture); this runner stays the
   cheap default because the faithful path needs the per-layer toolchain re-runs (Doxygen, etc.).
2. **A4/A5 vs an independent reference.** Scored here only as Δ-vs-full. Reference-edge
   precision/recall/F1 needs a curated reference *edge* set (induce it from the reference
   partition + the canonical graph: clusters C1→C2 iff some entity in C1 depends on one in
   C2). That induction is documented in ``induced_reference_edges`` but kept OFF the headline
   until the rule is pre-registered (§12) — it conflates "all edges" with "reference edges."
3. **Contract edges.** The engine also never-drops shared-contract edges; that tag is added
   *during* curation, so the pre-curation facts this runner reads don't carry it. Folded into
   the declared-exempt class here (a small over-count on systems with contract edges).

Usage::

    python baselines/rq3_ablation.py                 # all configured systems, all axes
    python baselines/rq3_ablation.py itk eshop       # a subset
    python baselines/rq3_ablation.py --axes a3,a5    # a subset of axes

Output: a per-axis table to stdout + ``out/<sys>/baseline/rq3-ablation.json``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from run_arcade import read_contains, read_depends  # noqa: E402  (baselines/ on sys.path[0])

from anon import model  # noqa: E402  (editable install)
from anon.confidence import _LAYER_OF  # noqa: E402  single-source the evidence ladder

REPO = Path(__file__).resolve().parent.parent

# system -> (language, fidelity granularity). The edge ablation always lifts the TARGET-level
# relationships (where weight/is_declared live) to the curated containers, so it runs at
# target/container altitude for every system regardless of the fidelity granularity.
SYSTEMS = {
    "itk": ("cpp", "file"),
    "abseil": ("cpp", "file"),
    "opencv": ("cpp", "file"),
    "eshop": ("cs", "target"),
    "orchardcore": ("cs", "target"),
    "nopcommerce": ("cs", "target"),
    "bash": ("cpp", "file"),      # autotools L0 via extract_autotools (was the limitation case)
    "libxml2": ("cpp", "file"),   # autotools/automake; monolithic-library contrast case
}
# opencv's representative curation is the module-granularity overlay (mirrors a2_ablation).
CURATION_ARCH = {"opencv": "opencv-mod"}

# Engine constants replicated from stages/apply_mapping_rules.py (kept in sync by the §7.2
# "frozen rules" discipline; PRIVATE_DOWN_WEIGHT is pinned there for reproducibility).
PRIVATE_DOWN_WEIGHT = 2
DEFAULT_MIN_WEIGHT = 3            # config.DEFAULTS["min_relationship_weight"]

# Cumulative evidence-layer rank for A1. interop is a declared cross-language seam (L0-stratum);
# runtime is the weakest (L3-stratum) — mirrors confidence._LAYER_OF's strata.
_LAYER_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "interop": 0, "runtime": 3}
A1_LAYERS = [("L0", 0), ("L0+L1", 1), ("+L2", 2), ("+L3", 3)]   # cumulative ceilings

A3_THRESHOLDS = [0, 1, 3, 5, 8]   # the §7.1 sweep; 3 = the shipping default (the full config)


# ---------------------------------------------------------------- edge-survival predicate

def _eff_weight(rel: dict, *, down_weight_private: bool) -> int:
    """The effective weight curation thresholds on (apply_mapping_rules.py ~676–686).

    PRIVATE-visibility evidence de-emphasizes a NON-declared edge by PRIVATE_DOWN_WEIGHT each
    when down_weight_private is on; declared edges keep their weight (only flagged)."""
    weight = int(rel.get("weight", 0))
    if down_weight_private and not rel.get("is_declared_dependency"):
        priv = sum(1 for e in rel.get("evidence", []) if e.get("visibility") == "private")
        if priv:
            weight = max(0, weight - PRIVATE_DOWN_WEIGHT * priv)
    return weight


def _survives(rel: dict, *, min_weight: int, declared_exempt: bool,
              down_weight_private: bool) -> bool:
    """Replicates the engine keep/drop decision (apply_mapping_rules.py ~693–703).

    A5 is ``declared_exempt``: with it ON a declared dep below threshold is KEPT (flagged
    ``suspicious-declared``); with it OFF a declared dep is thresholded like any other edge —
    the ablation that should visibly drop strong ``link`` / ``<ProjectReference>`` edges."""
    declared = bool(rel.get("is_declared_dependency"))
    weight = _eff_weight(rel, down_weight_private=down_weight_private)
    if declared and declared_exempt:
        return True                       # never weight-dropped (the load-bearing rule)
    return weight >= min_weight


def _layer_filtered(rel: dict, max_rank: int) -> dict | None:
    """A1: keep only evidence at layer rank ≤ max_rank, recompute weight + declared from the
    survivors (model.compute_weight / model.is_declared — single-sourced). None if no evidence
    survives (the edge would not exist at that evidence ceiling)."""
    ev = [e for e in rel.get("evidence", [])
          if _LAYER_RANK.get(_LAYER_OF.get(e.get("type")), 99) <= max_rank]
    if not ev:
        return None
    return {**rel, "evidence": ev, "weight": model.compute_weight(ev),
            "is_declared_dependency": model.is_declared(ev)}


# ---------------------------------------------------------------- edge-set construction

def container_edges(facts: dict, cmap: dict[str, str], predicate, *,
                    layer_rank: int | None = None) -> set[tuple[str, str]]:
    """Surviving relationships lifted to container→container edges (self-loops dropped).

    ``predicate(rel) -> bool`` decides edge survival; ``layer_rank`` (A1) first filters each
    relationship's evidence to that cumulative ceiling and recomputes its weight/declared."""
    edges: set[tuple[str, str]] = set()
    for r in facts.get("relationships", []):
        if layer_rank is not None:
            r = _layer_filtered(r, layer_rank)
            if r is None:
                continue
        if not predicate(r):
            continue
        cs, ct = cmap.get(r.get("source")), cmap.get(r.get("target"))
        if cs is None or ct is None or cs == ct:
            continue                      # endpoint excluded/external, or intra-container
        edges.add((cs, ct))
    return edges


def induced_reference_edges(system: str, gran: str) -> set[tuple[str, str]] | None:
    """The reference's container→container connectivity, induced from the reference partition +
    the canonical dependency graph — clusters C1→C2 iff some entity in C1 depends on an entity in
    C2. This is the edge set against which a *true* RQ3 edge precision/recall/F1 is scored.

    PRE-REGISTERED RULE (§12, fixed 2026-07-16, closes the "reference edges vs all graph edges"
    ambiguity in the eval plan): the reference edge set is exactly the transitive-free lift of the
    canonical `graph-<gran>.rsf` dependency edges through the reference partition `reference.rsf`
    (self-loops dropped). Both the reference edges AND the toggle-recovered edges are lifted through
    the SAME reference partition (§10a.4 "pure-detection probe" — nodes given by the reference, edges
    drawn by evidence), so the metric isolates edge DETECTION and is not curation-authorship-confounded."""
    ref = REPO / "references" / system / "reference.rsf"
    graph = REPO / "out" / system / "architecture" / "generated" / "graph" / f"graph-{gran}.rsf"
    if not ref.exists() or not graph.exists():
        return None
    cluster = read_contains(ref)
    edges: set[tuple[str, str]] = set()
    for u, v in read_depends(graph):
        cu, cv = cluster.get(u), cluster.get(v)
        if cu is not None and cv is not None and cu != cv:
            edges.add((cu, cv))
    return edges


def reference_partition(system: str, facts: dict) -> dict[str, str] | None:
    """The reference `entity -> cluster` map, but ONLY when its entities are the build TARGETS the
    A3/A4/A5 toggles operate on (C# csproj references: entity == target id == graph node). For
    file-granularity C++ references the entities are source files, so the target-level toggles here
    cannot be scored against them without the file-deps sidecar — we return None and skip the
    reference metric for that system (honest boundary, recorded in the run output as
    ``reference_scored: false``). Gate: ≥50% of the facts' targets must appear in the reference."""
    ref = REPO / "references" / system / "reference.rsf"
    if not ref.exists():
        return None
    cmap = read_contains(ref)
    target_ids = {t["id"] for t in facts.get("targets", [])}
    if not target_ids or not cmap:
        return None
    # The reference covers only the FIRST-PARTY targets (a subset of extracted-facts, which also
    # carries contracts/external/test targets), so gate on how much of the REFERENCE keys on real
    # targets — not on total-target coverage. File-granularity C++ references (entities == source
    # files, none of which are target ids) score ~0 here and are correctly skipped.
    matched = sum(1 for ent in cmap if ent in target_ids)
    return cmap if matched >= 0.5 * len(cmap) else None


# ---------------------------------------------------------------- metrics

def prf(recovered: set, reference: set) -> dict[str, float]:
    """Precision/recall/F1 of ``recovered`` against ``reference`` (here: the full-config edge
    set), plus the raw set deltas the §15 ablation tornado consumes."""
    tp = len(recovered & reference)
    prec = tp / len(recovered) if recovered else (1.0 if not reference else 0.0)
    rec = tp / len(reference) if reference else (1.0 if not recovered else 0.0)
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    union = recovered | reference
    return {"edges": len(recovered), "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4), "added": len(recovered - reference),
            "removed": len(reference - recovered),
            "jaccard": round(len(recovered & reference) / len(union), 4) if union else 1.0}


# ---------------------------------------------------------------- per-system driver

def load_inputs(system: str, gran: str):
    """(extracted-facts dict, target->container map). The map is the curated clusters export
    (opencv via its module-granularity overlay); targets absent from it are excluded edges."""
    arch = REPO / "out" / system / "architecture"
    facts_path = arch / "generated" / "extracted-facts.json"
    if not facts_path.exists():
        return None, None
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    # Prefer the system's fidelity curation (opencv -> opencv-mod), but the edge ablation lifts
    # TARGET-level edges, so fall back to the system's own target-granularity clusters when the
    # override only exported a finer granularity (opencv-mod is module-gran; abseil is file-gran).
    cmap = None
    for arch_id in (CURATION_ARCH.get(system, system), system):
        clusters = REPO / "out" / arch_id / "architecture" / "generated" / "graph" / "clusters-target.rsf"
        if clusters.exists():
            cmap = read_contains(clusters)
            break
    return facts, cmap


def _ref_metrics(facts: dict, ref_cmap: dict | None, ref_edges: set | None, pred) -> dict:
    """Edge precision/recall/F1 of the toggle-recovered edges (lifted through the REFERENCE
    partition) vs the induced reference edge set — the §12-pre-registered fidelity metric. Empty
    (all keys absent) when the system has no target-matching reference (C++ file-gran → skipped)."""
    if ref_cmap is None or ref_edges is None:
        return {}
    rec = container_edges(facts, ref_cmap, pred)
    p = prf(rec, ref_edges)
    return {"f1_ref": p["f1"], "precision_ref": p["precision"], "recall_ref": p["recall"],
            "edges_ref": p["edges"]}


def run_axes(system: str, facts: dict, cmap: dict[str, str], axes: set[str],
             gran: str = "target") -> dict:
    """Compute every requested axis as edge-set deltas vs the full config, AND — where the system
    has a target-level reference — the §12-pre-registered edge P/R/F1 vs the reference edge set."""
    full_pred = lambda r: _survives(r, min_weight=DEFAULT_MIN_WEIGHT,  # noqa: E731
                                    declared_exempt=True, down_weight_private=True)
    full = container_edges(facts, cmap, full_pred)
    ref_cmap = reference_partition(system, facts)
    ref_edges = induced_reference_edges(system, gran) if ref_cmap is not None else None
    out: dict = {"full_config_edges": len(full),
                 "reference_scored": ref_cmap is not None and ref_edges is not None,
                 "reference_edges": len(ref_edges) if ref_edges is not None else None,
                 "axes": {}}

    if "a1" in axes:
        out["axes"]["A1_evidence_layers"] = {
            label: {**prf(container_edges(facts, cmap, full_pred, layer_rank=rank), full),
                    # A1 coverage: relationships that retain ≥1 evidence item at this ceiling.
                    "rels_retained": sum(1 for r in facts.get("relationships", [])
                                         if _layer_filtered(r, rank) is not None)}
            for label, rank in A1_LAYERS}

    if "a3" in axes:
        out["axes"]["A3_min_weight"] = {
            str(t): {**prf(container_edges(facts, cmap, p3(t)), full),
                     **_ref_metrics(facts, ref_cmap, ref_edges, p3(t))}
            for t in A3_THRESHOLDS}

    if "a4" in axes:
        out["axes"]["A4_down_weight_private"] = {
            ("on" if dw else "off"): {**prf(container_edges(facts, cmap, p4(dw)), full),
                                      **_ref_metrics(facts, ref_cmap, ref_edges, p4(dw))}
            for dw in (True, False)}

    if "a5" in axes:
        # A5 only binds ABOVE the declared base weight (model.DECLARED_BASE=5) — at the default
        # threshold 3 every declared edge clears the bar anyway, so the rule is inert there. The
        # load-bearing probe is the SWEEP: at each threshold, how many container edges does the
        # never-drop exemption save (edges_saved = |exempt-on| − |exempt-off|)? The thesis (P§2
        # #2) predicts a growing gap as the threshold rises past the declared base. The vs-reference
        # metric makes it a FIDELITY number: turning the rule OFF drops the reference edge RECALL.
        a5: dict = {}
        for t in A3_THRESHOLDS:
            on = container_edges(facts, cmap, p5(t, True))
            off = container_edges(facts, cmap, p5(t, False))
            a5[f"t{t}"] = {**prf(off, on), "edges_saved_by_rule": len(on) - len(off),
                          # recall vs the reference with the rule ON vs OFF (the fidelity cost)
                          **{f"{k}_on": v for k, v in
                             _ref_metrics(facts, ref_cmap, ref_edges, p5(t, True)).items()},
                          **{f"{k}_off": v for k, v in
                             _ref_metrics(facts, ref_cmap, ref_edges, p5(t, False)).items()}}
        out["axes"]["A5_declared_never_dropped"] = a5
    return out


# Predicate factories (pull the toggle lambdas out of run_axes so the vs-full and vs-reference
# passes share ONE definition — the survival rule is never written twice).
def p3(t):
    return lambda r: _survives(r, min_weight=t, declared_exempt=True, down_weight_private=True)


def p4(dw):
    return lambda r: _survives(r, min_weight=DEFAULT_MIN_WEIGHT, declared_exempt=True,
                               down_weight_private=dw)


def p5(t, exempt):
    return lambda r: _survives(r, min_weight=t, declared_exempt=exempt, down_weight_private=True)


def run_system(system: str, axes: set[str]) -> dict | None:
    lang, gran = SYSTEMS[system]
    facts, cmap = load_inputs(system, gran)
    if facts is None or not cmap:
        print(f"  {system}: missing extracted-facts or clusters-target export — run "
              f"`arch run`+`arch export --graph` first; skipping", file=sys.stderr)
        return None
    res = run_axes(system, facts, cmap, axes, gran)
    res.update({"system": system, "language": lang, "granularity": "target"})
    (REPO / "out" / system / "baseline" / "rq3-ablation.json").write_text(
        json.dumps(res, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
    return res


# ---------------------------------------------------------------- reporting

def _print_axis(title: str, systems_res: dict[str, dict], axis_key: str, col_key: str):
    """One block per axis: rows = systems, cols = the axis's rungs, cell = f1 vs full config."""
    cols, rows = [], {}
    for sysname, res in systems_res.items():
        block = res.get("axes", {}).get(axis_key)
        if not block:
            continue
        cols = list(block)
        rows[sysname] = block
    if not rows:
        return
    print(f"\n## {title}  (cell = F1 vs full config / edges)")
    print(f"{'system':13} " + " ".join(f"{c:>13}" for c in cols))
    for sysname, block in rows.items():
        cells = [f"{block[c]['f1']:>5}/{block[c]['edges']:<4}" if c in block else f"{'—':>10}"
                 for c in cols]
        print(f"{sysname:13} " + " ".join(f"{x:>13}" for x in cells))


def _print_a5(systems_res: dict[str, dict]):
    """A5 is reported as edges *saved* by the never-drop rule at each threshold — the count of
    strong declared edges a scalar threshold would have cut. The thesis predicts this grows once
    the threshold passes the declared base weight (5)."""
    rows = {s: r["axes"]["A5_declared_never_dropped"]
            for s, r in systems_res.items() if "A5_declared_never_dropped" in r.get("axes", {})}
    if not rows:
        return
    cols = [f"t{t}" for t in A3_THRESHOLDS]
    print("\n## A5 declared-deps-never-dropped (the load-bearing inversion)  "
          "(cell = container edges SAVED by the rule, per min_weight threshold)")
    print(f"{'system':13} " + " ".join(f"{c:>8}" for c in cols))
    for s, block in rows.items():
        cells = [f"{block[c]['edges_saved_by_rule']:>8}" if c in block else f"{'—':>8}" for c in cols]
        print(f"{s:13} " + " ".join(cells))
    print("(0 at t≤5 is expected — declared edges clear the bar; a positive, growing count at "
          "t≥5/8 is the rule doing its job: keeping strong declared deps a threshold would drop.)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("systems", nargs="*", default=list(SYSTEMS),
                    help="systems to ablate (default: all configured)")
    ap.add_argument("--axes", default="a1,a3,a4,a5",
                    help="comma list of axes to run (default: all four)")
    args = ap.parse_args(argv)
    axes = {a.strip().lower() for a in args.axes.split(",") if a.strip()}

    results: dict[str, dict] = {}
    for s in (args.systems or list(SYSTEMS)):
        if s not in SYSTEMS:
            print(f"  {s}: not a configured fidelity system — skipping", file=sys.stderr)
            continue
        r = run_system(s, axes)
        if r:
            results[s] = r
    if not results:
        print("no systems produced results — see notes above.", file=sys.stderr)
        return 1

    if "a1" in axes:
        _print_axis("A1 evidence layers (build-graph-first; edge/coverage sensitivity)",
                    results, "A1_evidence_layers", "f1")
    if "a3" in axes:
        _print_axis("A3 min_relationship_weight sweep (operating point)",
                    results, "A3_min_weight", "f1")
    if "a4" in axes:
        _print_axis("A4 down_weight_private on/off", results, "A4_down_weight_private", "f1")
    if "a5" in axes:
        _print_a5(results)
    print("\ncell = edge-F1 vs the full shipping config / surviving container-edge count.")
    print("F1=1.000 means the toggle did not change the container edge set; lower = it did.")
    print("Per-system detail (precision/recall/added/removed/jaccard): out/<sys>/baseline/rq3-ablation.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
