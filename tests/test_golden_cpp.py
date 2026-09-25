"""Golden + determinism harness for the C++ (CMake) pilots — the L0 target-graph core.

The C++ analogue of ``test_golden.py`` (the C# toy harness), closing the §14 P7 / §19.1
"scale gap": fmt (smoke) and abseil (target-graph + curation stress) get byte-identical
regression coverage of the **deterministic, machine-portable L0 core** — the CMake File API
target graph + default curation + DSL generation.

WHY L0-ONLY (mirrors the toy golden's no-Roslyn choice, conftest.run_toy_pipeline):
the L0 target graph is reproducible on any box, while the Doxygen **L2/L3** tier
(namespaces/classes/include edges) is *Doxygen-version-sensitive* across machines, so it is
deliberately NOT committed as a golden — it is exercised by the gated tests in
``test_extract_cpp.py`` instead (plan §18.4/§19.5).

GATED (plan §19.5): the whole module skips when the CMake/Ninja/compiler toolchain or the
pinned fixture clone (``fixtures/<id>``, fetched via ``arch fetch``) is absent — so a CI box
without C++ tooling stays green, while a capable box gets the byte-identical gate.

fmt + abseil run whenever the toolchain is present; opencv (~1M LOC) is opt-in (it adds
minutes per CMake configure) — set ``ANON_SCALE_GOLDENS=1`` to include it.

Regenerate after an intentional extractor/generator change (needs the toolchain + clones)::

    $env:CC='gcc'; $env:CXX='g++'          # only if MinGW gcc/g++ is present (no MSVC `cl`)
    # fmt + abseil:
    $env:UPDATE_GOLDENS=1; .venv\\Scripts\\python -m pytest tests/test_golden_cpp.py
    # include the opencv scale golden too (slow):
    $env:ANON_SCALE_GOLDENS=1; .venv\\Scripts\\python -m pytest tests/test_golden_cpp.py -k opencv
    Remove-Item Env:\\UPDATE_GOLDENS, Env:\\ANON_SCALE_GOLDENS
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from anon import cmake_api
from anon.jsonio import dumps_json
from anon.model import content_hash, provenance_stripped
from anon.paths import resolve_workspace
from anon.stages import (apply_mapping_rules, extract_cmake, generate_structurizr,
                              normalize_facts)

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_BASE = REPO_ROOT / "tests" / "golden"
UPDATE = os.environ.get("UPDATE_GOLDENS") == "1"

# fmt = smoke (§19.1); abseil = fine-grained target graph + grouping stress (§19.1/§19.3).
# These are cheap enough to run whenever the toolchain is present.
CPP_FIXTURES = ["fmt", "abseil"]
# opencv = the ~1M-LOC scale case (§19.1): clean modular target graph, but a single CMake
# configure takes minutes, so running it 1-3x would dominate the suite. It is therefore an
# OPT-IN scale fixture — its golden is committed and verifiable, but the test only runs when
# ANON_SCALE_GOLDENS=1 (or when regenerating with UPDATE_GOLDENS=1).
SCALE_FIXTURES = ["opencv"]
ALL_FIXTURES = CPP_FIXTURES + SCALE_FIXTURES
_RUN_SCALE = os.environ.get("ANON_SCALE_GOLDENS") == "1"

_GOLDEN_FILES = ("generated-components.dsl", "generated-relationships.dsl",
                 "extracted-facts.canonical.json")


def _cpp_env() -> dict | None:
    """The compiler env CMake needs, or None if no usable C++ compiler is present."""
    if shutil.which("cl"):
        return {}
    if shutil.which("g++") and shutil.which("gcc"):
        return {"CXX": "g++", "CC": "gcc"}
    return None


_TOOLCHAIN_OK = (cmake_api.cmake_available() and shutil.which("ninja") is not None
                 and _cpp_env() is not None)

pytestmark = [
    # A real cmake/ninja/compiler run per fixture — the `integration` tier (slow,
    # deselected by default; run with `-m ''` or `-m integration`).
    pytest.mark.integration,
    pytest.mark.skipif(not _TOOLCHAIN_OK, reason="cmake/ninja/compiler unavailable (§19.5)"),
]


@pytest.fixture(autouse=True)
def _cpp_compiler_env(monkeypatch):
    """Point CMake at the MinGW compiler when MSVC `cl` is absent (matches §19.5 setup)."""
    for k, v in (_cpp_env() or {}).items():
        monkeypatch.setenv(k, v)


def _fixture_present(fixture_id: str) -> bool:
    return (REPO_ROOT / "fixtures" / fixture_id / "CMakeLists.txt").exists()


def _maybe_skip(fixture_id: str) -> None:
    """Skip a fixture when it is opt-in-only or its clone is absent (§19.4/§19.5)."""
    if fixture_id in SCALE_FIXTURES and not (_RUN_SCALE or UPDATE):
        pytest.skip(f"{fixture_id}: scale fixture (~1M LOC) — set ANON_SCALE_GOLDENS=1 "
                    "to run, or UPDATE_GOLDENS=1 to regenerate")
    if not _fixture_present(fixture_id):
        pytest.skip(f"fixtures/{fixture_id} not cloned — run `arch fetch {fixture_id}` (§19.4)")


def _run_l0(fixture_id: str, arch_dir: Path):
    """Run the deterministic C++ L0 pipeline: extract_cmake -> normalize -> curate -> generate.

    No Doxygen (extract_cpp_facts) — L0 only. extract_cmake self-configures via the CMake
    File API, so no pre-`configure` step is needed; defaults-only curation (no overlay)."""
    repo = REPO_ROOT / "fixtures" / fixture_id
    ws = resolve_workspace(repo, arch_dir, arch_dir / "rules")
    extract_cmake.run(ws)
    facts = normalize_facts.run(ws, repo=fixture_id, commit="test-commit", generated_at="t")
    apply_mapping_rules.run(ws)
    generate_structurizr.run(ws)
    return ws, facts


# Per-worker memo of ONE canonical L0 run per fixture. The byte-identical golden test and
# the two-runs determinism test's *first* run are the same computation, so we share a single
# CMake configure between them instead of each doing its own (#4: abseil's configure is the
# suite's long pole — this drops it from 3 configures/fixture to 2). Only immutable result
# bytes are cached; the determinism test still does a second, fully independent run below —
# that is the property it exists to prove.
_CANONICAL: dict[str, dict] = {}


def _canonical_l0(fixture_id: str, tmp_path_factory) -> dict:
    """One memoized L0 run for *fixture_id*: its DSL/fact bytes + content hash.

    The CMake configure (the expensive part) happens on the first call within this worker,
    while the autouse ``_cpp_compiler_env`` fixture is active; only immutable bytes are
    cached, so later hits need neither the compiler env nor a re-configure.
    """
    cached = _CANONICAL.get(fixture_id)
    if cached is None:
        arch = tmp_path_factory.mktemp(f"l0-{fixture_id}") / "architecture"
        ws, facts = _run_l0(fixture_id, arch)
        cached = {
            "components": ws.components_dsl.read_bytes(),
            "relationships": ws.relationships_dsl.read_bytes(),
            "canonical": dumps_json(provenance_stripped(facts)).encode("utf-8"),
            "hash": content_hash(facts),
        }
        _CANONICAL[fixture_id] = cached
    return cached


def _check_or_update(golden_path: Path, actual_bytes: bytes, label: str) -> None:
    if UPDATE:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_bytes(actual_bytes)
        return
    assert golden_path.exists(), (
        f"missing golden {golden_path} for {label}; regenerate with UPDATE_GOLDENS=1 (§6.5b)")
    assert actual_bytes == golden_path.read_bytes(), (
        f"{label} differs from golden {golden_path}.\n"
        "If this change is INTENTIONAL, regenerate goldens with UPDATE_GOLDENS=1 (§6.5b).")


@pytest.mark.parametrize("fid", ALL_FIXTURES)
def test_cpp_l0_golden_byte_identical(fid: str, tmp_path_factory) -> None:
    """L0 facts (provenance-stripped) + DSL fragments match the committed goldens (§18.4)."""
    _maybe_skip(fid)
    run = _canonical_l0(fid, tmp_path_factory)
    gdir = GOLDEN_BASE / fid
    _check_or_update(gdir / "generated-components.dsl", run["components"], f"{fid} components.dsl")
    _check_or_update(gdir / "generated-relationships.dsl", run["relationships"], f"{fid} relationships.dsl")
    _check_or_update(gdir / "extracted-facts.canonical.json", run["canonical"], f"{fid} canonical facts")


@pytest.mark.parametrize("fid", ALL_FIXTURES)
def test_cpp_l0_two_runs_byte_identical(fid: str, tmp_path: Path, tmp_path_factory) -> None:
    """§18.4 idempotency: two independent L0 runs hash identically.

    Independent of the committed goldens — proves the C++ extraction is deterministic in
    isolation (the precondition that makes the golden meaningful). Run "a" is the shared
    canonical run (itself a full independent run); run "b" is a fresh configure here."""
    _maybe_skip(fid)
    first = _canonical_l0(fid, tmp_path_factory)["hash"]
    _, fb = _run_l0(fid, tmp_path / "b" / "architecture")
    assert content_hash(fb) == first, f"{fid}: two independent L0 runs produced different facts"


def test_cpp_goldens_are_lf_only() -> None:
    """Committed C++ goldens must be LF-only (cross-OS byte stability, §22.3 / §1.1 #2)."""
    if UPDATE:
        return
    for fid in ALL_FIXTURES:
        for name in _GOLDEN_FILES:
            p = GOLDEN_BASE / fid / name
            if p.exists():
                assert b"\r\n" not in p.read_bytes(), f"{fid}/{name} contains CRLF — goldens must be LF-only"
