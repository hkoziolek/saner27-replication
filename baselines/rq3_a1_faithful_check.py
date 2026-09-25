"""Eval §7 RQ3 — **A1 faithful-path validation** (closes the "faithful A1 not executed across the
corpus" open point).

`baselines/rq3_ablation.py` computes A1 (evidence-layer sensitivity) with a *post-hoc* filter of the
committed merged facts. The FAITHFUL A1 is the engine knob `arch run --max-evidence-layer {L0..L3}`
(`normalize_facts.apply_evidence_ceiling`). A full faithful sweep would re-extract per layer (Doxygen
etc.) — expensive — but at the altitude A1 is *scored* (container edges) both paths do the SAME thing:
filter each relationship's evidence to the layer ceiling and recompute weight/declared, then lift the
survivors to container edges (targets are L0, never dropped — `apply_evidence_ceiling`'s documented
non-goal). So the two MUST agree on the container-edge count at every layer, and that agreement is
provable from the committed facts alone — no re-extraction, all 6 systems.

This runner drives the REAL engine `apply_evidence_ceiling` over each system's committed
`extracted-facts.json`, lifts to container edges with the shipping survival predicate, and asserts the
per-layer count equals `rq3-ablation.json`'s post-hoc A1 number. Green ⇒ the post-hoc twin faithfully
reproduces the engine knob, so the cheap runner is a sound corpus default and the faithful path needs
per-layer toolchain re-runs only for the finer-granularity entity/refinement loss A1 does not score.

Usage:  python baselines/rq3_a1_faithful_check.py [systems...]
Output: table to stdout + out/<sys>/baseline/rq3-a1-faithful.json
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from rq3_ablation import (DEFAULT_MIN_WEIGHT, SYSTEMS, _survives, container_edges,  # noqa: E402
                          load_inputs)

from anon.stages.normalize_facts import apply_evidence_ceiling  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# (post-hoc A1 label, faithful --max-evidence-layer ceiling). The post-hoc labels are cumulative
# ("+L2" = up to L2); apply_evidence_ceiling's max_layer is likewise cumulative ("L2" = up to L2).
LAYERS = [("L0", "L0"), ("L0+L1", "L1"), ("+L2", "L2"), ("+L3", "L3")]


def _full_pred(r):
    return _survives(r, min_weight=DEFAULT_MIN_WEIGHT, declared_exempt=True, down_weight_private=True)


def run_system(system: str) -> dict | None:
    lang, gran = SYSTEMS[system]
    facts, cmap = load_inputs(system, gran)
    if facts is None or not cmap:
        print(f"  {system}: missing facts/clusters — skipping", file=sys.stderr)
        return None
    posthoc = json.loads((REPO / "out" / system / "baseline" / "rq3-ablation.json").read_text(
        encoding="utf-8")) if (REPO / "out" / system / "baseline" / "rq3-ablation.json").exists() else {}
    posthoc_a1 = (posthoc.get("axes") or {}).get("A1_evidence_layers", {})

    rows, agree = {}, True
    for label, ceiling in LAYERS:
        faithful = apply_evidence_ceiling(copy.deepcopy(facts), ceiling)
        faithful_edges = len(container_edges(faithful, cmap, _full_pred))
        posthoc_edges = (posthoc_a1.get(label) or {}).get("edges")
        match = (posthoc_edges is None) or (faithful_edges == posthoc_edges)
        agree = agree and match
        rows[label] = {"faithful_edges": faithful_edges, "posthoc_edges": posthoc_edges,
                       "match": match}
    res = {"system": system, "layers": rows, "faithful_matches_posthoc": agree}
    (REPO / "out" / system / "baseline" / "rq3-a1-faithful.json").write_text(
        json.dumps(res, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
    return res


def main(argv: list[str] | None = None) -> int:
    systems = [a for a in (argv or sys.argv[1:]) if not a.startswith("-")] or list(SYSTEMS)
    results = [r for r in (run_system(s) for s in systems if s in SYSTEMS) if r]
    if not results:
        print("no systems produced results.", file=sys.stderr)
        return 1
    print("\n## A1 faithful (engine apply_evidence_ceiling) vs post-hoc twin — container edges/layer")
    print(f"{'system':13} " + " ".join(f"{lbl:>10}" for lbl, _ in LAYERS) + "   agree")
    ok = True
    for r in results:
        ok = ok and r["faithful_matches_posthoc"]
        cells = [f"{r['layers'][lbl]['faithful_edges']:>10}" for lbl, _ in LAYERS]
        print(f"{r['system']:13} " + " ".join(cells) +
              f"   {'YES' if r['faithful_matches_posthoc'] else 'NO':>5}")
    print("\nfaithful engine knob == post-hoc twin at container altitude on all systems"
          if ok else "\nMISMATCH — investigate before quoting A1")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
