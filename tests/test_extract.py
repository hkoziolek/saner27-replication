"""C# extraction-vertical tests (plan phase P2): the L0 build-graph parser
(``extract_build_graph``) and the SDK-dependent Roslyn L2 driver
(``extract_csharp_facts``).

Self-contained: every test that writes goes to pytest ``tmp_path`` — never to ``out/`` or
the fixture's ``architecture/``. Roslyn integration tests are skipped when ``dotnet`` or a
solution restore is unavailable (plan §18.4 graceful degradation / §19.5 build-env
caveat). Run just this file:

    .venv/Scripts/python.exe -m pytest tests/test_extract.py -q
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from anon import SCHEMA_VERSION
from anon.jsonio import load_json
from anon.paths import resolve_workspace
from anon.stages import (extract_build_graph, extract_csharp_facts,
                              normalize_facts, validate_facts)

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"

# The 5 expected toy project ids (stable scheme §6.3: csharp:csproj:<repo-relative-path>).
TOY_PROJECT_IDS = {
    "csharp:csproj:src/Web/Toy.Web.csproj",
    "csharp:csproj:src/Domain/Toy.Domain.csproj",
    "csharp:csproj:src/Messaging/Toy.Messaging.csproj",
    "csharp:csproj:src/Common/Toy.Common.csproj",
    "csharp:csproj:tests/Toy.Web.Tests/Toy.Web.Tests.csproj",
}


# --------------------------------------------------------------------------------------
# L0 build-graph parse (pure XML/text, no SDK) — task #1
# --------------------------------------------------------------------------------------

def _toy_fragment() -> dict:
    return extract_build_graph.build_fragment(TOY)


def _target_ids(fragment: dict) -> set[str]:
    return {t["id"] for t in fragment["targets"]}


def _rel_pairs(fragment: dict) -> set[tuple[str, str, str]]:
    """(source, target, first-evidence-type) for each relationship."""
    return {
        (r["source"], r["target"], r["evidence"][0]["type"])
        for r in fragment["relationships"]
    }


def test_csproj_sln_parse_yields_five_toy_projects():
    frag = _toy_fragment()
    proj_ids = {t["id"] for t in frag["targets"] if t["id"].startswith("csharp:csproj:")}
    assert proj_ids == TOY_PROJECT_IDS


def test_lift_filedeps_sidecar_writes_and_strips(tmp_path):
    """The §4.1 file-deps lifting: extract_csharp_facts turns the helper's ``fileDeps`` block
    into a file-deps/1 sidecar and removes it from the fact fragment. No SDK/helper needed —
    drives the pure post-processing on a synthetic fragment."""
    from anon import filedeps
    from anon.jsonio import dump_json, load_json
    from anon.stages.extract_csharp_facts import _lift_filedeps_sidecar

    ws = resolve_workspace(str(TOY), str(tmp_path / "arch"))
    ws.fragments.mkdir(parents=True, exist_ok=True)
    frag_path = ws.fragments / "roslyn-csharp.json"
    dump_json({
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [{"name": "roslyn-csharp", "version": "0.2.0"}], "coverage": {}},
        "targets": [{"id": "csharp:csproj:src/A/A.csproj", "name": "A", "type": "csproj",
                     "language": "csharp"}],
        "relationships": [],
        "fileDeps": {
            "files": {"src/A/a.cs": "csharp:csproj:src/A/A.csproj",
                      "src/B/b.cs": "csharp:csproj:src/B/B.csproj"},
            "edges": [["src/A/a.cs", "src/B/b.cs"]],
        },
    }, frag_path)

    _lift_filedeps_sidecar(ws, frag_path)

    # the fragment no longer carries the (non-fact-model) fileDeps block
    assert "fileDeps" not in load_json(frag_path)
    # a file-deps/1 sidecar was written, with the files map + edge intact
    sidecars = filedeps.load_sidecars(ws)
    assert len(sidecars) == 1
    sc = sidecars[0]
    assert sc["schema"] == "file-deps/1" and sc["language"] == "cs"
    assert sc["extractor"]["name"] == "roslyn-csharp"
    assert sc["files"]["src/A/a.cs"] == "csharp:csproj:src/A/A.csproj"
    assert ["src/A/a.cs", "src/B/b.cs"] in sc["edges"]


def test_lift_filedeps_noop_without_block(tmp_path):
    """An old helper build (no ``fileDeps``) leaves the fragment untouched and writes no sidecar."""
    from anon import filedeps
    from anon.jsonio import dump_json
    from anon.stages.extract_csharp_facts import _lift_filedeps_sidecar

    ws = resolve_workspace(str(TOY), str(tmp_path / "arch"))
    ws.fragments.mkdir(parents=True, exist_ok=True)
    frag_path = ws.fragments / "roslyn-csharp.json"
    dump_json({"schema_version": SCHEMA_VERSION, "targets": [], "relationships": []}, frag_path)
    _lift_filedeps_sidecar(ws, frag_path)
    assert filedeps.load_sidecars(ws) == []


def test_all_projects_are_csproj_typed_and_first_party():
    frag = _toy_fragment()
    for t in frag["targets"]:
        if t["id"].startswith("csharp:csproj:"):
            assert t["type"] == "csproj"
            assert t["language"] == "csharp"
            assert t["external"] is False
            assert "build:msbuild" in t["tags"]


def test_project_ref_edges_are_correct():
    frag = _toy_fragment()
    pairs = _rel_pairs(frag)
    expected_project_refs = {
        ("csharp:csproj:src/Web/Toy.Web.csproj",
         "csharp:csproj:src/Domain/Toy.Domain.csproj", "project_ref"),
        ("csharp:csproj:src/Web/Toy.Web.csproj",
         "csharp:csproj:src/Messaging/Toy.Messaging.csproj", "project_ref"),
        ("csharp:csproj:src/Domain/Toy.Domain.csproj",
         "csharp:csproj:src/Common/Toy.Common.csproj", "project_ref"),
        ("csharp:csproj:src/Messaging/Toy.Messaging.csproj",
         "csharp:csproj:src/Common/Toy.Common.csproj", "project_ref"),
        ("csharp:csproj:tests/Toy.Web.Tests/Toy.Web.Tests.csproj",
         "csharp:csproj:src/Web/Toy.Web.csproj", "project_ref"),
    }
    assert expected_project_refs <= pairs


def test_package_ref_edges_and_targets_present_pre_curation():
    """Test-project + NuGet package targets are present *before* curation (§4.2: the
    build graph is complete; exclusion is curation's job, §7.4)."""
    frag = _toy_fragment()
    ids = _target_ids(frag)
    # test project present pre-curation
    assert "csharp:csproj:tests/Toy.Web.Tests/Toy.Web.Tests.csproj" in ids
    # package targets present pre-curation (external, dropped only by drop_external later)
    assert "csharp:package:Serilog" in ids
    assert "csharp:package:Newtonsoft.Json" in ids
    assert "csharp:package:xunit" in ids
    for pkg in ("Serilog", "Newtonsoft.Json", "xunit"):
        t = next(t for t in frag["targets"] if t["id"] == f"csharp:package:{pkg}")
        assert t["external"] is True
        assert t["type"] == "package"
    # package_ref edges exist
    pairs = _rel_pairs(frag)
    assert ("csharp:csproj:src/Web/Toy.Web.csproj", "csharp:package:Serilog", "package_ref") in pairs
    assert ("csharp:csproj:src/Messaging/Toy.Messaging.csproj",
            "csharp:package:Newtonsoft.Json", "package_ref") in pairs
    assert ("csharp:csproj:tests/Toy.Web.Tests/Toy.Web.Tests.csproj",
            "csharp:package:xunit", "package_ref") in pairs


def test_project_ref_visibility_defaults_public():
    frag = _toy_fragment()
    for r in frag["relationships"]:
        for ev in r["evidence"]:
            if ev["type"] == "project_ref":
                assert ev.get("visibility") == "public"  # C# ProjectReference defaults public (§5)


def test_fragment_provenance_only_extractor_and_coverage():
    """Extractors set extractors + coverage; the runner stamps repo/commit/generated_at."""
    frag = _toy_fragment()
    prov = frag["provenance"]
    assert prov["extractors"] == [extract_build_graph.EXTRACTOR]
    assert set(prov.keys()) == {"extractors", "coverage"}
    assert "generated_at" not in prov and "commit" not in prov
    # L0 coverage recorded per project target
    for pid in TOY_PROJECT_IDS:
        assert prov["coverage"][pid]["L0"] is True


def test_build_graph_is_deterministic_and_order_independent():
    a = extract_build_graph.build_fragment(TOY)
    b = extract_build_graph.build_fragment(TOY)
    from anon.jsonio import dumps_json
    from anon.model import canonicalize
    assert dumps_json(canonicalize(a)) == dumps_json(canonicalize(b))


# --------------------------------------------------------------------------------------
# Hardening: solution folders, missing refs, MSBuild props, de-dup — task #1
# --------------------------------------------------------------------------------------

def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


_CSPROJ_LEAF = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net9.0</TargetFramework></PropertyGroup>
</Project>
"""


def test_solution_folder_grouping_becomes_hint_tag(tmp_path: Path):
    """Solution folders are captured as ``slnfolder:<name>`` hint tags (§4.2)."""
    repo = tmp_path / "repo"
    _write(repo / "src" / "Lib" / "Lib.csproj", _CSPROJ_LEAF)
    _write(repo / "src" / "App" / "App.csproj",
           '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
           '<ProjectReference Include="..\\Lib\\Lib.csproj" /></ItemGroup></Project>')
    folder_guid = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    lib_guid = "11111111-1111-1111-1111-111111111111"
    app_guid = "22222222-2222-2222-2222-222222222222"
    sln = (
        "Microsoft Visual Studio Solution File, Format Version 12.00\n"
        '#\n'
        f'Project("{{2150E333-8FDC-42A3-9474-1A3956D46DE8}}") = "Source", "Source", "{{{folder_guid}}}"\n'
        "EndProject\n"
        f'Project("{{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}}") = "Lib", "src\\Lib\\Lib.csproj", "{{{lib_guid}}}"\n'
        "EndProject\n"
        f'Project("{{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}}") = "App", "src\\App\\App.csproj", "{{{app_guid}}}"\n'
        "EndProject\n"
        "Global\n"
        "\tGlobalSection(NestedProjects) = preSolution\n"
        f"\t\t{{{lib_guid}}} = {{{folder_guid}}}\n"
        f"\t\t{{{app_guid}}} = {{{folder_guid}}}\n"
        "\tEndGlobalSection\n"
        "EndGlobal\n"
    )
    _write(repo / "Sln.sln", sln)

    frag = extract_build_graph.build_fragment(repo)
    by_id = {t["id"]: t for t in frag["targets"]}
    assert "slnfolder:Source" in by_id["csharp:csproj:src/Lib/Lib.csproj"]["tags"]
    assert "slnfolder:Source" in by_id["csharp:csproj:src/App/App.csproj"]["tags"]


def test_missing_referenced_csproj_still_emits_edge_and_placeholder(tmp_path: Path):
    """A ProjectReference to a non-existent csproj still yields the edge + a placeholder
    target so referential integrity holds (§5/§10)."""
    repo = tmp_path / "repo"
    _write(repo / "App" / "App.csproj",
           '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
           '<ProjectReference Include="..\\Ghost\\Ghost.csproj" /></ItemGroup></Project>')
    frag = extract_build_graph.build_fragment(repo)
    ids = _target_ids(frag)
    ghost_id = "csharp:csproj:Ghost/Ghost.csproj"
    assert ghost_id in ids  # placeholder materialized
    ghost = next(t for t in frag["targets"] if t["id"] == ghost_id)
    assert ghost["type"] == "unknown"
    assert "missing-ref" in ghost["tags"]
    pairs = _rel_pairs(frag)
    assert ("csharp:csproj:App/App.csproj", ghost_id, "project_ref") in pairs


def test_msbuild_property_in_projectref_is_expanded(tmp_path: Path):
    """Directory.Build.props-style ``$(MSBuildThisFileDirectory)`` refs resolve to the
    correct stable id rather than a mangled path (§4.2)."""
    repo = tmp_path / "repo"
    _write(repo / "src" / "Lib" / "Lib.csproj", _CSPROJ_LEAF)
    _write(repo / "src" / "App" / "App.csproj",
           '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
           '<ProjectReference Include="$(MSBuildThisFileDirectory)..\\Lib\\Lib.csproj" />'
           '</ItemGroup></Project>')
    frag = extract_build_graph.build_fragment(repo)
    pairs = _rel_pairs(frag)
    assert ("csharp:csproj:src/App/App.csproj",
            "csharp:csproj:src/Lib/Lib.csproj", "project_ref") in pairs


def test_duplicate_references_are_deduped(tmp_path: Path):
    """A reference declared twice (e.g. once in the .sln scan, once via direct scan, or a
    duplicated ItemGroup entry) does not double the edge."""
    repo = tmp_path / "repo"
    _write(repo / "src" / "Lib" / "Lib.csproj", _CSPROJ_LEAF)
    _write(repo / "src" / "App" / "App.csproj",
           '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
           '<ProjectReference Include="..\\Lib\\Lib.csproj" />'
           '<ProjectReference Include="..\\Lib\\Lib.csproj" />'  # duplicate
           '</ItemGroup></Project>')
    # also list App in a .sln so it is discovered twice (sln + direct scan)
    app_guid = "22222222-2222-2222-2222-222222222222"
    _write(repo / "Sln.sln",
           "Microsoft Visual Studio Solution File, Format Version 12.00\n"
           f'Project("{{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}}") = "App", "src\\App\\App.csproj", "{{{app_guid}}}"\n'
           "EndProject\n")
    frag = extract_build_graph.build_fragment(repo)
    app_to_lib = [
        r for r in frag["relationships"]
        if r["source"] == "csharp:csproj:src/App/App.csproj"
        and r["target"] == "csharp:csproj:src/Lib/Lib.csproj"
    ]
    assert len(app_to_lib) == 1  # de-duped
    # App target itself only once despite sln + scan discovery
    app_targets = [t for t in frag["targets"] if t["id"] == "csharp:csproj:src/App/App.csproj"]
    assert len(app_targets) == 1


def test_run_writes_fragment_to_workspace_fragments(tmp_path: Path):
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "architecture")
    out = extract_build_graph.run(ws)
    assert out == ws.fragments / "csharp-build-graph.json"
    assert out.exists()
    frag = load_json(out)
    assert {t["id"] for t in frag["targets"] if t["id"].startswith("csharp:csproj:")} == TOY_PROJECT_IDS


# --------------------------------------------------------------------------------------
# Graceful degradation (no SDK / no helper) — task #3, plan §18.4
# --------------------------------------------------------------------------------------

def test_roslyn_degrades_when_dotnet_absent(tmp_path: Path, monkeypatch):
    """When `dotnet` is not on PATH, the L2 stage logs and returns None (never raises)."""
    monkeypatch.setattr(extract_csharp_facts.shutil, "which", lambda _name: None)
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "architecture")
    assert extract_csharp_facts.helper_available() is False
    assert extract_csharp_facts.run(ws) is None


def test_roslyn_degrades_when_helper_project_missing(tmp_path: Path, monkeypatch):
    """When the helper project cannot be found, degrade gracefully."""
    monkeypatch.setattr(extract_csharp_facts, "helper_project_dir",
                        lambda: tmp_path / "nonexistent-helper")
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "architecture")
    assert extract_csharp_facts.helper_available() is False
    assert extract_csharp_facts.run(ws) is None


def test_roslyn_degrades_when_no_solution(tmp_path: Path, monkeypatch):
    """A repo with no .sln yields no Roslyn facts (returns None), even if the helper
    is available."""
    monkeypatch.setattr(extract_csharp_facts, "helper_available", lambda: True)
    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()
    ws = resolve_workspace(empty_repo, arch_dir=tmp_path / "architecture")
    assert extract_csharp_facts.run(ws) is None


def test_roslyn_degrades_when_build_fails(tmp_path: Path, monkeypatch):
    """If the helper build fails, the stage returns None rather than aborting the run."""
    monkeypatch.setattr(extract_csharp_facts, "helper_available", lambda: True)
    monkeypatch.setattr(extract_csharp_facts, "_find_solution", lambda _repo: TOY / "Toy.sln")
    monkeypatch.setattr(extract_csharp_facts, "_ensure_built", lambda _dir: None)
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "architecture")
    assert extract_csharp_facts.run(ws) is None


# --------------------------------------------------------------------------------------
# Roslyn integration — only when dotnet + restore are available (plan §19.5)
# --------------------------------------------------------------------------------------

def _restore_ok() -> bool:
    """True if the toy solution's projects are restored (project.assets.json present)."""
    if shutil.which("dotnet") is None:
        return False
    # A restored project writes obj/project.assets.json next to each csproj.
    return all(
        (TOY / sub / "obj" / "project.assets.json").exists()
        for sub in ("src/Web", "src/Domain", "src/Messaging", "src/Common",
                    "tests/Toy.Web.Tests")
    )


_INTEGRATION_AVAILABLE = (
    extract_csharp_facts.dotnet_available()
    and extract_csharp_facts.helper_available()
    and _restore_ok()
)
# The Roslyn path is `integration` (real dotnet/MSBuild — slow, deselected by default) AND
# skipped when the SDK/helper/restore is absent (as on CI). Both marks attach.
_skip_integration = lambda f: pytest.mark.integration(pytest.mark.skipif(
    not _INTEGRATION_AVAILABLE,
    reason="dotnet/helper/restore unavailable — Roslyn integration skipped (plan §18.4/§19.5)",
)(f))


@_skip_integration
def test_roslyn_helper_emits_symbol_use_and_components(tmp_path: Path):
    """The Roslyn helper emits per-project namespace components + cross-project symbol_use
    edges, using the same csproj id scheme so normalize merges them (§4.2/§6.3)."""
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "architecture")
    out = extract_csharp_facts.run(ws)
    assert out is not None and out.exists()
    frag = load_json(out)

    proj_ids = {t["id"] for t in frag["targets"]}
    assert TOY_PROJECT_IDS <= proj_ids  # same id scheme as the build graph

    # namespace-keyed component tier (§6.3)
    common = next(t for t in frag["targets"]
                  if t["id"] == "csharp:csproj:src/Common/Toy.Common.csproj")
    assert "Toy.Common" in common["namespaces"]
    comp_ids = {c["id"] for c in common["components"]}
    assert "csharp:csproj:src/Common/Toy.Common.csproj/component:Toy.Common" in comp_ids
    assert all(c["source"] == "namespace" for c in common["components"])

    # cross-project symbol_use edges with counts
    edges = {
        (r["source"], r["target"]): r["evidence"][0]
        for r in frag["relationships"]
    }
    domain_to_common = edges[("csharp:csproj:src/Domain/Toy.Domain.csproj",
                              "csharp:csproj:src/Common/Toy.Common.csproj")]
    assert domain_to_common["type"] == "symbol_use"
    assert domain_to_common["count"] >= 1


