"""§8.3 combined drift-under-degradation check (RQ6) — a REAL change and evidence loss
at the same time.

§8.1 (``harness.py``) seeds build-file mutations through the real pipeline with intact
evidence; §8.2 (``degradation.py``) induces evidence loss on UNCHANGED facts. Neither
tests the claim "degradation never hides erosion" with both at once. This module does:

    for every §8.1 mutation (same plans, same 95 instances):
        apply -> run the pipeline ONCE -> ``current``
        for every degradation MODE:
            degraded = degrade(current)          # the evidence loss happens in the NEW run
            diff     = diff_facts(baseline_pristine, degraded)
            classify every injected finding into exactly one outcome

Modes (the §8.2 L0 modes, applied to the *mutated* run, plus one adversarial mode):

* ``starve_l0_10`` / ``starve_l0_30`` — ``degradation.degrade_starve_l0`` (spread);
* ``starve_mutated`` — **targeted**: exactly the first-party targets the mutation touches
  (ids named by its label: new/removed targets, both endpoints of every new/removed edge,
  old+new of a rename, both merge members) lose their L0 coverage and their L0-only edges;
* ``lose_l0`` — ``degradation.degrade_lose_l0`` (the build-graph extractor dies).

Outcome per injected finding (the ``common._tagged`` tuples), first match wins:

* ``drift``     — reported as drift (it is a true positive of ``common.score``);
* ``gap``       — not reported as drift, but the finding's target — or, for an edge-like
  finding, either endpoint or the edge itself — is listed as a labelled
  ``coverage_gap_*`` entry (the report says "NOT drift — producing layer not extracted");
* ``advisory``  — absent from both, but the run is ADVISORY (§11.6): the report globally
  warns that evidence dropped below the floor / the baseline;
* ``silent``    — absent from both and the run is not advisory: the erosion is hidden.

The paper's claim holds iff no ``silent`` cell exists. Nothing here is tuned to make it
hold — whatever the data says is the result. ``common.score``'s false positives are kept
per mode (``fp``) so degradation-induced *phantom* drift is visible too.

Output ``out/<system>/mutation/combined.json`` (schema ``rq6-combined/1``, sorted keys,
LF, no timestamps)::

    {"schema": "rq6-combined/1", "system", "language", "idiom", "instances_requested",
     "l0_scoped": true, "baseline_targets", "baseline_l0_edges",
     "modes": {<mode>: {"by_operator": {<op>: CELL}, "totals": CELL,
                        "silent_findings": [{"mutation", "finding": [..]}]}},
     "mutations": [{"name", "operator", "expected_findings",
                    "modes": {<mode>: {"drift", "gap", "advisory", "silent", "fp",
                                       "advisory_run", "coverage_l0", "targets_starved",
                                       "edges_dropped", "starved"?, "touched_absent"?,
                                       "outcomes": [[[finding...], outcome], ...]}}}]}
    CELL = {"n": mutations, "findings", "drift", "gap", "advisory", "silent", "fp",
            "mutations_silent", "advisory_runs"}

Usage::

    python -m mutate.combined                        # five mutation systems, all modes
    python -m mutate.combined eshop --instances 4 --modes starve_mutated lose_l0
"""
from __future__ import annotations

import argparse
import copy
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .common import REPO_ROOT, MutCtx, _tagged, first_party_ids, score
from .degradation import (_coverage, _drop_edges, _evidence_layers, degrade_lose_l0,
                          degrade_starve_l0)
from .harness import (OP_ORDER, SYSTEMS, _pipeline, _prepare_work, _write_json,
                      _write_layering_rules)
from .operators import OPERATORS
from .run_rq6 import MUTATION_DEFAULT

MODES = ("starve_l0_10", "starve_l0_30", "starve_mutated", "lose_l0")
OUTCOMES = ("drift", "gap", "advisory", "silent")
SCHEMA = "rq6-combined/1"

_MERGE_DESC_RE = re.compile(r"merge build target (\S+) into (\S+)")


# ------------------------------------------------------------------ touched ids

