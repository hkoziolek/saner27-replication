"""Engine §2a — `arch adr check`: ADR ingest + conformance against the live model.

Locked down here:
  * **body-text constraint extraction** (the §2a.7 prerequisite — title-only marks most
    real ADRs NOT-A-CONSTRAINT, and the dangerous error is the silent false negative):
    sentences in the body parse, markdown emphasis is stripped, prose-noise around the
    element name is trimmed by resolving against the model;
  * conformance verdicts come from the same `check_layering` engine as `arch verify` —
    VIOLATED cites the offending edges with their evidence strength;
  * honest statuses: an unresolvable element is UNVERIFIED (never guessed), a
    constraint-free ADR is NOT-A-CONSTRAINT with "checked, none found" wording;
  * first-violating-commit reads COMMITTED fact snapshots only (§0.6) — "history
    unavailable" on out-of-tree workspaces, the real commit on an in-tree history;
  * determinism — same inputs → byte-identical artifacts.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import run_toy_pipeline

from anon import adr
from anon.jsonio import dump_json, load_json
from anon.paths import resolve_workspace

VIOLATED_ADR = """# 1. Web Frontend stays decoupled from Messaging

## Status

Accepted

## Decision

The **Web Frontend** must not depend on Messaging directly; all events go
through the Core Library.
"""

HELD_ADR = """# 2. Messaging never calls the Web Frontend

## Status

Accepted

## Decision

We decided that Messaging must never call the Web Frontend.
"""

PROSE_ADR = """# 3. Use PostgreSQL for persistence

## Status

Accepted

## Decision

We standardize on PostgreSQL 16 for all persistence needs.
"""

UNRESOLVABLE_ADR = """# 4. Billing isolation

## Decision

