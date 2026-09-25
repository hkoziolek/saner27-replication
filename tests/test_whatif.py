"""Engine §7.1 — the `arch whatif` simulator.

Locked down here:
  * the moat claim — ops only re-lift EXISTING evidence into different container
    boundaries (extract/merge/move) or remove relationships hypothetically (cut);
    targets/edges are never invented;
  * the artifacts are PROPOSALS under generated/whatif/ only — the curated facts on
    disk are byte-untouched;
  * canonicalize-before-diff and determinism (same op → byte-identical artifacts);
  * `arch risk --with-scenarios` attaches cut/extract proposals to cycle/hub findings.
"""
from __future__ import annotations

import pytest

from conftest import run_toy_pipeline

from anon.jsonio import load_json
from anon.stages import risk, whatif


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    return run_toy_pipeline(tmp_path_factory.mktemp("whatifrun") / "architecture")


def _t(tid, cid, name=None, **kw):
    t = {"id": tid, "name": name or tid, "container_id": cid,
         "container_name": cid.split(":")[-1]}
    t.update(kw)
    return t


def _r(s, t, w=5, **kw):
    r = {"source": s, "target": t, "weight": w, "is_declared_dependency": True,
         "evidence": [{"type": "project_ref", "detail": f"{s}->{t}"}]}
    r.update(kw)
    return r


def _facts(targets, relationships):
    return {"targets": targets, "relationships": relationships}


# --------------------------------------------------------------------------- ops

def test_merge_relifts_without_inventing(toy):
    facts = _facts([_t("a", "container:A"), _t("b", "container:B"),
                    _t("c", "container:C")],
                   [_r("a", "b"), _r("c", "a")])
    report = whatif.simulate(toy.ws, facts, "merge",
                             ids=["container:A", "container:B"], name="AB")
    assert report["before"]["containers"] == 3
    assert report["after"]["containers"] == 2
    # the a->b edge became intra-container; c->a survives as c->merged
    assert report["after"]["container_edges"] == 1
    assert "container:whatif:ab" in report["diff"]["added_containers"]
    assert sorted(report["diff"]["removed_containers"]) == ["container:A",
                                                            "container:B"]


def test_extract_breaks_nothing_and_cites_op(toy):
    facts = _facts([_t("a1", "container:A"), _t("a2", "container:A"),
                    _t("b", "container:B")],
                   [_r("b", "a1"), _r("a2", "a1")])
    report = whatif.simulate(toy.ws, facts, "extract", members=["a1"], name="Core")
    assert report["after"]["containers"] == 3
    assert report["op"] == {"op": "extract", "members": ["a1"],
                            "new_container": "container:whatif:core", "name": "Core"}
    # the formerly intra-container a2->a1 edge now surfaces as a container edge —
    # re-lifted from EXISTING evidence, not invented
    assert report["after"]["container_edges"] == 2


def test_cut_removes_edge_hypothetically(toy):
    facts = _facts([_t("a", "container:A"), _t("b", "container:B")],
                   [_r("a", "b"), _r("b", "a")])
    report = whatif.simulate(toy.ws, facts, "cut",
                             frm="container:A", to="container:B")
    assert report["before"]["cycles"] == 1
    assert report["after"]["cycles"] == 0
    assert report["delta"]["cycles"] == -1
    assert report["delta"]["cycles_broken"] == [["container:A", "container:B"]]
    assert report["op"]["removed_relationships"] == [{"source": "a", "target": "b"}]


def test_move_to_existing_container(toy):
    facts = _facts([_t("a", "container:A"), _t("b", "container:B")],
                   [_r("a", "b")])
    report = whatif.simulate(toy.ws, facts, "move", members=["a"],
                             to="container:B")
    assert report["after"]["containers"] == 1
    assert report["after"]["container_edges"] == 0  # became intra-container


def test_bad_params_raise_whatif_error(toy):
    facts = _facts([_t("a", "container:A")], [])
    with pytest.raises(whatif.WhatifError):
        whatif.simulate(toy.ws, facts, "extract", members=["nope"], name="X")
    with pytest.raises(whatif.WhatifError):
        whatif.simulate(toy.ws, facts, "merge", ids=["container:A"], name="X")
    with pytest.raises(whatif.WhatifError):
        whatif.simulate(toy.ws, facts, "cut", frm="container:A", to="container:A")
    with pytest.raises(whatif.WhatifError):
        whatif.simulate(toy.ws, facts, "move", members=["a"], to="container:Nope")


def test_container_resolves_by_display_name(toy):
    facts = _facts([_t("a", "container:A"), _t("b", "container:B")], [_r("a", "b")])
    facts["targets"][1]["container_name"] = "The B Side"
    report = whatif.simulate(toy.ws, facts, "cut", frm="container:A", to="The B Side")
    assert report["op"]["to"] == "container:B"


# --------------------------------------------------------------------------- artifacts

def test_run_writes_only_whatif_dir_and_is_deterministic(toy):
    curated_before = toy.ws.curated_facts.read_bytes()
    report = whatif.run(toy.ws, "merge",
                        ids=["container:coreLibrary", "container:messaging"],
                        name="Core Platform")
    assert report is not None
    slug = report["scenario"]
    json_path = toy.ws.whatif_dir / f"{slug}.json"
    md_path = toy.ws.whatif_dir / f"{slug}.md"
    a_json, a_md = json_path.read_bytes(), md_path.read_bytes()
    assert b"PROPOSAL" in a_json and b"PROPOSAL" in a_md
    # the curated facts are byte-untouched — nothing is ever applied (§7.1)
    assert toy.ws.curated_facts.read_bytes() == curated_before
    # same op → same default slug → byte-identical artifacts
    whatif.run(toy.ws, "merge",
               ids=["container:coreLibrary", "container:messaging"],
               name="Core Platform")
    assert json_path.read_bytes() == a_json
    assert md_path.read_bytes() == a_md


