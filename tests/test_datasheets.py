"""Engine §1 — `arch datasheet`: per-container datasheets + the guided tour.

Locked down here:
  * the headline L0/L1 content is always populated (named fan-in/out WITH evidence
    layers, owns, health) and the public-surface field is honestly conditional on L2
    ("L2 not extracted" — never guessed, §1.1);
  * honesty numbers are single-sourced (§0.4): the datasheet's mean confidence IS
    `confidence.container_confidence`, the stratum comes from `scorecard.element_strata`;
  * the tour walks by salience (weight + seam boost) and the critical-seams list is
    exhaustive regardless of weight (§1.1/§1.7);
  * determinism — same facts → byte-identical artifacts;
  * `arch run` emits the datasheet set as an advisory artifact (§1.4).
"""
from __future__ import annotations

import pytest

from conftest import run_toy_pipeline

from anon import confidence
from anon.jsonio import load_json
from anon.model import content_hash
from anon.stages import generate_docs


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    tr = run_toy_pipeline(tmp_path_factory.mktemp("dsrun") / "architecture")
    generate_docs.run_datasheets(tr.ws)
    return tr


def _sheet(toy, name):
    sheets = load_json(toy.ws.datasheets_json)
    return next(r for r in sheets["containers"] if r["name"] == name)


# --------------------------------------------------------------------------- contract

# The datasheets/3 per-row key set (enrichment plan §4.6) — part of the GUI/agent
# contract. `components` (the nested member sheets) is new in /3.
V3_KEYS = {"id", "name", "components", "narrative", "purpose", "owns", "metrics",
           "technology", "fan_in", "fan_out", "public_surface", "footprint", "vitals",
           "risk", "tests", "health"}


def test_datasheets_json_contract(toy):
    sheets = load_json(toy.ws.datasheets_json)
    assert sheets["schema"] == generate_docs.DATASHEETS_SCHEMA == "datasheets/3"
    # datasheets consume the same facts view as the DSL generator (enriched when
    # present, §1.2 "one lift, one truth"), so the hash cites that file.
    facts_path = toy.ws.enriched_facts if toy.ws.enriched_facts.exists() \
        else toy.ws.curated_facts
    assert sheets["derived_from_hash"] == content_hash(load_json(facts_path))
    assert len(sheets["containers"]) == 3
    for row in sheets["containers"]:
        assert set(row) == V3_KEYS
    # /3 adds a whole-system datasheet with aggregate metrics + structural counts.
    assert sheets["system"]["id"].startswith("system:")
    sysm = sheets["system"]["metrics"]
    assert sysm["available"] is True and sysm["engine"] == "metrics/1"
    # the system metrics carry the SAME per-language table the GUI renders (not just a
    # total LOC line): rows over every target, with the total reconciling to their sum.
    assert {r["language"] for r in sysm["by_language"]} == {"C#", "MSBuild"}
    for k in ("files", "code", "comment", "blank"):
        assert sysm["total"][k] == sum(r[k] for r in sysm["by_language"])
    assert sheets["system"]["counts"]["containers"] == 3


def test_fan_in_is_named_and_layer_cited(toy):
    core = _sheet(toy, "Core Library")
    names = {e["name"] for e in core["fan_in"]}
    assert names == {"Web Frontend", "Messaging"}
    for e in core["fan_in"]:
        assert e["evidence_layers"] == ["L0"], "the L0-only run cites L0"
        assert e["declared"] is True and e["weight"] > 0
    assert core["fan_out"] == [], "the leaf depends on nothing in-model"


def test_public_surface_honestly_absent_without_l2(toy):
    core = _sheet(toy, "Core Library")
    s = core["public_surface"]
    assert s["available"] is False
    assert s["reason"] == "L2 not extracted"
    assert s["symbols"] == []
    md = next(p for p in (toy.ws.docs_dir / "datasheets").glob("*.md")
              if "Core Library" in p.read_text(encoding="utf-8"))
    assert "L2 not extracted" in md.read_text(encoding="utf-8")


def test_public_surface_appears_with_public_l2_evidence(toy):
    facts = load_json(toy.ws.curated_facts)
    for r in facts["relationships"]:
        if r["target"].endswith("Toy.Domain.csproj"):
            r["evidence"].append({"type": "symbol_use", "detail": "OrderService",
                                  "visibility": "public"})
    sheets = generate_docs.build_datasheets(facts)
    core = next(r for r in sheets["containers"] if r["name"] == "Core Library")
    assert core["public_surface"]["available"] is True
    assert "OrderService" in core["public_surface"]["symbols"]
    # …and a container with L2 ran but nothing public says so, never "not extracted"
    other = next(r for r in sheets["containers"] if r["name"] == "Messaging")
    assert other["public_surface"]["reason"].startswith("L2 ran")


def test_health_is_single_sourced(toy):
    facts = load_json(toy.ws.curated_facts)
    conf = confidence.container_confidence(facts)
    for row in load_json(toy.ws.datasheets_json)["containers"]:
        assert row["health"]["mean_confidence"] == conf[row["id"]], \
            "the datasheet never recomputes confidence (§0.4)"
        assert row["health"]["coverage_stratum"] == "L0"
        assert row["health"]["drift"] == "stable"


