"""csproj SDK-role extractor tests (``extract_csproj_sdk``).

Classifies each C# project's deployable role from its ``<Project Sdk>`` + target framework and
emits an ``sdk-role:<role>`` grouping-hint tag (the eShop BuildingBlocks signal, §7-A2 follow-up).
Synthetic repos under ``tmp_path``; the extractor is a pure XML/text parse (no .NET SDK).

    .venv/Scripts/python.exe -m pytest tests/test_extract_csproj_sdk.py -q
"""
from __future__ import annotations

from pathlib import Path

from anon.paths import resolve_workspace
from anon.stages import extract_csproj_sdk as sdk


def _proj(repo: Path, name: str, project_xml: str) -> None:
    d = repo / "src" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.csproj").write_text(project_xml, encoding="utf-8")


def _role_of(fragment: dict, rel: str) -> list[str]:
    tid = f"csharp:csproj:{rel}"
    for t in fragment["targets"]:
        if t["id"] == tid:
            return t["tags"]
    return []


def test_sdk_role_classification():
    assert sdk.sdk_role('<Project Sdk="Microsoft.NET.Sdk.Web">') == "service-web"
    assert sdk.sdk_role('<Project Sdk="Microsoft.NET.Sdk.Worker">') == "service-worker"
    assert sdk.sdk_role('<Project Sdk="Microsoft.NET.Sdk">') == "library"
    assert sdk.sdk_role('<Project Sdk="Microsoft.NET.Sdk.Razor">') == "ui-library"
    assert sdk.sdk_role('<Project Sdk="Aspire.AppHost.Sdk/13.2.0">') == "orchestration"
    # a MAUI / mobile target framework -> client app even on the plain SDK
    assert sdk.sdk_role('<Project Sdk="Microsoft.NET.Sdk">\n<TargetFrameworks>net10.0-android;net10.0-ios</TargetFrameworks>') == "client-app"
    # no recognizable SDK -> no role claim (honest absence)
    assert sdk.sdk_role("<Project>") is None


def test_emits_sdk_role_tag_on_target(tmp_path: Path):
    _proj(tmp_path, "Catalog.API", '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>')
    _proj(tmp_path, "EventBus", '<Project Sdk="Microsoft.NET.Sdk"></Project>')
    frag = sdk.build_fragment(tmp_path)
    assert frag["relationships"] == []  # refine, never structure
    assert _role_of(frag, "src/Catalog.API/Catalog.API.csproj") == ["sdk-role:service-web"]
    assert _role_of(frag, "src/EventBus/EventBus.csproj") == ["sdk-role:library"]


def test_project_without_recognizable_sdk_is_untagged(tmp_path: Path):
    _proj(tmp_path, "Weird", "<Project Sdk=\"Some.Custom.Sdk\"></Project>")
    frag = sdk.build_fragment(tmp_path)
    assert frag["targets"] == []  # custom SDK -> no role -> no tag


def test_run_degrades_to_none_on_non_csharp_repo(tmp_path: Path):
    (tmp_path / "readme.md").write_text("no projects here\n", encoding="utf-8")
    ws = resolve_workspace(str(tmp_path), str(tmp_path / "arch"))
    assert sdk.run(ws) is None
    assert not (ws.fragments / "csproj-sdk-role.json").exists()


def test_build_fragment_is_deterministic(tmp_path: Path):
    _proj(tmp_path, "A", '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>')
    _proj(tmp_path, "B", '<Project Sdk="Microsoft.NET.Sdk"></Project>')
    assert sdk.build_fragment(tmp_path) == sdk.build_fragment(tmp_path)
