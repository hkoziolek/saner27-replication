"""Tests for the three previously-deferred items (plan §4.1 / §16#1 / §16#15):

  1. MSBuild/VS C++ L0 + L1 (``extract_msbuild_cpp`` + binlog→compile-DB ``msbuild_binlog``).
  2. Interop native-side resolution (``interop_resolve``): ``native:lib:*`` → ``cpp:target:*``.
  3. clang-scan-deps as a Doxygen alternative (``extract_clang_deps``).

Pure-logic tests always run; toolchain-dependent integration tests (CMake, MSBuild/MSVC) are
gated and skip cleanly where the tool is absent (plan §18.4/§19.5). Every write goes to
``tmp_path`` — fixtures stay pristine.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from anon import cmake_api, interop_resolve, msbuild_binlog
from anon.jsonio import load_json
from anon.model import make_relationship
from anon.paths import resolve_workspace
from anon.stages import (extract_clang_deps, extract_cmake, extract_interop,
                              extract_msbuild_cpp, normalize_facts)

REPO_ROOT = Path(__file__).resolve().parents[1]
MSBUILD_CPP = REPO_ROOT / "tests" / "fixtures" / "msbuild-cpp-repo"
INTEROP_MIXED = REPO_ROOT / "tests" / "fixtures" / "interop-mixed-repo"


# ======================================================================================
# Item 2 — interop native-side resolution (pure; always runs)
# ======================================================================================

def _mixed_facts() -> dict:
    return {
        "targets": [
            {"id": "csharp:csproj:src/Calc/Calc.csproj", "name": "Calc", "type": "csproj",
             "language": "csharp"},
            {"id": "native:lib:NativeMath", "name": "NativeMath", "type": "shared_lib",
             "language": "native", "external": True, "tags": ["external", "native"]},
            {"id": "cpp:target:NativeMath", "name": "NativeMath", "type": "shared_lib",
             "language": "cpp", "tags": ["build:cmake"]},
        ],
        "relationships": [
            make_relationship("csharp:csproj:src/Calc/Calc.csproj", "native:lib:NativeMath",
                              [{"type": "interop", "detail": '[DllImport("NativeMath")]'}]),
        ],
    }


def test_interop_resolves_native_to_cpp_target():
    facts = _mixed_facts()
    stats = interop_resolve.resolve(facts)
    assert stats["resolved"] == ["native:lib:NativeMath -> cpp:target:NativeMath"]
    ids = {t["id"] for t in facts["targets"]}
    assert "native:lib:NativeMath" not in ids          # boundary dropped
    assert "cpp:target:NativeMath" in ids
    r = facts["relationships"][0]
    assert r["source"] == "csharp:csproj:src/Calc/Calc.csproj"
    assert r["target"] == "cpp:target:NativeMath"        # edge rewritten to the real lib
    assert "interop-resolved" in r["tags"]
    assert any(e["type"] == "interop" for e in r["evidence"])  # interop evidence preserved
    cpp = next(t for t in facts["targets"] if t["id"] == "cpp:target:NativeMath")
    assert "interop:pinvoke" in cpp["tags"]


def test_interop_unix_libname_normalization():
    """A Unix-style ``[DllImport("libfoo")]`` resolves to a CMake target ``foo`` (strip lib)."""
    facts = {
        "targets": [
            {"id": "native:lib:libfoo", "name": "libfoo", "type": "shared_lib", "language": "native"},
            {"id": "cpp:target:foo", "name": "foo", "type": "shared_lib", "language": "cpp"},
        ],
        "relationships": [make_relationship("native:lib:libfoo", "cpp:target:foo",
                          [{"type": "interop", "detail": "x"}])] if False else [],
    }
    # add an edge into the native lib to exercise rewrite
    facts["relationships"] = [make_relationship("csharp:csproj:a", "native:lib:libfoo",
                              [{"type": "interop", "detail": '[DllImport("libfoo")]'}])]
    facts["targets"].append({"id": "csharp:csproj:a", "name": "A", "type": "csproj", "language": "csharp"})
    stats = interop_resolve.resolve(facts)
    assert stats["resolved"] == ["native:lib:libfoo -> cpp:target:foo"]


def test_interop_ambiguous_match_left_unresolved():
    """Two C++ shared libs with the same name -> ambiguous -> boundary kept (conservative)."""
    facts = {
        "targets": [
            {"id": "csharp:csproj:a", "name": "A", "type": "csproj", "language": "csharp"},
            {"id": "native:lib:Dup", "name": "Dup", "type": "shared_lib", "language": "native"},
            {"id": "cpp:target:Dup", "name": "Dup", "type": "shared_lib", "language": "cpp"},
            {"id": "cpp:vcxproj:x/Dup.vcxproj", "name": "Dup", "type": "shared_lib", "language": "cpp"},
        ],
        "relationships": [make_relationship("csharp:csproj:a", "native:lib:Dup",
                          [{"type": "interop", "detail": "x"}])],
    }
    stats = interop_resolve.resolve(facts)
    assert stats["resolved"] == []
    assert stats["unresolved"] == ["native:lib:Dup"]
    assert any(t["id"] == "native:lib:Dup" for t in facts["targets"])  # boundary kept


def test_interop_noop_without_native_targets():
    """A pure model with no native:lib:* is untouched (byte-identical guarantee)."""
    facts = {"targets": [{"id": "cpp:target:x", "name": "x", "type": "static_lib", "language": "cpp"}],
             "relationships": []}
    before = load_json_str(facts)
    interop_resolve.resolve(facts)
    assert load_json_str(facts) == before


def load_json_str(obj) -> str:
    import json
    return json.dumps(obj, sort_keys=True)


# ======================================================================================
# Item 1 — MSBuild/VS C++ L0 + static L1 (pure parse; always runs)
# ======================================================================================

def test_msbuild_cpp_l0_targets_and_edges(tmp_path: Path):
    frag = extract_msbuild_cpp.build_fragment(MSBUILD_CPP, cache_dir=tmp_path / "cache")
    assert frag is not None
    by_id = {t["id"]: t for t in frag["targets"]}
    assert by_id["cpp:vcxproj:App/App.vcxproj"]["type"] == "executable"
    assert by_id["cpp:vcxproj:MathLib/MathLib.vcxproj"]["type"] == "static_lib"
    for t in frag["targets"]:
        assert t["language"] == "cpp" and "build:msbuild" in t["tags"]
    # App references MathLib (project_ref edge)
    pairs = {(r["source"], r["target"], r["evidence"][0]["type"]) for r in frag["relationships"]}
    assert ("cpp:vcxproj:App/App.vcxproj", "cpp:vcxproj:MathLib/MathLib.vcxproj", "project_ref") in pairs


def test_msbuild_cpp_static_compile_db_reconstruction(tmp_path: Path):
    """Build-free L1: a compile_commands.json is reconstructed from .vcxproj include dirs+defines."""
    cache = tmp_path / "cache"
    frag = extract_msbuild_cpp.build_fragment(MSBUILD_CPP, cache_dir=cache)
    assert frag["provenance"]["msbuild_compile_db"] == "static"
    db = load_json(cache / "compile_commands.json")
    files = {Path(e["file"]).name for e in db}
    assert {"Math.cpp", "main.cpp"} <= files
    math = next(e for e in db if Path(e["file"]).name == "Math.cpp")
    assert "/IC" in math["command"] and "include" in math["command"]   # resolved $(ProjectDir)include
    assert "/DMATHLIB_BUILD" in math["command"]                         # PreprocessorDefinitions
    # L1 coverage flips true on the reconstruction
    assert frag["provenance"]["coverage"]["cpp:vcxproj:MathLib/MathLib.vcxproj"]["L1"] is True


def test_msbuild_cpp_degrades_without_vcxproj(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    ws = resolve_workspace(empty, arch_dir=tmp_path / "architecture")
    assert extract_msbuild_cpp.run(ws) is None


# ======================================================================================
# Item 1 — binlog→compile-DB parser (pure; always runs)
# ======================================================================================

# A realistic diagnostic-log fragment as MSBuild replays a CL task (incl. the /diagnostics
# flag that must NOT be mistaken for a /d define).
_CL_LOG = r'''
Task "CL"
  C:\VS\bin\cl.exe /c /IC:\repo\MathLib\include /Zi /nologo /W3 /diagnostics:column /DMATHLIB_BUILD /D_DEBUG /D"FOO=bar" Math.cpp
Done executing task "CL".
'''


def test_parse_cl_invocations_extracts_includes_defines():
    db = msbuild_binlog.parse_cl_invocations(_CL_LOG, base_dir=r"C:\repo\MathLib")
    assert len(db) == 1
    cmd = db[0]["command"]
    assert r"/IC:\repo\MathLib\include" in cmd
    assert "/DMATHLIB_BUILD" in cmd and "/D_DEBUG" in cmd and "/DFOO=bar" in cmd
    assert "diagnostics" not in cmd            # the /diagnostics flag is not a define
    assert Path(db[0]["file"]).name == "Math.cpp"


def test_parse_cl_invocations_ignores_non_compile_lines():
    assert msbuild_binlog.parse_cl_invocations("some text mentioning cl.exe but no source") == []


# ======================================================================================
# Item 3 — clang-scan-deps parser + edge aggregation (pure; always runs)
# ======================================================================================

def test_parse_make_deps():
    text = ("foo.o: \\\n"
            "  /repo/a/foo.cpp \\\n"
            "  /repo/a/foo.hpp \\\n"
            "  /repo/b/bar.hpp\n")
    rules = extract_clang_deps.parse_make_deps(text)
    assert len(rules) == 1
    out, deps = rules[0]
    assert deps[0].endswith("foo.cpp")
    assert any(d.endswith("bar.hpp") for d in deps)


def test_clang_build_fragment_aggregates_cross_target_includes(tmp_path: Path):
    """foo.cpp (target A) includes b/bar.hpp (target B) -> one include edge A->B."""
    a_dir = (tmp_path / "a").resolve()
    b_dir = (tmp_path / "b").resolve()
    resolver = extract_clang_deps.TargetResolver([
        ("cpp:target:A", a_dir, {(a_dir / "foo.cpp").resolve()}),
        ("cpp:target:B", b_dir, set()),
    ])
    deps = [("foo.o", [str(a_dir / "foo.cpp"), str(a_dir / "foo.hpp"), str(b_dir / "bar.hpp")])]
    frag = extract_clang_deps.build_fragment(deps, resolver)
    pairs = {(r["source"], r["target"]) for r in frag["relationships"]}
    assert ("cpp:target:A", "cpp:target:B") in pairs
    r = next(r for r in frag["relationships"] if r["target"] == "cpp:target:B")
    assert r["evidence"][0]["type"] == "include"


def test_clang_run_degrades_without_clang(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(extract_clang_deps.shutil, "which", lambda _n: None)
    ws = resolve_workspace(MSBUILD_CPP, arch_dir=tmp_path / "architecture")
    assert extract_clang_deps.run(ws) is None


def test_clang_file_deps_repo_internal_only(tmp_path: Path):
    """Eval §4.1 per-file sidecar: repo-relative file→file edges; system headers out."""
    repo = tmp_path
    a_dir = (repo / "a").resolve()
    b_dir = (repo / "b").resolve()
    resolver = extract_clang_deps.TargetResolver([
        ("cpp:target:A", a_dir, {(a_dir / "foo.cpp").resolve()}),
        ("cpp:target:B", b_dir, set()),
    ])
    deps = [("foo.o", [str(a_dir / "foo.cpp"), str(a_dir / "foo.hpp"),
                       str(b_dir / "bar.hpp"), "/usr/include/vector"])]
    files, edges = extract_clang_deps.file_deps(deps, resolver, repo)
    assert files == {"a/foo.cpp": "cpp:target:A", "a/foo.hpp": "cpp:target:A",
                     "b/bar.hpp": "cpp:target:B"}     # the system header never appears
    assert edges == {("a/foo.cpp", "a/foo.hpp"), ("a/foo.cpp", "b/bar.hpp")}


# ======================================================================================
# Code-review regression tests (the 12 findings)
# ======================================================================================

def test_clang_parse_make_deps_handles_windows_drive_colon_output():
    """Finding #5: an absolute-path make output (C:\\...\\foo.o:) must not split at the drive colon."""
    text = r"C:\build\foo.o: C:\src\foo.cpp C:\inc\bar.h"
    rules = extract_clang_deps.parse_make_deps(text)
    assert len(rules) == 1
    output, deps = rules[0]
    assert output == r"C:\build\foo.o"
    assert deps == [r"C:\src\foo.cpp", r"C:\inc\bar.h"]


