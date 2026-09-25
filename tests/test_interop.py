"""C# interop-detection stage tests (plan §16#15 / §5 `interop` evidence).

Covers:
  - fixture with DllImport + LibraryImport -> fragment with correct native:lib targets
    and interop edges from the owning csproj.
  - empty repo (no .cs files) -> None, no fragment written.
  - schema validation against schema/fact-model.schema.json.
  - determinism: two runs produce byte-identical output.

Run just this file:

    .venv/Scripts/python.exe -m pytest tests/test_interop.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anon import interop_resolve
from anon.jsonio import dump_json, dumps_json, load_json
from anon.model import canonicalize, content_hash, make_relationship
from anon.paths import resolve_workspace
from anon.stages import (extract_build_graph, extract_interop,
                              extract_msbuild_cpp, normalize_facts)

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEROP_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "interop-repo"
INTEROP_MIXED = REPO_ROOT / "tests" / "fixtures" / "interop-mixed"
SCHEMA_PATH = REPO_ROOT / "schema" / "fact-model.schema.json"

# Expected ids produced from the fixture.
CSPROJ_ID = "csharp:csproj:Calc/Calc.csproj"
LIB_NATIVECALC = "native:lib:nativecalc"
LIB_CRYPTO = "native:lib:crypto"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _target_by_id(fragment: dict, tid: str) -> dict:
    return next(t for t in fragment["targets"] if t["id"] == tid)


def _rel_by(fragment: dict, source: str, target: str) -> dict:
    return next(
        r for r in fragment["relationships"]
        if r["source"] == source and r["target"] == target
    )


# ---------------------------------------------------------------------------
# Core behaviour on the interop fixture
# ---------------------------------------------------------------------------

def test_run_on_fixture_produces_fragment(tmp_path: Path):
    """run() on the interop fixture writes a fragment and returns the path."""
    ws = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    assert out.exists()
    assert out.name == "csharp-interop.json"


def test_fragment_has_nativecalc_target(tmp_path: Path):
    """native:lib:nativecalc target is present, external, native language."""
    ws = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    frag = load_json(out)
    t = _target_by_id(frag, LIB_NATIVECALC)
    assert t["language"] == "native"
    assert t["type"] == "shared_lib"
    assert t["external"] is True
    assert "external" in t["tags"]
    assert "native" in t["tags"]


def test_fragment_has_crypto_target_dll_stripped(tmp_path: Path):
    """native:lib:crypto target exists; the .dll extension was stripped."""
    ws = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    frag = load_json(out)
    t = _target_by_id(frag, LIB_CRYPTO)
    assert t["name"] == "crypto"          # extension stripped
    assert t["language"] == "native"
    assert t["external"] is True


def test_fragment_has_csproj_target(tmp_path: Path):
    """A minimal csproj target is emitted so referential integrity holds."""
    ws = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    frag = load_json(out)
    t = _target_by_id(frag, CSPROJ_ID)
    assert t["type"] == "csproj"
    assert t["language"] == "csharp"
    assert t["name"] == "Calc"


def test_interop_edge_csproj_to_nativecalc(tmp_path: Path):
    """An interop relationship exists from the csproj to native:lib:nativecalc."""
    ws = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    frag = load_json(out)
    rel = _rel_by(frag, CSPROJ_ID, LIB_NATIVECALC)
    assert rel["evidence"][0]["type"] == "interop"
    assert "nativecalc" in rel["evidence"][0]["detail"]


def test_interop_edge_csproj_to_crypto(tmp_path: Path):
    """An interop relationship exists from the csproj to native:lib:crypto."""
    ws = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    frag = load_json(out)
    rel = _rel_by(frag, CSPROJ_ID, LIB_CRYPTO)
    assert rel["evidence"][0]["type"] == "interop"
    assert "crypto" in rel["evidence"][0]["detail"]


def test_no_duplicate_edges_for_same_lib(tmp_path: Path):
    """Two [DllImport("nativecalc")] calls in the same project collapse to one edge."""
    ws = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    frag = load_json(out)
    nativecalc_edges = [
        r for r in frag["relationships"]
        if r["source"] == CSPROJ_ID and r["target"] == LIB_NATIVECALC
    ]
    assert len(nativecalc_edges) == 1


def test_provenance_has_correct_extractor(tmp_path: Path):
    """The fragment provenance lists only the csharp-interop extractor."""
    ws = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    frag = load_json(out)
    assert frag["provenance"]["extractors"] == [extract_interop.EXTRACTOR]
    assert "generated_at" not in frag["provenance"]
    assert "commit" not in frag["provenance"]


# ---------------------------------------------------------------------------
# Graceful degradation — no P/Invoke found
# ---------------------------------------------------------------------------

def test_empty_repo_returns_none(tmp_path: Path):
    """A repo with no .cs files returns None and writes no fragment."""
    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()
    ws = resolve_workspace(empty_repo, arch_dir=tmp_path / "architecture")
    result = extract_interop.run(ws)
    assert result is None
    assert not (ws.fragments / "csharp-interop.json").exists()


def test_cs_without_pinvoke_returns_none(tmp_path: Path):
    """A repo with .cs files but no [DllImport]/[LibraryImport] returns None."""
    repo = tmp_path / "no-interop"
    (repo / "Lib").mkdir(parents=True)
    (repo / "Lib" / "Lib.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"></Project>', encoding="utf-8"
    )
    (repo / "Lib" / "Class1.cs").write_text(
        "namespace Lib; public class Class1 {}", encoding="utf-8"
    )
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    result = extract_interop.run(ws)
    assert result is None


def test_cs_outside_csproj_is_skipped(tmp_path: Path):
    """A .cs file with DllImport but no ancestor .csproj is silently skipped."""
    repo = tmp_path / "orphan"
    repo.mkdir()
    (repo / "Orphan.cs").write_text(
        '[DllImport("phantom")]\nstatic extern void F();', encoding="utf-8"
    )
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    result = extract_interop.run(ws)
    assert result is None


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_fragment_validates_against_schema(tmp_path: Path):
    """The emitted fragment must satisfy schema/fact-model.schema.json."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    ws = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    # The fragment provenance lacks the runner-injected required fields (repo/commit/
    # generated_at). Inject stubs so the full provenance definition validates.
    frag = load_json(out)
    frag["provenance"]["repo"] = "test/interop-repo"
    frag["provenance"]["commit"] = "test"
    frag["provenance"]["generated_at"] = "2026-01-01T00:00:00Z"
    jsonschema.validate(instance=frag, schema=schema)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_two_runs_are_byte_identical(tmp_path: Path):
    """Running the extractor twice on the same repo produces byte-identical fragments."""
    ws_a = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "a" / "architecture")
    ws_b = resolve_workspace(INTEROP_FIXTURE, arch_dir=tmp_path / "b" / "architecture")
    out_a = extract_interop.run(ws_a)
    out_b = extract_interop.run(ws_b)
    assert out_a is not None and out_b is not None
    assert out_a.read_bytes() == out_b.read_bytes()


