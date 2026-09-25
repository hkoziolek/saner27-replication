"""Drift / governance hardening tests (plan §11) — Agent B.

Self-contained: in-memory fact models; the one disk-touching test writes only under
``tmp_path`` (never ``out/`` or the fixture ``architecture/``). Covers coverage-aware
scoping (coverage-gap vs drift), the §11.6 ADVISORY downgrade, §11.2 layering violations
+ ``UNVERIFIED (degraded)``, §11.5 ``diff_snapshots``, and §7.3 suspected-rename.
"""
from __future__ import annotations

from pathlib import Path

from anon import cli
from anon.paths import resolve_workspace
from anon.stages import drift_report as dr

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"
TOY_RULES = TOY / "architecture" / "rules"


def _facts(targets, rels, coverage=None, *, repo="r", commit="c"):
    return {
        "schema_version": "1.0",
        "provenance": {"repo": repo, "commit": commit, "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": [], "coverage": coverage or {}},
        "targets": targets, "relationships": rels,
    }


def _t(tid, **kw):
    base = {"id": tid, "name": tid.split(":")[-1], "type": "csproj", "language": "csharp"}
    base.update(kw)
    return base


def _r(src, tgt, evidence, **kw):
    base = {"id": f"rel:{src}->{tgt}", "source": src, "target": tgt, "evidence": evidence}
    base.update(kw)
    return base


# --- coverage-aware scoping: coverage-gap vs drift (§11.1) ---------------------------

def test_removed_edge_is_drift_when_layer_covered_both_runs():
    base = _facts([_t("x:t:A"), _t("x:t:B")],
                  [_r("x:t:A", "x:t:B", [{"type": "include", "detail": "h", "count": 3}])],
                  coverage={"x:t:A": {"L0": True, "L2": True}, "x:t:B": {"L0": True, "L2": True}})
    # current still extracts L2 for A and B but the include edge is gone -> real drift
    cur = _facts([_t("x:t:A"), _t("x:t:B")], [],
                 coverage={"x:t:A": {"L0": True, "L2": True}, "x:t:B": {"L0": True, "L2": True}})
    diff = dr.diff_facts(base, cur)
    assert ("x:t:A", "x:t:B") in diff["removed_edges"]
    assert ("x:t:A", "x:t:B") not in diff["coverage_gap_edges"]


def test_removed_edge_is_coverage_gap_when_layer_dropped():
    base = _facts([_t("x:t:A"), _t("x:t:B")],
                  [_r("x:t:A", "x:t:B", [{"type": "include", "detail": "h", "count": 3}])],
                  coverage={"x:t:A": {"L0": True, "L2": True}, "x:t:B": {"L0": True, "L2": True}})
    # current LOST L2 (e.g. clang OOM): the include edge disappearing is a coverage-gap,
    # NOT erosion — §11.1 must not cry wolf.
    cur = _facts([_t("x:t:A"), _t("x:t:B")], [],
                 coverage={"x:t:A": {"L0": True, "L2": False}, "x:t:B": {"L0": True, "L2": False}})
    diff = dr.diff_facts(base, cur)
    assert ("x:t:A", "x:t:B") in diff["coverage_gap_edges"]
    assert ("x:t:A", "x:t:B") not in diff["removed_edges"]


def test_removed_interop_only_edge_is_coverage_gap_not_drift():
    # Theme 2 / 2d: interop is toolchain-gated and never recorded in provenance.coverage,
    # so an interop-only edge that disappears must be a coverage-gap, NOT L0 build drift.
    base = _facts([_t("x:t:A"), _t("x:t:B")],
                  [_r("x:t:A", "x:t:B", [{"type": "interop", "detail": "pinvoke"}])],
                  coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}})
    cur = _facts([_t("x:t:A"), _t("x:t:B")], [],
                 coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}})
    diff = dr.diff_facts(base, cur)
    assert ("x:t:A", "x:t:B") in diff["coverage_gap_edges"]
    assert ("x:t:A", "x:t:B") not in diff["removed_edges"]


