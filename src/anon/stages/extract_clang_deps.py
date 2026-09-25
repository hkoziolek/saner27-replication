"""Stage 1 — C++ L2 include graph via ``clang-scan-deps`` (plan §4.1, node 1).

An **alternative to the Doxygen L2** (:mod:`extract_cpp_facts`) for the include graph, driven
by a ``compile_commands.json`` (from the CMake export, or the MSBuild L1 reconstruction in
:mod:`extract_msbuild_cpp`). Where Doxygen parses doc-XML, this uses Clang's own preprocessor
via ``clang-scan-deps`` — so the include edges reflect the *actual* compile configuration
(the L1 flags), which is the whole reason L1 matters (§4.1). It emits the same ``include``
evidence edges under the same ``cpp:target:<name>`` / ``cpp:vcxproj:<path>`` ids, so
``normalize_facts`` merges and corroborates them exactly like the Doxygen edges (§6.4).

Pairing: Doxygen also yields the namespace/class component tier (which clang-scan-deps does
not), so the two are complementary — a repo with clang but no Doxygen still gets the L2
include graph here; a repo with both gets corroboration. This stage produces **edges only**
(no component tier).

Graceful degradation (plan §18.4): no ``clang-scan-deps`` on PATH, no compile DB, or no C++
model to attribute against → ``None`` and nothing written. The make-format parser is split out
and pure so it is unit-tested without clang.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION, cmake_api, filedeps
from ..jsonio import dump_json
from ..model import make_relationship
from ..paths import Workspace

log = logging.getLogger("anon.extract_clang_deps")

EXTRACTOR = {"name": "clang-scan-deps", "version": "0.1.0"}

_SCAN_TIMEOUT_S = 1800
_SRC_EXTS = (".c", ".cc", ".cpp", ".cxx")


def clang_scan_deps_available() -> bool:
    return shutil.which("clang-scan-deps") is not None


# --- target attribution (absolute-path, exact-then-dir-prefix) ------------------

class TargetResolver:
    """Map an absolute source/header path to a target id (exact source, then dir prefix)."""

    def __init__(self, entries: list[tuple[str, Path, set[Path]]]):
        # entries: (target_id, source_dir_abs, {source_file_abs, ...})
        self._exact: dict[Path, str] = {}
        self._dirs: list[tuple[Path, str]] = []
        for tid, src_dir, srcs in entries:
            for s in srcs:
                self._exact[s] = tid
            if src_dir is not None:
                self._dirs.append((src_dir, tid))
        self._dirs.sort(key=lambda x: len(str(x[0])), reverse=True)  # longest prefix wins

    def resolve(self, path: Path) -> str | None:
        try:
            p = path.resolve()
        except (OSError, ValueError):
            p = path
        if p in self._exact:
            return self._exact[p]
        for src_dir, tid in self._dirs:
            try:
                p.relative_to(src_dir)
                return tid
            except ValueError:
                continue
        return None


def resolver_from_cmake(build_dir: Path, repo: Path) -> TargetResolver | None:
    """Build a resolver from the CMake File API model (sources per ``cpp:target``)."""
    model = cmake_api.parse_file_api(build_dir, repo)
    if model is None:
        return None
    entries: list[tuple[str, Path, set[Path]]] = []
    for name, t in model.targets.items():
        srcs = {(repo / s).resolve() for s in t.sources}
        src_dir = (repo / t.source_dir).resolve() if t.source_dir else None
        entries.append((f"cpp:target:{name}", src_dir, srcs))
    return TargetResolver(entries)


def resolver_from_msbuild(repo: Path) -> TargetResolver | None:
    """Build a resolver from the MSBuild ``.vcxproj`` sources (``cpp:vcxproj`` ids)."""
    from .extract_msbuild_cpp import _all_vcxproj, _parse_vcxproj, _vcxproj_id
    vcxprojs = _all_vcxproj(repo)
    if not vcxprojs:
        return None
    entries: list[tuple[str, Path, set[Path]]] = []
    for v in vcxprojs:
        info = _parse_vcxproj(v, repo, None)
        srcs = {Path(s).resolve() for s in info.get("sources", [])}
        entries.append((_vcxproj_id(v, repo), v.parent.resolve(), srcs))
    return TargetResolver(entries)


# --- clang-scan-deps run + parse ------------------------------------------------

def run_scan_deps(compile_db: Path) -> str | None:
    """Run ``clang-scan-deps -format make`` over *compile_db*; return stdout or None."""
    if not clang_scan_deps_available():
        log.info("clang-scan-deps not on PATH — skipping clang L2 (§18.4)")
        return None
    cmd = ["clang-scan-deps", "-format", "make",
           "-compilation-database", str(compile_db)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_SCAN_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("clang-scan-deps failed: %s — degrading (§18.4)", exc)
        return None
    if proc.returncode != 0:
        log.warning("clang-scan-deps rc=%s — degrading (§18.4):\n%s",
                    proc.returncode, (proc.stderr or "").strip()[-1000:])
        return proc.stdout or None
    return proc.stdout


def parse_make_deps(text: str) -> list[tuple[str, list[str]]]:
    """Parse ``clang-scan-deps -format make`` output into ``[(output, [dep_paths]), ...]``.

    The make format is ``<output>: <dep> <dep> …`` with ``\\`` line-continuations; the first
    dependency is the translation unit and the rest are the headers it pulls in. Pure/testable.
    """
    # join line-continuations, then split into logical rules. clang emits one rule per TU.
    rules: list[tuple[str, list[str]]] = []
    # normalize continuations
    joined = text.replace("\\\r\n", " ").replace("\\\n", " ")
    for line in joined.splitlines():
        line = line.strip()
        if not line:
            continue
        # The rule-separator colon is the first ':' followed by whitespace/end. A Windows
        # drive-letter colon (e.g. an absolute output 'C:\build\foo.o:') is followed by a
        # path separator, never whitespace — so `line.partition(':')` would split there and
        # mangle the output + deps. Skip those drive colons.
        sep = next((i for i, ch in enumerate(line)
                    if ch == ":" and (i + 1 >= len(line) or line[i + 1].isspace())), -1)
        if sep == -1:
            continue
        output, deps_str = line[:sep], line[sep + 1:]
        deps = [d for d in deps_str.split() if d]
        if deps:
            rules.append((output.strip(), deps))
    return rules


def build_fragment(deps: list[tuple[str, list[str]]], resolver: TargetResolver) -> dict[str, Any]:
    """Aggregate file-level include deps into target->target ``include`` edges."""
    # (src_target, dst_target) -> (count, example header)
    counts: dict[tuple[str, str], int] = {}
    example: dict[tuple[str, str], str] = {}
    for _output, files in deps:
        # the TU is the first source-extension file; the rest are headers it includes.
        tu = next((f for f in files if f.lower().endswith(_SRC_EXTS)), None)
        if tu is None:
            continue
        src_target = resolver.resolve(Path(tu))
        if not src_target:
            continue
        for header in files:
            if header == tu:
                continue
            dst_target = resolver.resolve(Path(header))
            if not dst_target or dst_target == src_target:
                continue
            key = (src_target, dst_target)
            counts[key] = counts.get(key, 0) + 1
            example.setdefault(key, header)

    relationships: list[dict[str, Any]] = []
    for (src, dst), count in sorted(counts.items()):
        relationships.append(make_relationship(
            source=src, target=dst,
            evidence=[{"type": "include",
                       "detail": f"#include (clang-scan-deps) {Path(example[(src, dst)]).name}"
                                 + (f" (+{count - 1} more)" if count > 1 else ""),
                       "count": count}]))
    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR]},
        "targets": [],
        "relationships": relationships,
    }


def file_deps(deps: list[tuple[str, list[str]]], resolver: TargetResolver, repo: Path
              ) -> tuple[dict[str, str | None], set[tuple[str, str]]]:
    """Per-file ``(files, edges)`` for the eval sidecar (:mod:`anon.filedeps`).

    Repo-internal paths only — clang lists every system header a TU pulls in, but the
    file-granularity graph (eval §4.1) partitions *first-party* files, matching the SAR
    ground truths. Edges run TU → header (the make rule has no header→header nesting,
    so this is the flattened transitive include set per TU — recorded as-is)."""
    repo_resolved = repo.resolve()
    rel_cache: dict[str, str | None] = {}

    def _rel(path: str) -> str | None:
        if path not in rel_cache:
            try:
                rel_cache[path] = Path(path).resolve().relative_to(repo_resolved).as_posix()
            except (ValueError, OSError):
                rel_cache[path] = None  # outside the repo (system/SDK header)
        return rel_cache[path]

    files: dict[str, str | None] = {}
    edges: set[tuple[str, str]] = set()
    for _output, paths in deps:
        tu = next((f for f in paths if f.lower().endswith(_SRC_EXTS)), None)
        if tu is None:
            continue
        tu_rel = _rel(tu)
        if tu_rel is None:
            continue
        files.setdefault(tu_rel, resolver.resolve(Path(tu)))
        for header in paths:
            if header == tu:
                continue
            h_rel = _rel(header)
            if h_rel is None or h_rel == tu_rel:
                continue
            files.setdefault(h_rel, resolver.resolve(Path(header)))
            edges.add((tu_rel, h_rel))
    return files, edges


def run(ws: Workspace) -> Path | None:
    """Run the clang-scan-deps L2 include scan and write a fragment, or ``None`` on degrade."""
    if not clang_scan_deps_available():
        return None
    # locate a compile DB (CMake export, else MSBuild reconstruction) + a matching resolver.
    cmake_db = ws.cmake_build_dir / "compile_commands.json"
    msbuild_db = ws.cache / "msbuild-cpp" / "compile_commands.json"
    if cmake_db.exists():
        compile_db, resolver = cmake_db, resolver_from_cmake(ws.cmake_build_dir, ws.repo)
    elif msbuild_db.exists():
        compile_db, resolver = msbuild_db, resolver_from_msbuild(ws.repo)
    else:
        log.info("no compile_commands.json (CMake or MSBuild) — skipping clang L2 (§18.4)")
        return None
    if resolver is None:
        return None
    text = run_scan_deps(compile_db)
    if not text:
        return None
    deps = parse_make_deps(text)
    fragment = build_fragment(deps, resolver)
    if not fragment["relationships"]:
        return None
    out = ws.fragments / "clang-scan-deps.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_json(fragment, out)
    files, edges = file_deps(deps, resolver, ws.repo)
    if files:
        filedeps.write_sidecar(ws, filedeps.make_sidecar(EXTRACTOR, "cpp", files, edges))
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
    print(run(w) or "(degraded: no clang-scan-deps L2 facts)")
