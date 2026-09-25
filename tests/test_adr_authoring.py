"""ADR-mining plan §6 — the authoring agent + the §2a conformance loop (Tier 3).

Locked down here:
  * `draft_adr` fills ## Decision / ## Rationale / ## Considered alternatives ONLY from the
    human's supplied sections — an empty section stays a literal `TODO (human)` (§0.5);
  * supplied sections appear verbatim; Evidence + Linked-C4 are pre-filled from the candidate;
  * a `layering-convention` (structure) candidate offers a checkable constraint WITH a live
    verdict; a `technology` candidate offers NONE (§6.1 category-aware);
  * `promote_adr` is scaffold-once / never-overwrite (a second promote raises FileExistsError)
    and writes under the discovered ADR dir;
  * THE LOOP (§6.3): a promoted constraint-bearing ADR is VIOLATED when a violating edge exists
    and HELD when it does not — verified by `adr.run_check(..., history=False)`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from anon import adr, discover
from anon.jsonio import dump_json
from anon.model import canonicalize
from anon.paths import resolve_workspace

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"


class StubDrafter:
    """A draft provider: returns the same sections it was given (so the test asserts the
    human's words survive verbatim; restructuring is a no-op here)."""

    model_id = "stub-drafter"

    def complete(self, system_prompt, prompt, payload):
        return {"sections": dict(payload.get("sections", {}))}


def _tech_candidate():
    return {"slug": "tech-standardize-serilog", "kind": "tech-standardization",
            "category": "technology", "provenance": "detector", "method": "deterministic",
            "title": "Standardize on Serilog",
            "statement": "Serilog is referenced by 2 of 2 containers.",
            "elements": ["container:catalog", "container:ordering"], "linked_adrs": [],
            "n_sources": 2, "total_weight": 2,
            "evidence": [{"type": "package_ref", "detail": "Ordering references Serilog"}],
            "evidence_hash": "h", "derived_from_hash": "d"}


def _ws_with_candidate(tmp_path, candidate):
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "arch", rules_dir=tmp_path / "rules")
    dump_json({"schema": adr.STUBS_SCHEMA, "candidates": [candidate]},
              ws.adr_candidate_decisions_json)
    return ws


def _section_body(md, heading):
    """The first non-blank line of the section under *heading*."""
    after = md.split(heading, 1)[1].lstrip()
    return after.splitlines()[0]


# --------------------------------------------------------------------------- draft: human-only

def test_draft_fills_only_supplied_sections(tmp_path):
    """Supplied Decision appears verbatim; empty Rationale/Alternatives stay TODO (human)."""
    ws = _ws_with_candidate(tmp_path, _tech_candidate())
    draft = adr.draft_adr(ws, "tech-standardize-serilog",
                          {"decision": "We standardize on Serilog for all logging.",
                           "rationale": "", "alternatives": ""},
                          provider=StubDrafter())
    md = draft["draft_md"]
    assert _section_body(md, "## Decision") == "We standardize on Serilog for all logging."
    assert _section_body(md, "## Rationale") == "TODO (human)"
    assert _section_body(md, "## Considered alternatives") == "TODO (human)"


def test_draft_empty_input_leaves_every_human_section_todo(tmp_path):
    """A completely empty input leaves Context/Decision/Rationale/Consequences/Alternatives
    all as the literal `TODO (human)` — the §0.5 empty-bullets guard."""
    ws = _ws_with_candidate(tmp_path, _tech_candidate())
    draft = adr.draft_adr(ws, "tech-standardize-serilog", {})
    md = draft["draft_md"]
    for heading in ("## Context", "## Decision", "## Rationale", "## Consequences",
                    "## Considered alternatives"):
        assert _section_body(md, heading) == "TODO (human)", heading


