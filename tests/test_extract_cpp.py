"""C++ extraction-vertical tests (plan §4.1): the CMake File-API + graphviz L0/L1 parse
(``cmake_api`` / ``extract_cmake``) and the Doxygen L2/L3 refine layer (``extract_cpp_facts``).

Two tiers, mirroring ``test_extract.py``'s Roslyn split:
  * **Pure-parser tests** (always run) — feed hand-written graphviz/Doxygen XML to the
    parsers; no ``cmake``/``doxygen`` needed, so the parse logic is covered everywhere.
  * **Integration tests** (gated) — actually *configure* the in-repo ``cpp-toy`` CMake
    fixture and run Doxygen over it, then assert the extracted facts. Skipped cleanly when
    cmake/ninja/doxygen/a compiler are unavailable (plan §18.4/§19.5).

Self-contained: every write goes to pytest ``tmp_path`` — never the fixture's own tree.

    .venv/Scripts/python.exe -m pytest tests/test_extract_cpp.py -q
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from anon import cmake_api
from anon.jsonio import load_json
from anon.paths import resolve_workspace
from anon.stages import (apply_mapping_rules, extract_cmake, extract_cpp_facts,
                              generate_structurizr, normalize_facts, validate_facts)

REPO_ROOT = Path(__file__).resolve().parents[1]
CPP_TOY = REPO_ROOT / "tests" / "fixtures" / "cpp-toy-repo"

CPP_TARGET_IDS = {
    "cpp:target:common",
    "cpp:target:messaging",
    "cpp:target:pricing",
    "cpp:target:order_processor",
    "cpp:target:order_tests",
}


# ======================================================================================
# Pure-parser tests — no cmake/doxygen required
# ======================================================================================

# A trimmed CMake graphviz with the legend block + 3 first-party nodes (interface/static/
# executable) and edges of each visibility style. The legend must be stripped.
_SAMPLE_DOT = '''digraph "Sample" {
node [ fontsize = "12" ];
subgraph clusterLegend {
  label = "Legend";
  legendNode0 [ label = "Executable", shape = egg ];
  legendNode4 [ label = "Interface Library", shape = pentagon ];
  legendNode1 -> legendNode4 [ label = "Interface", style = dashed ];
  legendNode2 -> legendNode5 [ label = "Private", style = dotted ];
}
    "node0" [ label = "common", shape = pentagon ];
    "node1" [ label = "lib", shape = octagon ];
    "node1" -> "node0"  // lib -> common
    "node2" [ label = "app", shape = egg ];
    "node2" -> "node1" [ style = dotted ] // app -> lib
    "node3" [ label = "extlib", shape = septagon ];
    "node2" -> "node3" [ style = dashed ] // app -> extlib
}
'''


def test_graphviz_parse_nodes_edges_and_legend_strip(tmp_path: Path):
    dot = tmp_path / "g.dot"
    dot.write_text(_SAMPLE_DOT, encoding="utf-8")
    nodes, edges = cmake_api.parse_graphviz(dot)

    # legend nodes never leak; the 4 real nodes are typed by shape.
    assert set(nodes) == {"common", "lib", "app", "extlib"}
    assert nodes["common"][1] == "interface_lib"   # pentagon
    assert nodes["lib"][1] == "static_lib"          # octagon
    assert nodes["app"][1] == "executable"          # egg
    assert nodes["extlib"][1] == "external"         # septagon -> external

    by_pair = {(e.source, e.target): e.visibility for e in edges}
    assert by_pair[("lib", "common")] == "public"   # no style -> solid -> public
    assert by_pair[("app", "lib")] == "private"      # dotted
    assert by_pair[("app", "extlib")] == "interface" # dashed
    # no legend edges
    assert not any(e.source.startswith("legend") for e in edges)


def test_overlay_graphviz_drops_external_and_recovers_interface(tmp_path: Path):
    """A graphviz-only node typed `external` (septagon) is dropped; an interface-lib node
    the File API omitted is recovered as a first-party target (§4.1)."""
    dot = tmp_path / "anon.dot"
    dot.write_text(_SAMPLE_DOT, encoding="utf-8")
    model = cmake_api.CMakeModel()
    # Pretend the File API found only `lib` and `app` (interface `common` omitted).
    model.targets["lib"] = cmake_api.CMakeTarget(name="lib", type="static_lib")
    model.targets["app"] = cmake_api.CMakeTarget(name="app", type="executable")
    cmake_api.overlay_graphviz(model, tmp_path)

    assert "common" in model.targets          # interface lib recovered
    assert model.targets["common"].type == "interface_lib"
    assert "extlib" not in model.targets       # external/import dropped
    # edges now carry graphviz visibility; the app->extlib edge is dropped (extlib not a target)
    pairs = {(e.source, e.target): e.visibility for e in model.edges}
    assert pairs[("app", "lib")] == "private"
    assert pairs[("lib", "common")] == "public"
    assert ("app", "extlib") not in pairs


def test_ctest_dashboard_targets_filtered_from_fragment():
    """The fixed CTest/CDash dashboard UTILITY set (`include(CTest)`: Experimental/Nightly/
    Continuous + step sub-targets) is dropped at extraction — targets, coverage rows, AND
    any edge touching one — so build-tree ceremony never leaks into grouping signals (the
    "x64" proposed container holding NightlyTest/ExperimentalUpdate)."""
    model = cmake_api.CMakeModel()
    model.targets["lib"] = cmake_api.CMakeTarget(name="lib", type="static_lib",
                                                 source_dir="src/lib")
    for name in ("Nightly", "NightlyTest", "ExperimentalUpdate", "Continuous",
                 "NightlyMemoryCheck"):
        # UTILITY targets map to type "unknown"; an in-repo build dir gives them a
        # repo-relative build-tree path — the leak the filter closes.
        model.targets[name] = cmake_api.CMakeTarget(name=name, type="unknown",
                                                    source_dir="x64/CMakeFiles")
    model.edges.append(cmake_api.CMakeEdge(source="NightlyTest", target="lib",
                                           visibility="public"))
    frag = extract_cmake.fragment_from_model(model)

    ids = {t["id"] for t in frag["targets"]}
    assert ids == {"cpp:target:lib"}, "only the real target survives"
    assert set(frag["provenance"]["coverage"]) == {"cpp:target:lib"}
    assert frag["relationships"] == [], "edges touching a dropped target go with it"


def test_ctest_lookalike_non_utility_target_is_kept():
    """The filter is gated on the UTILITY type: a real compiled target that shares a
    dashboard name (impossible in practice — CMake refuses duplicates — but the gate is
    the safety) is NOT dropped; nor is any other utility target with a different name."""
    model = cmake_api.CMakeModel()
    model.targets["Nightly"] = cmake_api.CMakeTarget(name="Nightly", type="executable",
                                                     source_dir="src/nightly")
    model.targets["docs"] = cmake_api.CMakeTarget(name="docs", type="unknown",
                                                  source_dir="doc")
    frag = extract_cmake.fragment_from_model(model)
    ids = {t["id"] for t in frag["targets"]}
    assert ids == {"cpp:target:Nightly", "cpp:target:docs"}


# Minimal Doxygen XML: one file that includes another, both attributed to different targets,
# plus a class compound carrying a namespace (-> component + code tier).
def _write_doxygen_xml(xml_dir: Path) -> None:
    xml_dir.mkdir(parents=True, exist_ok=True)
    (xml_dir / "index.xml").write_text(
        '<doxygenindex>'
        '<compound refid="file_a" kind="file"><name>A.cpp</name></compound>'
        '<compound refid="file_b" kind="file"><name>B.hpp</name></compound>'
        '<compound refid="class_pub" kind="class"><name>ns::Publisher</name></compound>'
        '</doxygenindex>', encoding="utf-8")
    (xml_dir / "file_a.xml").write_text(
        '<doxygen><compounddef kind="file">'
        '<compoundname>A.cpp</compoundname>'
        '<includes refid="file_b" local="yes">b/B.hpp</includes>'
        '<location file="a/A.cpp"/>'
        '</compounddef></doxygen>', encoding="utf-8")
    (xml_dir / "file_b.xml").write_text(
        '<doxygen><compounddef kind="file">'
        '<compoundname>B.hpp</compoundname>'
        '<location file="b/B.hpp"/>'
        '</compounddef></doxygen>', encoding="utf-8")
    (xml_dir / "class_pub.xml").write_text(
        '<doxygen><compounddef kind="class">'
        '<compoundname>ns::Publisher</compoundname>'
        '<location file="a/Publisher.hpp"/>'
        '</compounddef></doxygen>', encoding="utf-8")


def test_parse_doxygen_components_and_include_edges(tmp_path: Path):
    xml_dir = tmp_path / "xml"
    _write_doxygen_xml(xml_dir)
    model = cmake_api.CMakeModel()
    model.targets["aTarget"] = cmake_api.CMakeTarget(name="aTarget", type="static_lib",
                                                     source_dir="a", sources=["a/A.cpp"])
    model.targets["bTarget"] = cmake_api.CMakeTarget(name="bTarget", type="static_lib",
                                                     source_dir="b", sources=["b/B.hpp"])
    frag, sidecar = extract_cpp_facts.parse_doxygen(xml_dir, model, tmp_path)

    # include edge a -> b (A.cpp includes b/B.hpp; different targets)
    rels = {(r["source"], r["target"]): r for r in frag["relationships"]}
    assert ("cpp:target:aTarget", "cpp:target:bTarget") in rels
    assert rels[("cpp:target:aTarget", "cpp:target:bTarget")]["evidence"][0]["type"] == "include"

    # component tier: ns::Publisher in a/Publisher.hpp attributes to aTarget (dir prefix)
    a = next(t for t in frag["targets"] if t["id"] == "cpp:target:aTarget")
    comps = {c["key"]: c for c in a.get("components", [])}
    assert "ns" in comps
    assert comps["ns"]["source"] == "namespace"
    assert {c["name"] for c in comps["ns"]["code"]} == {"Publisher"}

    # eval §4.1 per-file sidecar: the pre-aggregation file→file edge + attribution
    assert sidecar["schema"] == "file-deps/1"
    assert sidecar["files"] == {"a/A.cpp": "cpp:target:aTarget",
                                "b/B.hpp": "cpp:target:bTarget"}
    assert sidecar["edges"] == [["a/A.cpp", "b/B.hpp"]]


def test_run_degrades_gracefully_on_non_cmake_repo(tmp_path: Path):
    """A repo with no CMakeLists yields no C++ facts (returns None, never raises)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    ws = resolve_workspace(empty, arch_dir=tmp_path / "architecture")
    assert extract_cmake.run(ws) is None
    assert extract_cpp_facts.run(ws) is None