def test_cl_invocation_ignores_clang_cl_and_guards_flag_lookahead():
    """Findings #7/#8/#12: clang-cl.exe is not a cl invocation; bare /I doesn't eat /c; a
    `/D NAME=val.c` define value is not mistaken for a source TU."""
    # #8: clang-cl.exe (preceded by '-') is NOT matched as the cl.exe compiler token.
    assert msbuild_binlog.parse_cl_invocations(r"C:\llvm\clang-cl.exe /c foo.cpp") == []
    # #7: a bare '/I' followed by '/c' must not record '/c' as an include dir.
    db = msbuild_binlog.parse_cl_invocations(r"cl.exe /I /c foo.cpp")
    assert len(db) == 1
    assert "/I" not in db[0]["command"]            # no junk include captured from the bare /I
    # #12: '/D VER=ver.c' -> define captured, ver.c NOT treated as a source; foo.cpp is the TU.
    db2 = msbuild_binlog.parse_cl_invocations(r"cl.exe /D VER=ver.c /c foo.cpp")
    assert len(db2) == 1
    assert db2[0]["file"].endswith("foo.cpp")
    assert "/DVER=ver.c" in db2[0]["command"]


def test_apply_aliases_recomputes_weight_from_unioned_evidence():
    """Finding #2: edges collapsed by id_aliases recompute weight from the union (not max())."""
    from anon.model import compute_weight
    from anon.stages.apply_mapping_rules import _apply_aliases
    facts = {
        "targets": [
            {"id": "cpp:target:A", "name": "A", "type": "static_lib", "language": "cpp"},
            {"id": "cpp:target:B", "name": "B", "type": "static_lib", "language": "cpp"},
            {"id": "cpp:target:T", "name": "T", "type": "static_lib", "language": "cpp"},
        ],
        "relationships": [
            make_relationship("cpp:target:A", "cpp:target:T",
                              [{"type": "include", "detail": "a.h", "count": 5}]),
            make_relationship("cpp:target:B", "cpp:target:T",
                              [{"type": "include", "detail": "b.h", "count": 6}]),
        ],
    }
    out = _apply_aliases(facts, {"cpp:target:A": "cpp:target:M", "cpp:target:B": "cpp:target:M"})
    rels = out["relationships"]
    assert len(rels) == 1                       # the two edges collapsed onto M->T
    merged = rels[0]
    assert len(merged["evidence"]) == 2          # both include evidences unioned
    assert merged["weight"] == compute_weight(merged["evidence"]) == 11  # sum, NOT max(5,6)=6


