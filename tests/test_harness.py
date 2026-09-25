"""Harness tests (plan §19.4/§19.6): manifest validity, fetch placeholder-skip, export degrade.

No network is ever touched: the fetch-logic test uses a fake manifest in ``tmp_path`` whose
commits are placeholders, so every entry hits the skip path with zero clones (per Agent D
brief — ``arch fetch`` must not attempt network clones in tests).
"""
from __future__ import annotations

from pathlib import Path

from anon import export_views, fetch
from anon.paths import resolve_workspace

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_MANIFEST = REPO_ROOT / "manifest.yaml"


# --- the committed repo-root manifest --------------------------------------------

def test_real_manifest_is_structurally_valid() -> None:
    """The committed manifest.yaml has all §19.4 required fields and no duplicate ids."""
    report = fetch.validate_manifest(REAL_MANIFEST)
    assert report["ok"], f"manifest has errors: {report['errors']}"
    assert report["count"] >= 1


def test_real_manifest_placeholders_are_warned() -> None:
    """Every TODO-pin-sha entry surfaces as a warning (so fetch will skip it, §19.4)."""
    fixtures = fetch.load_manifest(REAL_MANIFEST)
    placeholders = [fx.id for fx in fixtures if not fx.is_pinned]
    report = fetch.validate_manifest(REAL_MANIFEST)
    for fid in placeholders:
        assert any(fid in w and "placeholder" in w for w in report["warnings"]), \
            f"expected a placeholder warning for {fid}"


def test_real_manifest_licenses_recognized() -> None:
    """Committed fixtures carry a RECOGNIZED license, and any restrictive one is flagged for
    confinement — never silently trusted (§19.6).

    §19.6 *permits* a restrictive outlier (e.g. nopCommerce) for non-redistributed local runs;
    the policy is that it must be classified and surfaced with a confinement warning, not that
    it is absent. So this asserts the flagging, not the banning."""
    fixtures = fetch.load_manifest(REAL_MANIFEST)
    report = fetch.validate_manifest(REAL_MANIFEST)
    for fx in fixtures:
        assert fx.license, f"{fx.id} has no license field"
        # Recognized = clearly permissive OR a KNOWN restrictive license — never an
        # unrecognized one that would slip past §19.6 review of committed derived goldens.
        assert (fx.license in fetch.PERMISSIVE_LICENSES
                or fx.license in fetch.RESTRICTIVE_LICENSES), \
            f"{fx.id} has an unrecognized license {fx.license!r} — classify it (§19.6)"
        # A restrictive fixture is allowed but MUST surface a confinement warning.
        if fx.license in fetch.RESTRICTIVE_LICENSES:
            assert any(fx.id in w and "confine" in w for w in report["warnings"]), \
                f"{fx.id} is restrictive but not flagged for confinement (§19.6)"


# --- manifest validation edge cases ---------------------------------------------