The Billing service must not call the Mainframe gateway.
"""


@pytest.fixture(scope="module")
def checked(tmp_path_factory):
    """One toy run with four planted ADRs, conformance-checked once."""
    tr = run_toy_pipeline(tmp_path_factory.mktemp("adrrun") / "architecture")
    adr_dir = tr.ws.repo / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-web-decoupled.md").write_text(VIOLATED_ADR, encoding="utf-8")
    (adr_dir / "0002-messaging-isolation.md").write_text(HELD_ADR, encoding="utf-8")
    (adr_dir / "0003-use-postgres.md").write_text(PROSE_ADR, encoding="utf-8")
    (adr_dir / "0004-billing.md").write_text(UNRESOLVABLE_ADR, encoding="utf-8")
    report = adr.run_check(tr.ws)
    return tr.ws, report


def _by_id(report, adr_id):
    return next(a for a in report["adrs"] if a["id"] == adr_id)


# --------------------------------------------------------------------------- verdicts

def test_body_constraint_violated_with_cited_edges(checked):
    _ws, report = checked
    a = _by_id(report, "0001")
    assert a["verdict"] == "VIOLATED"
    c = a["constraints"][0]
    assert c["source"] == "body", "the constraint lives in ## Decision, not the title"
    assert (c["from"], c["to"]) == ("Web Frontend", "Messaging")
    assert c["offending_edges"], "a VIOLATED constraint cites the live edge"
    assert c["offending_edges"][0]["evidence_strength"]
    assert c["first_violating_commit"] == "history unavailable"  # out-of-tree arch dir


def test_markdown_emphasis_is_stripped(checked):
    """`**Web Frontend** must not …` parses — emphasis must not defeat the regex."""
    _ws, report = checked
    assert _by_id(report, "0001")["constraints"], "the bolded sentence still parsed"


def test_held_constraint(checked):
    _ws, report = checked
    a = _by_id(report, "0002")
    assert a["verdict"] == "HELD"
    assert a["constraints"][0]["status"] == "HELD"
    assert a["constraints"][0]["offending_edges"] == []


def test_prose_adr_is_not_a_constraint_with_honest_wording(checked):
    _ws, report = checked
    a = _by_id(report, "0003")
    assert a["verdict"] == "NOT-A-CONSTRAINT"
    assert "none found" in a["note"], \
        "'checked, none found' is distinguished from 'could not parse' (§2a.7)"


def test_unresolvable_element_is_unverified_never_guessed(checked):
    _ws, report = checked
    a = _by_id(report, "0004")
    assert a["verdict"] == "UNVERIFIED"
    c = a["constraints"][0]
    assert c["status"] == "UNVERIFIED"
    assert "does not name a model element" in c["reason"]


def test_summary_and_artifacts(checked):
    ws, report = checked
    assert report["summary"] == {"HELD": 1, "VIOLATED": 1, "UNVERIFIED": 1,
                                 "NOT-A-CONSTRAINT": 1}
    assert report["gating"] is True  # full-coverage run, real violation
    assert ws.adr_conformance_json.exists()
    md = (ws.generated / "adr-conformance.md").read_text(encoding="utf-8")
    assert "[VIOLATED] 0001" in md and "[HELD] 0002" in md


def test_determinism_byte_identical(checked):
    ws, _report = checked
    a = ws.adr_conformance_json.read_bytes()
    adr.run_check(ws)
    assert ws.adr_conformance_json.read_bytes() == a


# --------------------------------------------------------------------------- units

def test_extract_constraints_title_and_body_deduped():
    rows = adr.extract_constraints(
        "UI must not call the Database",
        "## Decision\n\nThe UI must not call the Database.\n"
        "Also, Web must never access Storage directly.\n")
    pairs = {(r["from_raw"].lower(), r["to_raw"].lower()) for r in rows}
    assert ("web", "storage") in {(f.split()[-1], t.split()[0])
                                  for f, t in pairs} or len(rows) == 2
    sources = {r["source"] for r in rows}
    assert "title" in sources  # the title hit wins the dedupe for the UI->Database pair


def test_resolve_token_trims_prose_noise():
    lookup = {"web frontend": "Web Frontend", "messaging": "Messaging"}
    assert adr._resolve_token("We decided that the Web Frontend", lookup,
                              side="from") == "Web Frontend"
    assert adr._resolve_token("Messaging directly", lookup, side="to") == "Messaging"
    assert adr._resolve_token("the Mainframe gateway", lookup, side="to") is None


# --------------------------------------------------------------------------- full prose

def test_body_prose_is_carried_for_rendering(checked):
    """The §5.3 Decisions screen renders the human's full ADR prose, so the artifact
    carries the body verbatim — not just the interpreted constraint."""
    _ws, report = checked
    a = _by_id(report, "0001")
    assert a["body"] and "all events go" in a["body"], \
        "the full decision prose is carried, not only the parsed constraint"
    assert a["body"].startswith("# 1. Web Frontend"), "prose starts at the heading"


def test_adr_prose_strips_leading_front_matter():
    """_adr_prose drops a leading MADR/YAML front-matter block so the rendered prose
    starts at the first heading; a body without front-matter is returned unchanged."""
    fm = "---\nstatus: accepted\ndate: 2024-01-01\n---\n\n# Title\n\nBody text.\n"
    assert adr._adr_prose(fm) == "# Title\n\nBody text.\n"
    plain = "# Title\n\nNo front-matter here.\n"
    assert adr._adr_prose(plain) == plain
    assert adr._adr_prose(None) is None
    assert adr._adr_prose("") is None


# --------------------------------------------------------------------------- history

@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_first_violating_commit_over_committed_snapshots(tmp_path):
    """§0.6: the walk reads committed extracted-facts.json snapshots via `git show` —
    never re-extracts — and finds the first commit where the edge appears."""
    repo = tmp_path / "repo"
    gen = repo / "architecture" / "generated"
    gen.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                        "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    targets = [{"id": "a", "name": "A", "container_id": "A", "container_name": "A"},
               {"id": "b", "name": "B", "container_id": "B", "container_name": "B"}]
    git("init")
    dump_json({"targets": targets, "relationships": []},
              gen / "extracted-facts.json")
    git("add", "-A")
    git("commit", "-m", "clean")
    dump_json({"targets": targets,
               "relationships": [{"source": "a", "target": "b", "weight": 5,
                                  "evidence": [{"type": "project_ref", "detail": "x"}]}]},
              gen / "extracted-facts.json")
    git("add", "-A")
    git("commit", "-m", "violation appears")
    second = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            check=True, capture_output=True,
                            text=True).stdout.strip()

    ws = resolve_workspace(repo)
    assert adr.first_violating_commit(ws, "A", "B") == second
    # --since bounds the walk: starting after the first commit still finds the second
    first = subprocess.run(["git", "-C", str(repo), "rev-list", "--max-parents=0",
                            "HEAD"], check=True, capture_output=True,
                           text=True).stdout.strip()
    assert adr.first_violating_commit(ws, "A", "B", since=first) == second


def test_first_violating_commit_unavailable_out_of_tree(checked):
    ws, _report = checked
    assert adr.first_violating_commit(ws, "Web Frontend", "Messaging") is None


def test_baseline_snapshot_checks_that_model_not_current(checked, tmp_path):
    """§2a.4 --baseline: conformance over a given facts snapshot. A baseline without
    the offending edge turns the VIOLATED ADR into HELD."""
    ws, _report = checked
    clean = load_json(ws.curated_facts)
    clean["relationships"] = [
        r for r in clean["relationships"]
        if not (r["source"].endswith("Toy.Web.csproj")
                and r["target"].endswith("Toy.Messaging.csproj"))]
    baseline = tmp_path / "baseline-facts.json"
    dump_json(clean, baseline)
    report = adr.check_conformance(ws, baseline_path=str(baseline))
    a = next(x for x in report["adrs"] if x["id"] == "0001")
    assert a["verdict"] == "HELD", "the snapshot without the edge holds the decision"