def test_draft_prefills_evidence_and_linked_c4_from_candidate(tmp_path):
    ws = _ws_with_candidate(tmp_path, _tech_candidate())
    draft = adr.draft_adr(ws, "tech-standardize-serilog", {"decision": "Use Serilog."})
    md = draft["draft_md"]
    assert "## Evidence" in md and "Serilog" in md
    assert "## Linked C4 elements" in md
    assert "container:ordering" in md and "container:catalog" in md
    assert draft["machine_filled"] == ["evidence", "linked_c4_elements"]


def test_draft_unknown_slug_returns_none(tmp_path):
    ws = _ws_with_candidate(tmp_path, _tech_candidate())
    assert adr.draft_adr(ws, "no-such-candidate", {"decision": "x"}) is None


def test_draft_with_live_provider_routing_restructures_not_verbatim(tmp_path, monkeypatch):
    """End-to-end guard for the user-reported "Structure my notes just copied my notes verbatim":
    the LIVE AzureFoundryProvider must route the structure-adr payload so it returns
    {"sections": {...}}; then `_polish_sections` applies the restructured prose and `structured_by`
    reports the model — NOT the 'deterministic' verbatim fallback the routing bug always hit."""
    from anon import llm_provider
    from anon.llm_provider import LLMConfig

    ws = _ws_with_candidate(tmp_path, _tech_candidate())
    prov = llm_provider.make_provider(
        LLMConfig(endpoint="e", deployment="d", model_id="gpt-x", api_key="k"))
    monkeypatch.setattr(prov, "_raw_complete", lambda sp, um: json.dumps(
        {"sections": {"decision": "We adopt Serilog as the single logging library."}}))
    draft = adr.draft_adr(ws, "tech-standardize-serilog",
                          {"decision": "use serilog"}, provider=prov)
    assert draft["structured_by"] == "gpt-x"   # the model ran, not the verbatim fallback
    assert (_section_body(draft["draft_md"], "## Decision")
            == "We adopt Serilog as the single logging library.")


# ----------------------------------------------------- §6.1 category-aware constraint offer

def _ws_layering(tmp_path, *, with_violating_edge):
    """A UI/Data workspace with a held-layering candidate. `with_violating_edge` plants the
    forbidden `Data -> UI` edge so the offered constraint's verdict can be exercised."""
    repo = tmp_path / "repo"
    shutil.copytree(TOY, repo)
    ws = resolve_workspace(repo, arch_dir=tmp_path / "arch", rules_dir=tmp_path / "rules")
    rels = []
    if with_violating_edge:
        rels = [{"source": "csharp:csproj:src/Data/Data.csproj",
                 "target": "csharp:csproj:src/UI/UI.csproj", "weight": 5,
                 "evidence": [{"type": "project_ref", "detail": "x"}]}]
    curated = canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c",
                       "generated_at": "2026-01-01T00:00:00Z", "extractors": []},
        "targets": [
            {"id": "csharp:csproj:src/UI/UI.csproj", "name": "UI", "type": "csproj",
             "language": "csharp", "technology": ".NET",
             "container_id": "UI", "container_name": "UI"},
            {"id": "csharp:csproj:src/Data/Data.csproj", "name": "Data", "type": "csproj",
             "language": "csharp", "technology": ".NET",
             "container_id": "Data", "container_name": "Data"}],
        "relationships": rels})
    dump_json(curated, ws.curated_facts)
    dump_json(curated, ws.extracted_facts)
    ws.layering_rules.parent.mkdir(parents=True, exist_ok=True)
    ws.layering_rules.write_text("forbidden:\n  - from: Data\n    to: UI\n",
                                 encoding="utf-8", newline="")
    rules = {"forbidden": [{"from": "Data", "to": "UI"}]}
    result = adr.decision_candidates(curated, [], layering_rules=rules)
    lc = next(c for c in result["candidates"] if c["kind"] == "layering-convention")
    dump_json({"schema": adr.STUBS_SCHEMA, "candidates": [lc]},
              ws.adr_candidate_decisions_json)
    return ws, lc


