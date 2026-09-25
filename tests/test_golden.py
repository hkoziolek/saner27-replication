"""Golden-output regression harness for the toy (plan §19.4, §18.4, §1.1 metric 2).

Runs the toy pipeline into ``tmp_path`` and asserts the generated artifacts are
**byte-identical** to the committed goldens under ``tests/golden/toy/``:

  - ``generated-components.dsl``     — container/component DSL fragment (§9)
  - ``generated-relationships.dsl``  — container-edge DSL fragment (§9)
  - ``extracted-facts.canonical.json`` — provenance-stripped, canonicalized facts
    (§18.4: ``provenance.generated_at`` / ``commit`` are excluded from the comparison;
    everything else must match to the byte).

This is the **§18.4 determinism + §1.1 metric 2 (Stability) gate** wired as a CI
regression: same pinned inputs -> byte-identical fragments/facts, caught on every run.

**Regenerating goldens (§6.5b — regenerate, don't hand-patch).** After an *intended*
change to the generator/extractor, refresh the goldens in one reviewed commit by running
the suite with the ``UPDATE_GOLDENS=1`` env var set::

    UPDATE_GOLDENS=1 .venv/Scripts/python.exe -m pytest tests/test_golden.py

When that flag is set, the tests rewrite the golden bytes (LF-only) and pass, so the diff
lands in the goldens for review rather than being hand-edited.
"""
from __future__ import annotations

import os
from pathlib import Path

from anon.jsonio import dumps_json
from anon.model import content_hash, provenance_stripped

from conftest import GOLDEN_DIR, run_toy_pipeline

UPDATE = os.environ.get("UPDATE_GOLDENS") == "1"


def _check_or_update(golden_path: Path, actual_bytes: bytes, label: str) -> None:
    """Assert *actual_bytes* match the golden, or rewrite the golden under UPDATE_GOLDENS=1.

    Comparison is on raw bytes so a CRLF/encoding regression is caught (the determinism
    guarantee is byte-level + LF, §22.3 / §1.1 metric 2)."""
    if UPDATE:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_bytes(actual_bytes)
        return
    assert golden_path.exists(), (
        f"missing golden {golden_path} for {label}; "
        "regenerate with UPDATE_GOLDENS=1 (§6.5b)")
    expected = golden_path.read_bytes()
    assert actual_bytes == expected, (
        f"{label} differs from golden {golden_path}.\n"
        "If this change is INTENTIONAL, regenerate goldens with "
        "UPDATE_GOLDENS=1 and commit the diff for review (§6.5b).")


def test_golden_dsl_fragments_byte_identical(tmp_path: Path) -> None:
    """Generated DSL fragments match the committed goldens byte-for-byte (§18.4)."""
    tr = run_toy_pipeline(tmp_path / "architecture")
    _check_or_update(GOLDEN_DIR / "generated-components.dsl",
                     tr.ws.components_dsl.read_bytes(), "generated-components.dsl")
    _check_or_update(GOLDEN_DIR / "generated-relationships.dsl",
                     tr.ws.relationships_dsl.read_bytes(), "generated-relationships.dsl")


def test_golden_canonical_facts_byte_identical(tmp_path: Path) -> None:
    """Provenance-stripped canonical facts match the golden (volatile provenance excluded)."""
    from anon.jsonio import load_json

    tr = run_toy_pipeline(tmp_path / "architecture")
    facts = load_json(tr.ws.extracted_facts)
    canonical_bytes = dumps_json(provenance_stripped(facts)).encode("utf-8")
    _check_or_update(GOLDEN_DIR / "extracted-facts.canonical.json",
                     canonical_bytes, "extracted-facts.canonical.json")


def test_golden_facts_content_hash_matches(tmp_path: Path) -> None:
    """The §11.3 content hash of the run's facts equals the golden's (defense in depth)."""
    from anon.jsonio import load_json

    golden_path = GOLDEN_DIR / "extracted-facts.canonical.json"
    if UPDATE:  # golden is rewritten by the test above in this same run
        return
    tr = run_toy_pipeline(tmp_path / "architecture")
    facts = load_json(tr.ws.extracted_facts)
    golden_facts = load_json(golden_path)
    assert content_hash(facts) == content_hash(golden_facts), (
        "facts content hash drifted from the golden — regenerate with UPDATE_GOLDENS=1 (§6.5b)")


def test_goldens_are_lf_only() -> None:
    """Committed goldens must be LF-only (cross-OS byte stability, §22.3 / §1.1 metric 2)."""
    if UPDATE:
        return
    for name in ("generated-components.dsl", "generated-relationships.dsl",
                 "extracted-facts.canonical.json"):
        data = (GOLDEN_DIR / name).read_bytes()
        assert b"\r\n" not in data, f"{name} contains CRLF — goldens must be LF-only"


def test_two_independent_runs_are_byte_identical(tmp_path: Path) -> None:
    """§18.4 idempotency: two fresh runs into separate dirs yield identical fragments/facts.

    This is the *re-run* half of the §1.1 metric 2 stability gate — independent of the
    committed goldens, it proves the pipeline is deterministic in isolation."""
    from anon.jsonio import load_json

    a = run_toy_pipeline(tmp_path / "a" / "architecture")
    b = run_toy_pipeline(tmp_path / "b" / "architecture")

    assert a.ws.components_dsl.read_bytes() == b.ws.components_dsl.read_bytes()
    assert a.ws.relationships_dsl.read_bytes() == b.ws.relationships_dsl.read_bytes()
    fa = load_json(a.ws.extracted_facts)
    fb = load_json(b.ws.extracted_facts)
    assert content_hash(fa) == content_hash(fb), "two independent runs produced different facts"


def test_scenario_candidates_are_not_part_of_the_core_golden_contract(tmp_path: Path) -> None:
    """Advisory scenario candidates (dynamic-view plan §0.4#4/§11) live under
    ``generated/scenario-candidates/`` and are NEVER part of the determinism-hashed core:
    the curated facts (and their provenance-stripped content_hash) are bit-for-bit
    unaffected by candidate generation, and the sidecar tree is not a committed golden."""
    from anon.jsonio import load_json

    from anon.stages import scenario_candidates

    run = run_toy_pipeline(tmp_path / "architecture")
    # The candidate sidecar tree is advisory machine-output, written under generated/ — not
    # in the committed golden set (which is only the DSL fragments + canonical facts).
    cand_dir = run.ws.scenario_candidates_dir
    assert not (GOLDEN_DIR / "scenario-candidates").exists(), \
        "candidate sidecars must NOT be a committed golden surface (plan §12)"
    if cand_dir.exists():
        # toy has no Aspire AppHost and graph-walk is OFF by default -> an honest EMPTY index.
        index = load_json(cand_dir / "index.json")
        assert index["candidates"] == []

    # The hashed core is untouched: re-running the candidate sweep (even with the opt-in
    # graph-walk source, which DOES produce candidates on toy) leaves extracted-facts /
    # curated-facts and their provenance-stripped content hash bit-for-bit unchanged —
    # candidates never enter normalize_facts (plan §11).
    extracted_before = run.ws.extracted_facts.read_bytes()
    curated_before = run.ws.curated_facts.read_bytes()
    h_before = content_hash(load_json(run.ws.curated_facts))
    scenario_candidates.run_cli(run.ws, sources=["graph-walk"])
    assert run.ws.extracted_facts.read_bytes() == extracted_before
    assert run.ws.curated_facts.read_bytes() == curated_before
    assert content_hash(load_json(run.ws.curated_facts)) == h_before, \
        "candidate generation must not perturb the hashed core (plan §11)"
