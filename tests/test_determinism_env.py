"""Determinism is PATH-independent — the deployment hashed core does not depend on
whether ``helm``/``kustomize``/``kubectl`` happen to be installed (T1.1, plan
``plans/2026-06-05-anon-v2-directions.md``).

The bug this guards against: ``extract_deployment.run`` used to pick between two
*structurally different* deployment-fact sets based purely on whether the render tools
were on PATH — a rendered set (concrete k8s nodes, no ``partial:unrendered`` tag) when
present, vs a static set (chart-named node tagged ``partial:unrendered``) when absent.
That deployment facet flows through ``normalize`` into ``extracted-facts.json`` and IS
hashed, so a tooled dev box and a bare CI box produced different ``content_hash`` on any
charts-bearing repo — silently falsifying the determinism guarantee.

The fix makes the static (unrendered) representation the DEFAULT hashed core; rendering is
strictly opt-in (``resolve=True`` / ``ANON_RESOLVE_DEPLOYMENT``) and therefore off in
CI. These tests pin that contract: with the render tools faked present vs absent, the
default fragment — and the normalized ``content_hash`` — must be byte-identical.

All outputs are written under ``tmp_path``; the fixture is read-only. Follows the
Workspace-construction conventions in ``tests/test_deployment.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from anon import model
from anon.jsonio import load_json
from anon.paths import resolve_workspace
from anon.stages import extract_deployment, normalize_facts

REPO_ROOT = Path(__file__).resolve().parents[1]
CHARTS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "charts-repo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ws(tmp_path: Path, repo: Path, sub: str) -> object:
    base = tmp_path / sub
    return resolve_workspace(repo, arch_dir=base / "arch", rules_dir=base / "rules")


def _fake_render_tools(monkeypatch, *, present: bool) -> None:
    """Force ``_helm_available`` / ``_kustomize_available`` to *present* for the duration
    of the test, so the result cannot depend on the host's real PATH."""
    monkeypatch.setattr(extract_deployment, "_helm_available", lambda: present)
    monkeypatch.setattr(extract_deployment, "_kustomize_available", lambda: present)


# ---------------------------------------------------------------------------
# Test 1 (load-bearing) — the default hashed core is PATH-independent
# ---------------------------------------------------------------------------

class TestDeploymentPathIndependence:
    def test_default_fragment_byte_identical_regardless_of_tools(self, tmp_path, monkeypatch):
        """``run(ws)`` (default, resolve OFF) must produce a byte-identical deployment
        fragment whether or not helm/kustomize are 'installed' — the static path always
        wins, so the hashed core never depends on PATH."""
        # tools "present"
        ws_present = _make_ws(tmp_path, CHARTS_FIXTURE, sub="present")
        _fake_render_tools(monkeypatch, present=True)
        assert extract_deployment.run(ws_present) is not None
        bytes_present = (ws_present.fragments / "deployment.json").read_bytes()

        # tools "absent"
        ws_absent = _make_ws(tmp_path, CHARTS_FIXTURE, sub="absent")
        _fake_render_tools(monkeypatch, present=False)
        assert extract_deployment.run(ws_absent) is not None
        bytes_absent = (ws_absent.fragments / "deployment.json").read_bytes()

        assert bytes_present == bytes_absent, (
            "default deployment fragment must be byte-identical regardless of whether "
            "helm/kustomize are on PATH (rendering is opt-in, T1.1)"
        )

    def test_content_hash_identical_regardless_of_tools(self, tmp_path, monkeypatch):
        """Mirror the real gate: normalize the fragment into a full fact model and assert
        ``model.content_hash`` (the thing CI compares) is identical across tool presence."""

        def normalized_hash(sub: str, *, present: bool) -> str:
            ws = _make_ws(tmp_path, CHARTS_FIXTURE, sub=sub)
            _fake_render_tools(monkeypatch, present=present)
            extract_deployment.run(ws)
            facts = normalize_facts.run(
                ws, repo="charts-repo", commit="fixed-commit",
                generated_at=datetime.now(timezone.utc).isoformat(),
            )
            return model.content_hash(facts)

        hash_present = normalized_hash("hash_present", present=True)
        hash_absent = normalized_hash("hash_absent", present=False)
        assert hash_present == hash_absent, (
            "content_hash must be PATH-independent — same inputs -> same hash on a tooled "
            "dev box and a bare CI box (T1.1)"
        )


# ---------------------------------------------------------------------------
# Test 2 — the static partial:unrendered path is what lands in the hashed core
# ---------------------------------------------------------------------------

class TestStaticCoreIsHashed:
    def test_default_fragment_has_partial_unrendered_tag(self, tmp_path, monkeypatch):
        """Even with the render tools faked PRESENT, the default fragment uses the static
        path, so a ``partial:unrendered`` node tag is present in the hashed core."""
        _fake_render_tools(monkeypatch, present=True)
        ws = _make_ws(tmp_path, CHARTS_FIXTURE, sub="static_core")
        assert extract_deployment.run(ws) is not None
        frag = load_json(ws.fragments / "deployment.json")
        all_tags = {tag for n in frag["deployment"]["nodes"] for tag in n.get("tags", [])}
        assert "partial:unrendered" in all_tags, (
            "the static (unrendered) representation must be the default hashed core"
        )
        # And the opt-in fingerprint key must be ABSENT on the default path (so it cannot
        # leak environment state into the hash).
        assert "deployment_render" not in frag.get("provenance", {})


# ---------------------------------------------------------------------------
# Test 3 — opt-in render degrades gracefully when the tool is absent
# ---------------------------------------------------------------------------

class TestOptInRenderGracefulDegrade:
    def test_resolve_true_with_tools_absent_falls_back_to_static(self, tmp_path, monkeypatch):
        """``resolve=True`` but helm/kustomize not actually installed must NOT raise — it
        falls back to the static path and still emits a valid fragment (graceful, §18.4).
        The opt-in fingerprint records the (absent) toolchain so the choice is attestable."""
        _fake_render_tools(monkeypatch, present=False)
        ws = _make_ws(tmp_path, CHARTS_FIXTURE, sub="resolve_no_tools")
        result = extract_deployment.run(ws, resolve=True)
        assert result is not None
        frag = load_json(ws.fragments / "deployment.json")
        # fingerprint stamped on the opt-in path, honestly reporting tools as absent
        fp = frag.get("provenance", {}).get("deployment_render")
        assert fp == {"helm": False, "kustomize": False}
        # still the static topology
        all_tags = {tag for n in frag["deployment"]["nodes"] for tag in n.get("tags", [])}
        assert "partial:unrendered" in all_tags
