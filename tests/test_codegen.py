"""Build-time codegen detection tests (plan §4.1 / §16#14): when a configure-only CMake run
declares codegen (``add_custom_command`` / UTILITY / protobuf / moc / flatc) whose generated
**output source files are ABSENT** on disk, the owning target must be tagged ``partial:codegen``
so drift (§11.1) annotates rather than reporting phantom edges — and the generator target itself
is KEPT (only the missing generated *content* is flagged, §16#14 iv).

Pure-parser tests (always run) — they build a synthetic File-API ``codemodel-v2`` reply / a
``CMakeModel`` directly, mirroring ``tests/test_extract_cpp.py`` and
``tests/test_deferred_items.py``; no real cmake/ninja toolchain is required. The integration
test is gated behind the same skipif pattern those files use.

Determinism guard (the load-bearing invariant, CLAUDE.md): a target whose generated outputs
ARE present — and any non-codegen target — gets NO ``partial:codegen`` tag and NO coverage
perturbation, so existing C++ fixtures/goldens stay byte-identical.

    .venv/Scripts/python.exe -m pytest tests/test_codegen.py -q
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from anon import cmake_api
from anon.stages import extract_cmake


# ======================================================================================
# Helpers — assemble facts via the real extract_cmake fragment builder over a CMakeModel
# ======================================================================================

def _fragment(model: cmake_api.CMakeModel) -> dict:
    return extract_cmake.fragment_from_model(model)


def _by_id(frag: dict) -> dict[str, dict]:
    return {t["id"]: t for t in frag["targets"]}


# ======================================================================================
# Pure tests over a hand-built CMakeModel — the core tag/coverage/provenance contract
# ======================================================================================

def test_absent_codegen_source_tags_partial_codegen():
    """A custom-command target whose generated output is absent → tagged partial:codegen,
    L2/L3 coverage marked partial, and recorded in provenance.partial_codegen (§4.1/§16#14)."""
    model = cmake_api.CMakeModel()
    model.targets["protos"] = cmake_api.CMakeTarget(
        name="protos", type="static_lib", source_dir="gen",
        sources=["gen/foo.pb.cc"],
        has_codegen=True, generated_sources_absent=True,
        generated_sources=["gen/foo.pb.cc"],
    )
    frag = _fragment(model)

    t = _by_id(frag)["cpp:target:protos"]
    assert "partial:codegen" in t["tags"]
    # the generator target is KEPT, not dropped (§16#14 ii)
    assert t["id"] == "cpp:target:protos" and t["type"] == "static_lib"

    cov = frag["provenance"]["coverage"]["cpp:target:protos"]
    assert cov["L0"] is True                 # the target-graph node survives configure-only
    assert cov["L2"] == "partial" and cov["L3"] == "partial"
    assert frag["provenance"]["partial_codegen"] == ["cpp:target:protos"]


def test_present_codegen_source_is_not_tagged():
    """has_codegen but outputs PRESENT on disk (generated_sources_absent False) → normal: NO
    tag, NO coverage perturbation, NO provenance key (byte-identical determinism guard)."""
    model = cmake_api.CMakeModel()
    model.targets["protos"] = cmake_api.CMakeTarget(
        name="protos", type="static_lib", source_dir="gen",
        sources=["gen/foo.pb.cc"],
        has_codegen=True, generated_sources_absent=False,
        generated_sources=["gen/foo.pb.cc"],
    )
    frag = _fragment(model)

    t = _by_id(frag)["cpp:target:protos"]
    assert "partial:codegen" not in t["tags"]
    assert t["tags"] == ["build:cmake"]
    cov = frag["provenance"]["coverage"]["cpp:target:protos"]
    assert cov["L2"] is False and cov["L3"] is False
    assert "partial_codegen" not in frag["provenance"]


def test_plain_target_is_unchanged_noop():
    """A plain (non-codegen) target is byte-identical to the pre-feature output: tags exactly
    ['build:cmake'], coverage L2/L3 False, no provenance.partial_codegen key."""
    model = cmake_api.CMakeModel()
    model.targets["core"] = cmake_api.CMakeTarget(
        name="core", type="static_lib", source_dir="src", sources=["src/core.cpp"])
    frag = _fragment(model)

    t = _by_id(frag)["cpp:target:core"]
    assert t["tags"] == ["build:cmake"]
    cov = frag["provenance"]["coverage"]["cpp:target:core"]
    assert cov == {"L0": True, "L1": False, "L2": False, "L3": False}
    assert "partial_codegen" not in frag["provenance"]


def test_utility_custom_target_with_absent_output_is_partial():
    """A UTILITY (custom) target that produced no on-disk source is conservatively partial."""
    model = cmake_api.CMakeModel()
    model.targets["run_codegen"] = cmake_api.CMakeTarget(
        name="run_codegen", type="unknown",   # _TYPE_MAP maps UTILITY -> unknown
        has_codegen=True, generated_sources_absent=True)
    frag = _fragment(model)
    t = _by_id(frag)["cpp:target:run_codegen"]
    assert "partial:codegen" in t["tags"]
    assert frag["provenance"]["partial_codegen"] == ["cpp:target:run_codegen"]


def test_partial_codegen_tag_is_sorted_and_deduped():
    """tags come out sorted(set(...)) — 'build:cmake' < 'partial:codegen' deterministically."""
    model = cmake_api.CMakeModel()
    model.targets["z"] = cmake_api.CMakeTarget(
        name="z", type="executable",
        has_codegen=True, generated_sources_absent=True)
    tags = _by_id(_fragment(model))["cpp:target:z"]["tags"]
    assert tags == ["build:cmake", "partial:codegen"]      # sorted, no dups


def test_fragment_is_deterministic_with_codegen():
    """Two independent fragment builds over the same model are byte-identical (§18.4)."""
    from anon.jsonio import dumps_json
    from anon.model import canonicalize

    def mk() -> cmake_api.CMakeModel:
        m = cmake_api.CMakeModel()
        m.targets["b"] = cmake_api.CMakeTarget(name="b", type="static_lib",
                                               has_codegen=True, generated_sources_absent=True)
        m.targets["a"] = cmake_api.CMakeTarget(name="a", type="static_lib",
                                               sources=["a/a.cpp"])
        return m

    assert dumps_json(canonicalize(_fragment(mk()))) == dumps_json(canonicalize(_fragment(mk())))


# ======================================================================================
# Pure parser test — File API codemodel: isGenerated + on-disk absence → flags on CMakeTarget
# ======================================================================================

def _write_codemodel_reply(build_dir: Path, repo: Path, *,
                           gen_path: str, gen_on_disk: bool) -> None:
    """Write a minimal File-API codemodel-v2 reply for one static-lib target with a single
    ``isGenerated`` source at *gen_path* (repo-relative). When *gen_on_disk* the file is
    created so it resolves as PRESENT; otherwise it stays absent (configure-only reality)."""
    reply = build_dir / cmake_api._REPLY_REL
    reply.mkdir(parents=True, exist_ok=True)

    # the index points at a codemodel object file
    (reply / "index-0.json").write_text(json.dumps({
        "objects": [{"kind": "codemodel", "jsonFile": "codemodel-v2-0.json"}],
    }), encoding="utf-8")
    (reply / "codemodel-v2-0.json").write_text(json.dumps({
        "paths": {"source": str(repo)},
        "configurations": [{"targets": [{"name": "protos", "jsonFile": "target-protos.json"}]}],
    }), encoding="utf-8")
    (reply / "target-protos.json").write_text(json.dumps({
        "name": "protos", "id": "protos::@abc", "type": "STATIC_LIBRARY",
        "sources": [
            {"path": "src/hand.cpp"},                          # normal hand-written source
            {"path": gen_path, "isGenerated": True},           # add_custom_command output
        ],
        "dependencies": [],
    }), encoding="utf-8")

    # the hand-written source always exists; the generated one only when requested
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "hand.cpp").write_text("// hand\n", encoding="utf-8")
    if gen_on_disk:
        gp = repo / gen_path
        gp.parent.mkdir(parents=True, exist_ok=True)
        gp.write_text("// generated\n", encoding="utf-8")