def test_build_fragment_deterministic_canonical_form(tmp_path: Path):
    """canonicalize(build_fragment(..)) is identical across two calls."""
    a = extract_interop.build_fragment(INTEROP_FIXTURE)
    b = extract_interop.build_fragment(INTEROP_FIXTURE)
    assert a is not None and b is not None
    assert dumps_json(canonicalize(a)) == dumps_json(canonicalize(b))


# ===========================================================================
# §16#15 TAIL: C++/CLI bridges + COM (deferred-tail detection)
# ===========================================================================

def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _ids(frag: dict) -> set[str]:
    return {t["id"] for t in frag["targets"]}


def _has_edge(frag: dict, source: str, target: str) -> bool:
    return any(r["source"] == source and r["target"] == target for r in frag["relationships"])


# --- C++/CLI bridge --------------------------------------------------------

def test_cppcli_using_emits_clr_boundary_and_edge(tmp_path: Path):
    """A /clr .cpp with `#using <Bridge.dll>` + gcnew -> native:clr boundary + interop edge."""
    repo = tmp_path / "cppcli"
    _write(repo / "Bridge" / "Bridge.vcxproj",
           '<Project><ItemDefinitionGroup><ClCompile /></ItemDefinitionGroup></Project>')
    _write(repo / "Bridge" / "Bridge.cpp",
           '#using <Bridge.dll>\n'
           'using namespace System;\n'
           'void Make() { auto s = gcnew String("hi"); }\n')
    frag = extract_interop.build_fragment(repo)
    assert frag is not None

    proj_id = "cpp:vcxproj:Bridge/Bridge.vcxproj"
    clr_id = "native:clr:Bridge"
    assert clr_id in _ids(frag)
    assert proj_id in _ids(frag)

    boundary = next(t for t in frag["targets"] if t["id"] == clr_id)
    assert boundary["language"] == "native"          # schema enum (§16#15)
    assert boundary["external"] is True
    assert "clr" in boundary["tags"]
    assert "external" in boundary["tags"]

    assert _has_edge(frag, proj_id, clr_id)
    rel = next(r for r in frag["relationships"] if r["target"] == clr_id)
    assert rel["evidence"][0]["type"] == "interop"
    assert "#using <Bridge.dll>" in rel["evidence"][0]["detail"]

    # The owning C++ target is tagged as a managed↔native seam.
    owner = next(t for t in frag["targets"] if t["id"] == proj_id)
    assert "interop:cppcli" in owner.get("tags", [])


