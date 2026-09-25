"""Engine §6 — the oracle/query primitives (`anon.query`).

What is locked down here (engine §6.1–§6.3):

  * every answer is **cited** (edges carry evidence strength/layers) and carries
    ``derived_from_hash`` + the coverage caveat;
  * answers are **deterministic** — same model, same question → byte-identical JSON;
  * "no path found" / "unknown id (did you mean …?)" / "no rules configured" are
    first-class, honest answers — never a guess;
  * conformance reuses the `check_layering` machinery, so it can never disagree with
    `arch verify`;
  * the `/ask` router is deterministic keyword matching (no LLM), and the SCC pass in
    `stages/risk.py` finds directed cycles the toy graph does/doesn't have.

One toy pipeline run is shared module-wide (the primitives are pure reads).
"""
from __future__ import annotations

import pytest

from conftest import run_toy_pipeline

from anon import query
from anon.jsonio import dumps_json, load_json
from anon.model import content_hash
from anon.stages.risk import cycles, strongly_connected_components

WEB = "csharp:csproj:src/Web/Toy.Web.csproj"
DOMAIN = "csharp:csproj:src/Domain/Toy.Domain.csproj"
COMMON = "csharp:csproj:src/Common/Toy.Common.csproj"
MESSAGING = "csharp:csproj:src/Messaging/Toy.Messaging.csproj"


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    tr = run_toy_pipeline(tmp_path_factory.mktemp("queryrun") / "architecture")
    return tr.ws, load_json(tr.ws.curated_facts)


# --------------------------------------------------------------------------- envelope

def test_envelope_carries_hash_and_coverage_caveat(toy):
    _ws, facts = toy
    r = query.dependents(facts, "Core Library")
    assert r["schema"] == query.QUERY_SCHEMA
    assert r["derived_from_hash"] == content_hash(facts)
    assert r["coverage"]["floor"] == 0.80
    assert r["coverage"]["advisory"] is False  # toy is 100% L0


def test_determinism_same_question_same_bytes(toy):
    _ws, facts = toy
    a = dumps_json(query.blast_radius(facts, COMMON))
    b = dumps_json(query.blast_radius(facts, COMMON))
    assert a == b


# --------------------------------------------------------------------------- resolve

def test_resolve_container_by_name_and_id(toy):
    _ws, facts = toy
    by_name = query.resolve(facts, "Core Library")
    assert by_name["kind"] == "container"
    assert sorted(by_name["targets"]) == sorted([DOMAIN, COMMON])
    by_id = query.resolve(facts, by_name["id"])
    assert by_id["targets"] == by_name["targets"]


def test_resolve_unknown_offers_candidates_never_guesses(toy):
    _ws, facts = toy
    res = query.resolve(facts, "Core")
    assert res["kind"] == "unknown"
    assert res["targets"] == []
    assert any("coreLibrary" in c or "Core" in c for c in res["candidates"])
    r = query.dependents(facts, "Core")
    assert r["error"] == "unknown_element"
    assert r["resolved"] is None


# --------------------------------------------------------------------------- primitives

def test_dependents_direct_is_cited(toy):
    _ws, facts = toy
    r = query.dependents(facts, "Core Library")
    ids = [x["id"] for x in r["results"]]
    assert ids == sorted([MESSAGING, WEB]) or set(ids) == {MESSAGING, WEB}
    for x in r["results"]:
        assert x["edges"], "every direct dependent cites its edge(s)"
        for e in x["edges"]:
            assert e["evidence_strength"]
            assert e["evidence_layers"]


def test_dependencies_transitive_has_depth_and_path(toy):
    _ws, facts = toy
    r = query.dependencies(facts, WEB, transitive=True)
    by_id = {x["id"]: x for x in r["results"]}
    assert COMMON in by_id
    common = by_id[COMMON]
    assert common["depth"] == 2
    # the path reads source → … → target along forward edges
    assert common["path"][0] == WEB and common["path"][-1] == COMMON


def test_blast_radius_both_directions(toy):
    _ws, facts = toy
    r = query.blast_radius(facts, COMMON)
    impacted = {x["id"] for x in r["impacted"]["elements"]}
    assert impacted == {DOMAIN, MESSAGING, WEB}
    assert r["reachable"]["total"] == 0  # Common is the leaf


def test_paths_between_cited_chain(toy):
    _ws, facts = toy
    r = query.paths_between(facts, WEB, COMMON)
    assert r["found"] is True and r["shortest_length"] == 2
    for p in r["paths"]:
        assert p["elements"][0] == WEB and p["elements"][-1] == COMMON
        assert len(p["edges"]) == len(p["elements"]) - 1
        for e in p["edges"]:
            assert e["evidence"], "every hop carries its cited evidence"