# ======================================================================================
# Integration tests — gated on a working CMake + Ninja + Doxygen + compiler toolchain
# ======================================================================================

def _compiler_env() -> dict[str, str] | None:
    """Return env overrides that give CMake a working compiler, or None if none found.

    On a box with MSVC (`cl`) on PATH, no override is needed. Otherwise prefer MinGW g++/gcc,
    which CMake honors via the CC/CXX environment variables (plan §19.5 build-env caveat)."""
    if shutil.which("cl"):
        return {}
    if shutil.which("g++") and shutil.which("gcc"):
        return {"CXX": "g++", "CC": "gcc"}
    return None


_TOOLCHAIN = (
    cmake_api.cmake_available()
    and shutil.which("ninja") is not None
    and _compiler_env() is not None
)
def _integration(mark):
    """Compose the `integration` marker with a skip mark — both attach to the test.

    Every C++ L0/L2/L3 test needs a real cmake/doxygen toolchain, so it is `integration`
    (slow, deselected by default) *and* skipped when the toolchain is absent."""
    return lambda f: pytest.mark.integration(mark(f))


_skip_no_cmake = _integration(pytest.mark.skipif(
    not _TOOLCHAIN, reason="cmake/ninja/compiler unavailable — C++ L0 integration skipped (§19.5)"))
