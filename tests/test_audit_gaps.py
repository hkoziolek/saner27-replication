"""Tests for the v1-audit gap closures (orchestrator integration):
`arch run --diff` preview (§9.1.1), `arch curate --names` (§8.4), Structurizr DSL
validation wiring (§10/§14 P3), `--incremental` transparency, and L1 transitive NuGet (§4.2).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from anon import cli, preview
from anon.export_views import validate_workspace
from anon.jsonio import dump_json, load_json
from anon.paths import resolve_workspace
from anon.stages import enrich_llm
from anon.stages.extract_build_graph import _l1_transitive_packages, build_fragment

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"
TOY_RULES = TOY / "architecture" / "rules"
BUNDLED_CLI = REPO_ROOT / "tools" / "structurizr-cli"

# Every test below drives the real `arch` orchestrator on the C#-only toy and asserts on
# the deterministic L0 core (diff preview, name TSV, DSL validation, gates) — never on
# Roslyn output. Suppress the optional dotnet/Roslyn extractor so a dev box with the .NET
# SDK doesn't pay MSBuild cold-start on every run (conftest.no_dotnet_extract; §18.4/§19.5).
pytestmark = pytest.mark.usefixtures("no_dotnet_extract")


def _bundled_launcher() -> Path | None:
    """The bundled Structurizr CLI launcher for this platform, or None if not vendored."""
    for name in ("structurizr.bat", "structurizr.sh"):
        p = BUNDLED_CLI / name
        if p.exists():
            return p
    return None


# --- §9.1.1 run --diff preview -------------------------------------------------

def _curated(targets):
    return {"targets": targets, "relationships": []}


def test_preview_first_run():
    new = _curated([{"id": "x:t:a", "container_id": "c:1", "container_name": "One"}])
    diff = preview.compute(None, new)
    assert diff["first_run"] is True
    assert "c:1" in diff["entering_views"]


def test_preview_detects_regroup_and_new_container():
    old = _curated([
        {"id": "x:t:a", "container_id": "c:core", "container_name": "Core"},
        {"id": "x:t:b", "container_id": "c:core", "container_name": "Core"},
    ])
    new = _curated([
        {"id": "x:t:a", "container_id": "c:core", "container_name": "Core"},
        {"id": "x:t:b", "container_id": "c:msg", "container_name": "Messaging"},  # regrouped
    ])
    diff = preview.compute(old, new)
    assert any("x:t:b" in r for r in diff["regrouped"])      # layout-affecting
    assert "c:msg" in diff["entering_views"]                 # new container enters a view
    assert "c:msg" in diff["added_containers"]               # content
    md = preview.render(diff)
    assert "Layout-affecting changes" in md and "Content changes" in md


def test_preview_no_change_is_quiet():
    same = _curated([{"id": "x:t:a", "container_id": "c:1", "container_name": "One"}])
    diff = preview.compute(same, dict(same))
    assert diff["regrouped"] == [] and diff["entering_views"] == [] and diff["leaving_views"] == []
    assert "no layout-affecting changes" in preview.render(diff)


def test_run_diff_end_to_end(tmp_path, capsys):
    arch = tmp_path / "architecture"
    args = ["run", "--repo", str(TOY), "--arch-dir", str(arch),
            "--rules-dir", str(TOY_RULES), "--no-llm", "--diff"]
    assert cli.main(args) == 0            # first run: first-run preview
    capsys.readouterr()
    assert cli.main(args) == 0            # second run: prev exists -> quiet preview
    out = capsys.readouterr().out
    assert "Dry-run preview" in out
    assert (arch / "generated" / "run-diff.md").exists()


# --- §8.4 curate --names -------------------------------------------------------

def test_curate_names_emits_tsv(tmp_path):
    arch = tmp_path / "architecture"
    # extract first (curate reads extracted-facts.json), then `arch curate --names`.
    assert cli.main(["extract", "--repo", str(TOY), "--arch-dir", str(arch)]) == 0
    assert cli.main(["curate", "--repo", str(TOY), "--arch-dir", str(arch),
                     "--rules-dir", str(TOY_RULES), "--names"]) == 0
    ws = resolve_workspace(TOY, arch_dir=arch, rules_dir=TOY_RULES)
    tsv = ws.name_review_tsv.read_text(encoding="utf-8")
    assert tsv.startswith("id\traw_name\tllm_name\tnaming_confidence\tevidence")
    assert "Web Frontend" in tsv and "Core Library" in tsv


# --- §10/§14 P3 Structurizr DSL validation (graceful) --------------------------

def test_validate_workspace_degrades_without_cli(tmp_path):
    arch = tmp_path / "architecture"
    cli.main(["run", "--repo", str(TOY), "--arch-dir", str(arch),
              "--rules-dir", str(TOY_RULES), "--no-llm"])
    ws = resolve_workspace(TOY, arch_dir=arch, rules_dir=TOY_RULES)
    res = validate_workspace(ws)
    # On a host without the Structurizr CLI/Java this is a graceful skip, never a crash.
    assert res["validated"] or res["skipped"]


@pytest.mark.skipif(_bundled_launcher() is None or shutil.which("java") is None,
                    reason="bundled Structurizr CLI (tools/structurizr-cli) or Java 17 not available")
def test_toy_workspace_validates_against_bundled_cli(tmp_path, monkeypatch):
    """The generated toy workspace.dsl must actually parse in the real Structurizr DSL parser.

    Regression for the single-line ``{ tags ... }`` defect: the Structurizr DSL 5.x parser
    rejects single-line blocks ("too many tokens"), and the §10/§14-P3 DSL gate stayed
    latent because ``arch validate --workspace`` skips silently when the CLI is off PATH
    (CI never had it). This test puts the *bundled* CLI on PATH and asserts the generated
    workspace.dsl is genuinely validated — not skipped — so a future single-line regression
    fails the suite instead of slipping through to a pilot run.
    """
    arch = tmp_path / "architecture"
    assert cli.main(["run", "--repo", str(TOY), "--arch-dir", str(arch),
                     "--rules-dir", str(TOY_RULES), "--no-llm"]) == 0
    ws = resolve_workspace(TOY, arch_dir=arch, rules_dir=TOY_RULES)
    # Prepend the vendored CLI dir so _find_launcher() resolves it (it isn't on PATH by
    # default — the very reason the gate was never exercised before).
    monkeypatch.setenv("PATH", str(BUNDLED_CLI) + os.pathsep + os.environ.get("PATH", ""))
    res = validate_workspace(ws)
    assert res["skipped"] is False, f"expected a real DSL validation, got a skip: {res['reason']}"
    assert res["validated"] is True, f"Structurizr rejected the generated workspace.dsl: {res['reason']}"


# --- §4.2 L1 transitive NuGet --------------------------------------------------

def test_l1_absent_returns_none(tmp_path):
    csproj = tmp_path / "Foo" / "Foo.csproj"
    csproj.parent.mkdir(parents=True)
    csproj.write_text("<Project Sdk='Microsoft.NET.Sdk'></Project>", encoding="utf-8")
    assert _l1_transitive_packages(csproj) is None


def test_l1_reads_project_assets(tmp_path):
    repo = tmp_path / "repo"
    csproj = repo / "App" / "App.csproj"
    csproj.parent.mkdir(parents=True)
    csproj.write_text(
        "<Project Sdk='Microsoft.NET.Sdk'><ItemGroup>"
        "<PackageReference Include='Direct' Version='1.0.0'/></ItemGroup></Project>",
        encoding="utf-8")
    assets = csproj.parent / "obj" / "project.assets.json"
    assets.parent.mkdir(parents=True)
    dump_json({"version": 3, "targets": {"net9.0": {
        "Direct/1.0.0": {"type": "package"},
        "Transitive.Dep/2.1.0": {"type": "package"},
        "App/1.0.0": {"type": "project"},
    }}}, assets)
    pkgs = _l1_transitive_packages(csproj)
    assert pkgs == ["Direct", "Transitive.Dep"]      # sorted; project excluded
    # and through build_fragment: L1 coverage true + a transitive-tagged package target
    frag = build_fragment(repo)
    cov = frag["provenance"]["coverage"]["csharp:csproj:App/App.csproj"]
    assert cov["L1"] is True
    trans = [t for t in frag["targets"] if t["id"] == "csharp:package:Transitive.Dep"]
    assert trans and "transitive" in trans[0]["tags"]
