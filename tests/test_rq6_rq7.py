"""RQ6 (mutate/ seeded-mutation + degradation) and RQ7 (perf) harness tests.

Covers: the FileJournal exact-undo contract, deterministic candidate selection, the §8.2
degradation modes' suppression/advisory semantics on synthetic facts, the toy end-to-end
mutation run (real pipeline, all six operators), the aggregate→stats→tables wiring for the
new RQ6/RQ7 axes, and the ANON_SKIP_ROSLYN escape hatch.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _import(rel: str):
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agg = _import("analysis/aggregate_results.py")
stats_mod = _import("analysis/stats.py")
tables = _import("analysis/make_tables.py")

from mutate import degradation  # noqa: E402
from mutate.common import FileJournal, spread  # noqa: E402


# ------------------------------------------------------------------ journal / selection

def test_file_journal_undo_restores_bytes(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "sub" / "b.txt"
    a.write_bytes(b"original-a")
    b.parent.mkdir()
    b.write_bytes(b"original-b")
    j = FileJournal()
    j.write(a, "changed")
    j.write(tmp_path / "new.txt", "created")
    j.delete(b)
    j.rename(a, tmp_path / "renamed.txt")
    j.undo()
    assert a.read_bytes() == b"original-a"
    assert b.read_bytes() == b"original-b"
    assert not (tmp_path / "new.txt").exists()
    assert not (tmp_path / "renamed.txt").exists()


def test_spread_is_deterministic_and_bounded():
    items = [f"x{i}" for i in range(10)]
    assert spread(items, 3) == spread(items, 3)
    assert len(spread(items, 3)) == 3
    assert spread(items, 99) == items
    assert spread([], 3) == []


# ------------------------------------------------------------------ §8.2 degradation

def _synthetic_facts():
    """4 first-party targets, L0 coverage everywhere, one L2-only edge (b->c)."""
    def t(i):
        return {"id": i, "name": i, "type": "csproj", "language": "csharp"}

    def r(s, tt, kinds):
        return {"id": f"rel:{s}->{tt}", "source": s, "target": tt,
                "evidence": [{"type": k, "detail": k} for k in kinds]}

    ids = ["t:a", "t:b", "t:c", "t:d"]
    return {
        "schema_version": "1.0.0",
        "provenance": {"repo": "x", "commit": "c", "generated_at": "g",
                       "coverage": {i: {"L0": True, "L2": True} for i in ids}},
        "targets": [t(i) for i in ids],
        "relationships": [r("t:a", "t:b", ["project_ref"]),
                          r("t:b", "t:c", ["symbol_use"]),          # L2-only
                          r("t:c", "t:d", ["project_ref"]),
                          r("t:d", "t:a", ["project_ref"])],
    }


def test_degrade_disable_l2_reclassifies_not_drifts():
    base = _synthetic_facts()
    cur, info = degradation.degrade_disable_l2(base)
    assert info["manifest"]["edges_dropped"] == 1
    res = degradation._score_mode(base, cur, info, check_advisory_flip=False)
    assert res["would_be_false_findings"] == 1
    assert res["suppressed_on"] == 1 and res["leaked_on"] == 0
    assert res["fp_suppression_rate"] == 1.0
    assert res["erosion_visible"] is True


def test_degrade_starve_l0_below_floor_goes_advisory_and_unverified():
    base = _synthetic_facts()
    cur, info = degradation.degrade_starve_l0(base, 0.30)
    assert info["manifest"]["targets_starved"] >= 1
    res = degradation._score_mode(base, cur, info, check_advisory_flip=True)
    # any coverage drop below baseline is advisory (§11.6 "dropped below baseline")
    assert res["advisory"] is True
    assert res["leaked_on"] == 0
    lay = res["layering"]
    if lay["starved_rule_status"] is not None:
        assert lay["starved_rule_status"].startswith("UNVERIFIED")
    assert res.get("gating") is False


def test_degrade_lose_l0_all_coverage_gaps():
    base = _synthetic_facts()
    cur, info = degradation.degrade_lose_l0(base)
    assert info["manifest"]["targets_starved"] == 4
    res = degradation._score_mode(base, cur, info, check_advisory_flip=False)
    assert res["leaked_on"] == 0
    assert res["suppressed_on"] == res["would_be_false_findings"] > 0


# ------------------------------------------------------------------ toy end-to-end

@pytest.mark.integration
def test_mutation_harness_toy_end_to_end(tmp_path, monkeypatch):
    """All six operators through the REAL pipeline on the toy fixture: every injected
    signal detected (fn=0), zero false positives, rename classified as rename, and the
    id_aliases pin suppresses the rename drift (§6.3 partial survival)."""
    from mutate import harness
    monkeypatch.setitem(harness.SYSTEMS, "toy", dict(harness.SYSTEMS["toy"]))
    summary = harness.run_system("toy", instances=1, work_root=tmp_path / "work",
                                 fresh=True, keep_work=False,
                                 out_root=tmp_path / "out" / "toy")
    assert summary is not None
    tot = summary["totals"]
    assert tot["n"] >= 6                      # all six operators applicable on toy
    assert tot["fn"] == 0 and tot["fp"] == 0 and tot["tp"] > 0
    assert tot["rename_correct"] == tot["rename_total"] == 1
    assert tot["false_rename_clean"] == tot["false_rename_total"] == 1
    assert tot["alias_suppressed"] == tot["alias_total"] == 1
    # §14.1 artifact layout
    mdirs = list((tmp_path / "out" / "toy" / "mutation").glob("toy-*"))
    assert mdirs
    for f in ("label.json", "result.json", "drift.md"):
        assert (mdirs[0] / f).exists()


def test_skip_roslyn_knob(monkeypatch, tmp_path):
    from anon.paths import resolve_workspace
    from anon.stages import extract_csharp_facts
    monkeypatch.setenv("ANON_SKIP_ROSLYN", "1")
    ws = resolve_workspace(str(tmp_path), str(tmp_path / "arch"))
    assert extract_csharp_facts.run(ws) is None


# ------------------------------------------------------------------ analysis wiring

def _fake_out_tree(root: Path):
    mut = root / "eshop" / "mutation"
    mut.mkdir(parents=True)
    (mut / "mutation-summary.json").write_text(json.dumps({
        "schema": "rq6-mutation-summary/1", "system": "eshop", "language": "cs",
        "operators": {"remove_dep": {"applicable": True, "n": 2, "tp": 2, "fn": 0,
                                     "fp": 0, "detected": 2, "rename_total": 0,
                                     "rename_correct": 0, "false_rename_total": 0,
                                     "false_rename_clean": 0, "alias_total": 0,
                                     "alias_suppressed": 0},
                      "merge_targets": {"applicable": False, "n": 0}},
        "totals": {}}), encoding="utf-8")
    (mut / "degradation.json").write_text(json.dumps({
        "schema": "rq6-degradation/1", "system": "eshop", "language": "cs",
        "modes": {"disable_l2": {"would_be_false_findings": 5, "suppressed_on": 5,
                                 "leaked_on": 0, "fp_suppression_rate": 1.0,
                                 "advisory": False, "erosion_visible": True,
                                 "coverage_l0": 1.0}}}), encoding="utf-8")
    perf = root / "eshop" / "perf"
    perf.mkdir(parents=True)
    (perf / "rq7-perf.json").write_text(json.dumps({
        "schema": "rq7-perf/1", "system": "eshop", "language": "cs", "success": True,
        "size": {"kloc": 40.0, "targets": 19, "files": 680},
        "cold": {"wall_s": 100.0, "peak_rss_mb": 900.0, "stages": {"extract": 80.0}},
        "warm": {"wall_s": 20.0, "peak_rss_mb": 300.0, "stages": {"extract": 5.0}},
        "speedup": 5.0,
        "budget": {"cold_within": True, "warm_within": True}}), encoding="utf-8")
    # a perf-only Tier-B system must NOT join the fidelity corpus
    perf2 = root / "toy" / "perf"
    perf2.mkdir(parents=True)
    (perf2 / "rq7-perf.json").write_text(json.dumps({
        "schema": "rq7-perf/1", "system": "toy", "language": "cs", "success": True,
        "size": {"kloc": 0.5, "targets": 4, "files": 9},
        "cold": {"wall_s": 15.0, "peak_rss_mb": 250.0, "stages": {}},
        "warm": {"wall_s": 3.0, "peak_rss_mb": 60.0, "stages": {}},
        "speedup": 5.0,
        "budget": {"cold_within": True, "warm_within": True}}), encoding="utf-8")


def test_aggregate_stats_tables_wire_rq6_rq7(tmp_path):
    out_root = tmp_path / "out"
    _fake_out_tree(out_root)
    rows = list(agg.iter_rows(out_root, tmp_path / "refs"))
    for r in rows:
        r.setdefault("tier", "oss")
    axes = {r["axis"] for r in rows}
    assert {"RQ6-mut", "RQ6-deg", "RQ7"} <= axes
    # inapplicable operator emitted no rows (recorded, not faked)
    assert not [r for r in rows if r["axis"] == "RQ6-mut"
                and r["technique"] == "merge_targets"]
    # toy perf rows present (Tier-B scale point)
    assert [r for r in rows if r["axis"] == "RQ7" and r["system"] == "toy"]

    stats = stats_mod.compute_stats([{k: str(v) for k, v in r.items()} for r in rows])
    assert stats["rq6"]["mutation"]["overall"]["precision"] == 1.0
    assert stats["rq6"]["mutation"]["overall"]["recall"] == 1.0
    assert stats["rq6"]["degradation"]["overall"]["fp_suppression_rate"] == 1.0
    assert stats["rq6"]["degradation"]["overall"]["mcnemar_on_vs_off_p"] is not None
    assert stats["rq7"]["n_systems"] == 2
    assert stats["rq7"]["median_speedup"] == 5.0
    # Tier-B perf systems must not leak into the corpus
    assert "toy" not in stats["corpus"]["systems"]
    for macro in ("rqSixPrecision", "rqSixSuppressionRate", "rqSevenMedianSpeedup",
                  "rqSevenColdBudget"):
        assert macro in stats["summary_macros"]

    rq6_tex = tables.build_table("rq6_drift", [{k: str(v) for k, v in r.items()}
                                               for r in rows])
    assert "placeholder" not in rq6_tex and "remove\\_dep" in rq6_tex
    rq7_tex = tables.build_table("rq7_perf", [{k: str(v) for k, v in r.items()}
                                              for r in rows])
    assert "placeholder" not in rq7_tex and "eShop" in rq7_tex


def test_wilson_and_mcnemar_stdlib():
    lo, hi = stats_mod.wilson(9, 10)
    assert 0.55 < lo < 0.9 < hi <= 1.0
    assert stats_mod.wilson(0, 0) is None
    assert stats_mod.mcnemar_exact(0, 0) is None
    p = stats_mod.mcnemar_exact(10, 0)
    assert p is not None and p < 0.01
