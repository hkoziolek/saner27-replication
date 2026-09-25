"""Enrichment tests (plan §8) — Agent C vertical.

Covers no-llm passthrough byte-identical, cache_key sensitivity (model/prompt/schema/
facts change busts it), llm-propose name-review.tsv with a MOCK provider, degrade-on-
invalid-output with a mock, llm-accepted serving from name_overrides, and redaction-
before-cache-key. No real network call is ever made. Outputs go to pytest ``tmp_path``.
"""
from __future__ import annotations

from pathlib import Path

from anon.jsonio import dump_json, load_json
from anon.model import canonicalize
from anon.paths import resolve_workspace
from anon.stages import enrich_llm

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"
TOY_RULES = TOY / "architecture" / "rules"


def _curated() -> dict:
    return canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": "csharp:csproj:src/Web/Web.csproj", "name": "Web", "type": "csproj",
             "language": "csharp", "technology": ".NET assembly",
             "container_id": "container:webFrontend", "container_name": "Web Frontend",
             "responsibilities": ["Serves the web UI"]},
            {"id": "csharp:csproj:src/Core/Core.csproj", "name": "Core", "type": "csproj",
             "language": "csharp", "technology": ".NET assembly",
             "container_id": "container:coreLibrary", "container_name": "Core Library"},
        ],
        "relationships": [],
    })


def _ws(tmp_path: Path, sub: str = "a", rules: str | None = None):
    ws = resolve_workspace(TOY, arch_dir=tmp_path / sub / "arch", rules_dir=tmp_path / sub / "rules")
    dump_json(_curated(), ws.curated_facts)
    if rules is not None:
        ws.mapping_rules.parent.mkdir(parents=True, exist_ok=True)
        ws.mapping_rules.write_text(rules, encoding="utf-8", newline="")
    return ws


# --- mock providers (no network) -----------------------------------------------

class GoodProvider:
    model_id = "mock-good-1"

    def complete(self, system_prompt, prompt, element_facts):
        eid = element_facts["element_id"]
        return {
            "element_id": eid,
            "element_name": "Pretty " + element_facts["name"],
            "description": "A described element.",
            "responsibilities": ["do a thing"],
            "naming_confidence": "high",
            "evidence": [element_facts["name"]],
        }


class BadProvider:
    """Always returns schema-invalid output -> forces the §8.3 degrade path."""
    model_id = "mock-bad-1"

    def complete(self, system_prompt, prompt, element_facts):
        return {"element_id": element_facts["element_id"]}  # missing required fields


class RelationshipInventingProvider:
    """Tries to add relationships -> referential-integrity violation (§8.1)."""
    model_id = "mock-rel-1"

    def complete(self, system_prompt, prompt, element_facts):
        return {
            "element_id": element_facts["element_id"],
            "element_name": "X", "naming_confidence": "low",
            "relationships": [{"source": "a", "target": "b"}],
        }


# --- no-llm passthrough is byte-identical (§1.1 #2) ----------------------------

def test_no_llm_passthrough_byte_identical(tmp_path):
    ws_a = _ws(tmp_path, "a")
    ws_b = _ws(tmp_path, "b")
    enrich_llm.run(ws_a, "no-llm")
    enrich_llm.run(ws_b, "no-llm")
    assert ws_a.enriched_facts.read_bytes() == ws_b.enriched_facts.read_bytes()


def test_no_llm_preserves_targets(tmp_path):
    ws = _ws(tmp_path, "a")
    enriched = enrich_llm.run(ws, "no-llm")
    assert enriched["provenance"]["enrich_mode"] == "no-llm"
    # targets unchanged except provenance (no llm fields injected)
    assert all("llm_name" not in t for t in enriched["targets"])


def test_no_llm_makes_no_call(tmp_path):
    # NullProvider raises if used; no-llm must never reach it.
    ws = _ws(tmp_path, "a")
    enrich_llm.run(ws, "no-llm", provider=enrich_llm.NullProvider())  # must not raise


# --- cache_key sensitivity (§8.3) ----------------------------------------------

