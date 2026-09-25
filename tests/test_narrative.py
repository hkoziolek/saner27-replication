"""The LLM container-narrative layer (datasheet-enrichment plan §4).

Locked down here:
  * the citation validator — every key point must cite ids that RESOLVE in the curated
    facts; the backticked-id tripwire on the abstract; the anti-essay schema caps;
  * the terminal degrade is honest absence (``llm-narrative-failed``), never a
    half-validated paragraph;
  * narratives ride the enriched facts (`containers_narrative`), carry model id +
    source hash, and fold into the datasheet as ``source: "llm"`` with a working
    staleness anchor;
  * the same governance surface as naming: the egress preview/manifest grow a
    container section, and no provider call is ever made in tests or --no-llm.
"""
from __future__ import annotations

import pytest

from conftest import run_toy_pipeline

from anon.jsonio import load_json
from anon.model import content_hash
from anon.stages import enrich_llm, generate_docs
from anon.stages.validate_facts import find_schema

REDACTION = {"secret_patterns": [], "deny_list": [], "max_snippet_chars": 280}


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    return run_toy_pipeline(tmp_path_factory.mktemp("narrun") / "architecture")


class NarrativeProvider:
    """Returns a valid, grounded narrative for ANY narrative payload (system / container /
    component — they all carry `citable_ids`) AND a valid naming record for element
    payloads (the propose run exercises both passes)."""

    model_id = "mock-narrative-1"

    def complete(self, system_prompt, prompt, facts):
        if "citable_ids" in facts:  # a §4 narrative payload (system/container/component)
            return {"element_id": facts["element_id"],
                    "element_kind": facts.get("element_kind"),
                    "abstract_md": f"**{facts['name']}** owns "
                                   + ", ".join(facts.get("members", [])) + ".",
                    "key_points": [{"text": "Grounded point.",
                                    "cites": [facts["citable_ids"][0]]}],
                    "naming_confidence": "high"}
        return {"element_id": facts["element_id"], "element_name": facts["name"],
                "description": "d", "responsibilities": [],
                "naming_confidence": "high", "evidence": []}


class GhostCitingProvider(NarrativeProvider):
    """Cites an id that does not resolve -> must degrade after bounded retries."""

    model_id = "mock-ghost-1"

    def complete(self, system_prompt, prompt, facts):
        rec = super().complete(system_prompt, prompt, facts)
        if "citable_ids" in facts:
            rec["key_points"] = [{"text": "Invented.", "cites": ["csharp:csproj:ghost"]}]
        return rec


# ------------------------------------------------------------------ the validator

def _ctx(toy):
    curated = load_json(toy.ws.curated_facts)
    schema = load_json(find_schema("llm-narrative.schema.json"))
    containers, _cedges, universe = enrich_llm.narrative_context(curated)
    return curated, schema, set(containers), universe


def test_validator_rejects_unresolvable_citation(toy):
    _curated, schema, cids, universe = _ctx(toy)
    cid = sorted(cids)[0]
    rec = {"element_id": cid, "abstract_md": "x",
           "key_points": [{"text": "p", "cites": ["nope:nope"]}],
           "naming_confidence": "high"}
    err = enrich_llm._validate_narrative(rec, schema, cids, universe)
    assert err and "does not resolve" in err


def test_validator_rejects_uncited_point_via_schema(toy):
    """minItems 1 on cites: an uncited claim is invalid BY CONSTRUCTION (moat #2)."""
    _curated, schema, cids, universe = _ctx(toy)
    rec = {"element_id": sorted(cids)[0], "abstract_md": "x",
           "key_points": [{"text": "p", "cites": []}], "naming_confidence": "high"}
    err = enrich_llm._validate_narrative(rec, schema, cids, universe)
    assert err and err.startswith("schema:")


