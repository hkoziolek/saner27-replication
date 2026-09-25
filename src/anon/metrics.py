"""Deterministic cloc-like source metrics (datasheet-enrichment plan §3, ``metrics/1``).

Counts, per build target, per-language ``{files, blank, comment, code}`` plus a
``todo_count`` (``TODO``/``FIXME``/``HACK`` markers in comments). The numbers are
**Class-F facts** (plan §2): a pure function of the working tree at a commit, so they
live in the hashed extracted core — a commit that changes code *should* change the
fact-model hash.

Deliberately NOT cloc. Shelling out to cloc would add a perl runtime whose version
drifts across machines (a determinism hazard) and whose absence would make graceful
degradation the common case. This module promises **consistency, not cloc-parity**:
the extension→language map and the per-language comment syntax are pinned and
versioned as :data:`METRICS_ENGINE`; any heuristic improvement is a reviewable
version bump + golden regen, never silent churn.

Determinism traps handled here (plan §3.2):
  - **CRLF vs LF working trees** — lines are split from *bytes* (``splitlines`` over
    the decoded text handles ``\\r\\n``/``\\n``/``\\r`` identically), so the same file
    checked out with either line ending counts identically. Tested.
  - **Encoding** — decoded as UTF-8 with ``errors="replace"``; a stray invalid byte
    never raises and replaces identically everywhere.
  - **Ordering** — ``by_language`` rows are sorted by language name; the per-target
    attribution uses a longest-path-prefix rule with a sorted owner index.

Attribution: each file belongs to the target with the **deepest** owning ``path``
(most-specific wins), mirroring how a parent-directory CMake target must not swallow
files of its nested targets. Files under no target path are not counted. Targets with
no ``path`` (packages, external boundaries) get no metrics.

The naive block-comment state machine counts a mixed code+comment line as **code**
(the cloc convention). Python docstrings/triple-quoted strings count as code — a
documented limitation, not a bug.
"""
from __future__ import annotations

import logging
import time
from fnmatch import fnmatch
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("anon.metrics")

METRICS_ENGINE = "metrics/1"

# Files larger than this are counted in `files` but contribute no line counts
# (a generated bundle/megafile would dominate and slow the walk, plan §3.2).
MAX_FILE_BYTES = 2 * 1024 * 1024
_BINARY_SNIFF_BYTES = 8192

_TODO_MARKERS = ("TODO", "FIXME", "HACK")

# Default exclusions (plan §3.2) — extendable (never replaceable) via the
# `metrics.exclude` knob in mapping-rules.yaml. fnmatch-style where `*` crosses `/`.
DEFAULT_EXCLUDE = (
    "**/obj/**", "**/bin/**", "**/.git/**", "**/node_modules/**",
    "**/*.g.cs", "**/*_generated.*", "**/*.Designer.cs",
)
# Directory names pruned during the walk for speed (must stay consistent with
# DEFAULT_EXCLUDE — pruning is an optimization, the glob check is the contract).
_PRUNE_DIRS = {".git", "obj", "bin", "node_modules"}

# --- the pinned metrics/1 language tables -----------------------------------------
# ext -> display language. Closed, versioned: extending it is a METRICS_ENGINE bump.
_EXT_LANG: dict[str, str] = {
    ".c": "C", ".h": "C/C++ Header", ".cpp": "C++", ".cc": "C++", ".cxx": "C++",
    ".hpp": "C/C++ Header", ".hxx": "C/C++ Header", ".hh": "C/C++ Header",
    ".inl": "C/C++ Header",
    ".cs": "C#", ".csx": "C#",
    ".py": "Python", ".rb": "Ruby",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".go": "Go", ".rs": "Rust",
    ".ps1": "PowerShell", ".psm1": "PowerShell",
    ".sh": "Shell", ".bat": "Batch", ".cmd": "Batch",
    ".sql": "SQL", ".proto": "Protocol Buffers",
    ".xml": "XML", ".slnx": "XML",
    ".csproj": "MSBuild", ".vcxproj": "MSBuild", ".props": "MSBuild",
    ".targets": "MSBuild",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI",
    ".md": "Markdown", ".html": "HTML", ".htm": "HTML",
    ".cshtml": "Razor", ".razor": "Razor",
    ".css": "CSS", ".scss": "SCSS",
    ".cmake": "CMake", ".idl": "IDL",
}
_NAME_LANG: dict[str, str] = {
    "CMakeLists.txt": "CMake", "Dockerfile": "Dockerfile", "Makefile": "Makefile",
}