def test_cache_key_busts_on_model_prompt_schema_facts_change():
    schema = {"type": "object"}
    facts = {"element_id": "x:t:1", "name": "A"}
    base = enrich_llm.cache_key("m1", "p", "s", schema, facts)
    assert enrich_llm.cache_key("m2", "p", "s", schema, facts) != base       # model
    assert enrich_llm.cache_key("m1", "p2", "s", schema, facts) != base      # prompt
    assert enrich_llm.cache_key("m1", "p", "s2", schema, facts) != base      # system prompt
    assert enrich_llm.cache_key("m1", "p", "s", {"type": "string"}, facts) != base  # schema
    assert enrich_llm.cache_key("m1", "p", "s", schema, {"element_id": "x:t:2"}) != base  # facts
    # identical inputs -> identical key (stable)
    assert enrich_llm.cache_key("m1", "p", "s", schema, facts) == base


# --- llm-propose with a mock provider (§8.5/§8.4) ------------------------------

def test_llm_propose_emits_name_review_tsv(tmp_path):
    ws = _ws(tmp_path, "a", rules="version: 1\n")
    enriched = enrich_llm.run(ws, "llm-propose", provider=GoodProvider())
    assert enriched["provenance"]["enrich_mode"] == "llm-propose"
    assert ws.name_review_tsv.exists()
    tsv = ws.name_review_tsv.read_text(encoding="utf-8").splitlines()
    assert tsv[0] == "id\traw_name\tllm_name\tnaming_confidence\tevidence"
    rows = {line.split("\t")[0]: line.split("\t") for line in tsv[1:]}
    web = rows["csharp:csproj:src/Web/Web.csproj"]
    assert web[1] == "Web"                 # raw_name
    assert web[2] == "Pretty Web"          # llm_name
    assert web[3] == "high"                # naming_confidence
    # records marked proposed; cache meta recorded with resolved key components (§8.3)
    web_t = next(t for t in enriched["targets"] if t["id"].endswith("Web.csproj"))
    assert web_t["enrich_status"] == "proposed"
    cache = enriched["provenance"]["enrich_cache"]["csharp:csproj:src/Web/Web.csproj"]
    assert set(cache) >= {"cache_key", "model_id", "prompt_hash", "schema_hash", "facts_hash"}


# --- degrade on invalid output (§8.3 terminal policy) --------------------------

def test_degrade_on_invalid_output(tmp_path):
    ws = _ws(tmp_path, "a", rules="version: 1\n")
    enriched = enrich_llm.run(ws, "llm-propose", provider=BadProvider())
    web = next(t for t in enriched["targets"] if t["id"].endswith("Web.csproj"))
    # degraded: raw build name, empty desc, failure tags, low confidence (§8.3)
    assert web["llm_name"] == "Web"
    assert web["description"] == ""
    assert web["naming_confidence"] == "low"
    assert "llm-enrich-failed" in web["tags"] and "needs-curation" in web["tags"]


def test_degrade_on_relationship_invention(tmp_path):
    ws = _ws(tmp_path, "a", rules="version: 1\n")
    enriched = enrich_llm.run(ws, "llm-propose", provider=RelationshipInventingProvider())
    web = next(t for t in enriched["targets"] if t["id"].endswith("Web.csproj"))
    assert "llm-enrich-failed" in web["tags"]   # rel-inventing output rejected -> degrade


# --- llm-accepted serves pinned names without a call (§8.5) --------------------

def test_llm_accepted_serves_name_override(tmp_path):
    rules = ('version: 1\nname_overrides:\n'
             '  "csharp:csproj:src/Web/Web.csproj": "Web Frontend"\n')
    ws = _ws(tmp_path, "a", rules=rules)
    # GoodProvider would name it "Pretty Web"; the pin must win without a fresh call for Web.
    enriched = enrich_llm.run(ws, "llm-accepted", provider=GoodProvider())
    web = next(t for t in enriched["targets"] if t["id"].endswith("Web.csproj"))
    assert web["llm_name"] == "Web Frontend"
    assert web["enrich_status"] == "accepted"
    # Web was served from the pin -> no cache entry for it
    assert "csharp:csproj:src/Web/Web.csproj" not in enriched["provenance"].get("enrich_cache", {})


def test_llm_accepted_new_element_falls_back_to_propose(tmp_path):
    rules = ('version: 1\nname_overrides:\n'
             '  "csharp:csproj:src/Web/Web.csproj": "Web Frontend"\n')
    ws = _ws(tmp_path, "a", rules=rules)
    enriched = enrich_llm.run(ws, "llm-accepted", provider=GoodProvider())
    # Core has no pin -> proposed for that element only + needs-curation (§8.5)
    core = next(t for t in enriched["targets"] if t["id"].endswith("Core.csproj"))
    assert core["enrich_status"] == "proposed-unreviewed"
    assert "needs-curation" in core["tags"]


