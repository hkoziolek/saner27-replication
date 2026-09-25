"""Engine §7.4 — decision archaeology (`arch adr stubs`).

Locked down here:
  * the hard line — ## Decision / ## Rationale / ## Rejected alternatives are a literal
    `TODO (human)` in every stub; only the evidence section is filled;
  * the detectors — sole-provider (consistently-enforced pattern) and
    transport-inconsistency (majority seam kind with a small minority);
  * noise control — scale/minority-share gates, the hard cap, and suppression of
    candidates an existing ADR already covers;
  * determinism + the machine-owned sweep of generated/adr-stubs/.
"""
from __future__ import annotations

import pytest

from conftest import run_toy_pipeline

from anon import adr
from anon.jsonio import load_json


def _t(tid, cid, name=None, **kw):
    t = {"id": tid, "name": name or tid, "container_id": cid,
         "container_name": cid.split(":")[-1]}
    t.update(kw)
    return t


def _r(s, t, w=5, kind="project_ref"):
    return {"source": s, "target": t, "weight": w, "is_declared_dependency": True,
            "evidence": [{"type": kind, "detail": f"{s}->{t}"}]}


def _sole_provider_facts(n_sources=3):
    """n containers all link-depending on one `container:db`."""
    targets = [_t("db", "container:db", "Db")]
    rels = []
    for i in range(n_sources):
        targets.append(_t(f"s{i}", f"container:s{i}"))
        rels.append(_r(f"s{i}", "db", kind="link"))
    return {"targets": targets, "relationships": rels}


def _transport_facts(n_contract=4, n_runtime=1):
    """Most sources talk via contract seams; a small minority uses runtime."""
    targets = [_t("hub", "container:hub", "Hub")]
    rels = []
    for i in range(n_contract):
        targets.append(_t(f"c{i}", f"container:c{i}"))
        rels.append(_r(f"c{i}", "hub", kind="contract"))
    for i in range(n_runtime):
        targets.append(_t(f"r{i}", f"container:r{i}"))
        rels.append(_r(f"r{i}", "hub", kind="runtime"))
    return {"targets": targets, "relationships": rels}


# --------------------------------------------------------------------------- detectors

def test_sole_provider_detected_and_cited():
    result = adr.decision_candidates(_sole_provider_facts(), [])
    sole = [c for c in result["candidates"] if c["kind"] == "sole-provider"]
    assert len(sole) == 1
    c = sole[0]
    assert c["elements"] == ["container:db"]
    assert c["n_sources"] == 3
    assert c["evidence"], "the pattern cites its constituent edges"
    assert "link" in c["title"]


def test_sole_provider_needs_enough_sources():
    result = adr.decision_candidates(_sole_provider_facts(n_sources=2), [])
    assert result["candidates"] == []


def test_transport_inconsistency_detected():
    result = adr.decision_candidates(_transport_facts(), [])
    inc = [c for c in result["candidates"] if c["kind"] == "transport-inconsistency"]
    assert len(inc) == 1
    c = inc[0]
    assert "4 of 5" in c["statement"]
    assert c["elements"] == ["container:r0"]
    assert c["minority"][0]["id"] == "container:r0"


def test_large_minority_is_not_an_inconsistency():
    # 3 contract vs 2 runtime → minority share 0.4 > 0.34 → no candidate (noise control)
    result = adr.decision_candidates(_transport_facts(3, 2), [])
    assert [c for c in result["candidates"]
            if c["kind"] == "transport-inconsistency"] == []


# --------------------------------------------------------------------------- noise control

def test_cap_is_enforced_and_reported():
    facts = _sole_provider_facts()
    result = adr.decision_candidates(facts, [], cap=0)
    assert result["candidates"] == []
    assert result["dropped_over_cap"] == 1


def test_existing_adr_suppresses_candidate():
    facts = _sole_provider_facts()
    # a single-token target name must clear the 4-char floor to anchor an ADR link
    facts["targets"][0]["name"] = "Database"
    adrs = [{"id": "0007", "title": "Use Database as the single access layer",
             "status": "accepted", "path": "docs/adr/0007.md"}]
    result = adr.decision_candidates(facts, adrs)
    assert result["candidates"] == []
    assert len(result["suppressed"]) == 1
    assert "existing ADR" in result["suppressed"][0]["reason"]


# --------------------------------------------------------------------------- the hard line

