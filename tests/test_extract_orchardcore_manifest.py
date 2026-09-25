"""OrchardCore manifest-category extractor tests (``extract_orchardcore_manifest``).

The §13 follow-up signal (``plans/notes/2026-06-14-a2-curation-ablation.md`` +
``baselines/probe_orchardcore_manifest.py``): a module's ``Manifest.cs``
``[assembly: Feature(Category="…")]`` is the source-declared family the reference groups by, and
recovers it deterministically. This extractor surfaces it as a ``module-category:<Category>``
grouping-hint tag on the existing ``csharp:csproj:<rel>`` target — a refinement, never structure.

Self-contained: synthetic repos under pytest ``tmp_path`` (no clone/fixture needed). Run just this:

    .venv/Scripts/python.exe -m pytest tests/test_extract_orchardcore_manifest.py -q
"""
from __future__ import annotations

from pathlib import Path

from anon import SCHEMA_VERSION
from anon.paths import resolve_workspace
from anon.stages import extract_build_graph, normalize_facts
from anon.stages import extract_orchardcore_manifest as ocm


def _module(repo: Path, name: str, *categories: str) -> Path:
    """Create ``src/<name>/<name>.csproj`` + a ``Manifest.cs`` declaring one Feature per category."""
    d = repo / "src" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"></Project>\n', encoding="utf-8")
    body = "using OrchardCore.Modules.Manifest;\n"
    for i, c in enumerate(categories):
        body += f'[assembly: Feature(Id = "{name}.{i}", Name = "{name}", Category = "{c}")]\n'
    (d / "Manifest.cs").write_text(body, encoding="utf-8")
    return d


def _plain_csproj(repo: Path, name: str) -> Path:
    """A csproj with NO Manifest.cs (a framework/library project)."""
    d = repo / "src" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"></Project>\n', encoding="utf-8")
    return d


def _tag_for(fragment: dict, csproj_rel: str) -> list[str]:
    tid = f"csharp:csproj:{csproj_rel}"
    for t in fragment["targets"]:
        if t["id"] == tid:
            return t["tags"]
    return []


def test_tags_module_with_its_category(tmp_path: Path):
    _module(tmp_path, "OrchardCore.Admin", "Infrastructure")
    frag = ocm.build_fragment(tmp_path)
    assert frag["schema_version"] == SCHEMA_VERSION
    assert frag["relationships"] == []  # refine, never structure
    tags = _tag_for(frag, "src/OrchardCore.Admin/OrchardCore.Admin.csproj")
    assert tags == ["module-category:Infrastructure"]


def test_category_label_strips_non_alphanumerics(tmp_path: Path):
    # The reference label stems: "Content Management" -> ContentManagement etc.
    _module(tmp_path, "OrchardCore.Contents", "Content Management")
    _module(tmp_path, "OrchardCore.OpenId", "OpenID Connect")
    _module(tmp_path, "OrchardCore.Twitter", "X (Twitter)")
    frag = ocm.build_fragment(tmp_path)
    assert _tag_for(frag, "src/OrchardCore.Contents/OrchardCore.Contents.csproj") == ["module-category:ContentManagement"]
    assert _tag_for(frag, "src/OrchardCore.OpenId/OrchardCore.OpenId.csproj") == ["module-category:OpenIDConnect"]
    assert _tag_for(frag, "src/OrchardCore.Twitter/OrchardCore.Twitter.csproj") == ["module-category:XTwitter"]


def test_primary_category_is_most_common(tmp_path: Path):
    # A multi-feature module: the dominant category wins (modules are near-homogeneous).
    _module(tmp_path, "OrchardCore.Media", "Content", "Content", "Hosting")
    frag = ocm.build_fragment(tmp_path)
    assert _tag_for(frag, "src/OrchardCore.Media/OrchardCore.Media.csproj") == ["module-category:Content"]


def test_csproj_without_manifest_is_untagged(tmp_path: Path):
    _module(tmp_path, "OrchardCore.Admin", "Infrastructure")
    _plain_csproj(tmp_path, "OrchardCore.Abstractions")
    frag = ocm.build_fragment(tmp_path)
    ids = {t["id"] for t in frag["targets"]}
    assert "csharp:csproj:src/OrchardCore.Admin/OrchardCore.Admin.csproj" in ids
    assert "csharp:csproj:src/OrchardCore.Abstractions/OrchardCore.Abstractions.csproj" not in ids


def test_manifest_without_category_is_noop(tmp_path: Path):
    # A manifest that declares no Category (or uses a constant) yields no tag (honest absence).
    d = tmp_path / "src" / "OrchardCore.Plain"
    d.mkdir(parents=True)
    (d / "OrchardCore.Plain.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"></Project>\n', encoding="utf-8")
    (d / "Manifest.cs").write_text('[assembly: Module(Name = "Plain")]\n', encoding="utf-8")
    frag = ocm.build_fragment(tmp_path)
    assert frag["targets"] == []


def test_run_degrades_to_none_on_non_orchardcore_repo(tmp_path: Path):
    # A plain C# repo (no manifests) writes NO fragment and returns None (graceful no-op, §18.4).
    _plain_csproj(tmp_path, "Some.Library")
    ws = resolve_workspace(str(tmp_path), str(tmp_path / "arch"))
    assert ocm.run(ws) is None
    assert not (ws.fragments / "orchardcore-manifest.json").exists()


def test_tag_merges_onto_build_graph_target(tmp_path: Path):
    # normalize unions the category tag onto the build-graph's csproj target (§6.4c).
    _module(tmp_path, "OrchardCore.Admin", "Infrastructure")
    merged = normalize_facts.merge([
        extract_build_graph.build_fragment(tmp_path),
        ocm.build_fragment(tmp_path),
    ])
    tid = "csharp:csproj:src/OrchardCore.Admin/OrchardCore.Admin.csproj"
    target = next(t for t in merged["targets"] if t["id"] == tid)
    assert "build:msbuild" in target["tags"]              # from the build graph
    assert "module-category:Infrastructure" in target["tags"]  # unioned from this extractor
    assert target["type"] == "csproj"                     # build-graph scalar preserved


def test_build_fragment_is_deterministic(tmp_path: Path):
    _module(tmp_path, "OrchardCore.Admin", "Infrastructure")
    _module(tmp_path, "OrchardCore.Media", "Content")
    assert ocm.build_fragment(tmp_path) == ocm.build_fragment(tmp_path)
