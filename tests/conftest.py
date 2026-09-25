"""Shared pytest fixtures for the harness/golden/e2e verticals (Agent D).

The load-bearing fixture is :func:`toy_run`: it runs the **whole** toy pipeline
(``arch run --no-llm``) into the test's ``tmp_path`` and returns the resolved
:class:`Workspace`, the generator result, and the parsed ``run-report.json``. Every
test that needs a real end-to-end run shares this one, so the pipeline is invoked once
per test that asks for it and the assertions stay focused.

Determinism / isolation rules (per Agent D brief):
  - Outputs are written ONLY under ``tmp_path`` (``arch_dir`` is redirected out of tree).
  - The toy repo (``tests/fixtures/toy-repo``) and its committed rules are read-only —
    nothing is ever written back into the fixture, and ``out/`` is never touched.
  - The committed goldens under ``tests/golden/`` are read-only here (the golden harness
    rewrites them only when ``UPDATE_GOLDENS=1``, see test_golden.py).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from anon.jsonio import load_json
from anon.paths import Workspace, resolve_workspace

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY_REPO = REPO_ROOT / "tests" / "fixtures" / "toy-repo"
TOY_RULES = TOY_REPO / "architecture" / "rules"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "toy"

# Build artifacts that a local `dotnet restore`/build may leave in the fixture. They are
# gitignored (never committed), but their *presence on a dev box* would change extraction
# (e.g. L1 transitive NuGet reads obj/project.assets.json, §4.2). Golden/determinism tests
# must reflect the committed PRISTINE source, so we extract from a clean copy that excludes
# these — making the goldens reproducible regardless of local build state (plan §19.4).
_BUILD_ARTIFACTS = shutil.ignore_patterns("bin", "obj", ".vs", ".cache", ".structurizr", "out")

# The committed example snapshots (`examples/<id>/current/`, refreshed only by the deliberate
# scripts/snapshot-example.ps1) are read-only to the test suite — the same isolation rule as the
# toy fixture: every test redirects output to ``tmp_path``. This tree is where a stray write would
# be most damaging (it produces a spurious, easily-committed git diff), so we guard it explicitly.
EXAMPLES_DIR = REPO_ROOT / "examples"


def _snapshot_mtimes(root: Path) -> dict[str, int]:
    """{file path -> mtime_ns} for every file under *root* (empty if absent). Reads never change
    mtime, so an unchanged snapshot across the session proves nothing wrote into the tree."""
    if not root.exists():
        return {}
    out: dict[str, int] = {}
    for p in root.rglob("*"):
        if p.is_file():
            try:
                out[str(p)] = p.stat().st_mtime_ns
            except OSError:
                pass
    return out


@pytest.fixture(scope="session", autouse=True)
def _committed_examples_are_read_only():
    """Tripwire: fail the session if any test writes into the committed ``examples/`` snapshot.

    The suite's isolation contract is that outputs go ONLY to ``tmp_path`` — nothing is written
    back into a committed fixture/example tree. A test that resolves a Workspace at ``examples/…``
    (or an ``arch serve``/advisory path pointed there) would silently mutate a tracked snapshot and
    surface only as a confusing git diff. This guard makes that regression fail loudly at session
    teardown, naming the offending files, instead of leaking into someone's working tree."""
    before = _snapshot_mtimes(EXAMPLES_DIR)
    yield
    after = _snapshot_mtimes(EXAMPLES_DIR)
    touched = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    if touched:
        rel = [str(Path(p).relative_to(REPO_ROOT)) for p in touched]
        shown = ", ".join(rel[:15]) + (f" (+{len(rel) - 15} more)" if len(rel) > 15 else "")
        raise AssertionError(
            "the test suite wrote into the committed examples/ tree (isolation violation — "
            f"redirect output to tmp_path): {shown}")


def pristine_toy(dst: Path) -> Path:
    """Copy the toy fixture *source* to *dst* (excluding build artifacts); return the copy."""
    shutil.copytree(TOY_REPO, dst, ignore=_BUILD_ARTIFACTS)
    return dst


@dataclass
class ToyRun:
    """Result of one toy ``arch run`` into ``tmp_path``."""

    ws: Workspace
    gen: dict          # generate_structurizr.run() return: containers/edges/over_budget_views
    report: dict       # parsed run-report.json (stages + trust dashboard)


