"""Stage 1 — MSBuild / Visual Studio C++ extraction (plan §4.1 / §16#1, node 1).

The second C++ front-end (the first is CMake, :mod:`extract_cmake`). Visual Studio / MSBuild
C++ has **no native compile database** — ``CMAKE_EXPORT_COMPILE_COMMANDS`` doesn't apply and
the VS generator emits nothing — so the plan's path is: L0 from the ``.sln``/``.vcxproj``
project graph, and L1 by **reconstructing a ``compile_commands.json``** either from an MSBuild
**binary log** (``msbuild /bl`` → :mod:`anon.msbuild_binlog`) or, build-free, statically
from the project files. This module does both:

  * **L0 target graph** (pure XML, no MSBuild needed): one ``cpp:vcxproj:<path>`` target per
    ``.vcxproj``, typed from ``<ConfigurationType>`` (Application→executable, StaticLibrary→
    static_lib, DynamicLibrary→shared_lib, Utility→unknown), with ``<ProjectReference>`` link
    edges. This is the always-available backbone (the ``microsoft/terminal`` "L0-only" path).
  * **L1 compile DB**, in priority order: a pre-existing ``compile_commands.json`` › an MSBuild
    ``*.binlog`` (parsed by :mod:`msbuild_binlog`) › a **static reconstruction** from each
    project's ``<AdditionalIncludeDirectories>`` + ``<PreprocessorDefinitions>`` + ``<ClCompile>``
    sources (build-free; common ``$()``/``%()`` macros resolved). Whatever wins is written to
    the cache and flips the per-target ``L1`` coverage flag; it is what the clang L2 include
    scan (:mod:`extract_clang_deps`) consumes.

Stable ids (plan §6.3): ``cpp:vcxproj:<repo-relative-posix-path>`` — the project path is the
content-independent key (the same rationale as the C# ``.csproj`` path; a project *name* need
not be unique). The ``cpp:`` namespace + ``shared_lib`` typing means a DynamicLibrary here is
also a candidate for P/Invoke interop resolution (§16#15, :mod:`interop_resolve`).

Graceful degradation (plan §18.4): a repo with no ``.vcxproj`` yields ``None`` and writes
nothing; the static L1 reconstruction never needs MSBuild, so L0+L1 work on any OS.
"""
from __future__ import annotations

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..jsonio import dump_json
from ..model import make_relationship
from ..paths import Workspace

log = logging.getLogger("anon.extract_msbuild_cpp")

EXTRACTOR = {"name": "msbuild-cpp", "version": "0.1.0"}

# <ConfigurationType> -> fact-model schema type enum.
_CONFIG_TYPE = {
    "Application": "executable",
    "StaticLibrary": "static_lib",
    "DynamicLibrary": "shared_lib",
    "Utility": "unknown",
}

_MSBUILD_PROP = re.compile(r"\$\((?P<name>[A-Za-z_][A-Za-z0-9_]*)\)")
# Build-output / VCS dirs only — mirror extract_build_graph's set so a project under a
# feature dir legitimately named "packages" (or under ".vs") is NOT silently dropped.
_SKIP_DIRS = {"bin", "obj", ".git", "node_modules"}
_SRC_EXTS = (".cpp", ".cxx", ".cc", ".c")

# --- COM coclass identity capture (plan §16#15 / 5b) -------------------------------
# A native COM server's identity (CLSID / ProgID) — the thing the C#/C++ ``com:<…>`` boundary
# is keyed by — lives in its MIDL ``.idl`` (``coclass`` + ``uuid(…)``) and its ``.rgs``
# registration script (the ProgID + CLSID). Capturing it onto the cpp target as
# ``com_provides`` lets ``interop_resolve`` bind the COM seam *by identity*, not by name.
#
# Scoped to the cheap, common formats actually present in the test fixture (an ``.idl`` with a
# ``coclass``/``uuid`` and an ``.rgs`` ProgID). Exotic registration paths (custom registrars,
# pure-registry CLSIDs, TLB binaries) are deliberately NOT parsed — a server whose identity we
# can't see stays an honest dangling boundary (wrong is worse than absent).