def test_cppcli_clrsupport_vcxproj_tags_owner(tmp_path: Path):
    """`<CLRSupport>true</CLRSupport>` in a .vcxproj tags the owning target interop:cppcli."""
    repo = tmp_path / "cppcli-proj"
    _write(repo / "Mgd" / "Mgd.vcxproj",
           '<Project><PropertyGroup><CLRSupport>true</CLRSupport></PropertyGroup></Project>')
    # A plain native .cpp — the managed switch is on the project, not the source.
    _write(repo / "Mgd" / "Mgd.cpp", 'int f() { return 0; }\n')
    frag = extract_interop.build_fragment(repo)
    assert frag is not None
    owner = next(t for t in frag["targets"] if t["id"] == "cpp:vcxproj:Mgd/Mgd.vcxproj")
    assert "interop:cppcli" in owner.get("tags", [])


def test_native_only_vcxproj_clrsupport_false_is_no_op(tmp_path: Path):
    """`<CLRSupport>false</CLRSupport>` (the native default) is NOT a bridge -> no-op."""
    repo = tmp_path / "native-only"
    _write(repo / "N" / "N.vcxproj",
           '<Project><PropertyGroup><CLRSupport>false</CLRSupport></PropertyGroup></Project>')
    _write(repo / "N" / "N.cpp", 'int f() { return 1; }\n')
    assert extract_interop.build_fragment(repo) is None


# --- COM (C# side) ---------------------------------------------------------

def test_csharp_com_emits_com_boundary_and_edge(tmp_path: Path):
    """A C# [ComImport]/[Guid] + CoCreate-style activation -> com:* boundary + interop edge."""
    repo = tmp_path / "com-cs"
    _write(repo / "App" / "App.csproj", '<Project Sdk="Microsoft.NET.Sdk"></Project>')
    _write(repo / "App" / "Excel.cs",
           'using System.Runtime.InteropServices;\n'
           'namespace App;\n'
           '[ComImport]\n'
           '[Guid("00024500-0000-0000-C000-000000000046")]\n'
           'public interface IApplication {}\n'
           'public class Driver {\n'
           '    void Go() {\n'
           '        var t = System.Type.GetTypeFromProgID("Excel.Application");\n'
           '        var app = System.Activator.CreateInstance(t);\n'
           '    }\n'
           '}\n')
    frag = extract_interop.build_fragment(repo)
    assert frag is not None

    proj_id = "csharp:csproj:App/App.csproj"
    guid_id = "com:00024500-0000-0000-C000-000000000046"
    progid_id = "com:Excel.Application"
    ids = _ids(frag)
    assert guid_id in ids
    assert progid_id in ids

    for com_id in (guid_id, progid_id):
        boundary = next(t for t in frag["targets"] if t["id"] == com_id)
        assert boundary["language"] == "native"      # schema enum (§16#15)
        assert boundary["external"] is True
        assert "com" in boundary["tags"]
        assert _has_edge(frag, proj_id, com_id)
        rel = next(r for r in frag["relationships"] if r["target"] == com_id)
        assert rel["evidence"][0]["type"] == "interop"


def test_csharp_marshal_get_active_object(tmp_path: Path):
    """Marshal.GetActiveObject("ProgID") alone produces a com:* boundary."""
    repo = tmp_path / "com-active"
    _write(repo / "App" / "App.csproj", '<Project Sdk="Microsoft.NET.Sdk"></Project>')
    _write(repo / "App" / "Word.cs",
           'using System.Runtime.InteropServices;\n'
           'namespace App;\n'
           'public class W { void G() { var o = Marshal.GetActiveObject("Word.Application"); } }\n')
    frag = extract_interop.build_fragment(repo)
    assert frag is not None
    assert "com:Word.Application" in _ids(frag)
    assert _has_edge(frag, "csharp:csproj:App/App.csproj", "com:Word.Application")


# --- COM (C++ side) --------------------------------------------------------

