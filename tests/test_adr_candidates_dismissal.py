"""ADR-mining plan §3.1 — the hand-owned dismissal state (`rules/adr-candidates.yaml`).

Locked down here:
  * a `dismissed:` entry (written through the `dismiss_candidate` line-editor op) removes the
    slug from the pre-materialized inbox's `candidates` but NOT from the deterministic
    `adr-candidate-decisions.json` (still computed + auditable);
  * the dismissed row surfaces in the inbox `dismissed` list with its `dismiss_reason`;
  * **evidence-LOCAL expiry**: the dismissal lapses (the candidate resurfaces) when THIS
    candidate's `evidence_hash` changes, but is unaffected by an unrelated fact-model change;
  * the write op preserves comments / LF / quoting (C§8.1 discipline).
"""
from __future__ import annotations

from pathlib import Path

from anon import adr, rules_edit
from anon.config import load_yaml
from anon.jsonio import dump_json, load_json
from anon.paths import resolve_workspace

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"

SLUG = "tech-standardize-dapper-outlier"


def _candidate(slug=SLUG, evidence_hash="abc123"):
    return {"slug": slug, "kind": "tech-standardization", "category": "technology",
            "provenance": "detector", "method": "deterministic",
            "title": "split logging choice", "statement": "evidence here",
            "elements": ["container:x"], "linked_adrs": [], "n_sources": 1,
            "total_weight": 1, "evidence": [{"type": "package_ref", "detail": "d"}],
            "evidence_hash": evidence_hash, "derived_from_hash": "h"}


def _ws(tmp_path, *, candidate=None):
    """A workspace with one deterministic candidate written to the decisions JSON."""
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "arch", rules_dir=tmp_path / "rules")
    dump_json({"schema": adr.STUBS_SCHEMA, "candidates": [candidate or _candidate()]},
              ws.adr_candidate_decisions_json)
    return ws


def _dismiss(ws, slug, reason, evidence_hash):
    """Write a dismissal through the reviewed line-editor write surface (§3.1)."""
    ws.adr_candidates_yaml.parent.mkdir(parents=True, exist_ok=True)
    prior = (ws.adr_candidates_yaml.read_text(encoding="utf-8")
             if ws.adr_candidates_yaml.exists() else "")
    new = rules_edit.apply_ops(prior, [{"op": "dismiss_candidate", "slug": slug,
                                        "reason": reason,
                                        "evidence_hash": evidence_hash}])
    ws.adr_candidates_yaml.write_text(new, encoding="utf-8", newline="")
    return new


# --------------------------------------------------------------------------- filtering

def test_dismissal_removes_from_inbox_but_not_decisions(tmp_path):
    ws = _ws(tmp_path)
    _dismiss(ws, SLUG, "intentional — reporting service legitimately uses Dapper", "abc123")
    inbox = adr.materialize_inbox(ws)
    assert [c["slug"] for c in inbox["candidates"]] == [], "dismissed slug is gone from inbox"
    # but the candidate is STILL computed + auditable in the deterministic decisions artifact
    decisions = load_json(ws.adr_candidate_decisions_json)
    assert any(c["slug"] == SLUG for c in decisions["candidates"])


def test_dismissed_row_carries_its_reason(tmp_path):
    ws = _ws(tmp_path)
    reason = "intentional — reporting service legitimately uses Dapper"
    _dismiss(ws, SLUG, reason, "abc123")
    inbox = adr.materialize_inbox(ws)
    dismissed = inbox["dismissed"]
    assert len(dismissed) == 1
    assert dismissed[0]["slug"] == SLUG
    assert dismissed[0]["dismiss_reason"] == reason
    assert inbox["counts"] == {"total": 1, "shown": 0, "dismissed": 1,
                               "promoted": 0, "capped": 0}


# --------------------------------------------------------------- evidence-local expiry (§3.1)

def test_dismissal_lapses_when_candidate_evidence_hash_changes(tmp_path):
    """The dismissal is honored at hash `abc123`; when the candidate's OWN evidence_hash
    changes, the dismissal lapses and the candidate resurfaces once (§3.1)."""
    ws = _ws(tmp_path)
    _dismiss(ws, SLUG, "not worth an ADR", "abc123")
    assert adr.materialize_inbox(ws)["candidates"] == []   # dismissed at abc123

    # the candidate's evidence materially changes -> a new evidence_hash
    dump_json({"schema": adr.STUBS_SCHEMA,
               "candidates": [_candidate(evidence_hash="DIFFERENT")]},
              ws.adr_candidate_decisions_json)
    inbox = adr.materialize_inbox(ws)
    assert [c["slug"] for c in inbox["candidates"]] == [SLUG], "resurfaces on hash change"
    assert inbox["dismissed"] == [], "no longer in the dismissed drawer"


def test_dismissal_survives_unrelated_change(tmp_path):
    """An unrelated fact-model change that does NOT touch THIS candidate's evidence_hash must
    not lapse the dismissal (expiry is evidence-LOCAL, not whole-model — §3.1)."""
    ws = _ws(tmp_path)
    _dismiss(ws, SLUG, "not worth an ADR", "abc123")
    # a second, unrelated candidate appears; the dismissed candidate's hash is unchanged.
    dump_json({"schema": adr.STUBS_SCHEMA,
               "candidates": [_candidate(evidence_hash="abc123"),
                              _candidate(slug="some-other-candidate",
                                         evidence_hash="zzz999")]},
              ws.adr_candidate_decisions_json)
    inbox = adr.materialize_inbox(ws)
    shown = {c["slug"] for c in inbox["candidates"]}
    assert SLUG not in shown, "the dismissal still holds for the unchanged candidate"
    assert "some-other-candidate" in shown
    assert {c["slug"] for c in inbox["dismissed"]} == {SLUG}


# ---------------------------------------------------------------- write op discipline (C§8.1)

def test_dismiss_op_preserves_comments_and_lf(tmp_path):
    """The line-editor write surface keeps a hand comment + LF endings and re-parses clean."""
    ws = _ws(tmp_path)
    ws.adr_candidates_yaml.parent.mkdir(parents=True, exist_ok=True)
    seed = "# hand-owned dismissal state (ADR-mining §3.1)\ndismissed: []\n"
    # an empty flow `[]` is fail-closed by the line editor, so start from a real seed instead:
    seed = "# hand-owned dismissal state (ADR-mining §3.1)\n"
    ws.adr_candidates_yaml.write_text(seed, encoding="utf-8", newline="")
    new = _dismiss(ws, SLUG, "reason: keep # this comment-ish text intact", "abc123")
    assert new.startswith("# hand-owned dismissal state"), "the hand comment is preserved"
    assert "\r" not in new, "LF endings only"
    # the reason text (with a `#`) round-trips as data, not as a YAML comment
    parsed = load_yaml(ws.adr_candidates_yaml)
    entry = parsed["dismissed"][0]
    assert entry["slug"] == SLUG
    assert entry["reason"] == "reason: keep # this comment-ish text intact"
    assert entry["dismissed_at_evidence_hash"] == "abc123"


def test_dismiss_op_is_idempotent_on_same_slug_and_hash(tmp_path):
    """Re-dismissing the same slug at the same evidence_hash is a no-op (§3.1)."""
    ws = _ws(tmp_path)
    first = _dismiss(ws, SLUG, "not worth an ADR", "abc123")
    second = _dismiss(ws, SLUG, "not worth an ADR", "abc123")
    assert first == second, "a duplicate dismissal does not append a second entry"
    parsed = load_yaml(ws.adr_candidates_yaml)
    assert len(parsed["dismissed"]) == 1
