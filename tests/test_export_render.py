"""Tests for the §10.3 step-2 SVG render (`anon.export_views.render`) + `run(render=)`.

These are **hermetic**: no real renderer (PlantUML/Java/Graphviz/mermaid-cli/Kroki) is on a
CI agent, so we exercise the **degradation + dispatch logic** by monkeypatching
``shutil.which`` / the module's ``_find_*`` helpers and ``subprocess.run``. We assert:

* ``render`` SKIPS cleanly (no raise, ``rendered`` False, helpful reason) when no tool exists;
* with a fake ``plantuml`` "present", it builds the expected ``plantuml -tsvg <file>.puml``
  command and reports the resulting ``*.svg`` paths;
* ``run(..., render=False)`` (the default) never attempts a render.

SVG bytes from external renderers are NOT byte-stable, so they are CI artifacts under the
machine-owned ``generated/views/`` area and are never golden-compared (§10.3/§12) — these
tests therefore assert command/dispatch shape, not SVG content.
"""
from __future__ import annotations

from pathlib import Path

from anon import export_views
from anon.paths import resolve_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _views_dir(tmp_path: Path, *, fmt: str = "plantuml") -> Path:
    """A views/ dir seeded with one exported text view for ``fmt``."""
    out = tmp_path / "views"
    out.mkdir(parents=True, exist_ok=True)
    ext = export_views._FORMAT_EXT[fmt]
    (out / f"SystemContext{ext}").write_text("@startuml\n@enduml\n", encoding="utf-8", newline="")
    return out


def _disable_all_renderers(monkeypatch) -> None:
    """Pretend no renderer of any kind is available."""
    monkeypatch.setattr(export_views, "_find_plantuml", lambda: None)
    monkeypatch.setattr(export_views, "_find_mmdc", lambda: None)
    monkeypatch.setattr(export_views, "_find_dot", lambda: None)
    monkeypatch.setattr(export_views, "_kroki_url", lambda: None)


# ---------------------------------------------------------------------------
# render() — graceful degradation (§18.4)
# ---------------------------------------------------------------------------

def test_render_skips_when_no_text_views(tmp_path: Path) -> None:
    """An empty views/ dir -> skip with a 'run the text export first' reason, no raise."""
    out = tmp_path / "views"
    out.mkdir()
    report = export_views.render(out, "plantuml", log=lambda _m: None)
    assert report == {
        "rendered": False, "format": "plantuml", "svgs": [], "reason": report["reason"],
    }
    assert report["rendered"] is False
    assert ".puml" in report["reason"]


def test_render_skips_when_no_renderer_present(tmp_path: Path, monkeypatch) -> None:
    """Text views exist but no renderer/tooling -> log + skip, never raises (§18.4)."""
    out = _views_dir(tmp_path)
    _disable_all_renderers(monkeypatch)

    report = export_views.render(out, "plantuml", log=lambda _m: None)

    assert report["rendered"] is False
    assert report["svgs"] == []
    assert report["reason"] is not None
    assert "no on-prem renderer" in report["reason"]
    assert export_views._KROKI_ENV in report["reason"]


def test_render_rejects_unsupported_format(tmp_path: Path) -> None:
    """An unknown format degrades to a clean skip rather than raising."""
    report = export_views.render(tmp_path, "png", log=lambda _m: None)
    assert report["rendered"] is False
    assert "unsupported format" in report["reason"]


def test_render_never_raises_on_renderer_failure(tmp_path: Path, monkeypatch) -> None:
    """A renderer that exits non-zero / produces no SVG is captured, not propagated."""
    out = _views_dir(tmp_path)
    monkeypatch.setattr(export_views, "_find_plantuml", lambda: ["plantuml"])

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "graphviz not found"

    monkeypatch.setattr(export_views.subprocess, "run", lambda *a, **k: _Fail())

    report = export_views.render(out, "plantuml", log=lambda _m: None)

    assert report["rendered"] is False
    assert report["svgs"] == []
    assert "render" in report["reason"].lower()


# ---------------------------------------------------------------------------
# render() — dispatch: PlantUML
# ---------------------------------------------------------------------------