def test_cpp_com_import_tlb_and_cocreate(tmp_path: Path):
    """C++ `#import "X.tlb"` + CoCreateInstance(CLSID_Foo) -> com:* boundaries + edges."""
    repo = tmp_path / "com-cpp"
    _write(repo / "Host" / "Host.vcxproj",
           '<Project><ItemGroup /></Project>')
    _write(repo / "Host" / "Host.cpp",
           '#import "Office.tlb"\n'
           '#include <objbase.h>\n'
           'void Make() {\n'
           '    void* p = nullptr;\n'
           '    CoCreateInstance(CLSID_MyServer, nullptr, 1, IID_MyServer, &p);\n'
           '}\n')
    frag = extract_interop.build_fragment(repo)
    assert frag is not None

    proj_id = "cpp:vcxproj:Host/Host.vcxproj"
    ids = _ids(frag)
    assert "com:Office" in ids                       # .tlb extension stripped
    assert "com:MyServer" in ids                     # from CLSID_MyServer / IID_MyServer
    assert _has_edge(frag, proj_id, "com:Office")
    assert _has_edge(frag, proj_id, "com:MyServer")


# --- Determinism + schema for the tail -------------------------------------

def _mixed_tail_repo(root: Path) -> Path:
    repo = root / "mixed-tail"
    _write(repo / "Bridge" / "Bridge.vcxproj", '<Project><ItemGroup /></Project>')
    _write(repo / "Bridge" / "Bridge.cpp",
           '#using <Managed.dll>\nvoid M() { auto x = gcnew System::Object(); }\n')
    _write(repo / "App" / "App.csproj", '<Project Sdk="Microsoft.NET.Sdk"></Project>')
    _write(repo / "App" / "Com.cs",
           'using System.Runtime.InteropServices;\n'
           '[ComImport][Guid("11111111-0000-0000-0000-000000000001")] interface I {}\n')
    return repo


def test_tail_two_runs_byte_identical(tmp_path: Path):
    """C++/CLI + COM tail output is deterministic across two runs."""
    repo = _mixed_tail_repo(tmp_path)
    a = extract_interop.build_fragment(repo)
    b = extract_interop.build_fragment(repo)
    assert a is not None and b is not None
    assert dumps_json(canonicalize(a)) == dumps_json(canonicalize(b))


def test_tail_fragment_validates_against_schema(tmp_path: Path):
    """The C++/CLI + COM tail fragment satisfies schema/fact-model.schema.json."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    repo = _mixed_tail_repo(tmp_path)
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    out = extract_interop.run(ws)
    assert out is not None
    frag = load_json(out)
    frag["provenance"]["repo"] = "test/mixed-tail"
    frag["provenance"]["commit"] = "test"
    frag["provenance"]["generated_at"] = "2026-01-01T00:00:00Z"
    jsonschema.validate(instance=frag, schema=schema)


# --- No-op guarantee: neither C++/CLI nor COM -> nothing new ----------------

def test_plain_cpp_and_csharp_repo_is_no_op(tmp_path: Path):
    """A repo with plain C++ and plain C# (no P/Invoke, C++/CLI, or COM) -> None (no-op)."""
    repo = tmp_path / "plain"
    _write(repo / "Lib" / "Lib.csproj", '<Project Sdk="Microsoft.NET.Sdk"></Project>')
    _write(repo / "Lib" / "Class1.cs", 'namespace Lib; public class Class1 { int X; }\n')
    _write(repo / "Native" / "Native.vcxproj", '<Project><ItemGroup /></Project>')
    _write(repo / "Native" / "Native.cpp",
           '#include <vector>\nint add(int a, int b) { return a + b; }\n')
    _write(repo / "Native" / "Native.h", 'int add(int a, int b);\n')
    assert extract_interop.build_fragment(repo) is None
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    assert extract_interop.run(ws) is None
    assert not (ws.fragments / "csharp-interop.json").exists()


# ===========================================================================
# T2.4 — COM / C++-CLI interop RESOLUTION + precision (plan §5; mixed fixture)
# ===========================================================================
#
# These exercise interop_resolve.resolve() binding the three §16#15 seam prefixes to real
# cpp targets, the native-side COM identity capture in extract_msbuild_cpp, and the
# comment/string precision guard in extract_interop. All pure-parse (no toolchain).

GUID_CALC = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"   # CalcServer coclass CLSID (App.cs <-> .idl)