_skip_no_doxygen = _integration(pytest.mark.skipif(
    not (_TOOLCHAIN and extract_cpp_facts.doxygen_available()),
    reason="cmake/doxygen unavailable — C++ L2/L3 integration skipped (§19.5)"))


@pytest.fixture
def cpp_env(monkeypatch):
    for k, v in (_compiler_env() or {}).items():
        monkeypatch.setenv(k, v)
    return None


@_skip_no_cmake
def test_cmake_l0_extracts_five_targets_with_visibility(tmp_path: Path, cpp_env):
    ws = resolve_workspace(CPP_TOY, arch_dir=tmp_path / "architecture")
    out = extract_cmake.run(ws)
    assert out is not None and out.exists()
    frag = load_json(out)

    assert {t["id"] for t in frag["targets"]} == CPP_TARGET_IDS
    by_id = {t["id"]: t for t in frag["targets"]}
    assert by_id["cpp:target:common"]["type"] == "interface_lib"   # recovered from graphviz
    assert by_id["cpp:target:messaging"]["type"] == "static_lib"
    assert by_id["cpp:target:order_processor"]["type"] == "executable"
    for t in frag["targets"]:
        assert t["language"] == "cpp" and "build:cmake" in t["tags"]

    # visibility carried on link evidence: order_processor links its libs PRIVATE.
    vis = {}
    for r in frag["relationships"]:
        ev = r["evidence"][0]
        vis[(r["source"], r["target"])] = ev.get("visibility")
        assert ev["type"] == "link" and r["is_declared_dependency"] is True
    assert vis[("cpp:target:order_processor", "cpp:target:messaging")] == "private"
    assert vis[("cpp:target:messaging", "cpp:target:common")] == "public"


@_skip_no_cmake
def test_cmake_l1_compile_db_coverage(tmp_path: Path, cpp_env):
    """The Ninja generator emits compile_commands.json -> L1 coverage flips true (§4.1)."""
    ws = resolve_workspace(CPP_TOY, arch_dir=tmp_path / "architecture")
    frag = load_json(extract_cmake.run(ws))
    cov = frag["provenance"]["coverage"]["cpp:target:messaging"]
    assert cov["L0"] is True and cov["L1"] is True


