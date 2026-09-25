"""Stage 1 — C++ L0/L1 build-graph extraction via the CMake File API (plan §4.1, node 1).

Emits the C++ backbone fact *fragment* (plan §4.3): one ``cpp:target:<name>`` per CMake
target, with ``link`` evidence edges (carrying PUBLIC/PRIVATE/INTERFACE visibility, §6.2)
derived from the deterministic hybrid File-API + graphviz parse in :mod:`anon.cmake_api`.
This is the C++ analogue of the C# ``extract_build_graph`` (.sln/.csproj L0) and is the
*highest-signal* layer — already at container altitude (§4.1).

  * **L0 target graph** — node per CMake target (type-mapped: executable / static_lib /
    shared_lib / interface_lib / object_lib / unknown), edge per ``target_link_libraries``
    dependency. Header-only INTERFACE libs (absent from the File API) are recovered from the
    graphviz overlay; edge visibility comes from the graphviz edge style (§4.1).
  * **L1 compile DB** — presence of ``compile_commands.json`` (Ninja-generator export, §4.1)
    flips the per-target ``L1`` coverage flag; per-target source counts land in ``metrics``.
  * **Build-time codegen (§4.1 / §16#14)** — a target whose ``add_custom_command``/UTILITY
    generated sources are ABSENT during the configure-only run is tagged ``partial:codegen``
    (the generator target itself is KEPT; only the missing generated *content* is flagged) and
    has its L2/L3 coverage marked ``partial`` so drift (§11.1) annotates rather than reporting
    phantom edges. Targets whose generated outputs ARE present on disk are untouched.
  * **CTest/CDash dashboard targets are filtered** — the fixed UTILITY set ``include(CTest)``
    defines (``Experimental``/``Nightly``/``Continuous`` + their step sub-targets) is pure
    test-orchestration ceremony, never architecture, and with an in-repo build dir its
    build-tree paths leak into grouping signals (a proposed "x64" container holding
    ``NightlyTest``). See :data:`_CTEST_DASHBOARD_TARGETS`.

Stable ids (plan §6.3): ``cpp:target:<bare-cmake-target-name>`` — bare CMake target names
are unique within a project, so they are the natural content-independent key (NOT a slug,
NOT a path hash). Display name lives in ``name``.

Configure-only (plan §18.3): the extractor drives a ``cmake`` *configure* (no build) into a
gitignored cache build dir; reruns reuse it. Graceful degradation (plan §18.4): a non-CMake
repo, a missing ``cmake``, or a failed configure yields ``None`` and writes nothing, so the
rest of the run proceeds (and a C#-only repo stays byte-identical to a run without this stage).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..cmake_api import (CMakeModel, build_model, find_cmake_root,
                         resolve_preset_binary_dir)
from ..jsonio import dump_json
from ..model import make_relationship
from ..paths import Workspace

log = logging.getLogger("anon.extract_cmake")

EXTRACTOR = {"name": "cmake-graph", "version": "0.2.0"}  # 0.2.0: CTest dashboard filter

# The fixed CTest/CDash dashboard target set `include(CTest)` defines (CMake's
# CTestTargets.cmake): Experimental/Nightly/Continuous, each with Start/Update/Configure/
# Build/Test/Coverage/MemCheck/Submit step sub-targets, plus the legacy NightlyMemoryCheck
# alias. Pure test-orchestration ceremony — never part of the product build graph — and
# with an IN-repo build dir (a CMakePresets binaryDir under the source tree) their
# build-tree source dirs stay repo-relative, so they leak into path-based grouping signals
# (the proposed "x64" container holding NightlyTest/ExperimentalUpdate). Filtered at
# extraction, gated on the UTILITY type (mapped "unknown") so a real compiled target could
# never be dropped by name alone (CMake itself refuses a duplicate target name anyway).
# The name set is CLOSED and versioned with CMake's CTest module — deterministic.
_CTEST_DASHBOARD_TARGETS = frozenset(
    f"{mode}{step}"
    for mode in ("Experimental", "Nightly", "Continuous")
    for step in ("", "Start", "Update", "Configure", "Build", "Test",
                 "Coverage", "MemCheck", "Submit")
) | {"NightlyMemoryCheck"}


def _target_id(name: str) -> str:
    return f"cpp:target:{name}"


def _visibility_detail(src: str, dst: str, visibility: str) -> str:
    """A literal ``target_link_libraries`` citation for the edge evidence (§5)."""
    kw = {"public": "PUBLIC", "private": "PRIVATE", "interface": "INTERFACE"}.get(visibility, "PUBLIC")
    return f"target_link_libraries({src} {kw} {dst})"


def fragment_from_model(model: CMakeModel, *, extractor: dict[str, str] | None = None,
                        build_tag: str = "build:cmake",
                        link_detail=None) -> dict[str, Any]:
    """Turn a parsed :class:`CMakeModel` into a fact fragment (provenance added by runner).

    The model dataclasses are the neutral in-memory build-graph shape shared by every
    C/C++ front-end; *extractor*/*build_tag*/*link_detail* let a non-CMake front-end
    (:mod:`extract_autotools`) reuse this emission with its own provenance and evidence
    wording while keeping merge semantics identical."""
    extractor = extractor or EXTRACTOR
    link_detail = link_detail or _visibility_detail
    targets: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}
    partial_codegen: list[str] = []  # target ids whose generated sources are absent (§4.1/§16#14)
    # CTest/CDash dashboard ceremony is dropped wholesale (closed name set + UTILITY-type
    # gate; see _CTEST_DASHBOARD_TARGETS) — targets here AND their edges below, so no
    # dangling endpoint survives into the fragment (referential integrity, §5/§10).
    dashboard = {name for name, t in model.targets.items()
                 if name in _CTEST_DASHBOARD_TARGETS and t.type == "unknown"}
    for name in sorted(model.targets):
        if name in dashboard:
            continue
        t = model.targets[name]
        tid = _target_id(name)
        tags = [build_tag]
        # Build-time codegen with ABSENT outputs during a configure-only run (plan §4.1 /
        # §16#14 iv): the generator target is KEPT (it is a real build node) but its generated
        # source *content* — includes/symbols — does not exist yet, so we must not treat the
        # incomplete L2/L3 evidence as ground truth. Tag the owning target ``partial:codegen``
        # so drift (§11.1) annotates rather than reporting phantom edges, and record the gap in
        # coverage/provenance (L2/L3 marked partial below). A codegen target whose outputs ARE
        # present on disk is normal — no tag, no perturbation (byte-identical for those repos).
        codegen_partial = bool(t.has_codegen and t.generated_sources_absent)
        if codegen_partial:
            tags.append("partial:codegen")
            partial_codegen.append(tid)
        # A source dir OUTSIDE the repo normalizes to an ABSOLUTE path (cmake_api._posix_rel's
        # out-of-repo fallback). CMake emits such targets (custom/codegen nodes sourced under an
        # out-of-tree <build>/CMakeFiles; the CTest dashboard set is already dropped above) and
        # that absolute path is BOTH non-deterministic (the build dir varies per run) and
        # non-portable (machine-specific), which breaks the §18.4/§1.1#2 byte-identical gate.
        # Never emit it: a non-repo-relative path is dropped to "" (a build-tree node simply has
        # no repo path).
        src_dir = t.source_dir or ""
        if src_dir and Path(src_dir).is_absolute():
            src_dir = ""
        target: dict[str, Any] = {
            "id": tid,
            "name": name,
            "type": t.type,
            "language": "cpp",
            "path": src_dir,
            "external": False,
            "tags": sorted(set(tags)),
        }
        if t.sources:
            target["metrics"] = {"files": len(t.sources)}
        targets.append(target)
        cov: dict[str, Any] = {"L0": True, "L1": bool(model.has_compile_db), "L2": False, "L3": False}
        if codegen_partial:
            # The fine-grained tiers are unavailable for the missing generated content — record
            # them as PARTIAL so a degraded/partial target's disappearances are classified
            # coverage-gap-like, not drift (§11.1). L0 (the target-graph node + its declared
            # link edges) survives configure-only and stays True.
            cov["L2"] = "partial"
            cov["L3"] = "partial"
        coverage[tid] = cov

    # de-dup edges by (source, target); keep the most specific visibility (private/interface
    # over public) so a single declared edge carries the strongest signal (§6.2/§7.4).
    _vis_rank = {"public": 0, "interface": 1, "private": 2}
    best: dict[tuple[str, str], str] = {}
    for e in model.edges:
        if e.source not in model.targets or e.target not in model.targets:
            continue
        if e.source in dashboard or e.target in dashboard:
            continue  # a dropped dashboard target's edges go with it (no dangling ends)
        key = (e.source, e.target)
        if key not in best or _vis_rank.get(e.visibility, 0) > _vis_rank.get(best[key], 0):
            best[key] = e.visibility

    relationships: list[dict[str, Any]] = []
    for (src, dst), vis in sorted(best.items()):
        relationships.append(make_relationship(
            source=_target_id(src), target=_target_id(dst),
            evidence=[{"type": "link", "detail": link_detail(src, dst, vis),
                       "visibility": vis}],
        ))

    provenance: dict[str, Any] = {"extractors": [extractor], "coverage": coverage}
    if partial_codegen:
        # Deterministic, sorted record of the targets flagged ``partial:codegen`` (plan §4.1 /
        # §16#14). Surfaced for drift (§11.1) so these targets' missing fine-grained edges read
        # as coverage-gap-like, not phantom drift. Only emitted when non-empty, so it never
        # perturbs the byte-identical output of repos without absent codegen.
        provenance["partial_codegen"] = sorted(set(partial_codegen))

    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": provenance,
        "targets": targets,
        "relationships": relationships,
    }


def build_fragment(repo: Path, build_dir: Path, *, configure: bool = True,
                   extra_args: list[str] | None = None,
                   preset: str | None = None) -> dict[str, Any] | None:
    """Configure+parse *repo* and build the C++ L0/L1 fragment, or ``None`` if not CMake."""
    model = build_model(repo, build_dir, configure=configure, extra_args=extra_args,
                        preset=preset)
    if model is None or not model.targets:
        return None
    return fragment_from_model(model)


def run(ws: Workspace) -> Path | None:
    """Extract the C++ build graph and write the fragment, or ``None`` on graceful degrade.

    Returns the fragment path on success; ``None`` for a non-CMake repo / missing cmake /
    failed configure (the run proceeds on whatever other extractors produced, §18.4).

    The configure dir is ``ws.cmake_build_dir`` (``ANON_CMAKE_BUILD_DIR`` overrides
    the default ``.cache/cmake-build`` for projects that hard-cap the build-dir path
    length, e.g. ITK's 50-char FATAL_ERROR check).

    ``ANON_CMAKE_PRESET`` selects a CMakePresets *configure preset* for projects that
    only configure correctly through one (toolchain/cache vars a bare ``-S/-B`` can't
    supply). The configure then targets the PRESET's binaryDir, not ``ws.cmake_build_dir``;
    an unresolvable preset name degrades to the bare-configure path with a warning (§18.4).
    """
    # Escape hatch (§18.3 cheap-first), the CMake twin of ANON_SKIP_DOXYGEN: a repo
    # that ships BOTH a CMakeLists.txt and an autotools build (libxml2 >= 2.9) normally
    # gets CMake as the authoritative L0 (a real configure run); ANON_SKIP_CMAKE=1
    # stands this extractor down so the pure static autotools parse supplies L0 instead
    # (the eval §8.3 real-history runs, whose frozen rules were authored for that idiom).
    if os.environ.get("ANON_SKIP_CMAKE"):
        log.info("ANON_SKIP_CMAKE set — skipping the CMake L0/L1 extractor (§18.3)")
        return None
    build_dir = ws.cmake_build_dir
    preset = os.environ.get("ANON_CMAKE_PRESET") or None
    if preset:
        root = find_cmake_root(ws.repo)
        resolved = resolve_preset_binary_dir(root, preset) if root else None
        if resolved is not None:
            build_dir = resolved  # the preset owns its binaryDir; configure + parse there
        else:
            log.warning("ANON_CMAKE_PRESET=%s unresolved under %s — falling back to "
                        "the bare-configure path (§18.4)", preset, ws.repo)
            preset = None
    fragment = build_fragment(ws.repo, build_dir, preset=preset)
    if fragment is None:
        log.info("no C++ CMake facts for %s (not a CMake repo or configure failed) — "
                 "skipping C++ L0/L1 (§18.4)", ws.repo)
        return None
    out = ws.fragments / "cmake-graph.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_json(fragment, out)
    return out


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    import argparse

    from ..paths import resolve_workspace

    from ..logutil import setup_console_logging
    setup_console_logging()  # same timestamped console format as the `arch` CLI
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir)
    result = run(w)
    print(result if result else "(degraded: no C++ CMake facts)")