# --- redaction runs BEFORE the cache key (§8.3) --------------------------------

def test_redaction_scrubs_before_payload(tmp_path):
    redaction = {"secret_patterns": ["(?i)password\\s*[:=]\\s*\\S+"],
                 "deny_list": ["AcmeCorp"], "max_snippet_chars": 280}
    target = {"id": "x:t:1", "name": "AcmeCorp Service", "type": "csproj", "language": "csharp",
              "responsibilities": ["password=hunter2 internal note"]}
    payload = enrich_llm.element_payload(target, redaction)
    assert "AcmeCorp" not in payload["name"]
    assert "hunter2" not in payload.get("snippet", "")
    assert "[REDACTED]" in payload.get("snippet", "")


def test_snippet_truncated_to_cap():
    redaction = {"secret_patterns": [], "deny_list": [], "max_snippet_chars": 10}
    target = {"id": "x:t:1", "name": "n", "type": "csproj", "language": "csharp",
              "responsibilities": ["x" * 500]}
    payload = enrich_llm.element_payload(target, redaction)
    assert len(payload["snippet"]) == 10


# --- §8.3 richer payload context (deps-by-name, role, path, namespaces) -------

def test_element_payload_includes_role_path_deps_and_structure():
    red = {"secret_patterns": [], "deny_list": [], "max_snippet_chars": 280}
    target = {"id": "csharp:csproj:src/Catalog.API/Catalog.API.csproj",
              "name": "Catalog.API", "type": "csproj", "language": "csharp",
              "technology": ".NET assembly", "path": "src/Catalog.API",
              "container_name": "Catalog API",
              "namespaces": ["Catalog.API", "Catalog.API.Model"],
              "components": [{"name": "Catalog.API.Infrastructure"}]}
    p = enrich_llm.element_payload(target, red, dep_names=["Service Defaults", "Event Bus"])
    assert p["path"] == "src/Catalog.API"
    assert p["role"] == "Catalog API"                            # curated architectural role
    assert p["depends_on"] == ["Event Bus", "Service Defaults"]  # by NAME, sorted, not ids
    assert "Catalog.API.Model" in p["namespaces"]
    assert "Catalog.API.Infrastructure" in p["components"]


def test_element_payload_role_omitted_for_singleton():
    red = {"secret_patterns": [], "deny_list": [], "max_snippet_chars": 280}
    target = {"id": "x:t:a", "name": "Foo", "type": "csproj", "language": "csharp",
              "container_name": "Foo"}   # role == name (singleton) -> not repeated
    assert "role" not in enrich_llm.element_payload(target, red)


def _dep_curated() -> dict:
    return {
        "targets": [
            {"id": "x:cat", "name": "Catalog.API", "container_id": "c:cat", "container_name": "Catalog API"},
            {"id": "x:bsk", "name": "Basket.API", "container_id": "c:bsk", "container_name": "Basket API"},
            {"id": "x:bus", "name": "EventBus", "container_id": "c:bus", "container_name": "Event Bus"},
            {"id": "x:bus2", "name": "EventBusRabbitMQ", "container_id": "c:bus", "container_name": "Event Bus"},
        ],
        "relationships": [
            {"source": "x:cat", "target": "x:bus"},    # cross-container -> kept
            {"source": "x:bsk", "target": "x:bus"},    # cross-container -> kept
            {"source": "x:bus", "target": "x:bus2"},   # intra-container (both "Event Bus") -> skipped
        ],
    }


def test_dependency_names_resolves_cross_container_only():
    dn = enrich_llm.dependency_names(_dep_curated())
    assert dn["x:cat"] == ["Event Bus"]
    assert "x:bus" not in dn            # the bus has no cross-container OUTGOING edge


def test_dependency_context_resolves_incoming_used_by():
    deps, used_by = enrich_llm.dependency_context(_dep_curated())
    assert deps["x:cat"] == ["Event Bus"]
    # the leaf building block is described by its consumers (incoming, cross-container)
    assert used_by["x:bus"] == ["Catalog API", "Basket API"]
    assert "x:cat" not in used_by       # nothing depends on Catalog API here


