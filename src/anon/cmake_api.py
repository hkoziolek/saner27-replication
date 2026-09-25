"""CMake File API (codemodel v2) driver + parser, shared by the C++ extractors (plan §4.1).

Configure-only (plan §18.3): writes the File API query stub, runs ``cmake`` *configure*
with the Ninja generator (the only generator that emits ``compile_commands.json`` — the
VS generator silently ignores ``CMAKE_EXPORT_COMPILE_COMMANDS``, §4.1) into a cache build
dir, and parses the **codemodel v2** reply into stable ``cpp:target`` facts. No build/
compile — just configure, far cheaper to stand up in CI/containers.

Why a hybrid File-API + graphviz parse (plan §4.1):
  * The **File API codemodel** is parse-stable JSON and is authoritative for target *type*
    and *source files* (needed for the component/include tiers, §6.3). BUT a header-only
    ``INTERFACE`` library with no build artifact is **absent** from the codemodel, and the
    codemodel does not expose per-dependency PUBLIC/PRIVATE/INTERFACE visibility.
  * ``cmake --graphviz`` *does* render every node (incl. INTERFACE libs) and encodes
    visibility in the edge style (solid=PUBLIC / dotted=PRIVATE / dashed=INTERFACE, §4.1).
  * So we take **nodes+types+sources from the File API** and **the complete edge set with
    visibility from the graphviz**, unioning any INTERFACE-library nodes the graphviz adds.
    Either source alone degrades gracefully (graphviz-only loses sources; File-API-only
    loses interface-lib edges + visibility).

Graceful degradation (plan §18.4): every entry point returns ``None``/empty on a missing
tool, a non-CMake repo, or a failed configure — the C++ path then contributes nothing
rather than aborting the whole run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("anon.cmake_api")

# --- File API layout (relative to the build dir) -------------------------------
_QUERY_REL = ".cmake/api/v1/query"
_REPLY_REL = ".cmake/api/v1/reply"

# Cold configure can be slow on a large repo (opencv/abseil); generous budget (§1.1#6).
_CONFIGURE_TIMEOUT_S = 1800

# CMake target type -> fact-model schema `type` enum (§6.2 / schema). UTILITY (custom/
# codegen) is kept as `unknown` — the generator target is a real build node (§4 codegen
# policy iv), curation decides its fate.
_TYPE_MAP = {
    "EXECUTABLE": "executable",
    "STATIC_LIBRARY": "static_lib",
    "SHARED_LIBRARY": "shared_lib",
    "MODULE_LIBRARY": "shared_lib",
    "OBJECT_LIBRARY": "object_lib",
    "INTERFACE_LIBRARY": "interface_lib",
    "UTILITY": "unknown",
}

# graphviz node shape (from the CMake legend) -> schema `type`. Lets us type an
# INTERFACE/OBJECT/custom node the File API omitted, and skip external imports.
_SHAPE_TYPE = {
    "egg": "executable",
    "octagon": "static_lib",
    "doubleoctagon": "shared_lib",
    "tripleoctagon": "shared_lib",
    "pentagon": "interface_lib",
    "hexagon": "object_lib",
    "box": "unknown",          # custom target (UTILITY)
    "septagon": "external",    # "Unknown Library" == external/imported — dropped unless in File API
}

# graphviz edge style -> link visibility (plan §4.1 / §5/§6.2).
_STYLE_VISIBILITY = {"solid": "public", "dotted": "private", "dashed": "interface"}


@dataclass
class CMakeTarget:
    name: str                       # bare CMake target name (unique within project, §6.3)
    type: str                       # fact-model schema type enum
    api_id: str = ""                # File API internal id ("name::@hash") — NOT a stable id
    source_dir: str = ""            # repo-relative posix dir (best-effort)
    sources: list[str] = field(default_factory=list)  # repo-relative posix source files
    # Build-time codegen signals (plan §4.1 / §16#14). The File API marks a source produced
    # by add_custom_command with ``isGenerated``; a UTILITY target is a custom/codegen node.
    # ``has_codegen`` is True when the target declares any generated source (or is a custom
    # target); ``generated_sources_absent`` is True only when at least one such generated
    # source is NOT on disk during the configure-only run — i.e. its include/symbol content
    # is missing and must NOT be trusted as ground truth (the owning target is tagged
    # ``partial:codegen`` downstream so drift §11.1 annotates rather than phantom-reports).
    has_codegen: bool = False
    generated_sources_absent: bool = False
    generated_sources: list[str] = field(default_factory=list)  # repo-relative, isGenerated


@dataclass
class CMakeEdge:
    source: str                     # CMake target name
    target: str                     # CMake target name
    visibility: str = "public"      # public | private | interface


@dataclass
class CMakeModel:
    targets: dict[str, CMakeTarget] = field(default_factory=dict)   # by name
    edges: list[CMakeEdge] = field(default_factory=list)
    has_compile_db: bool = False
    build_dir: Path | None = None

    def source_to_target(self) -> dict[str, str]:
        """Map every known source-file path (repo-relative posix) -> owning target name."""
        out: dict[str, str] = {}
        for t in self.targets.values():
            for src in t.sources:
                out[src] = t.name
        return out


def cmake_available() -> bool:
    return shutil.which("cmake") is not None


def find_cmake_root(repo: Path) -> Path | None:
    """The directory holding the top-level ``CMakeLists.txt`` (repo root, else shallowest)."""
    if (repo / "CMakeLists.txt").exists():
        return repo
    candidates = sorted(repo.rglob("CMakeLists.txt"),
                        key=lambda p: (len(p.relative_to(repo).parts), str(p)))
    return candidates[0].parent if candidates else None


# --- CMakePresets (configure presets, plan §4.1) -------------------------------
# Some projects only configure correctly through a CMakePresets.json *configure preset* —
# it supplies toolchain cache vars a bare `cmake -S/-B` cannot (a pinned compiler,
# CMAKE_SYSTEM_PROCESSOR, SDK vars) that the project's find_package logic depends on.
# ANON_CMAKE_PRESET (or a manifest `cmake_preset`, exported by snapshot-example.ps1)
# selects one; the extractor then runs `cmake --preset <name>` into the PRESET's binaryDir.

_PRESET_FILES = ("CMakePresets.json", "CMakeUserPresets.json")


def _load_configure_presets(root: Path) -> dict[str, dict[str, Any]]:
    """Merge ``configurePresets`` from CMakePresets.json + CMakeUserPresets.json (user file
    wins), keyed by name. Empty dict when no preset file is present / parseable."""
    presets: dict[str, dict[str, Any]] = {}
    for fname in _PRESET_FILES:
        p = root / fname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", p, exc)
            continue
        for cp in data.get("configurePresets", []) or []:
            if isinstance(cp, dict) and cp.get("name"):
                presets[cp["name"]] = cp
    return presets


def _preset_field(presets: dict[str, dict[str, Any]], name: str, key: str,
                  seen: set[str] | None = None) -> Any:
    """Resolve a configure-preset field through ``inherits`` (the preset's own value wins;
    among inherited parents, earlier in the list wins — the CMakePresets rule)."""
    seen = seen or set()
    if name in seen or name not in presets:
        return None
    p = presets[name]
    if key in p:
        return p[key]
    inh = p.get("inherits") or []
    if isinstance(inh, str):
        inh = [inh]
    for parent in inh:
        v = _preset_field(presets, parent, key, seen | {name})
        if v is not None:
            return v
    return None


def _expand_preset_macros(value: str, root: Path, preset_name: str) -> str:
    """Expand the CMakePresets macros that can appear in a ``binaryDir`` (best-effort)."""
    import platform as _platform
    repl = {
        "${sourceDir}": root.as_posix(),
        "${sourceParentName}": root.parent.name,
        "${sourceDirName}": root.name,
        "${presetName}": preset_name,
        "${hostSystemName}": _platform.system(),
        "${pathListSep}": os.pathsep,
        "${dollar}": "$",
    }
    for k, v in repl.items():
        value = value.replace(k, v)
    # $env{X} / $penv{X} -> environment value (empty when unset).
    return re.sub(r"\$p?env\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), value)


def resolve_preset_binary_dir(root: Path, preset: str) -> Path | None:
    """The resolved ``binaryDir`` for a named configure preset, or ``None`` when the preset
    (or any preset file) is absent. Falls back to CMake's default
    ``${sourceDir}/build/${presetName}`` when ``binaryDir`` is unset in the chain."""
    presets = _load_configure_presets(root)
    if preset not in presets:
        if presets:
            log.warning("cmake preset %r not found under %s (available: %s)",
                        preset, root, ", ".join(sorted(presets)))
        return None
    raw = _preset_field(presets, preset, "binaryDir") or "${sourceDir}/build/${presetName}"
    return Path(_expand_preset_macros(str(raw), root, preset))


# --- configure (configure-only; no build) --------------------------------------

def _write_query(build_dir: Path) -> None:
    qdir = build_dir / _QUERY_REL
    qdir.mkdir(parents=True, exist_ok=True)
    # An empty query file requests the latest codemodel-v2 object (File API contract).
    (qdir / "codemodel-v2").touch()


def _write_graphviz_options(build_dir: Path) -> None:
    """Exclude external/imported libs from the graphviz so only first-party targets render
    (plan §4.1 ``GRAPHVIZ_EXTERNAL_LIBS=FALSE``). CMake reads this from the *binary* dir, so
    it never touches the pristine source tree (§19.4)."""
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "CMakeGraphVizOptions.cmake").write_text(
        "set(GRAPHVIZ_EXTERNAL_LIBS FALSE)\n"
        "set(GRAPHVIZ_GENERATE_PER_TARGET FALSE)\n"
        "set(GRAPHVIZ_GENERATE_DEPENDERS FALSE)\n",
        encoding="utf-8",
    )


def _reply_dir(build_dir: Path) -> Path | None:
    d = build_dir / _REPLY_REL
    if d.is_dir() and any(d.glob("index-*.json")):
        return d
    return None


def _configured_source_dir(build_dir: Path) -> Path | None:
    """The source dir a prior configure used, read from ``CMakeCache.txt``'s
    ``CMAKE_HOME_DIRECTORY``. Lets :func:`ensure_configured` refuse to reuse a reply that
    belongs to a DIFFERENT repo — a shared/stale build dir must never serve another repo's
    targets (the abseil-into-eShop contamination class)."""
    cache = build_dir / "CMakeCache.txt"
    if not cache.exists():
        return None
    try:
        for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("CMAKE_HOME_DIRECTORY:"):
                _, _, val = line.partition("=")
                return Path(val.strip()) if val.strip() else None
    except OSError:
        return None
    return None


def _same_dir(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def ensure_configured(repo: Path, build_dir: Path, *, generator: str = "Ninja",
                      extra_args: list[str] | None = None,
                      reconfigure: bool = False, preset: str | None = None) -> bool:
    """Configure *repo* into *build_dir* with the File API + graphviz enabled.

    Reuses an existing reply unless *reconfigure*. Returns ``True`` if a File API reply is
    available afterwards, ``False`` on any failure (caller degrades, §18.4).

    With *preset*, configures via ``cmake --preset <name>`` (the project's own toolchain /
    cache vars) instead of a bare ``-S/-B``; *build_dir* must then be the preset's resolved
    binaryDir (the caller resolves it via :func:`resolve_preset_binary_dir`).

    Extra ``-D`` args (or a compiler via the ``CC``/``CXX`` environment, honored natively by
    CMake) let a host pick its toolchain without code changes; ``ANON_CMAKE_ARGS`` in
    the environment is appended too (space-split) for the same reason.
    """
    root = find_cmake_root(repo)
    if root is None:
        log.info("no CMakeLists.txt under %s — not a CMake repo (§18.4)", repo)
        return False
    if not cmake_available():
        log.info("cmake not on PATH — skipping C++ L0/L1 (§18.4)")
        return False

    if not reconfigure and _reply_dir(build_dir) is not None:
        prior = _configured_source_dir(build_dir)
        if _same_dir(prior, root):
            return True  # reuse a prior configure FOR THIS REPO (cheap reruns, §18.4)
        # A reply exists but for a DIFFERENT source dir (a shared/stale build dir, e.g.
        # abseil's cache reused for eShop) — never serve another repo's targets. Wipe and
        # reconfigure cleanly; CMake itself refuses to reuse a cache whose source differs.
        log.info("cmake build dir was configured for %s, not %s — wiping + reconfiguring "
                 "(§18.4)", prior, root)
        shutil.rmtree(build_dir, ignore_errors=True)

    _write_query(build_dir)
    _write_graphviz_options(build_dir)
    graphviz_out = build_dir / "anon.dot"
    if preset:
        # The preset owns -S/-B/-G; we only AUGMENT it with the File-API/compile-db/graphviz
        # flags (allowed alongside --preset, unlike -G). cwd=root so CMakePresets.json
        # resolves; the query was written into build_dir = the preset's binaryDir above.
        cmd = ["cmake", "--preset", preset,
               "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", f"--graphviz={graphviz_out}"]
        cwd: str | None = str(root)
    else:
        cmd = ["cmake", "-S", str(root), "-B", str(build_dir), "-G", generator,
               "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", f"--graphviz={graphviz_out}"]
        cwd = None
    cmd += extra_args or []
    env_extra = os.environ.get("ANON_CMAKE_ARGS", "").split()
    cmd += env_extra
    log.info("configuring (configure-only): %s", " ".join(cmd))
    # Repeated in the failure warnings below: INFO may be filtered (ANON_LOG=warning),
    # and a bare "configure failed" with no source/build/preset context is undiagnosable.
    ctx = (f"source {root}, build dir {build_dir}"
           + (f", preset '{preset}'" if preset else "")
           + f"\n  command: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_CONFIGURE_TIMEOUT_S, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("cmake configure could not run: %s — degrading (§18.4)\n  %s", exc, ctx)
        return False
    if proc.returncode != 0:
        stale_ok = _reply_dir(build_dir) is not None
        log.warning("cmake configure failed (rc=%s) — %s (§18.4)\n  %s\n%s",
                    proc.returncode,
                    "reusing the PRIOR configure's reply (targets may be stale)"
                    if stale_ok else "degrading to no CMake facts",
                    ctx, (proc.stderr or proc.stdout or "").strip()[-2000:])
        # A reply may still exist from a prior good configure; accept it if so.
        return stale_ok
    return _reply_dir(build_dir) is not None


# --- File API codemodel parse --------------------------------------------------

def _posix_rel(path_str: str, top: Path, repo: Path) -> str:
    """Normalize a File API source path to a repo-relative POSIX string (best-effort)."""
    p = Path(path_str)
    if not p.is_absolute():
        p = top / p
    try:
        return p.resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        return path_str.replace("\\", "/")


def _source_exists(rel_or_path: str, top: Path, repo: Path) -> bool:
    """True iff a (repo-relative posix) source resolves to an existing file on disk.

    Used to decide whether a File-API ``isGenerated`` source is *absent* during a
    configure-only run (plan §4.1 / §16#14). Best-effort and conservative: any resolution
    error is treated as 'present' so we never spuriously tag a real, on-disk source as
    absent codegen (graceful degradation, §18.4)."""
    try:
        p = Path(rel_or_path)
        if p.is_absolute():
            return p.exists()
        # repo-relative posix (the normalized form) — try repo root first, then the CMake
        # source top as a fallback for paths that didn't normalize under the repo.
        if (repo / rel_or_path).exists():
            return True
        return (top / rel_or_path).exists()
    except OSError:
        return True


def _common_dir(sources: list[str]) -> str:
    """Longest common directory of a target's sources (its repo-relative 'path')."""
    if not sources:
        return ""
    parts = [s.split("/")[:-1] for s in sources]  # drop filenames
    common: list[str] = []
    for segs in zip(*parts):
        if len(set(segs)) == 1:
            common.append(segs[0])
        else:
            break
    return "/".join(common)


def parse_file_api(build_dir: Path, repo: Path) -> CMakeModel | None:
    """Parse the codemodel v2 reply in *build_dir* into a :class:`CMakeModel` (no edges yet).

    Returns ``None`` if no reply is present. Edges/visibility come from the graphviz overlay
    (:func:`overlay_graphviz`); the File API ``dependencies[]`` are used as an edge fallback.
    """
    reply = _reply_dir(build_dir)
    if reply is None:
        return None
    try:
        index_path = sorted(reply.glob("index-*.json"))[-1]
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError):
        return None

    # Locate the codemodel object referenced by the index.
    codemodel_file = None
    for obj in index.get("objects", []):
        if obj.get("kind") == "codemodel":
            codemodel_file = reply / obj["jsonFile"]
            break
    if codemodel_file is None or not codemodel_file.exists():
        return None
    try:
        codemodel = json.loads(codemodel_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    top = Path(codemodel.get("paths", {}).get("source", str(repo)))
    configs = codemodel.get("configurations", [])
    if not configs:
        return None
    # Prefer a configuration that actually has targets (single-config generators use [0]).
    config = max(configs, key=lambda c: len(c.get("targets", [])))

    model = CMakeModel(build_dir=build_dir)
    api_id_to_name: dict[str, str] = {}
    raw_deps: dict[str, list[str]] = {}  # target name -> [dep api ids]

    for tref in config.get("targets", []):
        tfile = reply / tref["jsonFile"]
        try:
            tdata = json.loads(tfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = tdata.get("name") or tref.get("name")
        if not name:
            continue
        ctype = tdata.get("type", "")
        sources = sorted(_posix_rel(s["path"], top, repo)
                         for s in tdata.get("sources", []) if s.get("path"))
        # Codegen detection (plan §4.1 / §16#14, conservative + deterministic): the File API
        # marks an add_custom_command output with ``isGenerated``; a UTILITY target is itself a
        # custom/codegen node. We surface the generated sources and, crucially, whether any are
        # ABSENT on disk during this configure-only run — an absent generated source means the
        # include/symbol evidence for it does not exist yet and must not be trusted (§16#14 iv).
        gen_sources = sorted(_posix_rel(s["path"], top, repo)
                             for s in tdata.get("sources", [])
                             if s.get("path") and s.get("isGenerated"))
        has_codegen = bool(gen_sources) or ctype == "UTILITY"
        # A generated source is "absent" if it does not resolve to an existing file under the
        # repo (configure-only never produced it). Resolve against ``top`` for relative paths.
        gen_absent = any(not _source_exists(s, top, repo) for s in gen_sources)
        # keep only real source files (drop headers? keep them — Doxygen maps both).
        tgt = CMakeTarget(
            name=name,
            type=_TYPE_MAP.get(ctype, "unknown"),
            api_id=tdata.get("id", tref.get("id", "")),
            source_dir=_common_dir(sources),
            sources=sources,
            has_codegen=has_codegen,
            generated_sources_absent=gen_absent,
            generated_sources=gen_sources,
        )
        model.targets[name] = tgt
        api_id_to_name[tgt.api_id] = name
        raw_deps[name] = [d["id"] for d in tdata.get("dependencies", []) if d.get("id")]

    # File API dependency edges (visibility unknown here -> public default; graphviz refines).
    for name, dep_ids in raw_deps.items():
        for dep in dep_ids:
            dep_name = api_id_to_name.get(dep)
            if dep_name and dep_name != name:
                model.edges.append(CMakeEdge(source=name, target=dep_name, visibility="public"))

    model.has_compile_db = (build_dir / "compile_commands.json").exists()
    return model


# --- graphviz overlay (complete edges + visibility + interface-lib nodes) ------

_DOT_NODE = re.compile(r'"(?P<id>node\d+)"\s*\[\s*label\s*=\s*"(?P<label>[^"]+)"'
                       r'(?:\s*,\s*shape\s*=\s*(?P<shape>\w+))?')
_DOT_EDGE = re.compile(r'"(?P<a>node\d+)"\s*->\s*"(?P<b>node\d+)"'
                       r'(?:\s*\[\s*style\s*=\s*(?P<style>\w+)[^\]]*\])?')


def _strip_legend(text: str) -> str:
    """Remove the ``subgraph clusterLegend { ... }`` block so legend nodes/edges don't leak."""
    start = text.find("subgraph clusterLegend")
    if start == -1:
        return text
    # find the matching closing brace of the subgraph
    brace = text.find("{", start)
    if brace == -1:
        return text
    depth = 0
    i = brace
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[:start] + text[i + 1:]
        i += 1
    return text[:start]


def parse_graphviz(dot_path: Path) -> tuple[dict[str, tuple[str, str]], list[CMakeEdge]]:
    """Parse a CMake graphviz file.

    Returns ``(nodes, edges)`` where ``nodes`` maps target *name* -> ``(node_id, type)`` and
    ``edges`` is a list of :class:`CMakeEdge` with visibility from the edge style. The legend
    subgraph is stripped first so its synthetic ``legendNode*`` entries never appear.
    """
    try:
        text = dot_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, []
    text = _strip_legend(text)

    id_to_name: dict[str, str] = {}
    nodes: dict[str, tuple[str, str]] = {}
    for m in _DOT_NODE.finditer(text):
        nid, label, shape = m.group("id"), m.group("label"), (m.group("shape") or "")
        # CMake renders an ALIAS target as a two-line label ``name\n(ns::alias)`` (literal
        # ``\n`` escape). Take the first line so the alias node collapses onto its real
        # target name instead of being added as a spurious duplicate (e.g. fmt's fmt::fmt).
        name = label.split("\\n", 1)[0].strip()
        ntype = _SHAPE_TYPE.get(shape, "unknown")
        id_to_name[nid] = name
        nodes[name] = (nid, ntype)

    edges: list[CMakeEdge] = []
    for m in _DOT_EDGE.finditer(text):
        a, b, style = id_to_name.get(m.group("a")), id_to_name.get(m.group("b")), m.group("style")
        if not a or not b:
            continue
        vis = _STYLE_VISIBILITY.get(style or "solid", "public")
        edges.append(CMakeEdge(source=a, target=b, visibility=vis))
    return nodes, edges


def overlay_graphviz(model: CMakeModel, build_dir: Path) -> CMakeModel:
    """Merge graphviz nodes/edges into *model*: add INTERFACE-lib nodes the File API omitted,
    and replace the edge set with the visibility-bearing graphviz edges when available.

    Graphviz-only nodes that map to an external/import shape (``septagon``) are dropped
    (``GRAPHVIZ_EXTERNAL_LIBS=FALSE`` should already exclude them; this is belt-and-suspenders).
    """
    dot_path = build_dir / "anon.dot"
    nodes, gedges = parse_graphviz(dot_path)
    if not nodes:
        return model  # no graphviz — keep the File API edges as-is

    for name, (_nid, ntype) in nodes.items():
        if name in model.targets:
            continue
        if ntype == "external":
            continue  # imported/external lib — not a first-party container
        # A node graphviz knows but the File API omitted (typically a header-only INTERFACE
        # lib, or an object/custom target). No source files available from graphviz.
        model.targets[name] = CMakeTarget(name=name, type=ntype if ntype != "unknown" else "interface_lib")

    if gedges:
        # graphviz carries the complete edge set with visibility — prefer it. Keep only
        # edges whose endpoints are known first-party targets.
        kept = [e for e in gedges
                if e.source in model.targets and e.target in model.targets]
        if kept:
            model.edges = kept
    return model


def build_model(repo: Path, build_dir: Path, *, configure: bool = True,
                extra_args: list[str] | None = None,
                preset: str | None = None) -> CMakeModel | None:
    """High-level entry: (optionally) configure, then parse File API + overlay graphviz.

    Returns a fully-populated :class:`CMakeModel`, or ``None`` if the repo is not a usable
    CMake project / configure failed and no prior reply exists (caller degrades, §18.4).

    *preset* configures via the project's CMakePresets configure preset (see
    :func:`ensure_configured`); *build_dir* must be that preset's binaryDir.
    """
    if configure and not ensure_configured(repo, build_dir, extra_args=extra_args,
                                            preset=preset):
        if _reply_dir(build_dir) is None:
            return None
    model = parse_file_api(build_dir, repo)
    if model is None:
        return None
    return overlay_graphviz(model, build_dir)