@_skip_integration
def test_roslyn_helper_output_is_deterministic(tmp_path: Path):
    a = extract_csharp_facts.run(resolve_workspace(TOY, arch_dir=tmp_path / "a"))
    b = extract_csharp_facts.run(resolve_workspace(TOY, arch_dir=tmp_path / "b"))
    assert a is not None and b is not None
    assert a.read_bytes() == b.read_bytes()  # helper output is byte-stable


@_skip_integration
def test_l0_and_l2_merge_corroborates_edges(tmp_path: Path):
    """L0 (project_ref) + L2 (symbol_use) fragments merge on (source,target); a
    corroborated declared edge gets confidence >= medium (§6.4/§6.5)."""
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "architecture")
    extract_build_graph.run(ws)
    assert extract_csharp_facts.run(ws) is not None
    merged = normalize_facts.merge(normalize_facts.load_fragments(ws))
    rel = next(
        r for r in merged["relationships"]
        if r["source"] == "csharp:csproj:src/Domain/Toy.Domain.csproj"
        and r["target"] == "csharp:csproj:src/Common/Toy.Common.csproj"
    )
    kinds = {e["type"] for e in rel["evidence"]}
    assert {"project_ref", "symbol_use"} <= kinds  # both fragments merged on the edge
    assert rel["is_declared_dependency"] is True
    assert rel["confidence"] in ("medium", "high")