def test_layering_candidate_offers_constraint_with_verdict(tmp_path):
    """A structure (layering-convention) candidate offers a checkable constraint with a live
    HELD verdict (no `Data -> UI` edge present)."""
    ws, lc = _ws_layering(tmp_path, with_violating_edge=False)
    draft = adr.draft_adr(ws, lc["slug"], {"decision": "Data must not depend on UI."})
    offered = draft["offered_constraints"]
    assert offered, "a layering-convention candidate offers a checkable constraint (§6.1)"
    o = offered[0]
    assert (o["from"], o["to"]) == ("Data", "UI")
    assert o["verdict"] == "HELD"
    assert o["verdict"] in {"HELD", "VIOLATED", "UNVERIFIED"}


def test_technology_candidate_offers_no_constraint(tmp_path):
    """A technology candidate offers NO constraint — 'standardize on EF' has no edge to forbid
    (§6.1 — forcing a constraint there is the distortion we avoid)."""
    ws = _ws_with_candidate(tmp_path, _tech_candidate())
    draft = adr.draft_adr(ws, "tech-standardize-serilog", {"decision": "Use Serilog."})
    assert draft["offered_constraints"] == []


# --------------------------------------------------------------------------- promote

def _seed_repo_ws(tmp_path, *, with_edge):
    """A writable repo copy + curated facts (UI, Data) with/without the UI->Data edge."""
    repo = tmp_path / "repo"
    shutil.copytree(TOY, repo)
    ws = resolve_workspace(repo, arch_dir=tmp_path / "arch", rules_dir=tmp_path / "rules")
    rels = []
    if with_edge:
        rels = [{"source": "csharp:csproj:src/UI/UI.csproj",
                 "target": "csharp:csproj:src/Data/Data.csproj", "weight": 5,
                 "evidence": [{"type": "project_ref", "detail": "x"}]}]
    curated = canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c",
                       "generated_at": "2026-01-01T00:00:00Z", "extractors": []},
        "targets": [
            {"id": "csharp:csproj:src/UI/UI.csproj", "name": "UI", "type": "csproj",
             "language": "csharp", "technology": ".NET",
             "container_id": "UI", "container_name": "UI"},
            {"id": "csharp:csproj:src/Data/Data.csproj", "name": "Data", "type": "csproj",
             "language": "csharp", "technology": ".NET",
             "container_id": "Data", "container_name": "Data"}],
        "relationships": rels})
    dump_json(curated, ws.curated_facts)
    dump_json(curated, ws.extracted_facts)
    # a candidate so the slug resolves for promote
    dump_json({"schema": adr.STUBS_SCHEMA,
               "candidates": [{"slug": "ui-isolation", "kind": "layering-convention",
                               "category": "structure", "title": "UI isolation",
                               "statement": "s", "elements": ["UI", "Data"], "evidence": [],
                               "n_sources": 1, "total_weight": 1, "linked_adrs": [],
                               "evidence_hash": "h", "derived_from_hash": "d",
                               "provenance": "detector", "method": "deterministic"}]},
              ws.adr_candidate_decisions_json)
    discover.run(ws)
    return ws


CONSTRAINT_BODY = ("# UI isolation\n\n## Status\n\nAccepted\n\n## Decision\n\n"
                   "The UI must not depend on Data.\n")


def test_promote_writes_under_discovered_dir_and_is_scaffold_once(tmp_path):
    ws = _seed_repo_ws(tmp_path, with_edge=False)
    res = adr.promote_adr(ws, "ui-isolation", body=CONSTRAINT_BODY, title="UI isolation")
    assert res is not None
    assert res["path"].endswith("0001-ui-isolation.md")
    assert res["dir"] == adr.adr_target_dir(ws)
    written = (ws.repo / res["path"]).read_text(encoding="utf-8")
    assert "The UI must not depend on Data." in written
    # never-overwrite: a second promote of the same slug raises
    with pytest.raises(FileExistsError):
        adr.promote_adr(ws, "ui-isolation", body="different body", title="dup")