def run_toy_pipeline(arch_dir: Path, *, rules_dir: Path | None = None,
                     with_roslyn: bool = False) -> ToyRun:
    """Run ``arch run --no-llm`` on the toy repo, writing all artifacts under *arch_dir*.

    Imported lazily so a stage-module import error surfaces inside the test, not at
    collection time. Mirrors ``cli.cmd_run`` but lets us capture the generator result and
    keep ``arch_dir`` out of the pristine fixture tree.

    ``with_roslyn`` defaults to **False**: the committed golden is the deterministic,
    SDK-independent **L0** core (build graph + curation + generation) that reproduces on
    any machine — no ``dotnet``/restore needed. The additive L2 Roslyn path (namespaces,
    symbol_use, component tier) is SDK+restore-dependent and is covered by the *gated*
    integration test in ``test_extract.py`` instead (plan §18.4/§19.5).
    """
    from datetime import datetime, timezone

    from anon.runreport import RunReport
    from anon.stages import (apply_mapping_rules, drift_report, enrich_llm,
                                  extract_build_graph, extract_csharp_facts,
                                  generate_structurizr, normalize_facts, validate_facts)

    # Extract from a pristine source copy so a local restore/build in the fixture can't
    # perturb the deterministic goldens (plan §19.4 — fixtures stay pristine).
    src = arch_dir.parent / "toy-src"
    if not src.exists():
        pristine_toy(src)
    ws = resolve_workspace(src, arch_dir, rules_dir or (src / "architecture" / "rules"))
    report = RunReport()
    commit = "test-commit"  # fixed so run-report stages are the only volatile bits

    with report.stage("extract"):
        extract_build_graph.run(ws)
        if with_roslyn:
            extract_csharp_facts.run(ws)  # additive L2; SDK+restore dependent (§19.5)
    with report.stage("normalize"):
        facts = normalize_facts.run(ws, repo=ws.repo.name, commit=commit,
                                    generated_at=datetime.now(timezone.utc).isoformat())
    with report.stage("validate-extracted"):
        validate_facts.validate(facts)
    with report.stage("curate"):
        curated = apply_mapping_rules.run(ws)
    with report.stage("validate-curated"):
        validate_facts.validate(curated)
    with report.stage("enrich"):
        enrich_llm.run(ws, "no-llm")
    with report.stage("generate"):
        gen = generate_structurizr.run(ws)
    with report.stage("drift"):
        drift_report.run(ws, commit=commit)

    cov = drift_report._coverage_l0(curated)
    needs_curation = sum(1 for t in curated.get("targets", [])
                         if "needs-curation" in t.get("tags", []))
    report.set_trust(
        coverage_pct={"L0": round(cov, 2)},
        unmapped_targets=needs_curation,
        over_budget_views=gen["over_budget_views"],
        edges_dropped=curated.get("provenance", {}).get("edges_dropped", 0),
    )
    report.write(ws)

    return ToyRun(ws=ws, gen=gen, report=load_json(ws.run_report))


@pytest.fixture
def toy_run(tmp_path: Path) -> ToyRun:
    """Run the toy pipeline into ``tmp_path`` and return the Workspace + results."""
    return run_toy_pipeline(tmp_path / "architecture")


@pytest.fixture
def no_dotnet_extract(monkeypatch):
    """Force the optional C# Roslyn (dotnet) extractor to no-op for this test.

    A full ``arch run`` / ``arch extract`` fires every applicable extractor. On a dev box
    with the .NET SDK installed, that includes the Roslyn helper — an MSBuild workspace
    load (plus a one-time helper build) costing tens of seconds *per run*. Orchestrator-
    level tests that only assert on the deterministic **L0** core (build graph + curation +
    generation + gates) don't need it, so we short-circuit the extractor to its graceful-
    degradation path — exactly what happens on CI, where no SDK is present (§18.4/§19.5).
    The Roslyn path itself is covered by the gated integration tests in ``test_extract.py``
    (run them with ``-m integration``). This is why ``run_toy_pipeline`` above is L0-only.
    """
    from anon.stages import extract_csharp_facts
    monkeypatch.setattr(extract_csharp_facts, "dotnet_available", lambda: False)
    monkeypatch.setattr(extract_csharp_facts, "helper_available", lambda: False)