def test_stub_rationale_is_literal_human_todo():
    result = adr.decision_candidates(_sole_provider_facts(), [])
    md = adr.render_stub(result["candidates"][0])
    for section in ("## Decision", "## Rationale", "## Rejected alternatives"):
        assert section in md
        after = md.split(section, 1)[1].lstrip()
        assert after.startswith("TODO (human)"), \
            f"{section} must be a literal human TODO — never machine-filled (§7.4)"
    assert "## Evidence" in md and "container_edge" in md
    assert "CANDIDATE" in md


# --------------------------------------------------------------------------- artifacts

@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    return run_toy_pipeline(tmp_path_factory.mktemp("stubsrun") / "architecture")


def test_run_stubs_writes_swept_deterministic_artifacts(toy):
    report = adr.run_stubs(toy.ws)
    assert report is not None
    assert report["schema"] == adr.STUBS_SCHEMA
    a = toy.ws.adr_candidate_decisions_json.read_bytes()
    # plant a stale stub — the dir is machine-owned and must be swept
    stale = toy.ws.adr_stubs_dir / "stale-candidate.md"
    stale.write_text("old", encoding="utf-8")
    adr.run_stubs(toy.ws)
    assert not stale.exists()
    assert toy.ws.adr_candidate_decisions_json.read_bytes() == a
    summary = adr.render_stub_summary(report)
    assert "NEVER invented" in summary


def test_whisper_weight_pattern_is_gated():
    """§7.4 noise control: a pattern must be high-WEIGHT, not just wide — three bare
    package_refs (weight 1 each, total 3) stay below _PATTERN_MIN_WEIGHT."""
    facts = _sole_provider_facts()
    for r in facts["relationships"]:
        r["weight"] = 1
    result = adr.decision_candidates(facts, [])
    assert result["candidates"] == []
    # transport seams below the weight gate are likewise not a pattern
    tfacts = _transport_facts()
    for r in tfacts["relationships"]:
        r["weight"] = 0
    assert adr.decision_candidates(tfacts, [])["candidates"] == []


# =============================================================== §4 Tier-1 detectors (v2)
# ADR-mining plan §4: each additional deterministic detector reads an existing fact stream,
# fires on a crafted fixture, and is a clean no-op on a fixture that lacks its signal. Each
# emits container/system-altitude candidates with structured evidence rows citing real ids.

def _one(result, kind):
    """The single candidate of *kind*, asserting exactly one fired."""
    hits = [c for c in result["candidates"] if c["kind"] == kind]
    assert len(hits) == 1, f"expected exactly one {kind}, got {len(hits)}"
    return hits[0]


def _tech_standard_facts(pkg="Serilog", n=4):
    """*n* containers each referencing *pkg* (+ a varied other so each is distinct)."""
    return {"targets": [_t(f"t{i}", f"container:c{i}", name=f"c{i}",
                           package_refs=[pkg, f"Other{i}"]) for i in range(n)],
            "relationships": []}


def _tech_inconsistency_facts(n_ef=8, n_dapper=2):
    """A split ORM choice: *n_ef* containers use EF Core, *n_dapper* use Dapper."""
    targets = [_t(f"ef{i}", f"container:ef{i}",
                  package_refs=["Microsoft.EntityFrameworkCore"]) for i in range(n_ef)]
    targets += [_t(f"dp{i}", f"container:dp{i}", package_refs=["Dapper"])
                for i in range(n_dapper)]
    return {"targets": targets, "relationships": []}


def _shared_kernel_facts(n_deps=5):
    """*n_deps* containers all link-depending on one `container:kernel` (high fan-in)."""
    targets = [_t("k", "container:kernel", "Kernel")]
    rels = []
    for i in range(n_deps):
        targets.append(_t(f"s{i}", f"container:s{i}"))
        rels.append(_r(f"s{i}", "k", kind="link"))
    return {"targets": targets, "relationships": rels}


def _cycle_facts():
    """A ⇄ B declared link cycle (Tarjan SCC of size 2, above the weight gate)."""
    return {"targets": [_t("a", "container:A", "A"), _t("b", "container:B", "B")],
            "relationships": [_r("a", "b", kind="link"), _r("b", "a", kind="link")]}


def _interop_facts(n=3):
    """*n* P/Invoke (kind="interop") relationships from distinct containers into NativeBridge."""
    targets = [_t("nb", "container:NativeBridge", "NativeBridge")]
    rels = []
    for i in range(n):
        targets.append(_t(f"m{i}", f"container:m{i}"))
        rels.append(_r(f"m{i}", "nb", kind="interop"))
    return {"targets": targets, "relationships": rels}