def test_element_payload_includes_used_by():
    red = {"secret_patterns": [], "deny_list": [], "max_snippet_chars": 280}
    target = {"id": "x:bus", "name": "EventBus", "type": "csproj", "language": "csharp",
              "container_name": "Event Bus"}
    p = enrich_llm.element_payload(target, red, dep_names=[],
                                   used_by=["Catalog API", "Basket API"])
    assert p["used_by"] == ["Basket API", "Catalog API"]   # sorted, by name
    assert "depends_on" not in p                           # no outgoing deps -> omitted


# --- §8.3 / §16#10b evidence-strength cross-check ------------------------------

class HighConfProvider:
    """Self-reports `naming_confidence: high` for every element (advisory only, §8.3)."""
    model_id = "mock-highconf-1"

    def complete(self, system_prompt, prompt, element_facts):
        eid = element_facts["element_id"]
        return {
            "element_id": eid,
            "element_name": "Pretty " + element_facts["name"],
            "description": "A described element.",
            "responsibilities": ["do a thing"],
            "naming_confidence": "high",
            "evidence": [element_facts["name"]],
        }


class LowConfProvider:
    """Self-reports `naming_confidence: low` -> flagged via the self-report half (§8.3)."""
    model_id = "mock-lowconf-1"

    def complete(self, system_prompt, prompt, element_facts):
        eid = element_facts["element_id"]
        return {
            "element_id": eid,
            "element_name": "Pretty " + element_facts["name"],
            "description": "A described element.",
            "responsibilities": ["do a thing"],
            "naming_confidence": "low",
            "evidence": [element_facts["name"]],
        }


def _strong_target() -> dict:
    """An element with K+ incident evidence items AND doc text -> STRONG evidence."""
    return {"id": "x:t:strong", "name": "Strong", "type": "csproj", "language": "csharp",
            "container_name": "Strong Container", "responsibilities": ["Does real work"]}


def _ev(n: int) -> list[dict]:
    return [{"type": "link"} for _ in range(n)]


# -- the pure evidence_strength() function (no model, no network) ---------------

def test_evidence_strength_weak_when_below_k():
    # 1 incident evidence item < default K (2) -> weak even with doc text present.
    curated = {"targets": [_strong_target()],
               "relationships": [{"source": "x:t:strong", "target": "x:other",
                                  "evidence": _ev(1)}]}
    assert enrich_llm.evidence_strength(_strong_target(), curated) == "weak"


def test_evidence_strength_strong_when_k_met_and_doc_text():
    curated = {"targets": [_strong_target()],
               "relationships": [{"source": "x:t:strong", "target": "x:a", "evidence": _ev(1)},
                                 {"source": "x:b", "target": "x:t:strong", "evidence": _ev(1)}]}
    # 1 outgoing + 1 incoming evidence = 2 >= K, doc text present -> strong.
    assert enrich_llm.evidence_strength(_strong_target(), curated) == "strong"


def test_evidence_strength_weak_when_no_doc_text():
    tgt = {"id": "x:t:nodoc", "name": "NoDoc", "type": "csproj", "language": "csharp"}
    curated = {"targets": [tgt],
               "relationships": [{"source": "x:t:nodoc", "target": "x:a", "evidence": _ev(3)}]}
    # plenty of evidence (3 >= K) but NO doc/README text -> weak (the no-doc-text rule).
    assert enrich_llm.evidence_strength(tgt, curated) == "weak"


def test_evidence_strength_weak_when_needs_curation():
    tgt = dict(_strong_target(), tags=["needs-curation"])
    curated = {"targets": [tgt],
               "relationships": [{"source": "x:t:strong", "target": "x:a", "evidence": _ev(5)}]}
    # K met + doc text, but freshly needs-curation (§7.3) -> weak.
    assert enrich_llm.evidence_strength(tgt, curated) == "weak"


def test_evidence_strength_k_override():
    tgt = _strong_target()
    curated = {"targets": [tgt],
               "relationships": [{"source": "x:t:strong", "target": "x:a", "evidence": _ev(2)}]}
    assert enrich_llm.evidence_strength(tgt, curated, k=2) == "strong"
    assert enrich_llm.evidence_strength(tgt, curated, k=3) == "weak"   # raise the bar


# -- the cross-check wired into the propose path --------------------------------

def _ws_with(tmp_path, curated, rules="version: 1\n", sub="cc"):
    ws = resolve_workspace(TOY, arch_dir=tmp_path / sub / "arch", rules_dir=tmp_path / sub / "rules")
    dump_json(canonicalize(curated), ws.curated_facts)
    ws.mapping_rules.parent.mkdir(parents=True, exist_ok=True)
    ws.mapping_rules.write_text(rules, encoding="utf-8", newline="")
    return ws