def test_parse_file_api_flags_absent_generated_source(tmp_path: Path):
    """File API parse: an isGenerated source ABSENT on disk → has_codegen &
    generated_sources_absent True; the extractor then tags it partial:codegen."""
    repo = tmp_path / "repo"
    build = tmp_path / "build"
    _write_codemodel_reply(build, repo, gen_path="gen/foo.pb.cc", gen_on_disk=False)

    model = cmake_api.parse_file_api(build, repo)
    assert model is not None
    t = model.targets["protos"]
    assert t.has_codegen is True
    assert t.generated_sources_absent is True
    assert t.generated_sources == ["gen/foo.pb.cc"]

    frag = _fragment(model)
    assert "partial:codegen" in _by_id(frag)["cpp:target:protos"]["tags"]


def test_parse_file_api_present_generated_source_is_not_partial(tmp_path: Path):
    """Same target but the generated source EXISTS on disk → has_codegen True but
    generated_sources_absent False → NO partial:codegen tag (no-op determinism)."""
    repo = tmp_path / "repo"
    build = tmp_path / "build"
    _write_codemodel_reply(build, repo, gen_path="gen/foo.pb.cc", gen_on_disk=True)

    model = cmake_api.parse_file_api(build, repo)
    assert model is not None
    t = model.targets["protos"]
    assert t.has_codegen is True
    assert t.generated_sources_absent is False

    frag = _fragment(model)
    assert "partial:codegen" not in _by_id(frag)["cpp:target:protos"]["tags"]