def test_validator_caps_are_hard_limits(toy):
    """The anti-essay gate (plan §9 threat a): 700-char abstract, ≤5 key points."""
    _curated, schema, cids, universe = _ctx(toy)
    cid = sorted(cids)[0]
    point = {"text": "p", "cites": [cid]}
    too_long = {"element_id": cid, "abstract_md": "x" * 701,
                "key_points": [point], "naming_confidence": "high"}
    assert enrich_llm._validate_narrative(too_long, schema, cids, universe)
    too_many = {"element_id": cid, "abstract_md": "x",
                "key_points": [dict(point) for _ in range(6)],
                "naming_confidence": "high"}
    assert enrich_llm._validate_narrative(too_many, schema, cids, universe)


def test_validator_backtick_tripwire_on_abstract(toy):
    _curated, schema, cids, universe = _ctx(toy)
    rec = {"element_id": sorted(cids)[0],
           "abstract_md": "Talks to `csharp:csproj:src/Invented/X.csproj` daily.",
           "key_points": [{"text": "p", "cites": [sorted(cids)[0]]}],
           "naming_confidence": "high"}
    err = enrich_llm._validate_narrative(rec, schema, cids, universe)
    assert err and "do not invent" in err


# --------------------------------------------------------------- propose + degrade

def test_propose_narratives_grounded_records(toy):
    curated = load_json(toy.ws.curated_facts)
    records, cache = enrich_llm.propose_narratives(curated, NarrativeProvider(),
                                                   REDACTION)
    assert set(records) == {"container:coreLibrary", "container:messaging",
                            "container:webFrontend"}
    rec = records["container:coreLibrary"]
    assert rec["model_id"] == "mock-narrative-1"
    assert rec["derived_from_hash"] == content_hash(curated)
    assert cache["container:coreLibrary"]["cache_key"]


def test_ghost_citation_degrades_to_honest_absence(toy):
    curated = load_json(toy.ws.curated_facts)
    records, _ = enrich_llm.propose_narratives(curated, GhostCitingProvider(),
                                               REDACTION)
    rec = records["container:coreLibrary"]
    assert "error" in rec and "does not resolve" in rec["error"]
    # …and the datasheet renders that as source none / llm-narrative-failed (§4.3)
    facts = dict(curated)
    facts["containers_narrative"] = records
    sheets = generate_docs.build_datasheets(facts)
    core = next(r for r in sheets["containers"] if r["name"] == "Core Library")
    assert core["narrative"]["source"] == "none"
    assert core["narrative"]["reason"].startswith("llm-narrative-failed")


def test_full_propose_run_folds_into_datasheets(toy):
    """End-to-end §4: enrich (propose) writes containers_narrative + the staleness
    anchor; the datasheet renders source llm, stale False, with the model id chip."""
    enrich_llm.run(toy.ws, "llm-propose", NarrativeProvider())
    enriched = load_json(toy.ws.enriched_facts)
    assert set(enriched["containers_narrative"]) == \
        {"container:coreLibrary", "container:messaging", "container:webFrontend"}
    # §4.6: the run now also narrates the system and the top-N salient components.
    assert enriched["system_narrative"]["element_id"].startswith("system:")
    assert enriched["system_narrative"]["abstract_md"]
    assert enriched["components_narrative"]  # coreLibrary is multi-member -> has components
    prov = enriched["provenance"]
    # egress_container_ids now spans every narrated element (containers + components + system)
    assert set(prov["egress_container_ids"]) >= set(enriched["containers_narrative"])
    assert enriched["system_narrative"]["element_id"] in prov["egress_container_ids"]
    assert set(enriched["components_narrative"]) <= set(prov["egress_container_ids"])
    assert prov["enrich_source_hash"]
    generate_docs.run_datasheets(toy.ws)
    sheets = load_json(toy.ws.datasheets_json)
    core = next(r for r in sheets["containers"] if r["name"] == "Core Library")
    n = core["narrative"]
    assert n["source"] == "llm" and n["stale"] is False
    assert n["model_id"] == "mock-narrative-1"
    assert n["key_points"][0]["cites"]
    md = generate_docs.render_datasheet(core)
    assert "LLM-generated (mock-narrative-1)" in md
    # cleanup: restore the no-llm enriched facts for any later module test
    enrich_llm.run(toy.ws, "no-llm")
    generate_docs.run_datasheets(toy.ws)