@_skip_no_cmake
def test_cmake_l0_is_deterministic(tmp_path: Path, cpp_env):
    from anon.jsonio import dumps_json
    from anon.model import canonicalize
    a = load_json(extract_cmake.run(resolve_workspace(CPP_TOY, arch_dir=tmp_path / "a")))
    b = load_json(extract_cmake.run(resolve_workspace(CPP_TOY, arch_dir=tmp_path / "b")))
    assert dumps_json(canonicalize(a)) == dumps_json(canonicalize(b))


@_skip_no_doxygen
def test_doxygen_l2_components_and_include_corroboration(tmp_path: Path, cpp_env):
    """Doxygen attributes namespaces to targets (component tier) and emits include edges that
    merge onto the L0 link edges -> corroborated confidence (§4.1/§6.4)."""
    ws = resolve_workspace(CPP_TOY, arch_dir=tmp_path / "architecture")
    extract_cmake.run(ws)
    out = extract_cpp_facts.run(ws)
    assert out is not None and out.exists()

    merged = normalize_facts.merge(normalize_facts.load_fragments(ws))
    rels = {(r["source"], r["target"]): r for r in merged["relationships"]}
    op_msg = rels[("cpp:target:order_processor", "cpp:target:messaging")]
    kinds = {e["type"] for e in op_msg["evidence"]}
    assert {"link", "include"} <= kinds              # L0 + L2 corroborate
    assert op_msg["is_declared_dependency"] is True

    msg = next(t for t in merged["targets"] if t["id"] == "cpp:target:messaging")
    comp_keys = {c["key"] for c in msg.get("components", [])}
    assert "company::messaging" in comp_keys


@_skip_no_doxygen
def test_cpp_full_pipeline_curate_and_generate(tmp_path: Path, cpp_env):
    """End-to-end on the cpp-toy fixture with its committed mapping-rules: extract -> curate
    -> generate a valid, in-budget model (§7/§9)."""
    rules_dir = CPP_TOY / "architecture" / "rules"
    ws = resolve_workspace(CPP_TOY, arch_dir=tmp_path / "architecture", rules_dir=rules_dir)
    extract_cmake.run(ws)
    extract_cpp_facts.run(ws)
    from datetime import datetime, timezone
    facts = normalize_facts.run(ws, repo="cpp-toy", commit="test",
                                generated_at=datetime.now(timezone.utc).isoformat())
    validate_facts.validate(facts)
    curated = apply_mapping_rules.run(ws)
    validate_facts.validate(curated)
    gen = generate_structurizr.run(ws)

    # order_tests excluded by the rules; 4 curated containers remain.
    container_names = {t["container_name"] for t in curated["targets"]}
    assert "Order Processor" in container_names      # name_overrides applied
    assert not any("order_tests" in t["id"] for t in curated["targets"])
    assert gen["over_budget_views"] == []            # readability GATE met (§1.1#1)
    assert gen["containers"] == 4


# --------------------------------------------------------------------------------------
# CMakePresets configure-preset resolution (pure; no cmake) — projects that only
# configure through a preset (datasheet/onboarding §4.1). ANON_CMAKE_PRESET picks one
# and extract_cmake configures into the preset's binaryDir.

def test_resolve_preset_binary_dir(tmp_path):
    import json
    (tmp_path / "CMakePresets.json").write_text(json.dumps({
        "version": 3,
        "configurePresets": [
            {"name": "base", "hidden": True, "generator": "Ninja",
             "binaryDir": "${sourceDir}/out/${presetName}"},
            {"name": "win-x64", "inherits": ["base"],
             "binaryDir": "${sourceDir}/out/x64/Debug"},
            {"name": "inherits-bdir", "inherits": ["base"]},
            {"name": "no-bdir-anywhere"},
        ],
    }), encoding="utf-8")
    # explicit binaryDir + ${sourceDir} expansion
    assert cmake_api.resolve_preset_binary_dir(tmp_path, "win-x64") == \
        tmp_path / "out" / "x64" / "Debug"
    # inherited binaryDir, with ${presetName} = the INVOKED preset, not the parent
    assert cmake_api.resolve_preset_binary_dir(tmp_path, "inherits-bdir") == \
        tmp_path / "out" / "inherits-bdir"
    # no binaryDir in the chain -> CMake's default ${sourceDir}/build/${presetName}
    assert cmake_api.resolve_preset_binary_dir(tmp_path, "no-bdir-anywhere") == \
        tmp_path / "build" / "no-bdir-anywhere"
    # unknown preset -> None (caller degrades to the bare-configure path)
    assert cmake_api.resolve_preset_binary_dir(tmp_path, "ghost") is None


def test_resolve_preset_binary_dir_no_preset_file(tmp_path):
    # no CMakePresets.json at all -> None (not a preset project)
    assert cmake_api.resolve_preset_binary_dir(tmp_path, "anything") is None