def touched_ids(expected: dict[str, Any], description: str = "") -> set[str]:
    """Every target id an injected label names: new/removed targets, both endpoints of
    every new/removed edge and layering violation, old+new of a rename. A merge's
    survivor is named only in the operator's fixed description ("merge build target B
    into A") when none of B's referrers had to be re-pointed, so it is read from there."""
    ids: set[str] = set(expected.get("new_targets", [])) | set(expected.get("removed_targets", []))
    for s, t in list(expected.get("new_edges", [])) + list(expected.get("removed_edges", [])):
        ids |= {s, t}
    for r in expected.get("suspected_renames", []):
        pair = (r["old"], r["new"]) if isinstance(r, dict) else tuple(r)
        ids |= set(pair)
    for v in expected.get("layering_violations", []):
        ids |= set(v)
    m = _MERGE_DESC_RE.search(description or "")
    if m:
        ids |= {m.group(1), m.group(2)}
    return ids


# ------------------------------------------------------------------ degradation modes

def degrade_starve_targets(facts: dict[str, Any], targets: set[str]) -> tuple[dict, dict]:
    """Targeted starvation: the given first-party targets lose their L0 coverage (they
    stay declared) and every L0-only edge touching them vanishes — the same per-target
    semantics as ``degradation.degrade_starve_l0`` with an explicit set instead of a
    spread fraction. Ids not present in *facts* (e.g. a removed target) cannot be
    starved there and are recorded as ``touched_absent``."""
    cur = copy.deepcopy(facts)
    present = set(targets) & first_party_ids(cur)
    cov = _coverage(cur)
    for tid in sorted(present):
        entry = cov.setdefault(tid, {})
        if isinstance(entry, dict):
            entry["L0"] = False
    dropped = _drop_edges(
        cur, lambda r: not (_evidence_layers(r) == {"L0"}
                            and (r["source"] in present or r["target"] in present)))
    manifest = {"mode": "starve_mutated", "edges_dropped": len(dropped),
                "targets_starved": len(present),
                "touched_absent": sorted(set(targets) - present)}
    return cur, {"dropped_edges": dropped, "starved": sorted(present), "manifest": manifest}


def degrade(mode: str, current: dict[str, Any], expected: dict[str, Any],
            description: str = "") -> tuple[dict, dict]:
    """Derive the degraded copy of the MUTATED run's facts for *mode*."""
    if mode == "starve_l0_10":
        return degrade_starve_l0(current, 0.10)
    if mode == "starve_l0_30":
        return degrade_starve_l0(current, 0.30)
    if mode == "starve_mutated":
        return degrade_starve_targets(current, touched_ids(expected, description))
    if mode == "lose_l0":
        return degrade_lose_l0(current)
    raise ValueError(f"unknown degradation mode {mode!r}")


# ------------------------------------------------------------------ classification

def _reported_as_gap(finding: tuple, gap_targets: set[str],
                     gap_edges: set[tuple[str, str]]) -> bool:
    kind = finding[0]
    if kind in ("new_target", "removed_target"):
        return finding[1] in gap_targets
    # edge-like: new_edge / removed_edge / violation (from, to) / rename (old, new)
    s, t = finding[1], finding[2]
    return (s, t) in gap_edges or s in gap_targets or t in gap_targets


def classify(expected: dict[str, Any], diff: dict[str, Any], sc: dict[str, Any],
             advisory: bool) -> list[tuple[tuple, str]]:
    """One outcome per injected finding (module docstring), in sorted finding order."""
    tp = {tuple(x) for x in sc.get("tp", [])}
    gap_targets = set(diff.get("coverage_gap_targets", []))
    gap_edges = {tuple(e) for e in diff.get("coverage_gap_edges", [])}
    out: list[tuple[tuple, str]] = []
    for f in sorted(_tagged(expected)):
        if f in tp:
            outcome = "drift"
        elif _reported_as_gap(f, gap_targets, gap_edges):
            outcome = "gap"
        elif advisory:
            outcome = "advisory"
        else:
            outcome = "silent"
        out.append((f, outcome))
    return out


def evaluate_mode(baseline: dict[str, Any], degraded: dict[str, Any],
                  expected: dict[str, Any], layering_rules: dict[str, Any],
                  layering_base: list[dict[str, Any]]) -> dict[str, Any]:
    """Diff the degraded MUTATED run against the pristine baseline and classify."""
    from anon.stages import drift_report as dr

    diff = dr.diff_facts(baseline, degraded)
    coverage = dr._coverage_l0(degraded)                                     # noqa: SLF001
    advisory = coverage < dr.COVERAGE_FLOOR or dr._dropped_below_baseline(baseline, degraded)  # noqa: SLF001
    layering = dr.check_layering(degraded, layering_rules)
    sc = score(expected, diff, layering, layering_base, baseline, degraded)
    outcomes = classify(expected, diff, sc, advisory)
    counts = {o: sum(1 for _, oc in outcomes if oc == o) for o in OUTCOMES}
    return {"outcomes": outcomes, "counts": counts, "fp": sc["fp_count"],
            "advisory": advisory, "coverage_l0": round(coverage, 4),
            "diff": diff, "score": sc}


