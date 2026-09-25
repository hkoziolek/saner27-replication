"""Tests for ADR adoption — rationale, never structure (plan §4.5.4).

Pure-function tests for the conservative name->element linking, the candidate-constraint
sniff, and the naming context, plus the workspace-level no-op guarantee (no ADRs -> no
file written -> determinism preserved). All writes go to ``tmp_path``.
"""
from __future__ import annotations

from pathlib import Path

from anon import adr
from anon.jsonio import dump_json, load_json
from anon.paths import resolve_workspace

_ADRS = [
    {"id": "0007", "title": "Adopt MQTT for the Telemetry Gateway", "status": "accepted"},
    {"id": "0012", "title": "The UI layer must not call the Database directly", "status": "accepted"},
    {"id": "0003", "title": "Use Postgres", "status": "proposed"},
]

_TARGETS = [
    {"id": "cpp:target:TelemetryGateway", "name": "Telemetry Gateway"},
    {"id": "cpp:target:App", "name": "App"},                 # too-generic single token
    {"id": "csharp:csproj:src/Db/Database.csproj", "name": "Database"},
]


def test_link_adrs_to_targets_multiword_contiguous_match():
    links = adr.link_adrs_to_targets(_ADRS, _TARGETS)
    # "Telemetry Gateway" appears contiguously in ADR 0007's title.
    assert links.get("cpp:target:TelemetryGateway") == ["0007"]
    # "Database" (single token, len>=4) appears in ADR 0012's title.
    assert links.get("csharp:csproj:src/Db/Database.csproj") == ["0012"]


def test_link_does_not_match_short_generic_single_token():
    # "App" is a single token below the 4-char anchor -> never matched.
    links = adr.link_adrs_to_targets(_ADRS, _TARGETS)
    assert "cpp:target:App" not in links


def test_candidate_layering_rules_extracts_constraint():
    rules = adr.candidate_layering_rules(_ADRS)
    assert len(rules) == 1
    r = rules[0]
    assert r["from"].lower().startswith("the ui layer") or "ui layer" in r["from"].lower()
    assert "database" in r["to"].lower()
    assert r["adr"] == "0012"


def test_candidate_rules_deterministic_and_empty_when_none():
    assert adr.candidate_layering_rules([{"id": "1", "title": "Use Postgres"}]) == []
    assert adr.candidate_layering_rules(_ADRS) == adr.candidate_layering_rules(_ADRS)


def test_naming_context_returns_titles_with_status():
    ctx = adr.naming_context(_ADRS, "Telemetry Gateway")
    assert ctx == ["Adopt MQTT for the Telemetry Gateway [accepted]"]
    assert adr.naming_context(_ADRS, "Nonexistent Service") == []


def test_run_is_noop_without_adrs(tmp_path: Path):
    """A repo with no ADRs writes nothing and does not touch extracted-facts (determinism)."""
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch")
    facts = {"targets": [{"id": "cpp:target:Foo", "name": "Foo", "tags": []}], "relationships": []}
    ws.extracted_facts.parent.mkdir(parents=True, exist_ok=True)
    dump_json(facts, ws.extracted_facts)
    before = ws.extracted_facts.read_bytes()
    # no discovered-artifacts.json and (tmp repo) no ADR dirs -> empty discovery -> no-op.
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    summary = adr.run(ws)
    assert summary == {"adrs": 0, "tagged": 0, "candidates": 0}
    assert ws.extracted_facts.read_bytes() == before          # untouched
    assert not ws.adr_candidates_md.exists()


def test_run_tags_targets_and_writes_candidates(tmp_path: Path):
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch")
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    facts = {"targets": [dict(t, tags=[]) for t in _TARGETS], "relationships": []}
    ws.extracted_facts.parent.mkdir(parents=True, exist_ok=True)
    dump_json(facts, ws.extracted_facts)
    # seed a discovery report so run() consumes it directly.
    dump_json({"models": [], "adrs": _ADRS, "counts": {}}, ws.discovered_artifacts)

    summary = adr.run(ws)
    assert summary["adrs"] == 3
    assert summary["tagged"] == 2           # TelemetryGateway + Database
    assert summary["candidates"] == 1

    out = load_json(ws.extracted_facts)
    by_id = {t["id"]: t for t in out["targets"]}
    assert "adr:0007" in by_id["cpp:target:TelemetryGateway"]["tags"]
    assert "adr:0012" in by_id["csharp:csproj:src/Db/Database.csproj"]["tags"]
    assert ws.adr_candidates_md.exists()
    assert "forbidden:" in ws.adr_candidates_md.read_text(encoding="utf-8")