# An attribute block [...] immediately preceding `coclass` — its uuid(…) is the server CLSID.
# We capture ONLY this, never interface IIDs / the TypeLib LIBID (§16#15 / 5b, conservative).
_IDL_COCLASS_BLOCK_RE = re.compile(r'\[(?P<attrs>[^\]]*)\]\s*coclass\b', re.IGNORECASE | re.DOTALL)
# uuid(XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX) — MIDL attribute (quotes optional, braces optional).
_IDL_UUID_RE = re.compile(
    r'\buuid\s*\(\s*["\']?\{?\s*'
    r'(?P<guid>[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})'
    r'\s*\}?["\']?\s*\)',
)
# A ProgID token in a .rgs script (e.g. ``ProgID = s '<MyServer.Object>'`` or a bare
# ``MyServer.Object = s 'Server'`` key) — dotted identifier, at least one dot, not a GUID.
_RGS_PROGID_RE = re.compile(r"['\"](?P<progid>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)['\"]")
# File-extension-like trailing segments that mean a dotted .rgs token is NOT a ProgID.
_NON_PROGID_SUFFIXES = {"dll", "exe", "ocx", "tlb", "lib", "pdb", "obj", "h", "c", "cpp", "cc",
                        "hpp", "rgs", "idl", "manifest", "def", "rc", "cs", "config"}


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _com_identities_from_idl(text: str) -> set[str]:
    """Capture the CLSID(s) of ``coclass`` declarations in one MIDL ``.idl`` body.

    ONLY the GUID in the attribute block immediately preceding a ``coclass`` (the creatable
    server's CLSID) is captured — NOT the interface IIDs or the TypeLib LIBID, which are not
    server identities a ``com:<…>`` boundary should bind to. Over-capturing those risks a FALSE
    COM bind (a consumer keyed on an interface IID binding to a server that merely declares it);
    a server whose CLSID we can't isolate stays an honest dangling boundary (§16#15 / 5b)."""
    out: set[str] = set()
    for m in _IDL_COCLASS_BLOCK_RE.finditer(text):
        um = _IDL_UUID_RE.search(m.group("attrs"))
        if um:
            out.add(um.group("guid").lower())
    return out


def _com_identities_from_rgs(text: str) -> set[str]:
    """Capture ProgIDs from one ``.rgs`` registration script (the C# ``GetTypeFromProgID`` key).

    ProgIDs only — file-extension-like dotted tokens (e.g. ``Foo.dll``) are filtered out, and
    the blanket GUID scan is intentionally dropped: a server's CLSID is taken from its ``.idl``
    ``coclass`` (above), so scooping every GUID out of the .rgs (TypeLib / AppID / interface-proxy
    GUIDs all live there too) would over-capture identities and risk a false COM bind (§16#15 / 5b)."""
    out: set[str] = set()
    for m in _RGS_PROGID_RE.finditer(text):
        pid = m.group("progid")
        if pid.rsplit(".", 1)[-1].lower() in _NON_PROGID_SUFFIXES:
            continue
        out.add(pid)
    return out