def _layering_facts():
    """A clean UI + Data partition with NO `Data -> UI` edge (the rule is HELD)."""
    return {"targets": [_t("u", "container:UI", "UI"), _t("d", "container:Data", "Data")],
            "relationships": []}


def _cqrs_skeleton(n=3):
    """*n* containers each with a type implementing `IRequestHandler<>` (component-altitude
    evidence that aggregates up to a container-altitude CQRS candidate)."""
    names = ["Ordering", "Catalog", "Basket", "Payment", "Shipping"][:n]
    facts = {"targets": [_t(f"t{i}", f"container:{names[i]}", name=names[i])
                         for i in range(n)],
             "relationships": []}
    skel = {f"t{i}": [{"name": f"H{i}", "kind": "class",
                       "interfaces": [f"IRequestHandler<Cmd{i},Res{i}>"]}]
            for i in range(n)}
    return facts, skel


# ----------------------------------------------------------------- each fires + clean no-op

def test_tech_standardization_fires_and_is_cited():
    c = _one(adr.decision_candidates(_tech_standard_facts("Serilog", 4), []),
             "tech-standardization")
    assert c["category"] == "technology"
    assert c["n_sources"] == 4          # one per container referencing the package
    assert c["elements"] == sorted(f"container:c{i}" for i in range(4))
    row = c["evidence"][0]
    assert row["type"] == "package_ref"
    assert row["package"] == "Serilog"
    assert row["element_ids"][0] in c["elements"]   # cites a real container id
    assert row["source_ids"] == ["t0"]              # …and the real source target id


def test_framework_package_is_categorised_framework():
    """A web/ui-framework package family lands in the `framework` category, not `technology`."""
    c = _one(adr.decision_candidates(_tech_standard_facts("Microsoft.AspNetCore.Mvc", 4), []),
             "tech-standardization")
    assert c["category"] == "framework"


def test_tech_standardization_clean_noop_below_share():
    """Only 1 of 4 containers references the package — below the 50% standardization share."""
    targets = [_t("t0", "container:c0", package_refs=["Serilog"])]
    targets += [_t(f"t{i}", f"container:c{i}") for i in range(1, 4)]
    result = adr.decision_candidates({"targets": targets, "relationships": []}, [])
    assert [c for c in result["candidates"] if c["kind"] == "tech-standardization"] == []


def test_tech_inconsistency_fires():
    c = _one(adr.decision_candidates(_tech_inconsistency_facts(8, 2), []),
             "tech-inconsistency")
    assert c["category"] == "technology"
    assert c["n_sources"] == 10
    assert any(row["type"] == "package_ref" for row in c["evidence"])
    assert "EntityFrameworkCore" in c["statement"] and "Dapper" in c["statement"]


def test_tech_inconsistency_clean_noop_when_minority_too_large():
    """6 EF vs 4 Dapper → minority share 0.4 > 0.34 → not an inconsistency (noise control)."""
    result = adr.decision_candidates(_tech_inconsistency_facts(6, 4), [])
    assert [c for c in result["candidates"] if c["kind"] == "tech-inconsistency"] == []


def test_shared_kernel_fires():
    c = _one(adr.decision_candidates(_shared_kernel_facts(5), []), "shared-kernel")
    assert c["elements"] == ["container:kernel"]
    assert c["n_sources"] == 5
    assert c["evidence"][0]["type"] == "container_edge"


def test_shared_kernel_clean_noop_below_fanin():
    """3 dependents is below the 4-fan-in floor → no shared-kernel candidate."""
    result = adr.decision_candidates(_shared_kernel_facts(3), [])
    assert [c for c in result["candidates"] if c["kind"] == "shared-kernel"] == []


def test_accepted_cycle_fires():
    c = _one(adr.decision_candidates(_cycle_facts(), []), "accepted-cycle")
    assert c["elements"] == ["container:A", "container:B"]
    assert c["category"] == "structure"


def test_accepted_cycle_clean_noop_acyclic():
    """A single A -> B edge is acyclic — no SCC of size ≥2."""
    facts = {"targets": [_t("a", "container:A"), _t("b", "container:B")],
             "relationships": [_r("a", "b", kind="link")]}
    result = adr.decision_candidates(facts, [])
    assert [c for c in result["candidates"] if c["kind"] == "accepted-cycle"] == []


def test_interop_boundary_fires():
    c = _one(adr.decision_candidates(_interop_facts(3), []), "interop-boundary")
    assert c["evidence"][0]["type"] == "interop"
    assert all(e in c["evidence"] for e in c["evidence"])
    # the seam sources are container ids (altitude discipline)
    assert all(eid.startswith("container:") for eid in c["elements"])