def _mixed_facts() -> dict:
    """Hand-built mixed managed+native model: one boundary per seam + its real native target."""
    return {
        "targets": [
            {"id": "csharp:csproj:App/App.csproj", "name": "App", "type": "csproj",
             "language": "csharp"},
            # P/Invoke
            {"id": "native:lib:NativeMath", "name": "NativeMath", "type": "shared_lib",
             "language": "native", "external": True, "tags": ["external", "native"]},
            {"id": "cpp:vcxproj:native/NativeMath.vcxproj", "name": "NativeMath",
             "type": "shared_lib", "language": "cpp", "tags": ["build:msbuild"]},
            # C++/CLI (the native target is /clr -> tagged interop:cppcli by extract_interop)
            {"id": "native:clr:Bridge", "name": "Bridge", "type": "shared_lib",
             "language": "native", "external": True, "tags": ["clr", "external"]},
            {"id": "cpp:vcxproj:Bridge/Bridge.vcxproj", "name": "Bridge", "type": "shared_lib",
             "language": "cpp", "tags": ["build:msbuild", "interop:cppcli"]},
            # COM (the native server captured its coclass identity as com_provides)
            {"id": f"com:{GUID_CALC}", "name": GUID_CALC, "type": "shared_lib",
             "language": "native", "external": True, "tags": ["com", "external"]},
            {"id": "cpp:vcxproj:CalcServer/CalcServer.vcxproj", "name": "CalcServer",
             "type": "shared_lib", "language": "cpp", "tags": ["build:msbuild"],
             "com_provides": [GUID_CALC, "CalcServer.Object"]},
        ],
        "relationships": [
            make_relationship("csharp:csproj:App/App.csproj", "native:lib:NativeMath",
                              [{"type": "interop", "detail": '[DllImport("NativeMath")]'}]),
            make_relationship("cpp:vcxproj:NativeHost/NativeHost.vcxproj", "native:clr:Bridge",
                              [{"type": "interop", "detail": "#using <Bridge.dll>"}]),
            make_relationship("csharp:csproj:App/App.csproj", f"com:{GUID_CALC}",
                              [{"type": "interop", "detail": f'[Guid("{GUID_CALC}")]'}]),
        ],
    }


# --- C++/CLI resolution ----------------------------------------------------

def test_clr_resolves_to_managed_cpp_target():
    """native:clr:Bridge binds to the /clr cpp target named Bridge; edge + target tagged."""
    facts = _mixed_facts()
    stats = interop_resolve.resolve(facts)
    assert "native:clr:Bridge -> cpp:vcxproj:Bridge/Bridge.vcxproj" in stats["resolved"]
    ids = {t["id"] for t in facts["targets"]}
    assert "native:clr:Bridge" not in ids                       # boundary dropped
    bridge = next(t for t in facts["targets"] if t["id"] == "cpp:vcxproj:Bridge/Bridge.vcxproj")
    assert "interop:cppcli" in bridge["tags"]
    edge = next(r for r in facts["relationships"]
                if r["target"] == "cpp:vcxproj:Bridge/Bridge.vcxproj")
    assert "interop-resolved" in edge["tags"]
    assert any(e["type"] == "interop" for e in edge["evidence"])


def test_clr_ambiguous_stays_dangling():
    """Two /clr cpp targets named Bridge -> ambiguous -> boundary kept (conservative)."""
    facts = {
        "targets": [
            {"id": "native:clr:Bridge", "name": "Bridge", "type": "shared_lib",
             "language": "native", "tags": ["clr", "external"]},
            {"id": "cpp:vcxproj:a/Bridge.vcxproj", "name": "Bridge", "type": "shared_lib",
             "language": "cpp", "tags": ["build:msbuild", "interop:cppcli"]},
            {"id": "cpp:vcxproj:b/Bridge.vcxproj", "name": "Bridge", "type": "shared_lib",
             "language": "cpp", "tags": ["build:msbuild", "interop:cppcli"]},
            {"id": "csharp:csproj:a", "name": "A", "type": "csproj", "language": "csharp"},
        ],
        "relationships": [make_relationship("csharp:csproj:a", "native:clr:Bridge",
                          [{"type": "interop", "detail": "x"}])],
    }
    stats = interop_resolve.resolve(facts)
    assert stats["resolved"] == []
    assert stats["unresolved"] == ["native:clr:Bridge"]
    assert any(t["id"] == "native:clr:Bridge" for t in facts["targets"])  # kept


def test_clr_does_not_bind_to_plain_native_target():
    """A native (non-/clr) cpp target sharing the name must NOT satisfy a native:clr boundary."""
    facts = {
        "targets": [
            {"id": "native:clr:Bridge", "name": "Bridge", "type": "shared_lib",
             "language": "native", "tags": ["clr"]},
            # same name but NOT tagged interop:cppcli -> not a managed bridge
            {"id": "cpp:vcxproj:x/Bridge.vcxproj", "name": "Bridge", "type": "shared_lib",
             "language": "cpp", "tags": ["build:msbuild"]},
            {"id": "csharp:csproj:a", "name": "A", "type": "csproj", "language": "csharp"},
        ],
        "relationships": [make_relationship("csharp:csproj:a", "native:clr:Bridge",
                          [{"type": "interop", "detail": "x"}])],
    }
    stats = interop_resolve.resolve(facts)
    assert stats["unresolved"] == ["native:clr:Bridge"]         # left dangling, no false bind