def test_interop_resolution_rewrites_deployment_edges():
    """Finding #9: a deployment edge referencing a resolved native id is rewritten, not dangled."""
    facts = {
        "targets": [
            {"id": "csharp:csproj:a", "name": "A", "type": "csproj", "language": "csharp"},
            {"id": "native:lib:NativeMath", "name": "NativeMath", "type": "shared_lib",
             "language": "native"},
            {"id": "cpp:target:NativeMath", "name": "NativeMath", "type": "shared_lib",
             "language": "cpp"},
        ],
        "relationships": [make_relationship("csharp:csproj:a", "native:lib:NativeMath",
                          [{"type": "interop", "detail": "x"}])],
        "deployment": {"nodes": [],
                       "edges": [{"source": "deploy:image:svc", "target": "native:lib:NativeMath",
                                  "kind": "packages", "evidence": [{"type": "deploy", "detail": "d"}]}]},
    }
    interop_resolve.resolve(facts)
    edge = facts["deployment"]["edges"][0]
    assert edge["target"] == "cpp:target:NativeMath"   # rewritten, no dangling native:lib id


def test_doxygen_resolver_does_not_mangle_leading_dot_paths():
    """Finding #6: _TargetResolver strips a './' PREFIX, not the char set {'.','/'}."""
    from anon.stages.extract_cpp_facts import _TargetResolver
    model = cmake_api.CMakeModel()
    model.targets["gen"] = cmake_api.CMakeTarget(name="gen", type="static_lib",
                                                 source_dir=".gen", sources=[".gen/x.cpp"])
    r = _TargetResolver(model)
    assert r.resolve(".gen/x.cpp") == "gen"        # leading dot preserved (not stripped to gen/)
    assert r.resolve("./.gen/x.cpp") == "gen"      # a real './' prefix is still stripped