def _write_manifest(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8", newline="")
    return path


def test_validate_manifest_flags_missing_fields(tmp_path: Path) -> None:
    m = _write_manifest(tmp_path / "m.yaml", """
version: 1
repos:
  - id: broken
    url: https://example.com/x
    # missing lang, build, license
    commit: deadbeef
""")
    report = fetch.validate_manifest(m)
    assert not report["ok"]
    assert any("missing required field" in e for e in report["errors"])


def test_validate_manifest_flags_restrictive_license(tmp_path: Path) -> None:
    m = _write_manifest(tmp_path / "m.yaml", """
version: 1
repos:
  - id: mysql
    url: https://github.com/mysql/mysql-server
    lang: cpp
    build: cmake
    license: GPL-2.0
    commit: deadbeefcafe
""")
    report = fetch.validate_manifest(m)
    assert report["ok"]  # license is a WARNING, not an error (§19.6 allows local-only use)
    assert any("restrictive" in w and "mysql" in w for w in report["warnings"])


def test_validate_manifest_flags_duplicate_ids(tmp_path: Path) -> None:
    m = _write_manifest(tmp_path / "m.yaml", """
version: 1
repos:
  - id: dup
    url: https://example.com/a
    lang: csharp
    build: msbuild
    license: MIT
    commit: aaaa
  - id: dup
    url: https://example.com/b
    lang: csharp
    build: msbuild
    license: MIT
    commit: bbbb
""")
    report = fetch.validate_manifest(m)
    assert not report["ok"]
    assert any("duplicate repo id" in e for e in report["errors"])


# --- fetch placeholder-skip behavior (NO NETWORK) --------------------------------

def test_fetch_skips_all_placeholders_without_cloning(tmp_path: Path) -> None:
    """A manifest of only-placeholder commits skips every entry — zero clones, no network."""
    m = _write_manifest(tmp_path / "manifest.yaml", """
version: 1
repos:
  - id: foo
    url: https://example.invalid/foo
    lang: csharp
    build: msbuild
    license: MIT
    commit: TODO-pin-sha
  - id: bar
    url: https://example.invalid/bar
    lang: cpp
    build: cmake
    license: Apache-2.0
    commit: TODO-pin-sha
""")
    logs: list[str] = []
    report = fetch.run(m, harness_root=tmp_path, log=logs.append)

    assert report["fetched"] == 0
    assert report["failed"] == 0
    assert report["status"] == {"foo": "skipped-placeholder", "bar": "skipped-placeholder"}
    # nothing was cloned -> no fixtures/ tree created
    assert not (tmp_path / "fixtures").exists() or not any((tmp_path / "fixtures").iterdir())
    assert any("placeholder" in line for line in logs)


def test_fetch_id_filter_selects_subset(tmp_path: Path) -> None:
    """``ids=`` limits the fetch to the named repos (still all placeholders -> skipped)."""
    m = _write_manifest(tmp_path / "manifest.yaml", """
version: 1
repos:
  - id: foo
    url: https://example.invalid/foo
    lang: csharp
    build: msbuild
    license: MIT
    commit: TODO-pin-sha
  - id: bar
    url: https://example.invalid/bar
    lang: cpp
    build: cmake
    license: MIT
    commit: TODO-pin-sha
""")
    report = fetch.run(m, ids=["bar"], harness_root=tmp_path, log=lambda _m: None)
    assert set(report["status"]) == {"bar"}
    assert report["status"]["bar"] == "skipped-placeholder"


def test_fixture_workspace_layout(tmp_path: Path) -> None:
    """§19.4 out-of-tree layout: clone in fixtures/, outputs in out/, rules in overlays/."""
    fx = fetch.Fixture(id="eshop", url="u", lang="csharp", build="msbuild",
                       license="MIT", commit="cafe")
    ws = fetch.fixture_workspace(tmp_path, fx)
    assert ws.repo == (tmp_path / "fixtures" / "eshop").resolve()
    assert ws.arch_dir == (tmp_path / "out" / "eshop" / "architecture").resolve()
    assert ws.rules_dir == (tmp_path / "overlays" / "eshop" / "architecture" / "rules").resolve()


# --- export graceful degradation -------------------------------------------------

def test_export_skips_when_no_workspace_dsl(tmp_path: Path) -> None:
    """No workspace.dsl -> export logs + skips, never raises (§18.4)."""
    ws = resolve_workspace(tmp_path / "repo", tmp_path / "arch")
    report = export_views.run(ws, "plantuml", log=lambda _m: None)
    assert report["exported"] is False
    assert "workspace.dsl" in report["reason"]


def test_export_degrades_when_cli_absent(tmp_path: Path, monkeypatch) -> None:
    """With a workspace.dsl present but no Structurizr CLI on PATH, export skips gracefully."""
    ws = resolve_workspace(tmp_path / "repo", tmp_path / "arch")
    ws.workspace_dsl.parent.mkdir(parents=True, exist_ok=True)
    ws.workspace_dsl.write_text('workspace "x" "y" {}\n', encoding="utf-8", newline="")

    monkeypatch.setattr(export_views, "_find_launcher", lambda: None)
    report = export_views.run(ws, "plantuml", log=lambda _m: None)
    assert report["exported"] is False
    assert "Structurizr CLI" in report["reason"]


def test_export_degrades_when_java_absent(tmp_path: Path, monkeypatch) -> None:
    """CLI present but Java missing -> skip with a clear reason (§22.3 needs Java 17+)."""
    ws = resolve_workspace(tmp_path / "repo", tmp_path / "arch")
    ws.workspace_dsl.parent.mkdir(parents=True, exist_ok=True)
    ws.workspace_dsl.write_text('workspace "x" "y" {}\n', encoding="utf-8", newline="")

    monkeypatch.setattr(export_views, "_find_launcher", lambda: "/usr/bin/structurizr.sh")
    monkeypatch.setattr(export_views, "_java_present", lambda: False)
    report = export_views.run(ws, "plantuml", log=lambda _m: None)
    assert report["exported"] is False
    assert "Java" in report["reason"]


def test_export_rejects_unknown_format(tmp_path: Path) -> None:
    ws = resolve_workspace(tmp_path / "repo", tmp_path / "arch")
    import pytest
    with pytest.raises(ValueError):
        export_views.run(ws, "svg", log=lambda _m: None)