# --------------------------------------------------------------------------------------
# Schema-changelog gate (§6.5d) — opt-in via validate(check_changelog=True)
# --------------------------------------------------------------------------------------

def _minimal_facts(schema_version: str) -> dict:
    """A schema-valid, referentially-clean facts dict at *schema_version* (no relationships)."""
    return {
        "schema_version": schema_version,
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [{"id": "x:t:1", "name": "1", "type": "csproj", "language": "csharp"}],
        "relationships": [],
    }


def test_validate_changelog_passes_for_current_schema_version():
    """The current SCHEMA_VERSION has a CHANGELOG.md entry, so the gate passes when enabled."""
    facts = _minimal_facts(SCHEMA_VERSION)  # currently "1.2" — has a CHANGELOG entry
    validate_facts.validate(facts, check_changelog=True)  # must not raise


def test_validate_changelog_raises_for_unknown_schema_version():
    """A schema-valid version with NO CHANGELOG.md entry fails the §6.5d gate when enabled.

    "1.99" matches the schema's ``^1\\.[0-9]+$`` constraint (so the JSON-Schema gate passes)
    but has no CHANGELOG section, so the changelog gate must raise.
    """
    facts = _minimal_facts("1.99")
    with pytest.raises(validate_facts.ValidationError, match="schema-changelog gate failed"):
        validate_facts.validate(facts, check_changelog=True)