def test_removed_l0_edge_is_drift_positive_control():
    # Positive control: a `link` (L0) edge that disappears WHILE L0 is still extracted for
    # both endpoints IS real drift — proves the interop fix didn't over-broaden to L0.
    base = _facts([_t("x:t:A"), _t("x:t:B")],
                  [_r("x:t:A", "x:t:B", [{"type": "link", "detail": "lib"}])],
                  coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}})
    cur = _facts([_t("x:t:A"), _t("x:t:B")], [],
                 coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}})
    diff = dr.diff_facts(base, cur)
    assert ("x:t:A", "x:t:B") in diff["removed_edges"]
    assert ("x:t:A", "x:t:B") not in diff["coverage_gap_edges"]


def test_removed_runtime_only_edge_is_coverage_gap_not_drift():
    # Same scoping for the toolchain-gated `runtime` (dynamic) evidence layer.
    base = _facts([_t("x:t:A"), _t("x:t:B")],
                  [_r("x:t:A", "x:t:B", [{"type": "runtime", "detail": "trace"}])],
                  coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}})
    cur = _facts([_t("x:t:A"), _t("x:t:B")], [],
                 coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}})
    diff = dr.diff_facts(base, cur)
    assert ("x:t:A", "x:t:B") in diff["coverage_gap_edges"]
    assert ("x:t:A", "x:t:B") not in diff["removed_edges"]


def test_removed_target_is_coverage_gap_when_run_lost_l0():
    base = _facts([_t("x:t:A"), _t("x:t:B")], [],
                  coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}})
    # current dropped target B AND has no L0 coverage anywhere -> degraded run, coverage-gap
    cur = _facts([_t("x:t:A", external=False)], [], coverage={"x:t:A": {"L0": False}})
    diff = dr.diff_facts(base, cur)
    assert "x:t:B" in diff["coverage_gap_targets"]
    assert "x:t:B" not in diff["removed_targets"]


# --- §11.6 ADVISORY downgrade -------------------------------------------------------