# --- COM resolution --------------------------------------------------------

def test_com_resolves_by_guid_and_progid():
    """com:<guid> and com:<progid> both bind to the native server whose com_provides lists them."""
    facts = _mixed_facts()
    # add a ProgID boundary too
    facts["targets"].append(
        {"id": "com:CalcServer.Object", "name": "CalcServer.Object", "type": "shared_lib",
         "language": "native", "tags": ["com", "external"]})
    facts["relationships"].append(
        make_relationship("csharp:csproj:App/App.csproj", "com:CalcServer.Object",
                          [{"type": "interop", "detail": 'GetTypeFromProgID("CalcServer.Object")'}]))
    stats = interop_resolve.resolve(facts)
    target = "cpp:vcxproj:CalcServer/CalcServer.vcxproj"
    assert f"com:{GUID_CALC} -> {target}" in stats["resolved"]
    assert f"com:CalcServer.Object -> {target}" in stats["resolved"]
    ids = {t["id"] for t in facts["targets"]}
    assert f"com:{GUID_CALC}" not in ids and "com:CalcServer.Object" not in ids
    server = next(t for t in facts["targets"] if t["id"] == target)
    assert "interop:com" in server["tags"]


def test_com_progid_match_is_case_insensitive():
    """A ProgID boundary binds regardless of case (registry ProgIDs are case-insensitive)."""
    facts = {
        "targets": [
            {"id": "com:calcserver.object", "name": "calcserver.object", "type": "shared_lib",
             "language": "native", "tags": ["com"]},
            {"id": "cpp:vcxproj:s/CalcServer.vcxproj", "name": "CalcServer", "type": "shared_lib",
             "language": "cpp", "tags": ["build:msbuild"], "com_provides": ["CalcServer.Object"]},
            {"id": "csharp:csproj:a", "name": "A", "type": "csproj", "language": "csharp"},
        ],
        "relationships": [make_relationship("csharp:csproj:a", "com:calcserver.object",
                          [{"type": "interop", "detail": "x"}])],
    }
    stats = interop_resolve.resolve(facts)
    assert stats["resolved"] == ["com:calcserver.object -> cpp:vcxproj:s/CalcServer.vcxproj"]


def test_com_without_captured_identity_stays_dangling():
    """A com:<id> with no native com_provides match stays dangling — honest, not a crash."""
    facts = {
        "targets": [
            {"id": f"com:{GUID_CALC}", "name": GUID_CALC, "type": "shared_lib",
             "language": "native", "tags": ["com"]},
            # a cpp server exists but its COM identity was never captured (no com_provides)
            {"id": "cpp:vcxproj:s/CalcServer.vcxproj", "name": "CalcServer", "type": "shared_lib",
             "language": "cpp", "tags": ["build:msbuild"]},
            {"id": "csharp:csproj:a", "name": "A", "type": "csproj", "language": "csharp"},
        ],
        "relationships": [make_relationship("csharp:csproj:a", f"com:{GUID_CALC}",
                          [{"type": "interop", "detail": "x"}])],
    }
    stats = interop_resolve.resolve(facts)         # must not raise
    assert stats["resolved"] == []
    assert stats["unresolved"] == [f"com:{GUID_CALC}"]
    assert any(t["id"] == f"com:{GUID_CALC}" for t in facts["targets"])   # boundary kept


def test_com_ambiguous_two_servers_stays_dangling():
    """Two native servers providing the same CLSID -> ambiguous -> boundary kept."""
    facts = {
        "targets": [
            {"id": f"com:{GUID_CALC}", "name": GUID_CALC, "type": "shared_lib",
             "language": "native", "tags": ["com"]},
            {"id": "cpp:vcxproj:a/S.vcxproj", "name": "S1", "type": "shared_lib",
             "language": "cpp", "tags": ["build:msbuild"], "com_provides": [GUID_CALC]},
            {"id": "cpp:vcxproj:b/S.vcxproj", "name": "S2", "type": "shared_lib",
             "language": "cpp", "tags": ["build:msbuild"], "com_provides": [GUID_CALC]},
            {"id": "csharp:csproj:a", "name": "A", "type": "csproj", "language": "csharp"},
        ],
        "relationships": [make_relationship("csharp:csproj:a", f"com:{GUID_CALC}",
                          [{"type": "interop", "detail": "x"}])],
    }
    stats = interop_resolve.resolve(facts)
    assert stats["resolved"] == [] and stats["unresolved"] == [f"com:{GUID_CALC}"]


# --- P/Invoke regression (the existing path stays intact) ------------------

