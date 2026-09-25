"""§8.2 degradation-correctness check (RQ6) — the P§11.6 stopping rules, measured.

Induces evidence degradation on the committed corpus facts (fact-level, with the induced
degradation recorded in a manifest — the §4 metrics-catalogue source "drift result +
induced-degradation manifest") and verifies the governance **downgrades instead of crying
wolf, without hiding erosion**:

* ``disable_l2``   — the semantic toolchain (Doxygen/clang/Roslyn) is gone: every
  L2-only edge disappears and per-target L2 coverage with it. Would-be-false
  "removed edge" findings must reclassify as ``coverage_gap_edges``.
* ``starve_l0_10`` / ``starve_l0_30`` — a spread 10% / 30% of first-party targets lose
  their L0 evidence (targets stay declared, their L0-only edges vanish). Per-finding
  reclassification again; at 30% the run drops below the 80% floor, so the whole report
  must additionally turn ADVISORY and stop gating (§11.6) — while a synthesized
  ``require_layers`` fitness rule on a starved edge must answer ``UNVERIFIED (degraded)``
  rather than silently OK (erosion not hidden), and one on an intact edge must stay
  VIOLATION.
* ``lose_l0``      — the build-graph extractor itself dies (a failed CMake configure):
  every L0-covered target vanishes. All disappearances must classify as coverage gaps.

The stopping-rules-OFF ablation is the coverage-naive reading of the same diff: count
``coverage_gap_*`` as drift and ignore the advisory flag — exactly what a differ without
§11.1/§11.6 would report. FP-suppression rate = suppressed / would-be-false findings.

Output: ``out/<system>/mutation/degradation.json`` (read by ``analysis/aggregate_results.py``).
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from .common import REPO_ROOT, first_party_ids, rel_index, spread

# the 8-system fidelity corpus (language as in baselines/a2_ablation.SYSTEMS)
CORPUS: dict[str, str] = {
    "itk": "cpp", "abseil": "cpp", "opencv": "cpp", "bash": "cpp", "libxml2": "cpp",
    "eshop": "cs", "orchardcore": "cs", "nopcommerce": "cs",
}

L0_KINDS = {"link", "project_ref"}
L2_KINDS = {"include", "symbol_use"}


def _facts_path(system: str) -> Path:
    return REPO_ROOT / "out" / system / "architecture" / "generated" / "curated-facts.json"


def _evidence_layers(rel: dict[str, Any]) -> set[str]:
    kinds = {e.get("type") for e in rel.get("evidence", []) or []}
    layers = set()
    if kinds & L0_KINDS:
        layers.add("L0")
    if kinds & {"package_ref"}:
        layers.add("L1")
    if kinds & L2_KINDS:
        layers.add("L2")
    if kinds & {"interop", "runtime"}:
        layers.add("other")
    return layers


def _coverage(facts: dict[str, Any]) -> dict[str, Any]:
    return facts.setdefault("provenance", {}).setdefault("coverage", {})


def _drop_edges(facts: dict[str, Any], keep) -> list[tuple[str, str]]:
    dropped = [(r["source"], r["target"])
               for r in facts.get("relationships", []) if not keep(r)]
    facts["relationships"] = [r for r in facts.get("relationships", []) if keep(r)]
    return sorted(dropped)


# ------------------------------------------------------------------ degradation modes

def degrade_disable_l2(baseline: dict[str, Any]) -> tuple[dict, dict]:
    cur = copy.deepcopy(baseline)
    dropped = _drop_edges(cur, lambda r: _evidence_layers(r) != {"L2"})
    for tid, layers in _coverage(cur).items():
        if isinstance(layers, dict):
            for k in [k for k in layers if k.upper().startswith("L2")]:
                layers.pop(k)
    manifest = {"mode": "disable_l2", "edges_dropped": len(dropped),
                "targets_starved": 0}
    return cur, {"dropped_edges": dropped, "starved": [], "manifest": manifest}


def degrade_starve_l0(baseline: dict[str, Any], fraction: float) -> tuple[dict, dict]:
    cur = copy.deepcopy(baseline)
    fp = sorted(first_party_ids(cur))
    starved = set(spread(fp, max(1, round(len(fp) * fraction))))
    cov = _coverage(cur)
    for tid in starved:
        entry = cov.setdefault(tid, {})
        if isinstance(entry, dict):
            entry["L0"] = False
    dropped = _drop_edges(
        cur, lambda r: not (_evidence_layers(r) == {"L0"}
                            and (r["source"] in starved or r["target"] in starved)))
    manifest = {"mode": f"starve_l0_{int(fraction * 100)}", "edges_dropped": len(dropped),
                "targets_starved": len(starved)}
    return cur, {"dropped_edges": dropped, "starved": sorted(starved),
                 "manifest": manifest}


def degrade_lose_l0(baseline: dict[str, Any]) -> tuple[dict, dict]:
    cur = copy.deepcopy(baseline)
    cov = _coverage(cur)
    lost = {tid for tid, layers in cov.items()
            if isinstance(layers, dict) and layers.get("L0")}
    cur["targets"] = [t for t in cur.get("targets", []) if t["id"] not in lost]
    dropped = _drop_edges(cur, lambda r: r["source"] not in lost
                          and r["target"] not in lost)
    for tid in lost:
        cov.pop(tid, None)
    manifest = {"mode": "lose_l0", "edges_dropped": len(dropped),
                "targets_starved": len(lost)}
    return cur, {"dropped_edges": dropped, "starved": sorted(lost), "manifest": manifest}


# ------------------------------------------------------------------ scoring one mode

def _score_mode(baseline: dict[str, Any], cur: dict[str, Any], info: dict[str, Any],
                check_advisory_flip: bool) -> dict[str, Any]:
    from anon.stages import drift_report as dr

    diff = dr.diff_facts(baseline, cur)
    # nothing in the degraded run is a real change → every reported disappearance is false
    on_false = len(diff["removed_targets"]) + len(diff["removed_edges"])
    suppressed = len(diff["coverage_gap_targets"]) + len(diff["coverage_gap_edges"])
    would_be_false = on_false + suppressed

    coverage = dr._coverage_l0(cur)                      # noqa: SLF001 — the tested rule
    advisory = coverage < dr.COVERAGE_FLOOR or dr._dropped_below_baseline(baseline, cur)  # noqa: SLF001

    out: dict[str, Any] = {
        "manifest": info["manifest"],
        "would_be_false_findings": would_be_false,
        "suppressed_on": suppressed,                      # reclassified coverage-gap
        "leaked_on": on_false,                            # still reported as drift
        "fp_suppression_rate": (suppressed / would_be_false) if would_be_false else None,
        "findings_off": would_be_false,                   # coverage-naive differ reports all
        # not hiding erosion: every induced disappearance is still REPORTED somewhere
        # (as drift or as a labelled coverage gap), never silently swallowed
        "erosion_visible": _all_reported(diff, info),
        "coverage_l0": round(coverage, 4),
        "advisory": advisory,
    }
    if check_advisory_flip:
        # §11.6: below the floor the run is advisory and a layering VIOLATION must not gate
        starved = set(info["starved"])
        rules = {"forbidden": []}
        base_rels = rel_index(baseline)
        starved_edge = next(((s, t) for (s, t) in sorted(info["dropped_edges"])
                             if s in starved or t in starved), None)
        intact_edge = next(((s, t) for (s, t), r in sorted(base_rels.items())
                            if s not in starved and t not in starved
                            and _evidence_layers(r) == {"L0"}
                            and not _is_external(baseline, s)
                            and not _is_external(baseline, t)), None)
        layering = []
        if starved_edge:
            rules["forbidden"].append({"from": starved_edge[0], "to": starved_edge[1],
                                       "require_layers": ["L0"]})
        if intact_edge:
            rules["forbidden"].append({"from": intact_edge[0], "to": intact_edge[1],
                                       "require_layers": ["L0"]})
        if rules["forbidden"]:
            layering = dr.check_layering(cur, rules)
        statuses = {(f["from"], f["to"]): f["status"] for f in layering}
        out["layering"] = {
            "starved_rule_status": statuses.get(starved_edge) if starved_edge else None,
            "intact_rule_status": statuses.get(intact_edge) if intact_edge else None,
            # honest degradation: starved rule answers UNVERIFIED, never a silent OK
            "starved_honest": (statuses.get(starved_edge, "").startswith("UNVERIFIED")
                               if starved_edge else None),
            "intact_still_violation": (statuses.get(intact_edge) == "VIOLATION"
                                       if intact_edge else None),
        }
        violations = [f for f in layering if f["status"] == "VIOLATION"]
        out["gating"] = bool(violations) and not advisory
    return out


def _all_reported(diff: dict[str, Any], info: dict[str, Any]) -> bool:
    reported_edges = {tuple(e) for e in diff["removed_edges"]} \
        | {tuple(e) for e in diff["coverage_gap_edges"]}
    reported_targets = set(diff["removed_targets"]) | set(diff["coverage_gap_targets"])
    for e in info["dropped_edges"]:
        s, t = tuple(e)
        if (s, t) not in reported_edges and s not in reported_targets \
                and t not in reported_targets:
            return False
    return True


def _is_external(facts: dict[str, Any], tid: str) -> bool:
    for t in facts.get("targets", []):
        if t["id"] == tid:
            return bool(t.get("external"))
    return True


# ------------------------------------------------------------------ entry point

def run_system(system: str, out_root: Path | None = None) -> dict[str, Any] | None:
    path = _facts_path(system)
    if not path.is_file():
        print(f"[degrade] {system}: no curated facts at {path} — skipping",
              file=sys.stderr)
        return None
    baseline = json.loads(path.read_text(encoding="utf-8"))

    modes: dict[str, dict[str, Any]] = {}
    cur, info = degrade_disable_l2(baseline)
    modes["disable_l2"] = _score_mode(baseline, cur, info, check_advisory_flip=False)
    for frac in (0.10, 0.30):
        cur, info = degrade_starve_l0(baseline, frac)
        modes[f"starve_l0_{int(frac * 100)}"] = _score_mode(
            baseline, cur, info, check_advisory_flip=True)
    cur, info = degrade_lose_l0(baseline)
    modes["lose_l0"] = _score_mode(baseline, cur, info, check_advisory_flip=False)

    result = {"schema": "rq6-degradation/1", "system": system,
              "language": CORPUS.get(system, "?"), "modes": modes}
    out_dir = (out_root or (REPO_ROOT / "out" / system)) / "mutation"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "degradation.json"
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="")
    line = ", ".join(
        f"{m}: {v['suppressed_on']}/{v['would_be_false_findings']} suppressed"
        + (" ADV" if v["advisory"] else "")
        for m, v in modes.items())
    print(f"[degrade] {system}: {line} -> {out_path}", file=sys.stderr)
    return result