def test_interop_boundary_clean_noop_below_sites():
    """2 interop sites is below the 3-site floor → no interop-boundary candidate."""
    result = adr.decision_candidates(_interop_facts(2), [])
    assert [c for c in result["candidates"] if c["kind"] == "interop-boundary"] == []


def test_layering_convention_fires_when_held():
    rules = {"forbidden": [{"from": "Data", "to": "UI"}]}
    c = _one(adr.decision_candidates(_layering_facts(), [], layering_rules=rules),
             "layering-convention")
    assert c["category"] == "structure"
    assert set(c["elements"]) <= {"container:UI", "container:Data"}


def test_layering_convention_clean_noop_without_rules():
    """No hand-authored layering rules → the detector is a clean no-op (opt-in signal)."""
    result = adr.decision_candidates(_layering_facts(), [], layering_rules=None)
    assert [c for c in result["candidates"] if c["kind"] == "layering-convention"] == []


def test_layering_convention_clean_noop_when_violated():
    """A present `Data -> UI` edge makes the rule VIOLATED, not HELD → no convention candidate."""
    facts = _layering_facts()
    facts["relationships"] = [_r("d", "u", kind="link")]
    rules = {"forbidden": [{"from": "Data", "to": "UI"}]}
    result = adr.decision_candidates(facts, [], layering_rules=rules)
    assert [c for c in result["candidates"] if c["kind"] == "layering-convention"] == []


def test_design_pattern_fires_from_api_skeleton():
    facts, skel = _cqrs_skeleton(3)
    c = _one(adr.decision_candidates(facts, [], api_skeleton=skel), "design-pattern")
    assert c["category"] == "pattern"
    assert "CQRS" in c["title"]


def test_design_pattern_clean_noop_without_skeleton():
    facts, _skel = _cqrs_skeleton(3)
    result = adr.decision_candidates(facts, [], api_skeleton=None)
    assert [c for c in result["candidates"] if c["kind"] == "design-pattern"] == []


# ------------------------------------------------------ §0.3#4 altitude: detect fine, emit coarse

def test_pattern_candidate_emitted_at_container_altitude():
    """The CQRS evidence is per-TARGET (component) but the candidate names CONTAINER ids only
    (§0.3 #4 detect-fine-emit-coarse) — no bare target id leaks into `elements`."""
    facts, skel = _cqrs_skeleton(3)
    c = _one(adr.decision_candidates(facts, [], api_skeleton=skel), "design-pattern")
    target_ids = {t["id"] for t in facts["targets"]}
    assert all(el.startswith("container:") for el in c["elements"])
    assert not (set(c["elements"]) & target_ids), "no component-altitude id leaked up"


# --------------------------------------------------------- §0.5 structural absence of rationale

def test_v2_candidates_have_no_rationale_field():
    """The §0.5 structural guard: NO candidate (any detector) carries a decision/rationale/
    alternatives field — the contract is enforced by the record SHAPE, not by a filter."""
    facts, skel = _cqrs_skeleton(3)
    everything = adr.decision_candidates(
        {"targets": _tech_standard_facts().get("targets"), "relationships": []}, [])
    for result in (everything,
                   adr.decision_candidates(facts, [], api_skeleton=skel),
                   adr.decision_candidates(_shared_kernel_facts(), []),
                   adr.decision_candidates(_cycle_facts(), [])):
        for c in result["candidates"]:
            for banned in ("decision", "rationale", "alternatives", "rejected_alternatives"):
                assert banned not in c, f"{c['kind']} must not carry a {banned!r} field (§0.5)"


# --------------------------------------------------- §7.4 the hard line, extended to new kinds

def test_render_stub_keeps_human_todo_for_every_new_kind():
    """Every new detector kind renders ## Decision / ## Rationale / ## Rejected alternatives
    as a literal `TODO (human)` — the §0.5/§7.4 line is never crossed by a new producer."""
    for kind in ("tech-standardization", "tech-inconsistency", "shared-kernel",
                 "accepted-cycle", "interop-boundary", "deployment-topology",
                 "layering-convention", "design-pattern", "mined-decision"):
        c = {"slug": f"s-{kind}", "kind": kind, "category": "structure",
             "title": f"T {kind}", "statement": "the cited evidence",
             "elements": ["container:x"],
             "evidence": [{"type": "package_ref", "detail": "d"}],
             "n_sources": 1, "total_weight": 1}
        md = adr.render_stub(c)
        for section in ("## Decision", "## Rationale", "## Rejected alternatives"):
            assert section in md
            after = md.split(section, 1)[1].lstrip()
            assert after.startswith("TODO (human)"), \
                f"{section} of a {kind} stub must be a literal human TODO (§7.4)"