def test_pinvoke_resolution_still_works_in_combined_pass():
    """native:lib:* still binds to its cpp target alongside the new CLR/COM passes (regression)."""
    facts = _mixed_facts()
    interop_resolve.resolve(facts)
    ids = {t["id"] for t in facts["targets"]}
    assert "native:lib:NativeMath" not in ids
    nm = next(t for t in facts["targets"]
              if t["id"] == "cpp:vcxproj:native/NativeMath.vcxproj")
    assert "interop:pinvoke" in nm["tags"]


# --- COM native-side identity capture (extract_msbuild_cpp) -----------------

def test_msbuild_captures_com_provides_from_idl_and_rgs(tmp_path: Path):
    """A .vcxproj whose .idl declares a coclass uuid + .rgs ProgID emits com_provides."""
    frag = extract_msbuild_cpp.build_fragment(INTEROP_MIXED, cache_dir=tmp_path / "cache")
    assert frag is not None
    server = next(t for t in frag["targets"] if t["name"] == "CalcServer")
    assert GUID_CALC in server["com_provides"]            # coclass CLSID from the .idl
    assert "CalcServer.Object" in server["com_provides"]  # ProgID from the .rgs
    # a non-COM target carries no com_provides (byte-identical for non-COM projects)
    nm = next(t for t in frag["targets"] if t["name"] == "NativeMath")
    assert "com_provides" not in nm


def test_idl_without_coclass_captures_no_identity(tmp_path: Path):
    """An interface-only .idl (no coclass) is not a creatable server -> no com_provides."""
    proj = tmp_path / "Iface" / "Iface.vcxproj"
    _write(proj, '<Project><PropertyGroup><ConfigurationType>DynamicLibrary'
                 '</ConfigurationType></PropertyGroup>'
                 '<ItemGroup><Midl Include="Iface.idl"/></ItemGroup></Project>')
    _write(tmp_path / "Iface" / "Iface.idl",
           '[ object, uuid(11111111-2222-3333-4444-555555555555) ]\n'
           'interface IThing : IUnknown { HRESULT Ping(); };\n')
    frag = extract_msbuild_cpp.build_fragment(tmp_path, cache_dir=tmp_path / "cache")
    t = next(t for t in frag["targets"] if t["name"] == "Iface")
    assert "com_provides" not in t


# --- Precision: comment/string-literal awareness ---------------------------

def test_commented_dllimport_mints_no_seam(tmp_path: Path):
    """A commented-out [DllImport] yields NO seam; a real one in the same file still does."""
    repo = tmp_path / "precision"
    _write(repo / "P" / "P.csproj", '<Project Sdk="Microsoft.NET.Sdk"></Project>')
    _write(repo / "P" / "P.cs",
           'using System.Runtime.InteropServices;\n'
           'namespace P;\n'
           'internal static class N {\n'
           '    // [DllImport("PhantomLib")] internal static extern void Ghost();\n'
           '    /* block: [DllImport("AlsoPhantom")] */\n'
           '    [DllImport("RealLib")] internal static extern void Real();\n'
           '}\n')
    frag = extract_interop.build_fragment(repo)
    assert frag is not None
    ids = _ids(frag)
    assert "native:lib:RealLib" in ids
    assert "native:lib:PhantomLib" not in ids       # line-comment token suppressed
    assert "native:lib:AlsoPhantom" not in ids      # block-comment token suppressed


def test_commented_comimport_mints_no_seam(tmp_path: Path):
    """A commented-out / string-literal [ComImport] yields no com boundary; a real one does."""
    repo = tmp_path / "precision-com"
    _write(repo / "P" / "P.csproj", '<Project Sdk="Microsoft.NET.Sdk"></Project>')
    _write(repo / "P" / "Real.cs",
           'using System.Runtime.InteropServices;\n'
           'namespace P;\n'
           '// [ComImport][Guid("aaaaaaaa-0000-0000-0000-000000000001")] phantom\n'
           '[ComImport]\n'
           '[Guid("bbbbbbbb-0000-0000-0000-000000000002")]\n'
           'internal interface IReal {}\n')
    frag = extract_interop.build_fragment(repo)
    assert frag is not None
    ids = _ids(frag)
    assert "com:bbbbbbbb-0000-0000-0000-000000000002" in ids        # real
    assert "com:aaaaaaaa-0000-0000-0000-000000000001" not in ids    # commented phantom


def test_string_literal_mentioning_dllimport_mints_no_seam(tmp_path: Path):
    """A string literal that merely mentions [DllImport] does not mint a seam (precision)."""
    repo = tmp_path / "precision-str"
    _write(repo / "P" / "P.csproj", '<Project Sdk="Microsoft.NET.Sdk"></Project>')
    _write(repo / "P" / "Doc.cs",
           'namespace P;\n'
           'internal static class Doc {\n'
           '    internal const string Help = "old code used [DllImport(\\"Ghost\\")] here";\n'
           '    internal const string V = @"verbatim [ComImport] sample";\n'
           '}\n')
    # no real seam anywhere -> the whole repo is a no-op (no phantom from the doc strings)
    assert extract_interop.build_fragment(repo) is None


