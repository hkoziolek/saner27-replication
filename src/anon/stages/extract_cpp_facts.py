"""Stage 1 — C++ L2/L3 semantic facts via Doxygen XML (plan §4.1, node 1).

The refine layer on top of the CMake L0 backbone (:mod:`extract_cmake`). Doxygen is the
pragmatic dual-purpose source the plan picks (§4.1): ``GENERATE_XML=YES`` yields structured
namespaces / classes / files / include-relations. This extractor harvests:

  * **L2 include graph** — file-level ``#include`` edges, aggregated+weighted to the owning
    CMake targets, emitted as ``include`` evidence that *corroborates* the L0 ``link`` edges
    (§5). A thousand includes between two targets collapse to one weighted edge (§4.1).
  * **L3 component/code tier** — namespaces become the component tier (the C++ heuristic:
    innermost namespace, falling back to the top-level subdir, recorded in ``source``, §6.3);
    classes/structs become the capped ``code[]`` tier (≤25 per namespace, §16#10b). Rendered
    only by opt-in drill-down views (§9.1).

Target attribution: a documented file/class is mapped to a CMake target by **exact File-API
source match first, then longest source-directory prefix** (so a header in ``src/messaging/``
attributes to ``messaging`` even though the File API only lists the ``.cpp``). It reuses the
build dir :mod:`extract_cmake` already configured — no second configure.

The id schemes match the rest of the pipeline so ``normalize_facts`` merges these facts onto
the L0 targets/edges (§6.4): ``cpp:target:<name>`` and the parent-qualified
``cpp:target:<name>/component:<namespace>`` / ``.../code:<Class>`` child ids (§6.3).

Graceful degradation (plan §18.4): a missing ``doxygen``, no CMake model to attribute
against, or a Doxygen failure yields ``None`` and writes nothing — the L0 backbone still
carries the run, and coverage honestly stays ``L2:false`` (§11).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from .. import cmake_api, filedeps
from ..jsonio import dump_json
from ..model import make_relationship
from ..paths import Workspace

log = logging.getLogger("anon.extract_cpp_facts")

EXTRACTOR = {"name": "doxygen-xml", "version": "0.1.0"}

# Cap the class/code tier per namespace so a code-level drill-down stays within the §1.1#1
# readability budget (resolves §16#10b's per-component class cap).
_CODE_CAP = 25

_DOXYGEN_TIMEOUT_S = 1800  # cold L2/L3 symbol extract budget (plan §1.1#6: <=30 min)


def _doxygen_timeout_s() -> int:
    """The Doxygen subprocess budget, ``ANON_DOXYGEN_TIMEOUT`` (seconds) or the
    §1.1#6 default. A host-environment knob like ``ANON_DOXYGEN_INPUT``: very large
    template-heavy repos (ITK: 7.6k files incl. vendored VXL/HDF5/GDCM) legitimately
    exceed the 30-min cold budget, and the eval's file-granularity export (§14.1) needs
    the full pass rather than an honest-but-empty degrade."""
    raw = os.environ.get("ANON_DOXYGEN_TIMEOUT", "").strip()
    try:
        return int(raw) if raw else _DOXYGEN_TIMEOUT_S
    except ValueError:
        log.warning("ignoring non-integer ANON_DOXYGEN_TIMEOUT=%r", raw)
        return _DOXYGEN_TIMEOUT_S

# Source extensions Doxygen documents that we attribute to targets.
_SRC_EXTS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl"}


def doxygen_available() -> bool:
    return shutil.which("doxygen") is not None


# --- Doxygen run (configure-only equivalent: parse, never build) ----------------

_DOXYFILE_TEMPLATE = """\
DOXYFILE_ENCODING      = UTF-8
PROJECT_NAME           = anon
OUTPUT_DIRECTORY       = {out}
INPUT                  = {inputs}
RECURSIVE              = YES
GENERATE_XML           = YES
XML_OUTPUT             = xml
GENERATE_HTML          = NO
GENERATE_LATEX         = NO
EXTRACT_ALL            = YES
EXTRACT_STATIC         = YES
EXTRACT_LOCAL_CLASSES  = YES
ENABLE_PREPROCESSING   = YES
FULL_PATH_NAMES        = YES
STRIP_FROM_PATH        = {repo}
QUIET                  = YES
WARNINGS               = NO
WARN_IF_UNDOCUMENTED   = NO
WARN_IF_DOC_ERROR      = NO
HAVE_DOT               = NO
EXCLUDE_PATTERNS       = {excludes}
"""

_DOXYGEN_DEFAULT_EXCLUDES = (
    "*/build/* */.cache/* */out/* */.git/* */bin/* */obj/* */node_modules/* "
    "*/third_party/* */thirdparty/* */3rdparty/* {repo}/*/test/* {repo}/*/tests/* "
    "{repo}/*/perf/* {repo}/*/samples/* {repo}/*/misc/* {repo}/*/.gen/*"
)


def _doxygen_excludes(repo: Path) -> str:
    """The Doxyfile EXCLUDE_PATTERNS — ``ANON_DOXYGEN_EXCLUDE`` (space-separated,
    used verbatim; set-but-empty means *no* excludes) or the defaults. A fixture-scope
    knob like ``ANON_DOXYGEN_INPUT``: the defaults drop vendored/test trees, but a
    published SAR ground truth may *include* them (ITK's covers Modules/ThirdParty and
    the module test/ dirs — and Doxygen pattern-matching is case-insensitive on Windows,
    so ``*/thirdparty/*`` silently eats ``Modules/ThirdParty`` too)."""
    raw = os.environ.get("ANON_DOXYGEN_EXCLUDE")
    template = _DOXYGEN_DEFAULT_EXCLUDES if raw is None else raw
    return template.replace("{repo}", repo.resolve().as_posix())


def _doxygen_inputs(repo: Path) -> str:
    """The Doxygen INPUT set, repo-relative tokens from ``ANON_DOXYGEN_INPUT`` or the
    whole repo by default (§18.3). Scoping INPUT to the library subtree (e.g. ``modules``)
    keeps a configure-only L2/L3 pass within the time budget on a very large repo (OpenCV),
    while ``STRIP_FROM_PATH`` stays the repo root so target attribution remains repo-relative."""
    raw = os.environ.get("ANON_DOXYGEN_INPUT", "").strip()
    if not raw:
        return repo.resolve().as_posix()
    parts = [(repo / token).resolve().as_posix() for token in raw.split()]
    return " ".join(parts) if parts else repo.resolve().as_posix()


def ensure_doxygen_xml(repo: Path, out_dir: Path, *, rerun: bool = False) -> Path | None:
    """Run Doxygen (XML only) over *repo* into *out_dir*, or reuse a prior XML dir.

    Returns the ``xml/`` directory or ``None`` on degrade (no doxygen / failure / no output).
    """
    xml_dir = out_dir / "xml"
    if not rerun and xml_dir.is_dir() and any(xml_dir.glob("*.xml")):
        return xml_dir
    if not doxygen_available():
        log.info("doxygen not on PATH — skipping C++ L2/L3 (§18.4)")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    doxyfile = out_dir / "Doxyfile"
    doxyfile.write_text(
        _DOXYFILE_TEMPLATE.format(out=out_dir.as_posix(), repo=repo.resolve().as_posix(),
                                  inputs=_doxygen_inputs(repo),
                                  excludes=_doxygen_excludes(repo)),
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(["doxygen", str(doxyfile)], capture_output=True, text=True,
                              timeout=_doxygen_timeout_s())
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("doxygen could not run: %s — degrading (§18.4)", exc)
        return None
    if proc.returncode != 0:
        # A non-zero Doxygen exit means the run aborted (it may have written a *partial* XML
        # tree). Do NOT treat that partial output as a complete L2/L3 fragment — that would
        # silently emit components/edges for only the files written before the abort while
        # coverage claims L2/L3 present. Degrade honestly instead (§18.4), so the L0/L1
        # backbone carries the run and coverage stays L2:false.
        log.warning("doxygen failed (rc=%s) — degrading, NOT using partial XML (§18.4):\n%s",
                    proc.returncode, (proc.stderr or "").strip()[-2000:])
        return None
    if xml_dir.is_dir() and any(xml_dir.glob("*.xml")):
        return xml_dir
    return None


# --- target attribution ---------------------------------------------------------

class _TargetResolver:
    """Map a repo-relative source path to a CMake target name (exact, then dir-prefix)."""

    def __init__(self, model: cmake_api.CMakeModel):
        self._exact = model.source_to_target()
        # (source_dir, name) sorted by dir length desc so the *longest* prefix wins;
        # ties broken by (dir, name) so resolution is deterministic.
        self._dirs = sorted(
            ((t.source_dir, t.name) for t in model.targets.values() if t.source_dir),
            key=lambda x: (-len(x[0]), x[0], x[1]),
        )
        # Convention tier (checked only after every true source_dir): a target whose
        # source_dir leaf is `src` also owns the rest of its module dir — the C/C++
        # `<module>/{include,src}` sibling layout (ITK: Modules/Core/Common/include
        # belongs to ITKCommon at Modules/Core/Common/src). Headers in include/ are
        # not target *sources* in older CMake, so prefix matching alone misses them.
        self._parent_dirs = sorted(
            ((t.source_dir.rstrip("/").rsplit("/", 1)[0], t.name)
             for t in model.targets.values()
             if t.source_dir and t.source_dir.rstrip("/").endswith("/src")),
            key=lambda x: (-len(x[0]), x[0], x[1]),
        )

    def resolve(self, path: str) -> str | None:
        # Strip a leading "./" PREFIX only — `str.lstrip("./")` removes a *character set*
        # ({'.', '/'}), which would eat the leading dot of ".gen/x.h" or the ".." of
        # "../shared/x.cpp" and break attribution. Loop to drop repeated "./" segments.
        path = path.replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if path in self._exact:
            return self._exact[path]
        for tier in (self._dirs, self._parent_dirs):
            for source_dir, name in tier:
                if path == source_dir or path.startswith(source_dir + "/"):
                    return name
        return None


# --- Doxygen XML parse ----------------------------------------------------------

def _rel(path: str, repo: Path) -> str:
    """Normalize a Doxygen location to repo-relative POSIX (STRIP_FROM_PATH should already)."""
    p = path.replace("\\", "/")
    if ":" in p[:3] or p.startswith("/"):  # absolute (drive or posix root)
        try:
            return Path(p).resolve().relative_to(repo.resolve()).as_posix()
        except (ValueError, OSError):
            return p.lstrip("/")
    return p.lstrip("./")


def _strip_template_args(fqn: str) -> str:
    """Remove balanced ``<...>`` template-argument groups from a qualified name.

    Template arguments can themselves contain ``::`` (e.g. ``char_t_impl<is_string<S>::value>``),
    which would otherwise corrupt the ``::``-split namespace derivation below. Depth-aware so
    nested templates are handled. ``fmt::detail::buffer<char>`` -> ``fmt::detail::buffer``.
    """
    out: list[str] = []
    depth = 0
    for ch in fqn:
        if ch == "<":
            depth += 1
        elif ch == ">":
            if depth > 0:
                depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out).strip()


def _local_name(text: str) -> str:
    return _strip_template_args(text).split("::")[-1]


def _namespace_of(fqn: str) -> str | None:
    """The namespace qualifier of a fully-qualified name, or None if unqualified.

    Template args are stripped first so ``::`` inside ``<...>`` never splits the namespace.
    """
    bare = _strip_template_args(fqn)
    return bare.rsplit("::", 1)[0] if "::" in bare else None


def parse_doxygen(xml_dir: Path, model: cmake_api.CMakeModel, repo: Path
                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse Doxygen XML into ``(fact_fragment, file_deps_sidecar)``.

    The fragment aggregates includes to target→target edges (§4.1); the sidecar keeps
    the pre-aggregation file→file edges + file→target attribution for the eval's
    file-granularity graph export (:mod:`anon.filedeps`)."""
    resolver = _TargetResolver(model)

    # index.xml lists every compound (class/struct/namespace/file/...).
    try:
        index = ET.parse(xml_dir / "index.xml").getroot()
    except (ET.ParseError, OSError):
        return _empty_fragment()

    class_refids: list[str] = []
    file_refids: list[str] = []
    for comp in index.findall("compound"):
        kind, refid = comp.get("kind"), comp.get("refid")
        if not refid:
            continue
        if kind in ("class", "struct"):
            class_refids.append(refid)
        elif kind == "file":
            file_refids.append(refid)

    # --- pass 1: file refid -> repo-relative location (for include resolution) ---
    file_loc: dict[str, str] = {}
    file_includes: dict[str, list[str]] = {}  # file refid -> [included file refids]
    for refid in file_refids:
        cd = _compounddef(xml_dir, refid)
        if cd is None:
            continue
        loc = cd.find("location")
        if loc is not None and loc.get("file"):
            file_loc[refid] = _rel(loc.get("file"), repo)
        incs = [inc.get("refid") for inc in cd.findall("includes") if inc.get("refid")]
        if incs:
            file_includes[refid] = incs

    # --- per-file graph for the eval export (BEFORE target aggregation, eval §4.1) ---
    resolved_target: dict[str, str | None] = {}  # file path -> cpp:target:<n> | None

    def _target_id(path: str) -> str | None:
        if path not in resolved_target:
            name = resolver.resolve(path)
            resolved_target[path] = f"cpp:target:{name}" if name else None
        return resolved_target[path]

    file_edges: set[tuple[str, str]] = set()

    # --- include edges: aggregate file includes to target->target (§4.1) ---
    include_counts: dict[tuple[str, str], int] = {}
    include_example: dict[tuple[str, str], str] = {}
    for refid, incs in file_includes.items():
        src_path = file_loc.get(refid)
        if not src_path:
            continue
        src_tid = _target_id(src_path)
        for inc_refid in incs:
            inc_path = file_loc.get(inc_refid)
            if not inc_path:
                continue
            file_edges.add((src_path, inc_path))
            dst_tid = _target_id(inc_path)
            if not src_tid or not dst_tid or dst_tid == src_tid:
                continue
            key = (src_tid.removeprefix("cpp:target:"), dst_tid.removeprefix("cpp:target:"))
            include_counts[key] = include_counts.get(key, 0) + 1
            include_example.setdefault(key, inc_path)

    # --- component/code tier from class compounds (§6.3) ---
    # target -> namespace-key -> {source, classes:set, code_ids:set}
    comp_by_target: dict[str, dict[str, dict[str, Any]]] = {}
    l2_files: dict[str, set[str]] = {}  # target -> set of attributed file paths (coverage)
    for refid in class_refids:
        cd = _compounddef(xml_dir, refid)
        if cd is None:
            continue
        name_el = cd.find("compoundname")
        loc = cd.find("location")
        if name_el is None or name_el.text is None or loc is None or not loc.get("file"):
            continue
        fqn = name_el.text.strip()
        loc_path = _rel(loc.get("file"), repo)
        target = resolver.resolve(loc_path)
        if not target:
            continue
        l2_files.setdefault(target, set()).add(loc_path)
        ns = _namespace_of(fqn)
        if ns:
            key, source = ns, "namespace"
        else:
            # C++ fallback: top-level subdir of the file (§6.3).
            parts = [p for p in loc_path.split("/")[:-1] if p]
            if not parts:
                continue
            key, source = parts[0], "subdir"
        entry = comp_by_target.setdefault(target, {}).setdefault(
            key, {"source": source, "code": []})
        cls = _local_name(fqn)
        if cls and cls not in entry["code"] and len(entry["code"]) < _CODE_CAP:
            entry["code"].append(cls)

    # --- assemble targets with their component tier ---
    targets: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}
    for tname in sorted(model.targets):
        tid = f"cpp:target:{tname}"
        namespaces = sorted(comp_by_target.get(tname, {}).keys())
        target: dict[str, Any] = {
            "id": tid, "name": tname, "type": model.targets[tname].type, "language": "cpp",
        }
        components = []
        for key in namespaces:
            entry = comp_by_target[tname][key]
            comp_id = f"{tid}/component:{key}"
            comp: dict[str, Any] = {
                "id": comp_id, "name": key, "level": "component",
                "source": entry["source"], "key": key,
            }
            code = [{"id": f"{comp_id}/code:{c}", "name": c, "level": "code"}
                    for c in sorted(entry["code"])]
            if code:
                comp["code"] = code
            components.append(comp)
        if components:
            target["components"] = components
            target["namespaces"] = [k for k in namespaces
                                    if comp_by_target[tname][k]["source"] == "namespace"]
        # only emit a target entry if this layer added something (components or coverage)
        if components or tname in l2_files:
            targets.append(target)
            n_files = len(l2_files.get(tname, set()))
            coverage[tid] = {"L2": f"{n_files} files" if n_files else True,
                             "L3": bool(components)}

    # --- include relationships ---
    relationships: list[dict[str, Any]] = []
    for (src, dst), count in sorted(include_counts.items()):
        example = include_example[(src, dst)]
        relationships.append(make_relationship(
            source=f"cpp:target:{src}", target=f"cpp:target:{dst}",
            evidence=[{"type": "include",
                       "detail": f'#include "{example}"' + (f" (+{count - 1} more)" if count > 1 else ""),
                       "count": count}],
        ))

    fragment = {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR], "coverage": coverage},
        "targets": targets,
        "relationships": relationships,
    }
    # Doxygen documents non-source compounds too (README.md, ChangeLog…) — the eval
    # file graph partitions SOURCE files (SAR ground truths are file→component over
    # code), so the sidecar is filtered to the same extension set used for attribution.
    def _is_source(path: str) -> bool:
        return ("." + path.rsplit(".", 1)[-1].lower()) in _SRC_EXTS if "." in path else False

    sidecar = filedeps.make_sidecar(
        EXTRACTOR, "cpp",
        files={p: _target_id(p) for p in file_loc.values() if _is_source(p)},
        edges={(a, b) for a, b in file_edges if _is_source(a) and _is_source(b)},
    )
    return fragment, sidecar


def _compounddef(xml_dir: Path, refid: str) -> ET.Element | None:
    try:
        root = ET.parse(xml_dir / f"{refid}.xml").getroot()
    except (ET.ParseError, OSError):
        return None
    return root.find("compounddef")


def _empty_fragment() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION,
            "provenance": {"extractors": [EXTRACTOR], "coverage": {}},
            "targets": [], "relationships": []}


# --- driver ---------------------------------------------------------------------

def build_fragment(repo: Path, build_dir: Path, doxy_dir: Path
                   ) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Parse the CMake model (reusing *build_dir*) + Doxygen XML into an L2/L3 fragment
    plus the eval file-deps sidecar, or ``None`` on degrade."""
    # Escape hatch (§18.3 cheap-first / §18.4): the L2/L3 Doxygen layer is the expensive one.
    # On a very large repo (opencv/ClickHouse scale) a full Doxygen run blows the time budget,
    # so ANON_SKIP_DOXYGEN=1 keeps the cheap L0/L1 backbone and honestly leaves L2 false.
    if os.environ.get("ANON_SKIP_DOXYGEN"):
        log.info("ANON_SKIP_DOXYGEN set — skipping C++ L2/L3 Doxygen layer (§18.3)")
        return None
    model = cmake_api.parse_file_api(build_dir, repo)
    if model is None:
        # extract_cmake didn't configure (or not a CMake repo) — try once ourselves.
        model = cmake_api.build_model(repo, build_dir, configure=True)
    if model is not None and model.targets:
        model = cmake_api.overlay_graphviz(model, build_dir)
    else:
        # Non-CMake C/C++: fall back to the autotools Makefile model (same neutral
        # CMakeModel shape, plan §4.1) so a bash/libxml2-style repo still gets its
        # Doxygen L2 include edges attributed to real build targets.
        from .extract_autotools import build_model as autotools_model
        model = autotools_model(repo)
        if model is None or not model.targets:
            log.info("no CMake model to attribute Doxygen facts against — skipping C++ L2/L3 (§18.4)")
            return None
        log.info("using the autotools Makefile model for Doxygen target attribution (§18.4)")

    xml_dir = ensure_doxygen_xml(repo, doxy_dir)
    if xml_dir is None:
        return None
    fragment, sidecar = parse_doxygen(xml_dir, model, repo)
    if not fragment["targets"] and not fragment["relationships"]:
        return None  # Doxygen ran but produced nothing attributable — degrade quietly
    return fragment, sidecar


def run(ws: Workspace) -> Path | None:
    """Extract C++ L2/L3 Doxygen facts and write the fragment, or ``None`` on degrade.

    Also writes the eval per-file sidecar (``file-deps-doxygen-xml.json``) — machine-
    local, outside the fact model, consumed by ``arch export --graph --granularity file``."""
    build_dir = ws.cmake_build_dir
    doxy_dir = ws.cache / "doxygen"
    result = build_fragment(ws.repo, build_dir, doxy_dir)
    if result is None:
        return None
    fragment, sidecar = result
    out = ws.fragments / "doxygen-xml.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_json(fragment, out)
    if sidecar["files"]:
        filedeps.write_sidecar(ws, sidecar)
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
    print(result if result else "(degraded: no C++ Doxygen facts)")