def test_purpose_carries_grouping_provenance(toy):
    core = _sheet(toy, "Core Library")
    assert core["purpose"]["grouped_by"] == "members"
    assert core["purpose"]["enriched"] is False  # --no-llm run
    assert core["purpose"]["text"]


# ------------------------------------------------------------- datasheets/2 blocks


def test_metrics_block_sums_member_languages(toy):
    """Enrichment plan §3: the metrics/1 counts land in the facts and the datasheet
    sums them per container at render time."""
    core = _sheet(toy, "Core Library")
    m = core["metrics"]
    assert m["available"] is True and m["engine"] == "metrics/1"
    langs = {r["language"] for r in m["by_language"]}
    assert "C#" in langs and "MSBuild" in langs  # .cs sources + .csproj files
    assert m["total"]["code"] > 0
    # Core Library owns Domain + Common -> at least 2 .cs + 2 .csproj files
    assert m["total"]["files"] >= 4
    assert m["total"]["files"] == sum(r["files"] for r in m["by_language"])


def test_technology_fingerprint_survives_drop_external(toy):
    """§6.1: package targets are dropped at curation (drop_external), but the DIRECT
    refs stamped on the consuming project keep the fingerprint alive."""
    msg = _sheet(toy, "Messaging")
    assert "Newtonsoft.Json" in msg["technology"]["packages"]
    web = _sheet(toy, "Web Frontend")
    assert "Serilog" in web["technology"]["packages"]
    core = _sheet(toy, "Core Library")
    assert core["technology"]["packages"] == []


def test_tests_block_reads_extracted_facts(toy):
    """§6.3: the toy CURATES OUT its test project — adjacency must still be found,
    from the extracted facts, and stay honestly labeled a proxy."""
    web = _sheet(toy, "Web Frontend")
    assert web["tests"]["test_targets"] == \
        ["csharp:csproj:tests/Toy.Web.Tests/Toy.Web.Tests.csproj"]
    assert web["tests"]["members_with_tests"] == 1
    assert "NOT coverage" in web["tests"]["note"]
    core = _sheet(toy, "Core Library")
    assert core["tests"]["test_targets"] == []


def test_narrative_honestly_absent_on_no_llm(toy):
    """§4.5: the no-llm/CI path renders the narrative block with source 'none' and a
    reason — the shape is identical with or without an LLM."""
    for row in load_json(toy.ws.datasheets_json)["containers"]:
        n = row["narrative"]
        assert n["source"] == "none" and n["abstract_md"] == ""
        assert "no LLM narrative" in n["reason"]


def test_narrative_override_wins_and_flags_dead_citations(toy):
    """§4.4: a hand-written override renders source 'human' and is never machine-
    rejected — an unresolvable citation becomes a warning, not a hard fail."""
    facts = load_json(toy.ws.curated_facts)
    overrides = {"narratives": {"container:coreLibrary": {
        "abstract_md": "The reusable core.",
        "key_points": [
            {"text": "All writes go through Domain.",
             "cites": ["csharp:csproj:src/Domain/Toy.Domain.csproj"]},
            {"text": "Cites a ghost.", "cites": ["csharp:csproj:src/Gone/Gone.csproj"]},
        ]}}}
    sheets = generate_docs.build_datasheets(facts, overrides=overrides)
    core = next(r for r in sheets["containers"] if r["name"] == "Core Library")
    n = core["narrative"]
    assert n["source"] == "human" and n["stale"] is False
    assert n["stale_citations"] == ["csharp:csproj:src/Gone/Gone.csproj"]
    md = generate_docs.render_datasheet(core)
    assert "Human-written" in md and "unresolvable citation" in md


def test_footprint_absent_without_measurements(toy):
    core = _sheet(toy, "Core Library")
    fp = core["footprint"]
    assert fp["available"] is False
    assert "arch measure" in fp["reason"]


def test_vitals_absent_outside_git(toy):
    """The toy copy under tmp_path is not a git repo — absence is the normal answer."""
    core = _sheet(toy, "Core Library")
    v = core["vitals"]
    assert v["available"] is False
    assert v["reason"] == "not a git repository"


def test_risk_echo_unavailable_without_report(toy):
    """run_toy_pipeline does not run `arch risk` -> the echo says so (§0.4: echo,
    never recompute)."""
    core = _sheet(toy, "Core Library")
    assert core["risk"]["available"] is False
    assert "arch risk" in core["risk"]["reason"]