def test_human_override_beats_llm_proposal(toy):
    curated = load_json(toy.ws.curated_facts)
    records, _ = enrich_llm.propose_narratives(curated, NarrativeProvider(), REDACTION)
    facts = dict(curated)
    facts["containers_narrative"] = records
    overrides = {"narratives": {"container:coreLibrary": {
        "abstract_md": "Hand-written truth.",
        "key_points": [{"text": "kp", "cites": ["container:messaging"]}]}}}
    sheets = generate_docs.build_datasheets(facts, overrides=overrides)
    core = next(r for r in sheets["containers"] if r["name"] == "Core Library")
    assert core["narrative"]["source"] == "human"
    assert core["narrative"]["abstract_md"] == "Hand-written truth."
    other = next(r for r in sheets["containers"] if r["name"] == "Messaging")
    assert other["narrative"]["source"] == "llm"


# ------------------------------------------------------------------- egress (T1.2)

def test_egress_preview_includes_container_payloads(toy):
    enrich_llm.write_egress_preview(toy.ws)
    preview = load_json(toy.ws.egress_preview)
    assert preview["container_count"] == 3
    p = next(c for c in preview["container_payloads"]
             if c["element_id"] == "container:coreLibrary")
    assert p["citable_ids"][0] == "container:coreLibrary"
    assert "members" in p and p["element_kind"] == "container"
    # the payload is facts the datasheet already shows + the informative-narratives §2 D/C/ADR
    # inputs and the §4 band ceilings — nothing else
    assert set(p) <= {"element_id", "element_kind", "name", "system", "members",
                      "metrics", "packages", "namespaces", "depends_on", "used_by",
                      "snippet", "citable_ids",
                      "size_band", "max_abstract_chars", "max_key_points",
                      "api_skeleton", "anchors", "adrs"}
    # §4 length band: every narrative payload carries its band + output ceiling
    assert p["size_band"] in {"leaf", "standard", "hub"}
    assert p["max_abstract_chars"] in {220, 450, 700}
    assert p["max_key_points"] in {2, 3, 5}
    # §4.6: component + system narrative payloads also preview their exact egress
    assert preview["narrative_count"] >= 1
    kinds = {n["element_kind"] for n in preview["narrative_payloads"]}
    assert kinds == {"component", "system"} or kinds == {"system"}


# ----------------------------------------------------- §4.6 component + system sheets

def test_component_and_system_datasheets_fold_in(toy):
    """A full propose run (§4.6) produces a system datasheet and nested component
    datasheets that carry the LLM narrative; a multi-member container nests its
    members, a single-member one does not."""
    curated = load_json(toy.ws.curated_facts)
    enriched = dict(curated)
    enr = enrich_llm.run(toy.ws, "llm-propose", NarrativeProvider())  # writes enriched
    enriched = load_json(toy.ws.enriched_facts)
    sheets = generate_docs.build_datasheets(enriched)
    # system block: aggregate metrics + the synthesized narrative
    sysd = sheets["system"]
    assert sysd["id"].startswith("system:") and sysd["metrics"]["available"]
    assert sysd["narrative"]["source"] == "llm"
    assert sysd["counts"]["containers"] >= 1
    # Core Library is multi-member -> nested components, at least one with an llm narrative
    core = next(r for r in sheets["containers"] if r["name"] == "Core Library")
    assert len(core["components"]) >= 2
    assert any(c["narrative"]["source"] == "llm" for c in core["components"])
    comp = core["components"][0]
    assert comp["metrics"]["available"] and "fan_in" in comp and "fan_out" in comp
    # restore the no-llm enriched facts for any later module test
    enrich_llm.run(toy.ws, "no-llm")
    generate_docs.run_datasheets(toy.ws)
    assert enr  # the run returned the enriched dict