def test_msbuild_cpp_does_not_skip_packages_dir(tmp_path: Path):
    """Finding #3: a .vcxproj under a feature dir named 'packages' is NOT dropped."""
    proj = tmp_path / "src" / "packages" / "Core" / "Core.vcxproj"
    proj.parent.mkdir(parents=True)
    proj.write_text('<?xml version="1.0"?><Project xmlns="http://schemas.microsoft.com/developer/'
                    'msbuild/2003"><PropertyGroup><ConfigurationType>StaticLibrary</ConfigurationType>'
                    '</PropertyGroup></Project>', encoding="utf-8")
    frag = extract_msbuild_cpp.build_fragment(tmp_path)
    assert frag is not None
    assert any(t["id"].endswith("Core.vcxproj") for t in frag["targets"])


def test_msbuild_cpp_binlog_discovery_is_scoped_not_whole_tree(tmp_path: Path):
    """Finding #1: a stray *.binlog buried elsewhere in the tree is NOT auto-used as the compile DB."""
    proj = tmp_path / "Lib" / "Lib.vcxproj"
    proj.parent.mkdir(parents=True)
    proj.write_text('<?xml version="1.0"?><Project xmlns="http://schemas.microsoft.com/developer/'
                    'msbuild/2003"><PropertyGroup><ConfigurationType>StaticLibrary</ConfigurationType>'
                    '</PropertyGroup><ItemGroup><ClCompile Include="a.cpp"/></ItemGroup></Project>',
                    encoding="utf-8")
    # a stray binlog in a deep, unrelated subdir must be ignored (only repo-root/cache/env count)
    stray = tmp_path / "deep" / "nested" / "unrelated.binlog"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"not-a-real-binlog")
    frag = extract_msbuild_cpp.build_fragment(tmp_path, cache_dir=tmp_path / "cache")
    assert frag["provenance"]["msbuild_compile_db"] == "static"   # stray binlog ignored