def test_render_plantuml_builds_expected_command(tmp_path: Path, monkeypatch) -> None:
    """A 'present' plantuml launcher -> `plantuml -tsvg <file>.puml`, reports the svg path."""
    out = _views_dir(tmp_path, fmt="plantuml")
    src = out / "SystemContext.puml"
    monkeypatch.setattr(export_views, "_find_plantuml", lambda: ["plantuml"])

    captured: dict = {}

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        # PlantUML -tsvg writes <stem>.svg alongside the source; simulate that side effect.
        src.with_suffix(".svg").write_bytes(b"<svg/>")

        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Ok()

    monkeypatch.setattr(export_views.subprocess, "run", _fake_run)

    report = export_views.render(out, "plantuml", log=lambda _m: None)

    assert captured["cmd"] == ["plantuml", "-tsvg", str(src)]
    assert report["rendered"] is True
    assert report["svgs"] == [str(src.with_suffix(".svg"))]
    assert report["reason"] is None


def test_render_plantuml_jar_prefix_is_passed_through(tmp_path: Path, monkeypatch) -> None:
    """A `java -jar plantuml.jar` prefix (no `plantuml` launcher) is honored verbatim."""
    out = _views_dir(tmp_path, fmt="plantuml")
    src = out / "SystemContext.puml"
    prefix = ["java", "-jar", "/opt/plantuml.jar"]
    monkeypatch.setattr(export_views, "_find_plantuml", lambda: list(prefix))

    captured: dict = {}

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        src.with_suffix(".svg").write_bytes(b"<svg/>")

        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Ok()

    monkeypatch.setattr(export_views.subprocess, "run", _fake_run)

    report = export_views.render(out, "plantuml", log=lambda _m: None)

    assert captured["cmd"] == [*prefix, "-tsvg", str(src)]
    assert report["rendered"] is True


# ---------------------------------------------------------------------------
# render() — dispatch: Mermaid + dot
# ---------------------------------------------------------------------------

def test_render_mermaid_uses_mmdc_in_out(tmp_path: Path, monkeypatch) -> None:
    """fmt=mermaid -> `mmdc -i <file>.mmd -o <file>.svg`."""
    out = _views_dir(tmp_path, fmt="mermaid")
    src = out / "SystemContext.mmd"
    monkeypatch.setattr(export_views, "_find_mmdc", lambda: "mmdc")

    captured: dict = {}

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        src.with_suffix(".svg").write_bytes(b"<svg/>")

        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Ok()

    monkeypatch.setattr(export_views.subprocess, "run", _fake_run)

    report = export_views.render(out, "mermaid", log=lambda _m: None)

    assert captured["cmd"] == ["mmdc", "-i", str(src), "-o", str(src.with_suffix(".svg"))]
    assert report["rendered"] is True


def test_render_dot_uses_graphviz(tmp_path: Path, monkeypatch) -> None:
    """fmt=dot -> `dot -Tsvg <file>.dot -o <file>.svg`."""
    out = _views_dir(tmp_path, fmt="dot")
    src = out / "SystemContext.dot"
    monkeypatch.setattr(export_views, "_find_dot", lambda: "dot")

    captured: dict = {}

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        src.with_suffix(".svg").write_bytes(b"<svg/>")

        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Ok()

    monkeypatch.setattr(export_views.subprocess, "run", _fake_run)

    report = export_views.render(out, "dot", log=lambda _m: None)

    assert captured["cmd"] == ["dot", "-Tsvg", str(src), "-o", str(src.with_suffix(".svg"))]
    assert report["rendered"] is True


# ---------------------------------------------------------------------------
# render() — Kroki fallback (on-prem only)
# ---------------------------------------------------------------------------

def test_kroki_url_env_is_on_prem_only(monkeypatch) -> None:
    """`_kroki_url` returns None unless the operator explicitly sets the env var."""
    monkeypatch.delenv(export_views._KROKI_ENV, raising=False)
    assert export_views._kroki_url() is None
    monkeypatch.setenv(export_views._KROKI_ENV, "http://kroki.internal:8000/")
    assert export_views._kroki_url() == "http://kroki.internal:8000"