def test_promote_unknown_slug_with_no_body_returns_none(tmp_path):
    ws = _seed_repo_ws(tmp_path, with_edge=False)
    assert adr.promote_adr(ws, "no-such-slug") is None


def test_promote_removes_candidate_from_inbox(tmp_path):
    """§6.3 / §8: a promoted candidate is the loop closed — it must LEAVE the inbox (it would
    otherwise linger forever as a candidate of itself, the duplicate the user hit). promote_adr
    re-materializes the inbox, and materialize_inbox drops any candidate that already has a
    promoted ADR file, reporting it under counts.promoted instead."""
    ws = _seed_repo_ws(tmp_path, with_edge=False)
    before = adr.materialize_inbox(ws)
    assert "ui-isolation" in [c["slug"] for c in before["candidates"]]
    assert before["counts"]["promoted"] == 0

    adr.promote_adr(ws, "ui-isolation", body=CONSTRAINT_BODY, title="UI isolation")

    after = adr.materialize_inbox(ws)
    assert "ui-isolation" not in [c["slug"] for c in after["candidates"]]
    assert "ui-isolation" not in [c["slug"] for c in after["dismissed"]]
    assert after["counts"]["promoted"] == 1


def test_promote_filter_does_not_drop_a_suffix_slug_candidate(tmp_path):
    """The promoted-file match is precise: a candidate whose slug is a SUFFIX of a longer
    promoted slug (`serilog` vs `tech-standardize-serilog`) must NOT be mistaken for promoted."""
    ws = _seed_repo_ws(tmp_path, with_edge=False)

    def _cand(slug, ev):
        return {"slug": slug, "kind": "tech-standardization", "category": "technology",
                "title": slug, "statement": "s", "elements": ["UI"], "evidence": [],
                "n_sources": 1, "total_weight": 1, "linked_adrs": [], "evidence_hash": ev,
                "derived_from_hash": "d", "provenance": "detector", "method": "deterministic"}

    dump_json({"schema": adr.STUBS_SCHEMA,
               "candidates": [_cand("tech-standardize-serilog", "h1"), _cand("serilog", "h2")]},
              ws.adr_candidate_decisions_json)
    adr.promote_adr(ws, "tech-standardize-serilog",
                    body="# X\n\n## Decision\n\nUse Serilog.\n", title="Standardize on Serilog")

    inbox = adr.materialize_inbox(ws)
    slugs = [c["slug"] for c in inbox["candidates"]]
    assert "serilog" in slugs                       # the suffix-slug candidate is untouched
    assert "tech-standardize-serilog" not in slugs  # the promoted one left
    assert inbox["counts"]["promoted"] == 1


# --------------------------------------------------------------------------- §6.3 the loop

def test_loop_promoted_constraint_is_violated_when_edge_exists(tmp_path):
    """§6.3 closure: promote 'UI must not depend on Data'; with a UI->Data edge present,
    `arch adr check` reports the constraint VIOLATED."""
    ws = _seed_repo_ws(tmp_path, with_edge=True)
    adr.promote_adr(ws, "ui-isolation", body=CONSTRAINT_BODY, title="UI isolation")
    report = adr.run_check(ws, history=False)
    ui = next(a for a in report["adrs"] if (a["title"] or "").startswith("# UI isolation")
              or "UI isolation" in (a["title"] or ""))
    assert ui["verdict"] == "VIOLATED"
    assert any(c["status"] == "VIOLATED" for c in ui["constraints"])


def test_loop_promoted_constraint_is_held_when_no_edge(tmp_path):
    """The mirror: with no UI->Data edge, the same promoted constraint is HELD."""
    ws = _seed_repo_ws(tmp_path, with_edge=False)
    adr.promote_adr(ws, "ui-isolation", body=CONSTRAINT_BODY, title="UI isolation")
    report = adr.run_check(ws, history=False)
    ui = next(a for a in report["adrs"] if "UI isolation" in (a["title"] or ""))
    assert ui["verdict"] == "HELD"