# ------------------------------------------------------------------ aggregation

def _empty_cell() -> dict[str, int]:
    return {"n": 0, "findings": 0, "drift": 0, "gap": 0, "advisory": 0, "silent": 0,
            "fp": 0, "mutations_silent": 0, "advisory_runs": 0}


def _add(cell: dict[str, int], res: dict[str, Any]) -> None:
    cell["n"] += 1
    for o in OUTCOMES:
        cell[o] += res[o]
    cell["findings"] += sum(res[o] for o in OUTCOMES)
    cell["fp"] += res["fp"]
    cell["mutations_silent"] += 1 if res["silent"] else 0
    cell["advisory_runs"] += 1 if res["advisory_run"] else 0


def aggregate(mutations: list[dict[str, Any]], modes: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in modes:
        by_op: dict[str, dict[str, int]] = {}
        totals = _empty_cell()
        silent: list[dict[str, Any]] = []
        for m in mutations:
            res = m["modes"].get(mode)
            if res is None:
                continue
            _add(by_op.setdefault(m["operator"], _empty_cell()), res)
            _add(totals, res)
            for finding, outcome in res["outcomes"]:
                if outcome == "silent":
                    silent.append({"mutation": m["name"], "finding": list(finding)})
        out[mode] = {"by_operator": by_op, "totals": totals, "silent_findings": silent}
    return out


# ------------------------------------------------------------------ entry point

def run_system(system: str, ops: list[str] | None = None, modes: list[str] | None = None,
               instances: int = 4, work_root: Path | None = None, fresh: bool = True,
               keep_work: bool = False, out_root: Path | None = None) -> dict | None:
    """Run the combined experiment for one system; returns the result dict."""
    from anon.config import load_yaml
    from anon.stages import drift_report

    cfg = SYSTEMS.get(system)
    if cfg is None:
        print(f"[combined] unknown system {system!r}", file=sys.stderr)
        return None
    fixture = REPO_ROOT / cfg["fixture"]
    if not fixture.exists():
        print(f"[combined] {system}: no fixture at {fixture} — skipping", file=sys.stderr)
        return None
    ops = [o for o in (ops or OP_ORDER) if o in OPERATORS]
    modes = [m for m in (modes or MODES) if m in MODES]
    work_root = work_root or (REPO_ROOT / "out" / system / "mutation" / "_work-combined")
    out_dir = (out_root or (REPO_ROOT / "out" / system)) / "mutation"

    # same L0-scoped knobs as harness.run_system — baseline and mutated runs share them
    os.environ["ANON_SKIP_DOXYGEN"] = "1"
    os.environ["ANON_SKIP_ROSLYN"] = "1"

    repo, rules = _prepare_work(system, cfg, work_root, fresh)
    arch_base = work_root / system / "arch-baseline"
    arch_run = work_root / system / "arch-run"

    t0 = time.monotonic()
    print(f"[combined] {system}: baseline pipeline run ...", file=sys.stderr)
    baseline = _pipeline(repo, arch_base, rules)
    ctx = MutCtx(system=system, idiom=cfg["idiom"], repo=repo, baseline=baseline)
    print(f"[combined] {system}: baseline {len(ctx.fp)} first-party targets, "
          f"{len(ctx.l0)} L0 edges ({time.monotonic() - t0:.1f}s)", file=sys.stderr)

    # identical plans to harness.run_system -> the identical mutation set
    plans = {op: OPERATORS[op].plan(ctx, instances) for op in ops}
    _write_layering_rules(rules, plans.get("add_forbidden_dep", []))
    layering_rules = load_yaml(rules / "layering-rules.yaml")
    layering_base = drift_report.check_layering(baseline, layering_rules)

    mutations: list[dict[str, Any]] = []
    for op in ops:
        for i, cand in enumerate(plans[op], 1):
            m = OPERATORS[op].apply(ctx, cand)
            if m is None:
                print(f"[combined] {system}-{op}-{i:02d}: not applicable to candidate "
                      f"{cand!r} — skipped", file=sys.stderr)
                continue
            m.name = f"{system}-{op}-{i:02d}"
            try:
                current = _pipeline(repo, arch_run, rules)   # the real change, ONCE
            finally:
                m.journal.undo()
            entry: dict[str, Any] = {"name": m.name, "operator": op,
                                     "expected_findings": len(_tagged(m.expected)),
                                     "modes": {}}
            bits = []
            for mode in modes:
                degraded, info = degrade(mode, current, m.expected, m.description)
                ev = evaluate_mode(baseline, degraded, m.expected, layering_rules,
                                   layering_base)
                res: dict[str, Any] = {
                    **ev["counts"], "fp": ev["fp"],
                    "advisory_run": ev["advisory"], "coverage_l0": ev["coverage_l0"],
                    "targets_starved": info["manifest"]["targets_starved"],
                    "edges_dropped": info["manifest"]["edges_dropped"],
                    "outcomes": [[list(f), oc] for f, oc in ev["outcomes"]],
                }
                if mode == "starve_mutated":
                    res["starved"] = info["starved"]
                    res["touched_absent"] = info["manifest"]["touched_absent"]
                entry["modes"][mode] = res
                bits.append(f"{mode} d={res['drift']} g={res['gap']} a={res['advisory']} "
                            f"s={res['silent']} fp={res['fp']}"
                            + (" ADV" if res["advisory_run"] else ""))
            mutations.append(entry)
            print(f"[combined] {m.name}: " + " | ".join(bits), file=sys.stderr)

    result = {
        "schema": SCHEMA, "system": system, "language": cfg["language"],
        "idiom": cfg["idiom"], "instances_requested": instances, "l0_scoped": True,
        "baseline_targets": len(ctx.fp), "baseline_l0_edges": len(ctx.l0),
        "modes": aggregate(mutations, modes), "mutations": mutations,
    }
    out_path = out_dir / "combined.json"
    _write_json(out_path, result)
    for mode in modes:
        t = result["modes"][mode]["totals"]
        print(f"[combined] {system} {mode}: {t['n']} mutations / {t['findings']} findings: "
              f"drift={t['drift']} gap={t['gap']} advisory={t['advisory']} "
              f"silent={t['silent']} fp={t['fp']} "
              f"(advisory runs {t['advisory_runs']}/{t['n']})", file=sys.stderr)
    print(f"[combined] {system}: {len(mutations)} mutations x {len(modes)} modes "
          f"({time.monotonic() - t0:.0f}s) -> {out_path}", file=sys.stderr)
    if not keep_work:
        shutil.rmtree(work_root / system, ignore_errors=True)
    return result


def _split(values: list[str]) -> list[str]:
    return [v.strip() for item in values for v in item.split(",") if v.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("systems", nargs="*", help=f"systems (default: {' '.join(MUTATION_DEFAULT)})")
    ap.add_argument("--ops", nargs="+", default=list(OP_ORDER),
                    help="mutation operators (comma or space separated)")
    ap.add_argument("--modes", nargs="+", default=list(MODES),
                    help=f"degradation modes (default: {' '.join(MODES)})")
    ap.add_argument("--instances", type=int, default=4,
                    help="mutation instances per operator per system (default 4)")
    ap.add_argument("--work", help="work-area root (default out/<sys>/mutation/_work-combined)")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work copies after the run")
    ap.add_argument("--reuse-work", action="store_true",
                    help="reuse an existing work copy instead of a fresh one")
    args = ap.parse_args(argv)
    ops = _split(args.ops)
    modes = _split(args.modes)
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        ap.error(f"unknown mode(s) {unknown}; choose from {list(MODES)}")

    rc = 0
    for s in args.systems or MUTATION_DEFAULT:
        if s not in SYSTEMS:
            print(f"[combined] {s}: not a configured mutation system", file=sys.stderr)
            continue
        try:
            run_system(s, ops=ops, modes=modes, instances=args.instances,
                       work_root=Path(args.work) if args.work else None,
                       fresh=not args.reuse_work, keep_work=args.keep_work)
        except Exception as exc:  # noqa: BLE001 — keep going, report at the end
            print(f"[combined] {s}: FAILED — {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