def test_advisory_header_when_coverage_below_floor(tmp_path):
    # 1 of 2 first-party targets has L0 -> 50% < 80% floor -> ADVISORY, non-gating.
    cur = _facts([_t("x:t:A"), _t("x:t:B")], [],
                 coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": False}})
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    from anon.jsonio import dump_json
    dump_json(cur, ws.extracted_facts)               # no curated -> run() reads extracted
    md = dr.run(ws, baseline_path=None, commit="deadbeef")
    assert "ADVISORY" in md
    assert "coverage below floor" in md
    # writes only under tmp_path
    assert ws.drift_md.exists() and str(tmp_path) in str(ws.drift_md)


def test_advisory_when_coverage_dropped_below_baseline():
    base = _facts([_t("x:t:A"), _t("x:t:B")], [],
                  coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}})  # 100%
    cur = _facts([_t("x:t:A"), _t("x:t:B")], [],
                 coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}})
    # force a drop below baseline by making current 100% but baseline... invert:
    assert dr._dropped_below_baseline(base, cur) is False
    cur_degraded = _facts([_t("x:t:A"), _t("x:t:B")], [],
                          coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": False}})  # 50%
    assert dr._dropped_below_baseline(base, cur_degraded) is True


# --- §11.2 layering / fitness checks ------------------------------------------------

def test_layering_violation_detected():
    facts = _facts(
        [_t("x:t:ui", container_id="container:ui", container_name="UI"),
         _t("x:t:db", container_id="container:db", container_name="Database")],
        [_r("x:t:ui", "x:t:db", [{"type": "project_ref", "detail": "ref"}])])
    rules = {"forbidden": [{"from": "UI", "to": "Database"}]}
    findings = dr.check_layering(facts, rules)
    assert len(findings) == 1
    assert findings[0]["status"] == "VIOLATION"
    assert ("x:t:ui", "x:t:db") in findings[0]["edges"]


def test_layering_unverified_when_coverage_degraded():
    # no offending edge present, but the rule requires L2 coverage that's missing ->
    # UNVERIFIED (degraded), never a silent pass (§11.2 evidence-positive).
    facts = _facts(
        [_t("x:t:ui", container_name="UI"), _t("x:t:db", container_name="Database")],
        [], coverage={"x:t:ui": {"L0": True, "L2": False}, "x:t:db": {"L0": True, "L2": False}})
    rules = {"forbidden": [{"from": "UI", "to": "Database", "require_layers": ["L2"]}]}
    findings = dr.check_layering(facts, rules)
    assert findings[0]["status"] == "UNVERIFIED (degraded)"


def test_layering_ok_when_no_edge_and_coverage_present():
    facts = _facts(
        [_t("x:t:ui", container_name="UI"), _t("x:t:db", container_name="Database")],
        [], coverage={"x:t:ui": {"L0": True, "L2": True}, "x:t:db": {"L0": True, "L2": True}})
    rules = {"forbidden": [{"from": "UI", "to": "Database", "require_layers": ["L2"]}]}
    assert dr.check_layering(facts, rules)[0]["status"] == "OK"


# --- evaluate(): structured gate-able result (Theme 2 / 2c) -------------------------

def _evaluate_ws(tmp_path, cur, *, advisory_subdir):
    """Materialize current facts + a layering-rules.yaml with a violated forbidden pair,
    then return (workspace, result-of-evaluate)."""
    from anon.jsonio import dump_json
    root = tmp_path / advisory_subdir
    ws = resolve_workspace(root / "repo", arch_dir=root / "arch", rules_dir=root / "rules")
    dump_json(cur, ws.extracted_facts)               # no curated -> evaluate reads extracted
    ws.layering_rules.parent.mkdir(parents=True, exist_ok=True)
    ws.layering_rules.write_text(
        'forbidden:\n  - from: "UI"\n    to: "Database"\n', encoding="utf-8", newline="")
    return ws, dr.evaluate(ws, baseline_path=None, commit="cafef00d")


def test_evaluate_gates_on_violation_when_not_advisory(tmp_path):
    # UI -> Database edge exists (forbidden) AND L0 coverage is 100% (not advisory) -> gating.
    cur = _facts(
        [_t("x:t:ui", container_name="UI"), _t("x:t:db", container_name="Database")],
        [_r("x:t:ui", "x:t:db", [{"type": "project_ref", "detail": "ref"}])],
        coverage={"x:t:ui": {"L0": True}, "x:t:db": {"L0": True}})
    ws, result = _evaluate_ws(tmp_path, cur, advisory_subdir="ok")
    assert result["advisory"] is False
    assert result["violations"] and result["violations"][0]["status"] == "VIOLATION"
    assert result["gating"] is True
    # run() still returns the same md string evaluate() produced.
    assert dr.run(ws, baseline_path=None, commit="cafef00d") == result["md"]


def test_evaluate_does_not_gate_when_advisory(tmp_path):
    # Same forbidden violation, but L0 coverage is 50% < floor -> advisory -> never gating.
    cur = _facts(
        [_t("x:t:ui", container_name="UI"), _t("x:t:db", container_name="Database")],
        [_r("x:t:ui", "x:t:db", [{"type": "project_ref", "detail": "ref"}])],
        coverage={"x:t:ui": {"L0": True}, "x:t:db": {"L0": False}})
    _, result = _evaluate_ws(tmp_path, cur, advisory_subdir="advisory")
    assert result["advisory"] is True
    assert result["violations"]               # the violation is still surfaced...
    assert result["gating"] is False          # ...but a degraded run never gates (§11.6).


# --- §7.3 suspected rename / move ---------------------------------------------------

def test_suspected_rename_when_new_appears_and_old_disappears():
    base = _facts([_t("x:t:OldName")], [], coverage={"x:t:OldName": {"L0": True}})
    cur = _facts([_t("x:t:NewName")], [], coverage={"x:t:NewName": {"L0": True}})
    diff = dr.diff_facts(base, cur)
    assert "x:t:NewName" in diff["new_targets"]
    assert "x:t:OldName" in diff["removed_targets"]
    renames = {(r["old"], r["new"]) for r in diff["suspected_renames"]}
    assert ("x:t:OldName", "x:t:NewName") in renames
    # surfaced in the rendered report
    md = dr.render(diff, "c", advisory=False, coverage=1.0)
    assert "Suspected renames" in md and "suspected rename/move" in md


# --- §11.5 diff_snapshots (release-to-release) --------------------------------------

def test_diff_snapshots_add_remove_change():
    a = _facts(
        [_t("x:t:A", container_id="container:core", container_name="Core"),
         _t("x:t:Gone", container_id="container:gone", container_name="Gone")],
        [_r("x:t:A", "x:t:Gone", [{"type": "project_ref", "detail": "r"}], kind="uses")])
    b = _facts(
        [_t("x:t:A", container_id="container:core", container_name="Core Library"),  # renamed
         _t("x:t:New", container_id="container:new", container_name="New")],          # added
        [_r("x:t:A", "x:t:New", [{"type": "project_ref", "detail": "r"}], kind="calls")])  # added rel

    snap = dr.diff_snapshots(a, b)
    assert "container:new" in snap["added_containers"]
    assert "container:gone" in snap["removed_containers"]
    changed = {c["id"]: c for c in snap["changed_containers"]}
    assert changed["container:core"]["from_name"] == "Core" and \
        changed["container:core"]["to_name"] == "Core Library"
    assert ("x:t:A", "x:t:New") in snap["added_relationships"]
    assert ("x:t:A", "x:t:Gone") in snap["removed_relationships"]

    md = dr.render_snapshots(snap, "v1.0", "v2.0")
    assert "v1.0 → v2.0" in md
    assert "Core" in md and "Core Library" in md


def test_diff_snapshots_changed_relationship_kind():
    a = _facts([_t("x:t:A"), _t("x:t:B")],
               [_r("x:t:A", "x:t:B", [{"type": "project_ref", "detail": "r"}], kind="uses")])
    b = _facts([_t("x:t:A"), _t("x:t:B")],
               [_r("x:t:A", "x:t:B", [{"type": "project_ref", "detail": "r"}], kind="depends on")])
    snap = dr.diff_snapshots(a, b)
    changed = snap["changed_relationships"]
    assert len(changed) == 1
    assert changed[0]["from_kind"] == "uses" and changed[0]["to_kind"] == "depends on"


# --- §11.2/§14 P6: `arch drift` surfaces a seeded layering violation end-to-end ----

def test_arch_drift_cli_flags_seeded_layering_violation(tmp_path):
    """End-to-end via the CLI: a forbidden edge that EXISTS in the curated graph is caught.

    Also covers the `arch drift --rules-dir` wiring (drift must read layering-rules.yaml).
    """
    arch = tmp_path / "arch"
    assert cli.main(["run", "--repo", str(TOY), "--arch-dir", str(arch),
                     "--rules-dir", str(TOY_RULES), "--no-llm"]) == 0
    # The toy graph has Web Frontend -> Messaging; forbid it (+ a reverse rule that holds).
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "layering-rules.yaml").write_text(
        'forbidden:\n'
        '  - from: "Web Frontend"\n    to: "Messaging"\n'
        '  - from: "Messaging"\n    to: "Web Frontend"\n', encoding="utf-8")
    assert cli.main(["drift", "--repo", str(TOY), "--arch-dir", str(arch),
                     "--rules-dir", str(rules)]) == 0
    md = (arch / "generated" / "architecture-drift.md").read_text(encoding="utf-8")
    assert "Layering / fitness checks" in md
    assert "VIOLATION" in md and "Web Frontend" in md and "Messaging" in md
    assert "OK" in md   # the reverse rule has no matching edge -> passes


def test_empty_first_party_set_is_zero_coverage_and_advisory(tmp_path):
    """An EMPTY first-party target set is 0% L0 coverage, not vacuously 100%: when the
    build-graph extractor produced nothing, the run must be advisory. The old convention
    (1.0) let a target added while the extractor was lost go unreported and unflagged; the
    RQ6 combined change-plus-degradation experiment (mutate/combined.py, 2026-08-30) found it."""
    empty = {"targets": [{"id": "ext:pkg", "external": True}], "relationships": [],
             "provenance": {"coverage": {}}}
    assert dr._coverage_l0(empty) == 0.0
    baseline = {"targets": [{"id": "a", "external": False}], "relationships": [],
                "provenance": {"coverage": {"a": {"L0": True}}}}
    assert dr._coverage_l0(baseline) == 1.0
    assert dr._dropped_below_baseline(baseline, empty) is True
