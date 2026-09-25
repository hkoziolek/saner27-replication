"""Deployment-bearing golden harness — the entry surface the TOY golden cannot catch.

This pins **two** surfaces on a model that genuinely carries runtime (`calls`) edges, the
gap left open by the build-only toy fixture (decision #6):

  - ``detect-entries.json`` — ``salience.detect_entries(facts)``. ``generate_docs.build_tour``
    DELEGATES its entry selection to ``detect_entries`` (it only re-orders by external fan-in
    afterwards), so goldening ``detect_entries`` on a deployment model pins the exact entry
    surface the guided tour starts from. The detector ranks over the **combined build+runtime**
    container in-degree plus the high-confidence roots (``sdk-role:service-web``/``worker``,
    controllers, executables) — none of which the toy fixture exercises because it has no
    runtime edges, leaving the unified entry-detector change golden-INVISIBLE there.
  - ``scenario-candidates-index.json`` + ``runtime-webapp.json`` — Source B runtime candidates
    (``scenario_candidates.run(ws, facts, sources=["runtime"])``), the deterministic candidate
    index + body derived from the Aspire ``.WithReference`` wiring.

The fixture (``tests/fixtures/aspire-apphost/``) is parsed by ``extract_runtime.run`` into
REAL runtime ``calls`` edges (WebApp -> Basket.API, WebApp -> Catalog.API, plus a
workload->infra hop). On top of those runtime edges we add ONE **build** relationship
(``Basket.API -> Catalog.API``) so the build in-degree and the runtime in-degree genuinely
DIFFER: ``Basket.API`` has zero build in-degree (so the OLD build-only entry rule would have
called it an entry) but non-zero RUNTIME in-degree (so the NEW combined rule correctly does
NOT). This golden therefore DISCRIMINATES the old build-only entry rule from the new combined
build+runtime rule — an entry-detection change on deployment-bearing repos now FAILS this
golden (regenerate with ``UPDATE_GOLDENS=1`` and commit the diff for review, §6.5b).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from anon.jsonio import dumps_json, load_json
from anon.paths import resolve_workspace
from anon.stages import extract_runtime, scenario_candidates
from anon import salience

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "aspire-apphost"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "aspire"

UPDATE = os.environ.get("UPDATE_GOLDENS") == "1"

# The three workload containers (each csproj is its own container in this minimal fixture),
# mirroring tests/test_scenario_candidates.py.
_WEBAPP = "csharp:csproj:WebApp/WebApp.csproj"
_BASKET = "csharp:csproj:Basket.API/Basket.API.csproj"
_CATALOG = "csharp:csproj:Catalog.API/Catalog.API.csproj"


def _check_or_update(golden_path: Path, actual_bytes: bytes, label: str) -> None:
    """Assert *actual_bytes* match the golden, or rewrite it under UPDATE_GOLDENS=1.

    Same byte-level (CRLF/encoding-catching) contract as ``test_golden._check_or_update``."""
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


def _deployment_facts(repo: Path):
    """Build a DETERMINISTIC deployment-bearing fact model from the Aspire fixture.

    - REAL runtime ``calls`` edges come from ``extract_runtime.run`` (WebApp -> Basket.API /
      Catalog.API + the Basket.API -> redis infra hop).
    - A fixed ``targets[]`` (each csproj its own container; WebApp tagged
      ``sdk-role:service-web`` so it ranks as the high-confidence entry root).
    - ONE **build** ``relationships[]`` edge (Basket.API -> Catalog.API) so build in-degree and
      runtime in-degree DIFFER — this is what makes the golden discriminate the old build-only
      entry rule from the new combined build+runtime rule.

    Returns ``(ws, facts)``.
    """
    ws = resolve_workspace(repo, arch_dir=repo / "_arch")
    out = extract_runtime.run(ws)
    assert out is not None, "the Aspire AppHost fixture must yield a runtime fragment"
    deployment = load_json(out)["deployment"]

    targets = []
    for cid, name, tag in (
        (_WEBAPP, "WebApp", "sdk-role:service-web"),
        (_BASKET, "Basket.API", None),
        (_CATALOG, "Catalog.API", None),
    ):
        targets.append({
            "id": cid, "container_id": cid, "container_name": name,
            "tags": [tag] if tag else [],
        })
    # One BUILD edge: Basket.API -> Catalog.API. Catalog.API thus has build in-degree, but
    # Basket.API has only RUNTIME in-degree (from WebApp) — the two in-degrees differ.
    relationships = [{
        "source": _BASKET, "target": _CATALOG, "weight": 5,
        "evidence": [{"type": "project_ref", "detail": _BASKET}],
    }]
    facts = {"targets": targets, "relationships": relationships, "deployment": deployment}
    return ws, facts


def _build_goldens(repo: Path) -> dict[str, bytes]:
    """Run the deployment-bearing pipeline once and return the three golden byte payloads."""
    shutil.copytree(FIXTURE, repo)
    ws, facts = _deployment_facts(repo)

    entries, reasons = salience.detect_entries(facts)
    detect_bytes = dumps_json({"entries": entries, "reasons": reasons}).encode("utf-8")

    scenario_candidates.run(ws, facts, sources=["runtime"])
    index_bytes = (ws.scenario_candidates_dir / "index.json").read_bytes()
    cand_bytes = (ws.scenario_candidates_dir / "runtime" / "runtime-webapp.json").read_bytes()
    return {
        "detect-entries.json": detect_bytes,
        "scenario-candidates-index.json": index_bytes,
        "runtime-webapp.json": cand_bytes,
    }


def test_deployment_goldens_byte_identical(tmp_path: Path) -> None:
    """detect_entries (build_tour's entry surface) + Source-B runtime candidates on a
    DEPLOYMENT model match the committed goldens byte-for-byte.

    This pins ``build_tour``'s entry surface (it delegates to ``detect_entries``) plus the
    Source-B runtime candidates on a model WITH runtime edges — the surface the toy golden
    cannot catch (decision #6). An entry-detection change on deployment-bearing repos now
    FAILS this golden; if intentional, regenerate with UPDATE_GOLDENS=1 and commit the diff."""
    goldens = _build_goldens(tmp_path / "repo")
    for name, payload in goldens.items():
        _check_or_update(GOLDEN_DIR / name, payload, name)


def test_deployment_goldens_are_lf_only() -> None:
    """The committed deployment goldens must be LF-only (cross-OS byte stability, §22.3)."""
    if UPDATE:
        return
    for name in ("detect-entries.json", "scenario-candidates-index.json", "runtime-webapp.json"):
        data = (GOLDEN_DIR / name).read_bytes()
        assert b"\r\n" not in data, f"{name} contains CRLF — goldens must be LF-only"


def test_two_independent_deployment_runs_are_byte_identical(tmp_path: Path) -> None:
    """§18.4 idempotency: two fresh deployment-bearing builds yield identical bytes for all
    three goldens (the re-run half of the stability gate, independent of the committed bytes)."""
    a = _build_goldens(tmp_path / "a")
    b = _build_goldens(tmp_path / "b")
    assert a.keys() == b.keys()
    for name in a:
        assert a[name] == b[name], f"two independent runs produced different bytes for {name}"