def test_commented_cpp_using_mints_no_clr_seam(tmp_path: Path):
    """A commented-out #using <X.dll> in C++ yields no native:clr boundary; a real one does."""
    repo = tmp_path / "precision-clr"
    _write(repo / "B" / "B.vcxproj",
           '<Project><PropertyGroup><CLRSupport>true</CLRSupport></PropertyGroup>'
           '<ItemGroup><ClCompile Include="B.cpp"/></ItemGroup></Project>')
    _write(repo / "B" / "B.cpp",
           '// #using <Phantom.dll>\n'
           '#using <Real.dll>\n'
           'void M() { auto x = gcnew System::Object(); }\n')
    frag = extract_interop.build_fragment(repo)
    assert frag is not None
    ids = _ids(frag)
    assert "native:clr:Real" in ids
    assert "native:clr:Phantom" not in ids


# --- End-to-end on the mixed fixture (extract + normalize) ------------------

def _extract_and_normalize_mixed(tmp_path: Path) -> dict:
    ws = resolve_workspace(INTEROP_MIXED, arch_dir=tmp_path / "architecture")
    extract_build_graph.run(ws)       # csharp:csproj:App
    extract_msbuild_cpp.run(ws)       # cpp:vcxproj:* + com_provides capture
    extract_interop.run(ws)           # native:lib / native:clr / com boundaries
    return normalize_facts.run(ws, repo="interop-mixed", commit="test")


def test_mixed_fixture_resolves_all_three_seams(tmp_path: Path):
    """Full extract+normalize on the mixed fixture binds P/Invoke, C++/CLI and COM seams."""
    facts = _extract_and_normalize_mixed(tmp_path)
    ids = {t["id"] for t in facts["targets"]}
    # every synthetic boundary resolved + dropped
    for boundary in ("native:lib:NativeMath", "native:clr:Bridge",
                     f"com:{GUID_CALC}", "com:CalcServer.Object"):
        assert boundary not in ids, f"{boundary} should have resolved + dropped"
    by_name = {t["name"]: t for t in facts["targets"] if t["id"].startswith("cpp:vcxproj")}
    assert "interop:pinvoke" in by_name["NativeMath"]["tags"]
    assert "interop:cppcli" in by_name["Bridge"]["tags"]
    assert "interop:com" in by_name["CalcServer"]["tags"]
    resolved = facts["provenance"].get("interop_resolved", [])
    assert any("native:clr:Bridge ->" in r for r in resolved)
    assert any(f"com:{GUID_CALC} ->" in r for r in resolved)


def _resolved_hash_for(merged: dict) -> str:
    """content_hash of *merged* after resolve() — content_hash strips volatile provenance."""
    interop_resolve.resolve(merged)
    return content_hash(canonicalize(merged))


def test_mixed_fixture_resolution_is_deterministic_under_reorder(tmp_path: Path):
    """resolve() over reordered targets/relationships is byte-identical (content_hash).

    content_hash strips volatile provenance (generated_at/commit/interop_resolved), so this
    isolates the *structural* effect of resolution: reversing the merged inputs must not change
    the resolved targets/relationships/evidence."""
    ws_a = resolve_workspace(INTEROP_MIXED, arch_dir=tmp_path / "a")
    extract_build_graph.run(ws_a)
    extract_msbuild_cpp.run(ws_a)
    extract_interop.run(ws_a)
    merged_a = normalize_facts.merge(normalize_facts.load_fragments(ws_a))

    ws_b = resolve_workspace(INTEROP_MIXED, arch_dir=tmp_path / "b")
    extract_build_graph.run(ws_b)
    extract_msbuild_cpp.run(ws_b)
    extract_interop.run(ws_b)
    merged_b = normalize_facts.merge(normalize_facts.load_fragments(ws_b))
    merged_b["targets"] = list(reversed(merged_b["targets"]))
    merged_b["relationships"] = list(reversed(merged_b["relationships"]))

    assert _resolved_hash_for(merged_a) == _resolved_hash_for(merged_b)


def test_mixed_fixture_fragment_validates_against_schema(tmp_path: Path):
    """The normalized mixed-fixture facts (with com_provides) validate against the schema."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    facts = _extract_and_normalize_mixed(tmp_path)
    jsonschema.validate(instance=facts, schema=schema)