def _com_provides_for(vcxproj: Path, info: dict[str, Any]) -> list[str]:
    """Capture the COM identities a ``.vcxproj`` target provides (sorted, deterministic).

    Scans the IDL files the project compiles via ``<Midl Include=…>`` (and any ``.idl`` sibling
    in the project dir) plus any ``.rgs`` registration script in the project dir. Returns a
    sorted, de-duped list (CLSIDs lowercased; ProgIDs verbatim) or ``[]`` if nothing capturable
    — in which case the COM boundary correctly stays dangling."""
    idents: set[str] = set()
    proj_dir = vcxproj.parent
    idl_files: set[Path] = set()
    for inc in info.get("midl", []):
        p = (proj_dir / inc.replace("\\", "/"))
        if p.suffix.lower() == ".idl":
            idl_files.add(p)
    # Also any .idl / .rgs sibling in the project dir (a server typically registers itself there).
    for p in sorted(proj_dir.glob("*.idl")):
        idl_files.add(p)
    for idl in sorted(idl_files):
        try:
            idents |= _com_identities_from_idl(idl.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
    for rgs in sorted(proj_dir.glob("*.rgs")):
        try:
            idents |= _com_identities_from_rgs(rgs.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
    return sorted(idents)


def _rel(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _vcxproj_id(vcxproj: Path, repo: Path) -> str:
    return f"cpp:vcxproj:{_rel(vcxproj, repo)}"


def _all_vcxproj(repo: Path) -> list[Path]:
    out = []
    for p in repo.rglob("*.vcxproj"):
        if not (set(part.lower() for part in p.parts) & _SKIP_DIRS):
            out.append(p.resolve())
    return sorted(out)


def _expand_macros(value: str, vcxproj: Path, sln_dir: Path | None) -> str:
    """Resolve the common MSBuild macros in an include path (best-effort, build-free)."""
    root = (str(sln_dir) + os.sep) if sln_dir else ""
    props = {
        "projectdir": str(vcxproj.parent) + os.sep,
        "msbuildprojectdirectory": str(vcxproj.parent),
        "msbuildthisfiledirectory": str(vcxproj.parent) + os.sep,
        "solutiondir": root,
        # $(OpenConsoleDir) (microsoft/terminal) and other repo-root anchors used in place of
        # $(SolutionDir) all denote the solution/repo root — map them to the same base so a
        # ``$(OpenConsoleDir)src\foo\foo.vcxproj`` reference resolves to the real target rather
        # than a phantom (plan §4.1; the macro is defined in common.openconsole.props as the
        # repo root). The build-free :func:`_resolve_project_ref` is the general fallback.
        "openconsoledir": root,
        "configuration": "Debug",
        "platform": "x64",
        "platformtarget": "x64",
    }

    def _sub(m: re.Match[str]) -> str:
        return props.get(m.group("name").lower(), "")

    return _MSBUILD_PROP.sub(_sub, value)


def _resolve_project_ref(raw: str, vcxproj: Path, repo: Path) -> Path:
    """Resolve a ``<ProjectReference Include=…>`` to an on-disk ``.vcxproj`` path (build-free).

    MSBuild repos routinely anchor cross-project references with a repo-root macro
    (``$(SolutionDir)``, microsoft/terminal's ``$(OpenConsoleDir)``, …). Resolving the raw
    string relative to the referring project dir would then land on a *phantom* path (the
    unexpanded ``$(…)`` becomes a literal directory), fragmenting the build graph — the very
    structure that is the source of truth (plan §4.1, principle #1).

    Try, in order, and take the first candidate that EXISTS on disk: (1) macros expanded
    relative to the referring project dir (repo root anchors the solution-style macros),
    (2) all ``$(…)`` macros stripped and the remaining tail resolved against the repo root.
    Falls back to (1) unchanged when nothing resolves, so a genuinely dangling reference still
    produces a stable ``missing-ref`` placeholder id (§5) — preserving prior behavior for
    macro-free references."""
    norm = raw.replace("\\", "/")
    # (1) macros expanded, relative to the project dir (repo root = solution-style macro base).
    cand1 = (vcxproj.parent / _expand_macros(norm, vcxproj, repo)).resolve()
    if cand1.exists():
        return cand1
    # (2) strip any repo-root macro segment, resolve the remaining relative tail at the repo root.
    stripped = _MSBUILD_PROP.sub("", norm).lstrip("/")
    cand2 = (repo / stripped).resolve()
    if cand2.exists():
        return cand2
    return cand1


def _first_text(root: ET.Element, tag: str) -> str | None:
    for el in root.iter():
        if _strip_ns(el.tag) == tag and (el.text or "").strip():
            return el.text.strip()
    return None


def _parse_vcxproj(vcxproj: Path, repo: Path, sln_dir: Path | None) -> dict[str, Any]:
    """Parse one ``.vcxproj`` for type, name, project refs, sources, include dirs, defines."""
    try:
        root = ET.parse(vcxproj).getroot()
    except (ET.ParseError, OSError):
        return {}
    cfg_type = _first_text(root, "ConfigurationType") or "Utility"
    name = (_first_text(root, "ProjectName") or _first_text(root, "RootNamespace")
            or vcxproj.stem)

    project_refs: list[tuple[Path, str]] = []
    sources: list[str] = []
    includes: list[str] = []
    defines: list[str] = []
    midl: list[str] = []
    for el in root.iter():
        tag = _strip_ns(el.tag)
        inc = el.get("Include")
        if tag == "ProjectReference" and inc:
            project_refs.append((_resolve_project_ref(inc, vcxproj, repo), inc))
        elif tag == "ClCompile" and inc and inc.lower().endswith(_SRC_EXTS):
            sources.append((vcxproj.parent / inc.replace("\\", "/")).resolve().as_posix())
        elif tag == "Midl" and inc:  # <Midl Include="Server.idl"> — a compiled type library (§16#15)
            midl.append(inc)
        elif tag == "AdditionalIncludeDirectories" and (el.text or "").strip():
            for d in el.text.split(";"):
                d = d.strip()
                if d and not d.startswith("%("):
                    includes.append(_expand_macros(d, vcxproj, sln_dir))
        elif tag == "PreprocessorDefinitions" and (el.text or "").strip():
            for d in el.text.split(";"):
                d = d.strip()
                if d and not d.startswith("%("):
                    defines.append(d)
    return {
        "type": _CONFIG_TYPE.get(cfg_type, "unknown"),
        "name": name,
        "project_refs": project_refs,
        "sources": sorted(set(sources)),
        "includes": list(dict.fromkeys(includes)),  # de-dup, keep order
        "defines": list(dict.fromkeys(defines)),
        "midl": list(dict.fromkeys(midl)),           # <Midl> .idl references (COM identity, §16#15)
    }


def _static_compile_db(projects: dict[Path, dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize a ``compile_commands.json`` from project include dirs + defines (build-free).

    One entry per ``ClCompile`` source; the command is a plausible ``cl.exe`` line carrying the
    project's resolved ``/I`` include dirs and ``/D`` defines so a downstream clang scan
    (:mod:`extract_clang_deps`) sees the right search paths. Approximate (no full MSBuild
    evaluation, property sheets, or per-config nuance) — tagged as such by coverage, not facts.
    """
    db: list[dict[str, Any]] = []
    for vcxproj, info in projects.items():
        directory = str(vcxproj.parent)
        flags = [f'/I{inc}' for inc in info["includes"]] + [f'/D{d}' for d in info["defines"]]
        for src in info["sources"]:
            db.append({
                "directory": directory,
                "file": src,
                "command": "cl.exe " + " ".join(flags + ["/c", src]),
            })
    return db


def build_fragment(repo: Path, *, cache_dir: Path | None = None,
                   binlog: Path | None = None) -> dict[str, Any] | None:
    """Build the MSBuild C++ L0/L1 fragment for *repo*, or ``None`` if no ``.vcxproj``.

    *cache_dir* receives the reconstructed ``compile_commands.json`` (L1); *binlog*, if given
    (or auto-discovered), is parsed for real ``cl.exe`` command lines in preference to the
    static reconstruction.
    """
    repo = repo.resolve()
    vcxprojs = _all_vcxproj(repo)
    if not vcxprojs:
        return None

    # The repo root anchors solution-style macros ($(SolutionDir)/$(OpenConsoleDir)) in both
    # ProjectReference paths and include dirs (§4.1); a repo with a real .sln elsewhere still
    # resolves because _resolve_project_ref also probes the repo-relative tail.
    projects: dict[Path, dict[str, Any]] = {}
    for v in vcxprojs:
        parsed = _parse_vcxproj(v, repo, repo)
        if parsed:
            projects[v] = parsed

    # --- L1 compile DB: binlog (real cl.exe lines) › static reconstruction --------
    compile_db: list[dict[str, Any]] = []
    db_source = "static"
    if binlog is None:
        # SCOPED, deterministic discovery — never grab an arbitrary `*.binlog` from anywhere
        # in the tree (a stray/unrelated one would silently become this repo's compile DB, and
        # a full-tree rglob is wasteful). Look only at: an explicit `ANON_MSBUILD_BINLOG`
        # path, the cache dir a build step would target, and the conventional repo-root
        # `msbuild.binlog` (the default `msbuild /bl` output location).
        candidates: list[Path] = []
        env_bl = os.environ.get("ANON_MSBUILD_BINLOG")
        if env_bl:
            candidates.append(Path(env_bl))
        if cache_dir is not None:
            candidates.append(cache_dir / "msbuild.binlog")
        candidates.append(repo / "msbuild.binlog")
        binlog = next((p for p in candidates if p.exists()), None)
    if binlog is not None:
        from .. import msbuild_binlog
        db = msbuild_binlog.compile_db_from_binlog(binlog)
        if db:
            compile_db = db
            db_source = "binlog"
    if not compile_db:
        compile_db = _static_compile_db(projects)
    has_l1 = bool(compile_db)
    if cache_dir is not None and compile_db:
        cache_dir.mkdir(parents=True, exist_ok=True)
        dump_json(compile_db, cache_dir / "compile_commands.json")

    # --- assemble targets + edges -------------------------------------------------
    id_by_path = {v: _vcxproj_id(v, repo) for v in vcxprojs}
    targets: list[dict[str, Any]] = []
    target_ids: set[str] = set()   # O(1) membership for the dangling-ref placeholder check
    coverage: dict[str, Any] = {}
    for v in vcxprojs:
        info = projects.get(v, {})
        tid = id_by_path[v]
        target: dict[str, Any] = {
            "id": tid,
            "name": info.get("name", v.stem),
            "type": info.get("type", "unknown"),
            "language": "cpp",
            "path": _rel(v.parent, repo),
            "external": False,
            "tags": ["build:msbuild"],
        }
        if info.get("sources"):
            target["metrics"] = {"files": len(info["sources"])}
        # Capture the COM coclass identities this target provides (CLSID/ProgID) from its
        # .idl/.rgs so interop_resolve can bind a COM seam BY IDENTITY (§16#15 / 5b). Only
        # emitted when non-empty, so a non-COM .vcxproj stays byte-identical.
        com_provides = _com_provides_for(v, info)
        if com_provides:
            target["com_provides"] = com_provides
        targets.append(target)
        target_ids.add(tid)
        coverage[tid] = {"L0": True, "L1": has_l1, "L2": False, "L3": False}

    rels_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for v in vcxprojs:
        info = projects.get(v, {})
        src_id = id_by_path[v]
        for ref_path, raw in info.get("project_refs", []):
            tgt_id = id_by_path.get(ref_path) or _vcxproj_id(ref_path, repo)
            key = (src_id, tgt_id)
            if key not in rels_by_key:
                rels_by_key[key] = make_relationship(
                    source=src_id, target=tgt_id,
                    evidence=[{"type": "project_ref",
                               "detail": f'<ProjectReference Include="{raw}">',
                               "visibility": "public"}])
            # materialize a placeholder for a dangling ref so referential integrity holds (§5)
            if tgt_id not in target_ids:
                targets.append({
                    "id": tgt_id, "name": ref_path.stem, "type": "unknown",
                    "language": "cpp", "path": _rel(ref_path.parent, repo),
                    "external": False, "tags": ["build:msbuild", "missing-ref"]})
                target_ids.add(tgt_id)

    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR], "coverage": coverage,
                       "msbuild_compile_db": db_source if has_l1 else "none"},
        "targets": targets,
        "relationships": list(rels_by_key.values()),
    }


def run(ws: Workspace) -> Path | None:
    """Extract the MSBuild C++ graph + reconstruct L1, or ``None`` if no ``.vcxproj``."""
    cache_dir = ws.cache / "msbuild-cpp"
    fragment = build_fragment(ws.repo, cache_dir=cache_dir)
    if fragment is None:
        log.info("no .vcxproj under %s — skipping MSBuild C++ (§18.4)", ws.repo)
        return None
    out = ws.fragments / "msbuild-cpp.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_json(fragment, out)
    return out


if __name__ == "__main__":  # pragma: no cover
    import argparse

    from ..paths import resolve_workspace

    from ..logutil import setup_console_logging
    setup_console_logging()  # same timestamped console format as the `arch` CLI
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir)
    print(run(w) or "(degraded: no MSBuild C++ facts)")