# language -> (line-comment prefixes, block-comment (start, end) pairs).
# A language absent here has NO comment syntax (every non-blank line is code).
_C_LIKE = (("//",), (("/*", "*/"),))
_HASH = (("#",), ())
_XML_LIKE = ((), (("<!--", "-->"),))
_SYNTAX: dict[str, tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = {
    "C": _C_LIKE, "C++": _C_LIKE, "C/C++ Header": _C_LIKE, "C#": _C_LIKE,
    "JavaScript": _C_LIKE, "TypeScript": _C_LIKE, "Java": _C_LIKE, "Go": _C_LIKE,
    "Rust": _C_LIKE, "CSS": _C_LIKE, "SCSS": _C_LIKE, "Protocol Buffers": _C_LIKE,
    "IDL": _C_LIKE, "Razor": _C_LIKE,
    "SQL": (("--",), (("/*", "*/"),)),
    "Python": _HASH, "Ruby": _HASH, "Shell": _HASH, "YAML": _HASH, "TOML": _HASH,
    "CMake": _HASH, "Dockerfile": _HASH, "Makefile": _HASH,
    "INI": ((";", "#"), ()),
    "PowerShell": (("#",), (("<#", "#>"),)),
    "Batch": (("REM ", "rem ", "::"), ()),
    "XML": _XML_LIKE, "MSBuild": _XML_LIKE, "HTML": _XML_LIKE,
}


def classify_language(filename: str) -> str | None:
    """The pinned metrics/1 filename→language rule; None = not a counted source file."""
    if filename in _NAME_LANG:
        return _NAME_LANG[filename]
    dot = filename.rfind(".")
    if dot <= 0:
        return None
    return _EXT_LANG.get(filename[dot:].lower())


def _is_binary(head: bytes) -> bool:
    return b"\x00" in head


def count_text(text: str, language: str) -> dict[str, int]:
    """Count ``{blank, comment, code, todo}`` lines of *text* under *language*'s syntax.

    The naive state machine (plan §3.2): blank stays blank even inside a block
    comment; a line mixing code and comment counts as code; TODO markers are counted
    in comment lines and in the tail after a line-comment marker on code lines.
    """
    line_markers, block_pairs = _SYNTAX.get(language, ((), ()))
    blank = comment = code = todo = 0
    in_block_end: str | None = None

    def _has_todo(s: str) -> bool:
        return any(m in s for m in _TODO_MARKERS)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            blank += 1
            continue
        if in_block_end is not None:
            end = line.find(in_block_end)
            if end == -1:
                comment += 1
                if _has_todo(line):
                    todo += 1
                continue
            rest = line[end + len(in_block_end):].strip()
            in_block_end = None
            if _has_todo(line[:end]):
                todo += 1
            if not rest:
                comment += 1
                continue
            line = rest  # fall through: classify the remainder
        if line_markers and any(line.startswith(m) for m in line_markers):
            comment += 1
            if _has_todo(line):
                todo += 1
            continue
        opened = False
        for start, end_marker in block_pairs:
            if line.startswith(start):
                opened = True
                close = line.find(end_marker, len(start))
                if close == -1:
                    in_block_end = end_marker
                    comment += 1
                    if _has_todo(line):
                        todo += 1
                else:
                    rest = line[close + len(end_marker):].strip()
                    if _has_todo(line[:close]):
                        todo += 1
                    if rest:
                        code += 1
                    else:
                        comment += 1
                break
        if opened:
            continue
        code += 1
        # a trailing line comment on a code line can still carry a TODO
        for m in line_markers:
            pos = line.find(m)
            if pos > 0 and _has_todo(line[pos:]):
                todo += 1
                break
        # a block comment opened mid-line after code continues onto the next lines
        for start, end_marker in block_pairs:
            pos = line.find(start)
            if pos > 0 and line.find(end_marker, pos + len(start)) == -1:
                in_block_end = end_marker
                break
    return {"blank": blank, "comment": comment, "code": code, "todo": todo}


def _excluded(rel: str, globs: tuple[str, ...]) -> bool:
    """fnmatch the repo-relative posix path (also anchored with a leading '/', so a
    leading '**/' pattern matches top-level entries too)."""
    return any(fnmatch(rel, g) or fnmatch("/" + rel, g) for g in globs)


def _owner_index(facts: dict[str, Any]) -> list[tuple[str, str]]:
    """``(path, target_id)`` sorted longest-path-first — most-specific owner wins."""
    idx = []
    for t in facts.get("targets", []) or []:
        p = (t.get("path") or "").replace("\\", "/").strip("/")
        if p and t.get("id"):
            idx.append((p, t["id"]))
    # longest first; ties broken by (path, id) so the index is fully deterministic
    return sorted(idx, key=lambda pi: (-len(pi[0]), pi[0], pi[1]))


def measure(repo: Path, facts: dict[str, Any],
            exclude: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """One walk over *repo*, attributing each counted file to its deepest owning
    target. Returns ``{target_id: metrics-block}`` for targets with ≥1 counted file."""
    globs = tuple(DEFAULT_EXCLUDE) + tuple(exclude or ())
    owners = _owner_index(facts)
    if not owners:
        return {}
    # per target: language -> [files, blank, comment, code]; plus todo total
    tally: dict[str, dict[str, list[int]]] = {}
    todos: dict[str, int] = {}

    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE_DIRS)
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            rel = full.relative_to(repo).as_posix()
            if _excluded(rel, globs):
                continue
            lang = classify_language(fn)
            if lang is None:
                continue
            tid = None
            for p, owner in owners:
                if rel == p or rel.startswith(p + "/"):
                    tid = owner
                    break
            if tid is None:
                continue
            try:
                size = full.stat().st_size
                with open(full, "rb") as fh:
                    head = fh.read(_BINARY_SNIFF_BYTES)
                    if _is_binary(head):
                        continue
                    # an oversize file is counted in `files` only (plan §3.2)
                    data = None if size > MAX_FILE_BYTES else head + fh.read()
            except OSError:
                continue
            row = tally.setdefault(tid, {}).setdefault(lang, [0, 0, 0, 0])
            row[0] += 1
            if data is None:
                continue
            c = count_text(data.decode("utf-8", errors="replace"), lang)
            row[1] += c["blank"]
            row[2] += c["comment"]
            row[3] += c["code"]
            todos[tid] = todos.get(tid, 0) + c["todo"]

    out: dict[str, dict[str, Any]] = {}
    for tid, langs in tally.items():
        by_lang = [{"language": lang, "files": v[0], "blank": v[1],
                    "comment": v[2], "code": v[3]}
                   for lang, v in sorted(langs.items())]
        out[tid] = {
            "engine": METRICS_ENGINE,
            "blank": sum(r["blank"] for r in by_lang),
            "comment": sum(r["comment"] for r in by_lang),
            "code": sum(r["code"] for r in by_lang),
            "todo_count": todos.get(tid, 0),
            "by_language": by_lang,
        }
    return out


def annotate(facts: dict[str, Any], repo: Path,
             exclude: list[str] | None = None) -> dict[str, Any]:
    """Fold measured source metrics onto ``targets[].metrics`` (post-merge, like
    ``interop_resolve`` — the walker needs the *merged* target paths for the
    deepest-owner attribution, so it cannot be an independent fragment; deviation
    from the plan's 'new extractor' phrasing recorded here on purpose).

    Existing metrics keys another extractor supplied (e.g. cmake's declared-source
    ``files``) are preserved; this only adds the metrics/1 keys. Mutates + returns
    *facts*."""
    # The one full repo walk is what dominates `normalize` wall-clock on a large tree —
    # say so on the console (console-only; the counted facts stay deterministic).
    start = time.monotonic()
    measured = measure(repo, facts, exclude)
    files = sum(r["files"] for m in measured.values() for r in m["by_language"])
    log.info("source metrics (%s): counted %d file(s) across %d target(s) in one repo "
             "walk (%.1fs)", METRICS_ENGINE, files, len(measured),
             time.monotonic() - start)
    for t in facts.get("targets", []) or []:
        m = measured.get(t.get("id"))
        if not m:
            continue
        existing = dict(t.get("metrics") or {})
        existing.update(m)
        t["metrics"] = existing
    return facts
