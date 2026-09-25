"""RQ6 combined drift-under-degradation harness tests (``mutate/combined.py``).

Covers: the touched-id extraction from an injected label, the targeted starvation mode,
the four-way outcome classifier on synthetic facts (drift / gap / advisory / silent each
exercised), the aggregation cells, and a toy end-to-end run through the real pipeline
(one operator, one mode) mirroring ``test_mutation_harness_toy_end_to_end``.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mutate import combined  # noqa: E402
from mutate.common import make_expected  # noqa: E402
from anon.stages import drift_report  # noqa: E402


# ------------------------------------------------------------------ synthetic facts

def _facts():
    """4 first-party targets with full L0 coverage; every edge L0-only."""
    def t(i):
        return {"id": i, "name": i, "type": "csproj", "language": "csharp"}

    def r(s, tt, kinds):
        return {"id": f"rel:{s}->{tt}", "source": s, "target": tt,
                "evidence": [{"type": k, "detail": k} for k in kinds]}

    ids = ["t:a", "t:b", "t:c", "t:d"]
    return {
        "schema_version": "1.0.0",
        "provenance": {"repo": "x", "commit": "c", "generated_at": "g",
                       "coverage": {i: {"L0": True, "L2": False} for i in ids}},
        "targets": [t(i) for i in ids],
        "relationships": [r("t:a", "t:b", ["project_ref"]),
                          r("t:b", "t:c", ["project_ref"]),
                          r("t:c", "t:d", ["project_ref"]),
                          r("t:d", "t:a", ["project_ref"])],
    }


def _without_edge(facts, s, t):
    out = copy.deepcopy(facts)
    out["relationships"] = [r for r in out["relationships"]
                            if (r["source"], r["target"]) != (s, t)]
    return out


def _evaluate(base, degraded, expected):
    rules = {"forbidden": []}
    return combined.evaluate_mode(base, degraded, expected, rules,
                                  drift_report.check_layering(base, rules))


def _outcome_of(ev, finding):
    return dict((tuple(f), oc) for f, oc in ev["outcomes"])[finding]


# ------------------------------------------------------------------ touched ids

def test_touched_ids_covers_every_label_shape():
    exp = make_expected(new_targets=["t:n"], removed_targets=["t:o"],
                        new_edges=[("t:x", "t:n")], removed_edges=[("t:x", "t:o")],
                        suspected_renames=[("t:o", "t:n")],
                        layering_violations=[("t:p", "t:q")])
    assert combined.touched_ids(exp) == {"t:n", "t:o", "t:x", "t:p", "t:q"}
    # a merge names its survivor only in the operator's fixed description
    exp = make_expected(removed_targets=["t:b"])
    assert combined.touched_ids(exp, "merge build target t:b into t:a") == {"t:a", "t:b"}


def test_starve_targets_is_targeted_and_records_absent_ids():
    base = _facts()
    cur, info = combined.degrade_starve_targets(base, {"t:d", "t:gone"})
    assert info["starved"] == ["t:d"]
    assert info["manifest"]["touched_absent"] == ["t:gone"]
    assert info["manifest"]["targets_starved"] == 1
    assert cur["provenance"]["coverage"]["t:d"]["L0"] is False
    assert cur["provenance"]["coverage"]["t:a"]["L0"] is True      # untouched
    assert sorted(info["dropped_edges"]) == [("t:c", "t:d"), ("t:d", "t:a")]
    assert len(cur["relationships"]) == 2
    assert base["relationships"] and len(base["relationships"]) == 4  # input untouched


def test_degrade_dispatch_modes():
    base = _facts()
    exp = make_expected(removed_edges=[("t:a", "t:b")])
    for mode in combined.MODES:
        cur, info = combined.degrade(mode, base, exp)
        assert info["manifest"]["mode"] == mode
    with pytest.raises(ValueError):
        combined.degrade("nope", base, exp)


# ------------------------------------------------------------------ classifier

def test_classifier_drift_and_silent_without_coverage_drop():
    """Evidence intact (coverage unchanged, run NOT advisory): a reported removed edge is
    ``drift``; an injected new target that never shows up is ``silent`` — the hidden
    case the experiment exists to detect."""
    base = _facts()
    degraded = _without_edge(base, "t:a", "t:b")          # the real change survives
    exp = make_expected(removed_edges=[("t:a", "t:b")], new_targets=["t:e"])
    ev = _evaluate(base, degraded, exp)
    assert ev["advisory"] is False and ev["coverage_l0"] == 1.0
    assert _outcome_of(ev, ("removed_edge", "t:a", "t:b")) == "drift"
    assert _outcome_of(ev, ("new_target", "t:e")) == "silent"
    assert ev["counts"] == {"drift": 1, "gap": 0, "advisory": 0, "silent": 1}
    assert ev["fp"] == 0


def test_classifier_gap_and_advisory_under_starvation():
    """Starving t:d drops c->d and d->a (labelled coverage gaps) and makes the run
    advisory: an injected removed edge on a starved endpoint is ``gap``; an injected new
    edge that the starvation swallowed is ``advisory`` (globally warned, not labelled)."""
    base = _facts()
    mutated = copy.deepcopy(base)
    mutated["relationships"].append({"id": "rel:t:a->t:c", "source": "t:a", "target": "t:c",
                                     "evidence": [{"type": "project_ref", "detail": "x"}]})
    mutated = _without_edge(mutated, "t:c", "t:d")        # the real change: c->d removed
    degraded, _ = combined.degrade_starve_targets(mutated, {"t:d", "t:c"})
    exp = make_expected(removed_edges=[("t:c", "t:d")], new_edges=[("t:a", "t:c")])
    ev = _evaluate(base, degraded, exp)
    assert ev["advisory"] is True
    assert ("t:c", "t:d") in {tuple(e) for e in ev["diff"]["coverage_gap_edges"]}
    assert _outcome_of(ev, ("removed_edge", "t:c", "t:d")) == "gap"
    assert _outcome_of(ev, ("new_edge", "t:a", "t:c")) == "advisory"
    assert ev["counts"] == {"drift": 0, "gap": 1, "advisory": 1, "silent": 0}


def test_classifier_gap_via_endpoint_target_and_rename():
    """lose_l0 on a rename: the old id is a labelled coverage-gap target, so the removed
    target, its removed edge and the rename pair are all ``gap``; the new id (never
    present in the degraded run) is ``advisory`` or ``silent`` by the run flag."""
    base = _facts()
    mutated = copy.deepcopy(base)
    for tgt in mutated["targets"]:
        if tgt["id"] == "t:d":
            tgt["id"] = "t:d2"
    for rel in mutated["relationships"]:
        for k in ("source", "target"):
            if rel[k] == "t:d":
                rel[k] = "t:d2"
    mutated["provenance"]["coverage"]["t:d2"] = mutated["provenance"]["coverage"].pop("t:d")
    degraded, _ = combined.degrade("lose_l0", mutated, {})
    exp = make_expected(new_targets=["t:d2"], removed_targets=["t:d"],
                        removed_edges=[("t:c", "t:d")], new_edges=[("t:c", "t:d2")],
                        suspected_renames=[("t:d", "t:d2")])
    ev = _evaluate(base, degraded, exp)
    assert _outcome_of(ev, ("removed_target", "t:d")) == "gap"
    assert _outcome_of(ev, ("removed_edge", "t:c", "t:d")) == "gap"
    assert _outcome_of(ev, ("rename", "t:d", "t:d2")) == "gap"
    assert _outcome_of(ev, ("new_target", "t:d2")) in ("advisory", "silent")
    assert _outcome_of(ev, ("new_target", "t:d2")) == (
        "advisory" if ev["advisory"] else "silent")


def test_aggregate_cells_and_silent_list():
    muts = [
        {"name": "m1", "operator": "remove_dep", "expected_findings": 2,
         "modes": {"lose_l0": {"drift": 1, "gap": 1, "advisory": 0, "silent": 0, "fp": 0,
                               "advisory_run": True, "outcomes": []}}},
        {"name": "m2", "operator": "add_target", "expected_findings": 1,
         "modes": {"lose_l0": {"drift": 0, "gap": 0, "advisory": 0, "silent": 1, "fp": 2,
                               "advisory_run": False,
                               "outcomes": [[["new_target", "t:e"], "silent"]]}}},
    ]
    agg = combined.aggregate(muts, ["lose_l0"])
    tot = agg["lose_l0"]["totals"]
    assert tot == {"n": 2, "findings": 3, "drift": 1, "gap": 1, "advisory": 0,
                   "silent": 1, "fp": 2, "mutations_silent": 1, "advisory_runs": 1}
    assert agg["lose_l0"]["by_operator"]["add_target"]["silent"] == 1
    assert agg["lose_l0"]["silent_findings"] == [
        {"mutation": "m2", "finding": [["new_target", "t:e"], "silent"][0]}]


# ------------------------------------------------------------------ toy end-to-end

@pytest.mark.integration
def test_combined_toy_end_to_end(tmp_path, monkeypatch):
    """One operator (remove_dep) x one mode (starve_mutated) through the REAL pipeline on
    the toy fixture: the artifact has the documented shape and every injected finding is
    classified exactly once."""
    monkeypatch.setitem(combined.SYSTEMS, "toy", dict(combined.SYSTEMS["toy"]))
    res = combined.run_system("toy", ops=["remove_dep"], modes=["starve_mutated"],
                              instances=1, work_root=tmp_path / "work", fresh=True,
                              keep_work=False, out_root=tmp_path / "out" / "toy")
    assert res is not None and res["schema"] == "rq6-combined/1"
    path = tmp_path / "out" / "toy" / "mutation" / "combined.json"
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == res
    assert "\r" not in path.read_bytes().decode("utf-8")
    assert list(res["modes"]) == ["starve_mutated"]
    tot = res["modes"]["starve_mutated"]["totals"]
    assert tot["n"] == 1 and tot["findings"] >= 1
    assert tot["drift"] + tot["gap"] + tot["advisory"] + tot["silent"] == tot["findings"]
    m = res["mutations"][0]
    assert m["operator"] == "remove_dep" and m["name"].startswith("toy-remove_dep-")
    r = m["modes"]["starve_mutated"]
    assert r["targets_starved"] >= 1 and r["advisory_run"] is True
    assert len(r["outcomes"]) == m["expected_findings"]
    assert {o[-1] for o in r["outcomes"]} <= set(combined.OUTCOMES)
    assert not (tmp_path / "work" / "toy").exists()      # work copy cleaned up