def test_render_shows_proposal_and_diff(toy):
    facts = _facts([_t("a", "container:A"), _t("b", "container:B")],
                   [_r("a", "b"), _r("b", "a")])
    report = whatif.simulate(toy.ws, facts, "cut",
                             frm="container:A", to="container:B")
    md = whatif.render(report)
    assert "PROPOSAL" in md
    assert "cycles: 1 → 0 (-1)" in md
    assert "Architecture diff  (current → whatif:cut)" in md


# --------------------------------------------------------- layout twin (GUI §5.4)

def test_layout_twin_is_engine_emitted_and_deterministic(toy):
    """The §5.4 canvas form rides an ENGINE-emitted layout twin (iron rule GUI §2.1):
    both sides laid out by the §6.9 engine, the hypothetical container present on the
    after side only, overlap-free, and a pure function of facts + op."""
    from anon import layout as layout_mod
    facts = _facts([_t("a1", "container:A"), _t("a2", "container:A"),
                    _t("b", "container:B")],
                   [_r("b", "a1"), _r("a2", "a1")])
    report = whatif.simulate(toy.ws, facts, "extract", members=["a1"], name="Core")
    twin = report["layout"]
    assert twin["engine"] == layout_mod.ENGINE
    before_ids = {n["id"] for n in twin["before"]["nodes"]}
    after_ids = {n["id"] for n in twin["after"]["nodes"]}
    assert "container:whatif:core" in after_ids - before_ids
    assert before_ids <= after_ids | {"container:whatif:core"}
    for side in ("before", "after"):
        assert twin[side]["metrics"]["node_overlaps"] == 0
        assert all(isinstance(n["x"], int) and isinstance(n["y"], int)
                   for n in twin[side]["nodes"])
    # the re-lifted a2->a1 edge surfaces on the after canvas, engine-routed
    after_edges = {(e["source"], e["target"]) for e in twin["after"]["edges"]}
    assert ("container:A", "container:whatif:core") in after_edges
    # deterministic: the same op over the same facts -> the identical twin
    again = whatif.simulate(toy.ws, facts, "extract", members=["a1"], name="Core")
    assert again["layout"] == twin


def test_simulate_without_layout_omits_the_twin(toy):
    facts = _facts([_t("a", "container:A"), _t("b", "container:B")], [_r("a", "b")])
    report = whatif.simulate(toy.ws, facts, "cut", frm="container:A",
                             to="container:B", with_layout=False)
    assert "layout" not in report


# ------------------------------------------------- risk --with-scenarios (engine §3.4)

def test_risk_with_scenarios_attaches_proposals(toy):
    # a 2-cycle plus a hub with two members and external dependents
    targets = [_t("a", "container:A"), _t("b", "container:B"),
               _t("h1", "container:hub"), _t("h2", "container:hub")]
    rels = [_r("a", "b"), _r("b", "a")]
    for i in range(4):
        targets.append(_t(f"n{i}", f"container:n{i}"))
        rels.append(_r(f"n{i}", "h1"))
    report = risk.build_report(toy.ws, _facts(targets, rels), with_scenarios=True)
    cyc = [f for f in report["findings"] if f["kind"] == "cycle"]
    assert cyc and cyc[0]["scenario"]["op"]["op"] == "cut"
    assert "PROPOSAL" in cyc[0]["scenario"]["note"]
    assert cyc[0]["scenario"]["delta"]["cycles"] == -1
    hubs = [f for f in report["findings"] if f["kind"] == "hub"
            and f["elements"] == ["container:hub"]]
    assert hubs and hubs[0]["scenario"]["op"]["op"] == "extract"
    assert hubs[0]["scenario"]["op"]["members"] == ["h1"]
    assert "hub_fan_in" in hubs[0]["scenario"]
    # a risk finding carries op + delta only — never coordinates (with_layout=False)
    assert all("layout" not in f.get("scenario", {})
               for f in report["findings"] if "scenario" in f)


def test_risk_without_scenarios_attaches_nothing(toy):
    facts = _facts([_t("a", "container:A"), _t("b", "container:B")],
                   [_r("a", "b"), _r("b", "a")])
    report = risk.build_report(toy.ws, facts)
    assert all("scenario" not in f for f in report["findings"])


def test_extract_refuses_colliding_whatif_container_id(toy):
    """A pre-existing container:whatif:<slug> id must refuse, not silently merge."""
    facts = _facts([_t("a", "container:whatif:core", "A"), _t("b", "container:B")],
                   [_r("b", "a")])
    with pytest.raises(whatif.WhatifError, match="already exists"):
        whatif.simulate(toy.ws, facts, "extract", members=["b"], name="Core")
    with pytest.raises(whatif.WhatifError, match="already exists"):
        whatif.simulate(toy.ws, facts, "merge",
                        ids=["container:whatif:core", "container:B"], name="Core")


def test_module_entry_point_survives_non_utf8_stdout(toy):
    """`python -m anon.stages.whatif` with a redirected (locale-encoded) stdout
    must not die on the report's `→` — the §22.3 reconfig is shared, not CLI-only."""
    import os
    import subprocess
    import sys
    env = {**os.environ}
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    out = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-m", "anon.stages.whatif", "merge",
         "--repo", str(toy.ws.repo), "--arch-dir", str(toy.ws.arch_dir),
         "--rules-dir", str(toy.ws.rules_dir),
         "--ids", "container:coreLibrary", "--ids", "container:messaging",
         "--name", "Audit Merge"],
        capture_output=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    assert "PROPOSAL" in out.stdout.decode("utf-8", "replace")
