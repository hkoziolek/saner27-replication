"""Engine §3 — the `arch risk` structural maintainability-risk stage.

Locked down here:
  * the honest header — NOT-ATAM disclaimer, ran-vs-degraded detector list, explicit
    `not_evaluated` entries ("couldn't check" is never "none found", §3.1);
  * transparent severity — every finding carries `severity_inputs` and the formulas are
    echoed in the report (§3.3);
  * detectors fire on synthetic graphs that exhibit each risk (cycle, hub, SPOF,
    coverage gap, churn over a real throwaway git repo);
  * a below-floor run downgrades every finding to advisory (§2.5);
  * determinism — same facts → byte-identical artifacts.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import run_toy_pipeline

from anon.jsonio import dump_json, load_json
from anon.model import content_hash
from anon.paths import resolve_workspace
from anon.stages import risk


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    return run_toy_pipeline(tmp_path_factory.mktemp("riskrun") / "architecture")


def _facts(targets, relationships, **extra):
    base = {"targets": targets, "relationships": relationships}
    base.update(extra)
    return base


def _t(tid, cid, name=None, **kw):
    t = {"id": tid, "name": name or tid, "container_id": cid,
         "container_name": cid.split(":")[-1]}
    t.update(kw)
    return t


def _r(s, t, w=5, evidence=None, **kw):
    r = {"source": s, "target": t, "weight": w, "is_declared_dependency": True,
         "evidence": evidence or [{"type": "project_ref", "detail": f"{s}->{t}"}]}
    r.update(kw)
    return r


# --------------------------------------------------------------------------- header

def test_report_header_is_honest(toy):
    report = risk.run(toy.ws)
    assert report["schema"] == risk.RISK_SCHEMA
    assert report["derived_from_hash"] == content_hash(load_json(toy.ws.curated_facts))
    assert "NOT an ATAM" in report["not_atam"]
    assert report["severity_formulas"]["hub"]
    nev = {d["detector"] for d in report["detectors"]["not_evaluated"]}
    assert "layering" in nev          # toy has no layering-rules.yaml
    assert "deployment-spof" in nev   # no deployment facet
    deg = {d["detector"] for d in report["detectors"]["degraded"]}
    assert "main-sequence" in deg     # pristine L0 run has no visibility-bearing L2
    md = (toy.ws.generated / "risk-report.md").read_text(encoding="utf-8")
    assert "Not ATAM" in md and "couldn't check" in md and 'NOT "none found"' in md


def test_determinism_byte_identical(toy):
    risk.run(toy.ws)
    a = toy.ws.risk_report_json.read_bytes()
    a_md = (toy.ws.generated / "risk-report.md").read_bytes()
    risk.run(toy.ws)
    assert toy.ws.risk_report_json.read_bytes() == a
    assert (toy.ws.generated / "risk-report.md").read_bytes() == a_md


# --------------------------------------------------------------------------- detectors

def test_cycle_detected_and_cited(toy):
    facts = _facts([_t("a", "container:A"), _t("b", "container:B")],
                   [_r("a", "b"), _r("b", "a")])
    report = risk.build_report(toy.ws, facts)
    cyc = [f for f in report["findings"] if f["kind"] == "cycle"]
    assert len(cyc) == 1
    assert cyc[0]["elements"] == ["container:A", "container:B"]
    assert cyc[0]["evidence"], "the cycle cites its constituent edges"
    assert cyc[0]["severity_inputs"]["total_weight"] == 10


def test_hub_detected_with_recomputable_inputs(toy):
    targets = [_t("hub", "container:hub", "Hub")]
    rels = []
    for i in range(5):
        targets.append(_t(f"n{i}", f"container:n{i}"))
        rels.append(_r(f"n{i}", "hub"))  # 5 in
    for i in range(5, 8):
        targets.append(_t(f"n{i}", f"container:n{i}"))
        rels.append(_r("hub", f"n{i}"))  # 3 out
    report = risk.build_report(toy.ws, _facts(targets, rels))
    hubs = [f for f in report["findings"] if f["kind"] == "hub"]
    assert hubs and hubs[0]["elements"] == ["container:hub"]
    si = hubs[0]["severity_inputs"]
    assert si["Ca"] == 5 and si["Ce"] == 3 and si["neighbors"] == 8
    # the documented formula is recomputable by hand (§3.3)
    assert hubs[0]["severity"] == round(min(1.0, 8 / (si["n_containers"] - 1)), 2)
    assert "ripple" in hubs[0]["message"]


def test_severity_calc_substitutes_concrete_values(toy):
    """§3.3 explainability: every finding also carries the formula WITH its concrete
    inputs substituted, ending in the emitted score — exactly what the GUI risk popover
    renders, so the user can follow the arithmetic, not just the recipe."""
    targets = [_t("hub", "container:hub", "Hub")]
    rels = []
    for i in range(5):
        targets.append(_t(f"n{i}", f"container:n{i}"))
        rels.append(_r(f"n{i}", "hub"))
    for i in range(5, 8):
        targets.append(_t(f"n{i}", f"container:n{i}"))
        rels.append(_r("hub", f"n{i}"))
    rels += [_r("n0", "n1"), _r("n1", "n0")]  # a 2-cycle alongside the hub
    report = risk.build_report(toy.ws, _facts(targets, rels))
    kinds = {f["kind"] for f in report["findings"]}
    assert {"hub", "cycle"} <= kinds
    for f in report["findings"]:
        assert f["severity_calc"], f"no severity_calc for {f['kind']}"
        assert f["severity_calc"].endswith(f"= {f['severity']:.2f}"), \
            "the calc string ends in the score it explains"
    hub = next(f for f in report["findings"] if f["kind"] == "hub")
    n = hub["severity_inputs"]["n_containers"]
    assert hub["severity_calc"] == f"min(1, 8/({n}−1)) = {hub['severity']:.2f}"
    assert "Calc:" in risk.render(report)


def test_layering_violation_finding(toy, tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "layering-rules.yaml").write_text(
        'forbidden:\n  - from: "container:A"\n    to: "container:B"\n',
        encoding="utf-8")
    ws = resolve_workspace(toy.ws.repo, toy.ws.arch_dir, rules_dir)
    facts = _facts([_t("a", "container:A"), _t("b", "container:B")],
                   [_r("a", "b")])
    report = risk.build_report(ws, facts)
    lv = [f for f in report["findings"] if f["kind"] == "layering-violation"]
    assert lv and lv[0]["severity"] == 0.9
    assert "layering" in report["detectors"]["ran"]


def test_deployment_spof_detected(toy):
    targets = [_t(f"t{i}", f"container:c{i}") for i in range(3)]
    rels = []
    deployment = {"nodes": [{"id": "infra:database:db", "kind": "infra"}],
                  "edges": [{"source": f"t{i}", "target": "infra:database:db",
                             "kind": "calls"} for i in range(3)]}
    report = risk.build_report(toy.ws, _facts(targets, rels, deployment=deployment))
    spof = [f for f in report["findings"] if f["kind"] == "deployment-spof"]
    assert spof and spof[0]["severity_inputs"]["containers_routed"] == 3
    assert "deployment-spof" in report["detectors"]["ran"]


def test_interop_seam_flagged(toy):
    facts = _facts(
        [_t("cs", "container:managed"), _t("cpp", "container:native")],
        [_r("cs", "cpp", evidence=[{"type": "interop", "detail": "DllImport NativeMath"}])])
    report = risk.build_report(toy.ws, facts)
    seams = [f for f in report["findings"] if f["kind"] == "interop-seam"]
    assert seams and "ABI" in seams[0]["message"]
    assert seams[0]["evidence"][0]["type"] == "interop"


def test_coverage_gap_is_advisory_never_red(toy):
    facts = _facts([_t("a", "container:A"), _t("b", "container:A")],
                   [],
                   provenance={"coverage": {"a": {"L0": True}, "b": {}}})
    report = risk.build_report(toy.ws, facts)
    gaps = [f for f in report["findings"] if f["kind"] == "coverage-gap"]
    assert gaps and gaps[0]["advisory"] is True
    assert "ABSENCE" in gaps[0]["message"]


def test_below_floor_downgrades_everything_to_advisory(toy):
    # a cyclic model whose coverage is below the floor: the cycle finding itself
    # must carry advisory=True (§2.5 — never confidently red over a thin model).
    facts = _facts([_t("a", "container:A"), _t("b", "container:B")],
                   [_r("a", "b"), _r("b", "a")],
                   provenance={"coverage": {"a": {}, "b": {}}})
    report = risk.build_report(toy.ws, facts)
    assert report["coverage"]["advisory"] is True
    assert all(f.get("advisory") for f in report["findings"])


def test_martin_runs_when_visibility_l2_present(toy):
    ev_pub = [{"type": "symbol_use", "detail": "PublicApi", "visibility": "public"}]
    targets = [_t("a", "container:A"), _t("b", "container:B"), _t("c", "container:C")]
    rels = [_r("a", "b", evidence=ev_pub), _r("c", "b")]
    report = risk.build_report(toy.ws, _facts(targets, rels))
    assert "main-sequence" in report["detectors"]["ran"]
    assert not any(d["detector"] == "main-sequence"
                   for d in report["detectors"]["degraded"])


# --------------------------------------------------------------------------- churn

@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_churn_over_a_real_git_history(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "Hot").mkdir(parents=True)
    (repo / "src" / "Cold").mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                        "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    git("init")
    for i in range(3):  # 3 commits touching Hot, 1 touching Cold
        (repo / "src" / "Hot" / "a.cs").write_text(f"// v{i}\n", encoding="utf-8")
        if i == 0:
            (repo / "src" / "Cold" / "b.cs").write_text("// v0\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-m", f"c{i}")

    ws = resolve_workspace(repo)
    facts = _facts(
        [_t("hot", "container:hot", path="src/Hot/Hot.csproj"),
         _t("cold", "container:cold", path="src/Cold/Cold.csproj")],
        [_r("cold", "hot", w=9)])
    report = risk.build_report(ws, facts)
    assert "churn-coupling" in report["detectors"]["ran"]
    churn = [f for f in report["findings"] if f["kind"] == "churn-coupling"]
    assert churn, "the hot, coupled container is a hotspot"
    si = churn[0]["severity_inputs"]
    assert si["churn_commits"] == 3 and si["granularity"] == "directory"
    assert churn[0]["elements"] == ["container:hot"]


def test_churn_not_evaluated_outside_git(toy, tmp_path):
    # toy runs extract from a pristine COPY under tmp — not a git repo → honest skip
    report = risk.run(toy.ws)
    nev = {d["detector"]: d["reason"] for d in report["detectors"]["not_evaluated"]}
    assert "churn-coupling" in nev
    assert "git" in nev["churn-coupling"]


# --------------------------------------------------------------------------- ranking

def test_findings_ranked_by_severity(toy):
    targets = [_t("a", "container:A"), _t("b", "container:B")]
    rels = [_r("a", "b"), _r("b", "a",
                             evidence=[{"type": "interop", "detail": "x"}])]
    report = risk.build_report(toy.ws, _facts(targets, rels))
    sevs = [f["severity"] for f in report["findings"]]
    assert sevs == sorted(sevs, reverse=True)


def test_top_caps_findings_but_counts_total(toy):
    targets = [_t("a", "container:A"), _t("b", "container:B")]
    rels = [_r("a", "b"), _r("b", "a",
                             evidence=[{"type": "interop", "detail": "x"}])]
    report = risk.build_report(toy.ws, _facts(targets, rels), top=1)
    assert len(report["findings"]) == 1
    assert report["total_findings"] >= 2