def test_parse_file_api_no_generated_sources_is_clean(tmp_path: Path):
    """A target with only hand-written sources → has_codegen False, no flags (byte-identical
    to the pre-feature parse for ordinary repos)."""
    repo = tmp_path / "repo"
    build = tmp_path / "build"
    reply = build / cmake_api._REPLY_REL
    reply.mkdir(parents=True, exist_ok=True)
    (reply / "index-0.json").write_text(json.dumps({
        "objects": [{"kind": "codemodel", "jsonFile": "cm.json"}]}), encoding="utf-8")
    (reply / "cm.json").write_text(json.dumps({
        "paths": {"source": str(repo)},
        "configurations": [{"targets": [{"name": "core", "jsonFile": "t.json"}]}]}),
        encoding="utf-8")
    (reply / "t.json").write_text(json.dumps({
        "name": "core", "type": "STATIC_LIBRARY",
        "sources": [{"path": "src/core.cpp"}], "dependencies": []}), encoding="utf-8")

    model = cmake_api.parse_file_api(build, repo)
    assert model is not None
    t = model.targets["core"]
    assert t.has_codegen is False and t.generated_sources_absent is False
    assert _by_id(_fragment(model))["cpp:target:core"]["tags"] == ["build:cmake"]


# ======================================================================================
# Integration test — gated on a real cmake/ninja/compiler toolchain (§19.5), same pattern
# as test_extract_cpp.py / test_deferred_items.py. Skips cleanly when unavailable.
# ======================================================================================

def _compiler_env() -> dict[str, str] | None:
    if shutil.which("cl"):
        return {}
    if shutil.which("g++") and shutil.which("gcc"):
        return {"CXX": "g++", "CC": "gcc"}
    return None


_TOOLCHAIN = (cmake_api.cmake_available()
              and shutil.which("ninja") is not None
              and _compiler_env() is not None)


@pytest.mark.skipif(not _TOOLCHAIN,
                    reason="cmake/ninja/compiler unavailable — codegen integration skipped (§19.5)")
def test_configure_only_codegen_target_is_tagged_partial(tmp_path: Path, monkeypatch):
    """Configure (no build) a tiny project whose target consumes an add_custom_command output
    that does NOT exist yet → the configure-only run tags it partial:codegen (§4.1/§16#14)."""
    for k, v in (_compiler_env() or {}).items():
        monkeypatch.setenv(k, v)

    repo = tmp_path / "repo"
    repo.mkdir()
    # gen.hpp is produced at BUILD time by a custom command; absent during configure-only.
    (repo / "main.cpp").write_text('#include "gen.hpp"\nint main(){return G;}\n', encoding="utf-8")
    (repo / "CMakeLists.txt").write_text(
        'cmake_minimum_required(VERSION 3.20)\n'
        'project(codegen_toy CXX)\n'
        'add_custom_command(\n'
        '  OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/gen.hpp\n'
        '  COMMAND ${CMAKE_COMMAND} -E echo "#define G 1" > '
        '${CMAKE_CURRENT_BINARY_DIR}/gen.hpp)\n'
        'add_executable(app main.cpp ${CMAKE_CURRENT_BINARY_DIR}/gen.hpp)\n'
        'target_include_directories(app PRIVATE ${CMAKE_CURRENT_BINARY_DIR})\n',
        encoding="utf-8")

    build = tmp_path / "build"
    frag = extract_cmake.build_fragment(repo, build)
    assert frag is not None
    app = next(t for t in frag["targets"] if t["id"] == "cpp:target:app")
    # the generated gen.hpp was never built → app is flagged partial:codegen, but kept.
    assert "partial:codegen" in app["tags"]
    assert frag["provenance"]["coverage"]["cpp:target:app"]["L2"] == "partial"