def _strong_curated() -> dict:
    """A single element with doc text + K incident evidence items (strong)."""
    return {
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": "x:t:web", "name": "Web", "type": "csproj", "language": "csharp",
             "technology": ".NET assembly", "container_id": "c:web", "container_name": "Web",
             "responsibilities": ["Serves the web UI"]},
            {"id": "x:t:core", "name": "Core", "type": "csproj", "language": "csharp",
             "container_id": "c:core", "container_name": "Core"},
        ],
        "relationships": [
            {"id": "r1", "source": "x:t:web", "target": "x:t:core",
             "evidence": [{"type": "link"}, {"type": "project_ref"}]},
        ],
    }


def test_crosscheck_low_self_confidence_is_flagged(tmp_path):
    ws = _ws_with(tmp_path, _strong_curated(), sub="lowconf")
    enriched = enrich_llm.run(ws, "llm-propose", provider=LowConfProvider())
    web = next(t for t in enriched["targets"] if t["id"] == "x:t:web")
    # self-report low -> flagged even though evidence is strong (the cautious half).
    assert web["naming_confidence"] == "low"
    assert web["evidence_strength"] == "strong"
    assert web["review_flag"] is True
    assert "needs-curation" in web["tags"]


def test_crosscheck_high_confidence_but_weak_evidence_still_flagged(tmp_path):
    # The whole point: confident self-report, but sparse evidence + no doc text -> flagged.
    weak = {
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": "x:t:lonely", "name": "Lonely", "type": "csproj", "language": "csharp",
             "container_id": "c:lonely", "container_name": "Lonely"},  # no responsibilities
        ],
        "relationships": [],   # no incident evidence at all (< K)
    }
    ws = _ws_with(tmp_path, weak, sub="weakhigh")
    enriched = enrich_llm.run(ws, "llm-propose", provider=HighConfProvider())
    t = next(x for x in enriched["targets"] if x["id"] == "x:t:lonely")
    assert t["naming_confidence"] == "high"     # self-report says high...
    assert t["evidence_strength"] == "weak"     # ...but the deterministic check disagrees
    assert t["review_flag"] is True             # cross-check BITES -> flagged
    assert "needs-curation" in t["tags"]


def test_crosscheck_high_confidence_strong_evidence_not_flagged(tmp_path):
    ws = _ws_with(tmp_path, _strong_curated(), sub="strong")
    enriched = enrich_llm.run(ws, "llm-propose", provider=HighConfProvider())
    web = next(t for t in enriched["targets"] if t["id"] == "x:t:web")
    # high confidence + K-met evidence + doc text -> NOT flagged; both values surfaced.
    assert web["naming_confidence"] == "high"
    assert web["evidence_strength"] == "strong"
    assert web["review_flag"] is False
    assert "needs-curation" not in web.get("tags", [])
    # Core has weak evidence (only incident edge gives it 2, but no doc text) -> flagged.
    core = next(t for t in enriched["targets"] if t["id"] == "x:t:core")
    assert core["evidence_strength"] == "weak"   # no doc/README text
    assert core["review_flag"] is True


def test_crosscheck_surfaces_both_values_in_enriched_facts(tmp_path):
    ws = _ws_with(tmp_path, _strong_curated(), sub="both")
    enrich_llm.run(ws, "llm-propose", provider=HighConfProvider())
    enriched = load_json(ws.enriched_facts)
    web = next(t for t in enriched["targets"] if t["id"] == "x:t:web")
    # both the self-report and the computed signal persist to enriched-facts.json (diff shows why).
    assert {"naming_confidence", "evidence_strength", "review_flag"} <= set(web)


def test_crosscheck_k_override_via_naming_block(tmp_path):
    # Raise K to 3 via mapping-rules naming block -> the 2-evidence Web target goes weak.
    rules = "version: 1\nnaming:\n  evidence_strength_k: 3\n"
    ws = _ws_with(tmp_path, _strong_curated(), rules=rules, sub="koverride")
    enriched = enrich_llm.run(ws, "llm-propose", provider=HighConfProvider())
    web = next(t for t in enriched["targets"] if t["id"] == "x:t:web")
    assert web["evidence_strength"] == "weak"    # 2 < 3 now
    assert web["review_flag"] is True