def test_no_path_found_is_first_class(toy):
    _ws, facts = toy
    r = query.paths_between(facts, COMMON, WEB)  # against the dependency direction
    assert r["found"] is False
    assert r["paths"] == []
    assert "advisory" in r["note"]


def test_cycles_toy_is_acyclic(toy):
    _ws, facts = toy
    r = query.cycles_of(facts, WEB)
    assert r["found"] is False
    assert r["target_cycles"] == [] and r["container_cycles"] == []


def test_conformance_forbidden_and_allowed(toy):
    _ws, facts = toy
    rules = {"forbidden": [{"from": "Web Frontend", "to": "Messaging"}]}
    r = query.conformance_of(facts, WEB, MESSAGING, rules)
    assert r["verdict"] == "FORBIDDEN" and r["allowed"] is False
    assert r["matched_rules"][0]["status"] == "VIOLATION"  # the edge actually exists
    assert r["existing_edges"], "the live offending edge is cited"

    r2 = query.conformance_of(facts, MESSAGING, WEB, rules)  # reverse direction: allowed
    assert r2["verdict"] == "ALLOWED" and r2["allowed"] is True


def test_conformance_no_rules_is_honest(toy):
    _ws, facts = toy
    r = query.conformance_of(facts, WEB, MESSAGING, {})
    assert r["verdict"] == "ALLOWED"
    assert r["rules_present"] is False
    assert "abstention" in r["note"] or "not a verified guarantee" in r["note"]


def test_explain_delegates_to_explain_module(toy):
    _ws, facts = toy
    r = query.explain_of(facts, WEB)
    assert r["target"]["placement_reason"]
    r2 = query.explain_of(facts, WEB, to=DOMAIN)
    assert r2["found"] is True and r2["edges"][0]["evidence"]


# --------------------------------------------------------------------------- /ask router

@pytest.mark.parametrize("q,expected", [
    ("what depends on Core Library?", "dependents"),
    ("what does Web Frontend depend on?", "dependencies"),
    ("blast radius of Core Library?", "blast-radius"),
    ("what is impacted if I change Core Library?", "blast-radius"),
    ("why does Web Frontend reach Core Library?", "path"),
    ("is it allowed for Web Frontend to call Messaging?", "conformance"),
    ("any cycles around Core Library?", "cycles"),
    ("why is Web Frontend here?", "explain"),
])
def test_ask_routing(toy, q, expected):
    _ws, facts = toy
    r = query.ask(facts, q=q)
    assert r.get("error") is None, r
    assert r["primitive"] == expected
    assert r["routed"]["q"] == q


def test_ask_mention_extraction_is_word_bounded(toy):
    _ws, facts = toy
    # "Web Frontend" must be extracted as one mention, in question order
    r = query.ask(facts, q="why does Web Frontend reach Core Library?")
    assert r["routed"]["mentions"] == ["Web Frontend", "Core Library"]


def test_ask_unroutable_and_unmentioned_are_errors(toy):
    _ws, facts = toy
    assert query.ask(facts, q="tell me a joke")["error"] == "bad_query"
    assert query.ask(facts, q="what depends on the flux capacitor?")["error"] == "bad_query"


# --------------------------------------------------------------------------- CLI surface

def test_run_writes_reproducible_result(toy):
    ws, facts = toy
    r1 = query.run(ws, "dependents", ident="Core Library", out=True)
    path = r1["written_to"]
    r2 = query.run(ws, "dependents", ident="Core Library", out=True)
    assert r2["written_to"] == path, "same question over the same model → same file"
    on_disk = load_json(path)
    assert on_disk["derived_from_hash"] == content_hash(facts)
    assert "written_to" not in on_disk, "the persisted artifact stays path-free"


def test_render_text_is_unicode_and_cited(toy):
    _ws, facts = toy
    text = query.render(query.dependents(facts, "Core Library"))
    assert "derived_from" in text
    assert "project_ref" in text  # the citation reaches the human surface


# --------------------------------------------------------------------------- risk core

def test_tarjan_finds_directed_cycles_deterministically():
    adj = {"a": ["b"], "b": ["c"], "c": ["a", "d"], "d": [], "e": ["e"]}
    sccs = strongly_connected_components(["a", "b", "c", "d", "e"], adj)
    assert ["a", "b", "c"] in sccs
    cyc = cycles(["a", "b", "c", "d", "e"], adj)
    assert cyc == [["a", "b", "c"], ["e"]]  # SCC≥2 plus the self-loop; sorted


def test_tarjan_acyclic_graph_has_no_cycles():
    adj = {"a": ["b", "c"], "b": ["c"], "c": []}
    assert cycles(["a", "b", "c"], adj) == []
    assert strongly_connected_components(["a", "b", "c"], adj) == [["a"], ["b"], ["c"]]