# ======================================================================================
# Gated integration tests
# ======================================================================================

def _cpp_env() -> dict | None:
    if shutil.which("cl"):
        return {}
    if shutil.which("g++") and shutil.which("gcc"):
        return {"CXX": "g++", "CC": "gcc"}
    return None


_CMAKE_OK = (cmake_api.cmake_available() and shutil.which("ninja") is not None
             and _cpp_env() is not None)
_MSBUILD = msbuild_binlog.find_msbuild()


@pytest.mark.integration
@pytest.mark.skipif(not _CMAKE_OK, reason="cmake/ninja/compiler unavailable (§19.5)")
def test_interop_resolution_end_to_end_mixed_repo(tmp_path, monkeypatch):
    """Full extraction on the mixed C#/C++ fixture binds the P/Invoke to the real cpp target."""
    for k, v in (_cpp_env() or {}).items():
        monkeypatch.setenv(k, v)
    ws = resolve_workspace(INTEROP_MIXED, arch_dir=tmp_path / "architecture")
    # C# build graph (for the Calc.csproj target) + interop scan + CMake (NativeMath shared lib)
    from anon.stages import extract_build_graph
    extract_build_graph.run(ws)
    extract_interop.run(ws)
    extract_cmake.run(ws)
    facts = normalize_facts.run(ws, repo="interop-mixed", commit="test")

    ids = {t["id"] for t in facts["targets"]}
    assert "cpp:target:NativeMath" in ids
    assert "native:lib:NativeMath" not in ids     # resolved + dropped
    resolved = facts["provenance"].get("interop_resolved", [])
    assert any("cpp:target:NativeMath" in r for r in resolved)
    # the P/Invoke edge now points C# -> the real C++ lib
    edge = next((r for r in facts["relationships"]
                 if r["target"] == "cpp:target:NativeMath"
                 and r["source"].startswith("csharp:csproj:")), None)
    assert edge is not None
    assert any(e["type"] == "interop" for e in edge["evidence"])


@pytest.mark.integration
@pytest.mark.skipif(_MSBUILD is None, reason="MSBuild.exe unavailable — binlog path skipped (§19.5)")
def test_msbuild_binlog_to_compile_db_end_to_end(tmp_path: Path):
    """Build MathLib with MSBuild /bl into tmp, then reconstruct the compile DB from the binlog."""
    proj = MSBUILD_CPP / "MathLib" / "MathLib.vcxproj"
    # bare -bl writes msbuild.binlog into the working dir; build into tmp so the fixture stays clean.
    try:
        subprocess.run(
            [_MSBUILD, str(proj), "-bl", "-t:Rebuild", "-p:Configuration=Debug", "-p:Platform=x64",
             f"-p:IntDir={tmp_path / 'int'}\\", f"-p:OutDir={tmp_path / 'bin'}\\",
             "-nologo", "-verbosity:minimal"],
            cwd=tmp_path, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"MSBuild build could not run: {exc}")
    binlog = tmp_path / "msbuild.binlog"
    if not binlog.exists():
        pytest.skip("MSBuild produced no binlog (C++ toolset/env unavailable)")
    db = msbuild_binlog.compile_db_from_binlog(binlog)
    assert db, "expected at least one cl.exe compile command from the binlog"
    math = next((e for e in db if Path(e["file"]).name == "Math.cpp"), None)
    assert math is not None
    assert "include" in math["command"]          # the /I MathLib\include path
    assert "MATHLIB_BUILD" in math["command"]     # the project's /D define