def test_crosscheck_is_deterministic(tmp_path):
    ws_a = _ws_with(tmp_path, _strong_curated(), sub="det_a")
    ws_b = _ws_with(tmp_path, _strong_curated(), sub="det_b")
    enrich_llm.run(ws_a, "llm-propose", provider=HighConfProvider())
    enrich_llm.run(ws_b, "llm-propose", provider=HighConfProvider())
    assert ws_a.enriched_facts.read_bytes() == ws_b.enriched_facts.read_bytes()


# --- §8.4 review-queue acceptance: accept_proposals splice ----------------------

WEB_ID = "csharp:csproj:src/Web/Web.csproj"
CORE_ID = "csharp:csproj:src/Core/Core.csproj"


def test_accept_proposals_pins_name_and_description(tmp_path):
    rules = ("# reviewed file - comments must survive the splice\n"
             "version: 1\n"
             "group:\n"
             "  - container: Web Frontend   # hand comment\n"
             "    members: [\"" + WEB_ID + "\"]\n")
    ws = _ws(tmp_path, "acc", rules=rules)
    enrich_llm.run(ws, "llm-propose", provider=GoodProvider())

    res = enrich_llm.accept_proposals(ws, [WEB_ID])
    import yaml
    merged = yaml.safe_load(res["new_text"])
    assert merged["name_overrides"][WEB_ID] == "Pretty Web"
    assert merged["description_overrides"][WEB_ID] == "A described element."
    # comment-preserving: every original byte block still present
    assert "# reviewed file - comments must survive the splice" in res["new_text"]
    assert "# hand comment" in res["new_text"]
    assert res["accepted"] == [WEB_ID]


def test_accept_proposals_replaces_existing_pin_in_place(tmp_path):
    rules = ("version: 1\n"
             "name_overrides:\n"
             "  # keep me\n"
             '  "' + WEB_ID + '": "Old Name"\n'
             '  "x:t:other": "Untouched"\n')
    ws = _ws(tmp_path, "rep", rules=rules)
    enrich_llm.run(ws, "llm-propose", provider=GoodProvider())

    res = enrich_llm.accept_proposals(ws, [WEB_ID], with_descriptions=False)
    import yaml
    merged = yaml.safe_load(res["new_text"])
    assert merged["name_overrides"][WEB_ID] == "Pretty Web"      # replaced
    assert merged["name_overrides"]["x:t:other"] == "Untouched"  # untouched
    assert "# keep me" in res["new_text"]                        # comment survives
    assert res["new_text"].count(WEB_ID) == 1                    # no duplicate key
    assert "description_overrides" not in merged                 # names-only mode


def test_accept_proposals_refuses_without_proposals(tmp_path):
    import pytest
    ws = _ws(tmp_path, "no")
    with pytest.raises(ValueError):          # no enriched-facts.json at all
        enrich_llm.accept_proposals(ws, [WEB_ID])
    enrich_llm.run(ws, "no-llm")
    with pytest.raises(ValueError):          # a no-llm run proposed nothing
        enrich_llm.accept_proposals(ws, [WEB_ID])


def test_accept_proposals_unknown_id_is_keyerror(tmp_path):
    import pytest
    ws = _ws(tmp_path, "unk")
    enrich_llm.run(ws, "llm-propose", provider=GoodProvider())
    with pytest.raises(KeyError):
        enrich_llm.accept_proposals(ws, ["x:t:nope"])


def test_llm_accepted_serves_pinned_description_without_call(tmp_path):
    # both targets fully pinned -> NullProvider must never be reached (§8.5),
    # and the pinned description rides the same egress-free path as the name.
    rules = ("version: 1\n"
             "name_overrides:\n"
             '  "' + WEB_ID + '": "Web App"\n'
             '  "' + CORE_ID + '": "Core Library"\n'
             "description_overrides:\n"
             '  "' + WEB_ID + '": "Serves the storefront UI."\n')
    ws = _ws(tmp_path, "desc", rules=rules)
    enriched = enrich_llm.run(ws, "llm-accepted",
                              provider=enrich_llm.NullProvider())
    web = next(t for t in enriched["targets"] if t["id"] == WEB_ID)
    core = next(t for t in enriched["targets"] if t["id"] == CORE_ID)
    assert web["description"] == "Serves the storefront UI."
    assert web["enrich_status"] == "accepted"
    assert "description" not in core or core.get("description") != web["description"]