def test_validate_default_skips_changelog_gate():
    """The default validate(facts) (check_changelog=False) never consults the changelog,
    so an undocumented-but-schema-valid version still passes."""
    facts = _minimal_facts("1.99")  # no CHANGELOG entry
    validate_facts.validate(facts)  # must not raise — gate is opt-in


def test_check_schema_changelog_flags_missing_entry_directly():
    """The §6.5d helper itself reports a problem for a version lacking a CHANGELOG entry,
    and none for the current one (it returns problems rather than raising)."""
    assert validate_facts.check_schema_changelog(schema_version="1.99")
    assert validate_facts.check_schema_changelog(schema_version=SCHEMA_VERSION) == []


# --------------------------------------------------------------------------------------
# Fragment hygiene — a stale/foreign fragment from a PRIOR run must never be merged
# (the abseil-into-eShop contamination class: a degraded extractor leaves no fragment,
#  but normalize globs the whole dir, so cmd_extract must reset it first).

def test_cmd_extract_resets_stale_foreign_fragment(tmp_path):
    from anon import cli
    from anon.jsonio import dump_json
    from anon.runreport import RunReport

    src = tmp_path / "toy-src"
    shutil.copytree(TOY, src)
    ws = resolve_workspace(src, tmp_path / "arch", src / "architecture" / "rules")
    # Plant a stale cmake fragment, as if a prior (mis-pointed) abseil run left it behind.
    ws.fragments.mkdir(parents=True, exist_ok=True)
    dump_json({"schema_version": SCHEMA_VERSION,
               "provenance": {"extractors": [{"name": "cmake-graph", "version": "0.1.0"}]},
               "targets": [{"id": "cpp:target:absl_phantom", "name": "absl_phantom",
                            "type": "static_lib", "language": "cpp", "path": "",
                            "external": False, "tags": []}],
               "relationships": []},
              ws.fragments / "cmake-graph.json")
    # The toy repo has no CMakeLists, so extract_cmake degrades and writes nothing — the
    # stale fragment would survive WITHOUT the reset and contaminate the facts.
    facts = cli.cmd_extract(ws, RunReport(), "toy-repo", "test-commit")
    ids = {t["id"] for t in facts["targets"]}
    assert "cpp:target:absl_phantom" not in ids, "stale foreign fragment leaked into facts"
    assert not (ws.fragments / "cmake-graph.json").exists(), \
        "the degraded cmake extractor's stale fragment was not cleared"