def test_risk_echo_and_stale_guard(toy):
    facts = load_json(toy.ws.curated_facts)
    h = content_hash(facts)
    report = {"derived_from_hash": h, "findings": [
        {"kind": "cycle", "severity": 0.7,
         "elements": ["container:coreLibrary", "container:messaging"]},
        {"kind": "main-sequence", "severity": 0.55,
         "elements": ["container:coreLibrary"],
         "severity_inputs": {"instability_I": 0.2, "abstractness_A": 0.25}},
    ]}
    sheets = generate_docs.build_datasheets(facts, risk_report=report)
    core = next(r for r in sheets["containers"] if r["name"] == "Core Library")
    assert core["risk"]["available"] is True and core["risk"]["in_cycle"] is True
    assert core["risk"]["instability_I"] == 0.2
    assert core["risk"]["distance_D"] == 0.55
    web = next(r for r in sheets["containers"] if r["name"] == "Web Frontend")
    assert web["risk"]["finding_kinds"] == []
    # a report computed from DIFFERENT facts is refused, never half-trusted
    stale = generate_docs.build_datasheets(
        facts, risk_report={"derived_from_hash": "deadbeef", "findings": []})
    core2 = next(r for r in stale["containers"] if r["name"] == "Core Library")
    assert core2["risk"]["available"] is False
    assert "different facts" in core2["risk"]["reason"]


def test_rendered_markdown_carries_v2_sections(toy):
    md = next(p for p in (toy.ws.docs_dir / "datasheets").glob("*.md")
              if "Core Library" in p.read_text(encoding="utf-8")
              ).read_text(encoding="utf-8")
    for needle in ("**Metrics**", "**Technology**", "**Footprint**", "**Vitals**",
                   "**Risk**", "**Tests**", "_Narrative:"):
        assert needle in md, f"missing {needle}"


# --------------------------------------------------------------------------- tour

def test_tour_walks_from_the_entry_point(toy):
    tour = load_json(toy.ws.datasheets_tour_json)
    assert tour["schema"] == generate_docs.TOUR_SCHEMA
    # Web Frontend is the only container nothing internal depends on
    assert tour["entry_points"][0] == "container:webFrontend"
    assert tour["walk"][0]["from_name"] == "Web Frontend"
    assert tour["critical_seams"] == []  # toy has no interop/contract/boundary edges


def test_tour_salience_boosts_seams_over_weight():
    """A low-weight interop edge must outrank a chatty utility edge (§1.1)."""
    facts = {"targets": [
        {"id": "entry", "name": "Entry", "container_id": "c:entry",
         "container_name": "Entry"},
        {"id": "util", "name": "Util", "container_id": "c:util",
         "container_name": "Util"},
        {"id": "native", "name": "Native", "container_id": "c:native",
         "container_name": "Native"}],
        "relationships": [
            {"source": "entry", "target": "util", "weight": 8,
             "is_declared_dependency": True,
             "evidence": [{"type": "project_ref", "detail": "chatty"}]},
            {"source": "entry", "target": "native", "weight": 2,
             "is_declared_dependency": True,
             "evidence": [{"type": "interop", "detail": "DllImport"}]}]}
    tour = generate_docs.build_tour(facts)
    assert tour["walk"][0]["to"] == "c:native", \
        "weight 2 + seam boost 10 beats weight 8"
    assert "seam (interop/contract)" in tour["walk"][0]["salience_reasons"]
    seams = tour["critical_seams"]
    assert any(s["source"] == "c:entry" and s["target"] == "c:native"
               for s in seams), "the seam list is exhaustive regardless of weight"


# --------------------------------------------------------------------------- artifacts

def test_determinism_byte_identical(toy):
    a = toy.ws.datasheets_json.read_bytes()
    t = toy.ws.datasheets_tour_json.read_bytes()
    generate_docs.run_datasheets(toy.ws)
    assert toy.ws.datasheets_json.read_bytes() == a
    assert toy.ws.datasheets_tour_json.read_bytes() == t


def test_stale_per_container_sheets_are_dropped(toy):
    ds_dir = toy.ws.docs_dir / "datasheets"
    ghost = ds_dir / "goneContainer.md"
    ghost.write_text("## Gone\n", encoding="utf-8")
    generate_docs.run_datasheets(toy.ws)
    assert not ghost.exists(), "machine-owned dir: sheets of removed containers go away"


def test_arch_run_emits_datasheets(tmp_path):
    """§1.4: the `arch run` CLI path emits the full datasheet set (advisory) — and the
    risk echo joins the report `arch run` just wrote (regression: risk reads the
    CURATED tier while datasheets read the ENRICHED one; the staleness guard must
    accept both, not cry 'different facts' on every run)."""
    from conftest import pristine_toy
    from anon import cli
    src = pristine_toy(tmp_path / "toy-src")
    arch = tmp_path / "architecture"
    rc = cli.main(["run", "--repo", str(src), "--arch-dir", str(arch),
                   "--rules-dir", str(src / "architecture" / "rules"), "--no-llm"])
    assert rc == 0
    assert (arch / "generated" / "docs" / "datasheets.json").exists()
    assert (arch / "generated" / "docs" / "datasheets" / "tour.json").exists()
    sheets = load_json(arch / "generated" / "docs" / "datasheets.json")
    for row in sheets["containers"]:
        assert row["risk"]["available"] is True, row["risk"]