# --------------------------------------------------- §3 category-aware ADR suppression branches

def _named_tech_facts():
    """4 containers (named Ordering/Catalog/Basket/Payment) all referencing Serilog."""
    names = ["Ordering", "Catalog", "Basket", "Payment"]
    return {"targets": [_t(f"t{i}", f"container:{nm}", name=nm,
                           package_refs=["Serilog", f"Other{i}"])
                        for i, nm in enumerate(names)],
            "relationships": []}


def test_tech_adr_naming_one_container_does_not_suppress():
    """A technology candidate is NOT suppressed by an ADR that names ONE element but not the
    package family — the broad 'standardize on Serilog' candidate survives (§3)."""
    adrs = [{"id": "0001", "title": "Ordering uses a bespoke logger",
             "status": "accepted", "path": "docs/adr/0001.md"}]
    result = adr.decision_candidates(_named_tech_facts(), adrs)
    kept = [c for c in result["candidates"] if c["kind"] == "tech-standardization"]
    assert len(kept) == 1, "naming one container must not bury a system-wide tech candidate"
    assert "0001" in kept[0]["linked_adrs"], "the ADR is still surfaced as a link"


def test_tech_adr_naming_family_and_element_suppresses():
    """When the ADR names BOTH the package family (Serilog) AND a candidate element, the
    technology candidate IS suppressed (the duplicate-ADR case, §3)."""
    adrs = [{"id": "0002", "title": "Ordering standardizes on Serilog for logging",
             "status": "accepted", "path": "docs/adr/0002.md"}]
    result = adr.decision_candidates(_named_tech_facts(), adrs)
    assert [c for c in result["candidates"] if c["kind"] == "tech-standardization"] == []
    assert len(result["suppressed"]) == 1
    assert "0002" in result["suppressed"][0]["reason"]


# ----------------------------------------------------------------- §7 per-category sub-budgets

def test_per_category_budget_scales_with_its_own_unit():
    """The technology cap grows with the count of distinct package FAMILIES (≈1 per 3),
    not with size; a one-family repo caps at 1, a many-family repo at more (§7)."""
    one_family = {"targets": [_t(f"t{i}", f"container:c{i}",
                                 package_refs=["Serilog", "NLog", "log4net"])
                              for i in range(4)], "relationships": []}
    many_family = {"targets": [_t(f"t{i}", f"container:c{i}", package_refs=[
        "Serilog", "Microsoft.EntityFrameworkCore", "MediatR", "grpc", "AutoMapper",
        "Polly", "FluentValidation", "xunit", "StackExchange.Redis"])
        for i in range(4)], "relationships": []}
    b1 = adr.per_category_budget(one_family)
    bN = adr.per_category_budget(many_family)
    assert b1["technology"] == 1
    assert bN["technology"] > b1["technology"]
    assert bN["pattern"] == 3            # patterns are a small fixed cap


def test_flood_of_one_category_is_capped_and_dropped_reported():
    """Three logging packages (one family) each standardized across containers → three
    tech-standardization candidates, but the technology sub-budget is 1 → 2 are dropped
    and `dropped_over_cap` reports it (§7)."""
    facts = {"targets": [_t(f"t{i}", f"container:c{i}",
                            package_refs=["Serilog", "NLog", "log4net"])
                         for i in range(4)], "relationships": []}
    result = adr.decision_candidates(facts, [])
    tech = [c for c in result["candidates"] if c["category"] == "technology"]
    assert len(tech) == result["budget"]["technology"] == 1
    assert result["dropped_over_cap"] == 2


# ------------------------------------------------------------- byte-identical second run (v2)

def test_run_stubs_decisions_and_inbox_byte_identical(toy):
    """A second `run_stubs` produces byte-identical decisions JSON AND inbox JSON (the v2
    determinism gate — Tier-1 detectors + the pre-materialized inbox are pure)."""
    adr.run_stubs(toy.ws)
    dec_a = toy.ws.adr_candidate_decisions_json.read_bytes()
    inbox_a = toy.ws.adr_candidate_inbox_json.read_bytes()
    adr.run_stubs(toy.ws)
    assert toy.ws.adr_candidate_decisions_json.read_bytes() == dec_a
    assert toy.ws.adr_candidate_inbox_json.read_bytes() == inbox_a