def test_render_kroki_fallback_posts_and_writes_svg(tmp_path: Path, monkeypatch) -> None:
    """No local renderer but ANON_KROKI_URL set -> POST text, write returned SVG."""
    out = _views_dir(tmp_path, fmt="plantuml")
    src = out / "SystemContext.puml"
    # No PlantUML/mmdc/dot, but a self-hosted Kroki is configured.
    monkeypatch.setattr(export_views, "_find_plantuml", lambda: None)
    monkeypatch.setattr(export_views, "_find_mmdc", lambda: None)
    monkeypatch.setattr(export_views, "_find_dot", lambda: None)
    monkeypatch.setattr(export_views, "_kroki_url", lambda: "http://kroki.internal:8000")

    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"<svg>kroki</svg>"

    def _fake_urlopen(req, *a, **k):
        captured["url"] = req.full_url
        captured["data"] = req.data
        return _Resp()

    monkeypatch.setattr(export_views.urllib.request, "urlopen", _fake_urlopen)

    report = export_views.render(out, "plantuml", log=lambda _m: None)

    assert captured["url"] == "http://kroki.internal:8000/plantuml/svg"
    assert b"@startuml" in captured["data"]
    assert report["rendered"] is True
    assert report["svgs"] == [str(src.with_suffix(".svg"))]
    assert src.with_suffix(".svg").read_bytes() == b"<svg>kroki</svg>"


# ---------------------------------------------------------------------------
# run(render=...) integration
# ---------------------------------------------------------------------------

def test_run_render_false_never_renders(tmp_path: Path, monkeypatch) -> None:
    """The default run(render=False) succeeds at text export and never touches the renderer."""
    ws = resolve_workspace(tmp_path / "repo", tmp_path / "arch")
    ws.workspace_dsl.parent.mkdir(parents=True, exist_ok=True)
    ws.workspace_dsl.write_text('workspace "x" "y" {}\n', encoding="utf-8", newline="")

    monkeypatch.setattr(export_views, "_find_launcher", lambda: "structurizr")
    monkeypatch.setattr(export_views, "_java_present", lambda: True)

    def _fake_run(cmd, *a, **k):
        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Ok()

    monkeypatch.setattr(export_views.subprocess, "run", _fake_run)

    # Make any render attempt blow up so we can prove it was never called.
    def _boom(*a, **k):
        raise AssertionError("render() must not run when render=False")

    monkeypatch.setattr(export_views, "_render", _boom)

    report = export_views.run(ws, "plantuml", log=lambda _m: None)

    assert report["exported"] is True
    assert "render" not in report


def test_run_render_true_invokes_render_after_export(tmp_path: Path, monkeypatch) -> None:
    """run(render=True) calls the renderer after a successful text export and embeds its report."""
    ws = resolve_workspace(tmp_path / "repo", tmp_path / "arch")
    ws.workspace_dsl.parent.mkdir(parents=True, exist_ok=True)
    ws.workspace_dsl.write_text('workspace "x" "y" {}\n', encoding="utf-8", newline="")

    monkeypatch.setattr(export_views, "_find_launcher", lambda: "structurizr")
    monkeypatch.setattr(export_views, "_java_present", lambda: True)

    def _fake_run(cmd, *a, **k):
        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Ok()

    monkeypatch.setattr(export_views.subprocess, "run", _fake_run)

    calls: dict = {}

    def _fake_render(out_dir, fmt, *, log=None):
        calls["out_dir"] = out_dir
        calls["fmt"] = fmt
        return {"rendered": True, "format": fmt, "svgs": ["x.svg"], "reason": None}

    monkeypatch.setattr(export_views, "_render", _fake_render)

    report = export_views.run(ws, "plantuml", render=True, log=lambda _m: None)

    assert report["exported"] is True
    assert calls["out_dir"] == ws.views_export
    assert calls["fmt"] == "plantuml"
    assert report["render"] == {
        "rendered": True, "format": "plantuml", "svgs": ["x.svg"], "reason": None,
    }


def test_run_render_true_skipped_export_does_not_render(tmp_path: Path, monkeypatch) -> None:
    """If the text export itself skips (no CLI), render is not attempted even with render=True."""
    ws = resolve_workspace(tmp_path / "repo", tmp_path / "arch")
    ws.workspace_dsl.parent.mkdir(parents=True, exist_ok=True)
    ws.workspace_dsl.write_text('workspace "x" "y" {}\n', encoding="utf-8", newline="")

    monkeypatch.setattr(export_views, "_find_launcher", lambda: None)  # no Structurizr CLI

    def _boom(*a, **k):
        raise AssertionError("render() must not run when the text export skipped")

    monkeypatch.setattr(export_views, "_render", _boom)

    report = export_views.run(ws, "plantuml", render=True, log=lambda _m: None)

    assert report["exported"] is False
    assert "render" not in report
