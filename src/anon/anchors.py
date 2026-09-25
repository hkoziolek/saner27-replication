"""Informative-narratives plan §2.2 — (C) curated **anchor excerpts**.

A *small, deterministically chosen* set of real source snippets handed to the §4 narrative
model under a hard per-element char budget. Selection is a **pure function of facts +
``file-deps``** (so it is hashable, previewable, cacheable, §2.2); the only I/O is reading
the head of the chosen files. Three signals, in priority order:

  1. the element's **README / ``*.md``** (top of file) — the authors' own framing;
  2. the **entry point** (``Program.cs`` / ``Startup.cs`` / ``*Module.cs`` / ``main.*``) —
     the header region, where wiring/responsibility lives;
  3. the **1-3 highest-centrality files** by in-repo fan-in within the element (the files
     everything else in the element imports — the conceptual core, ranked from the
     ``file-deps/1`` sidecar).

Never whole files: every excerpt is head-capped. Redaction is applied by the caller
(:mod:`enrich_llm`) so the exact bytes that leave are governed by the one scrub +
egress-preview path (§7).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Per-excerpt head cap (chars) and the default count of centrality files. The total budget
# (how many excerpts survive) is the caller's band budget (§4); these bound a single excerpt.
HEAD_CHARS = 800
MAX_CENTRALITY_FILES = 3

_ENTRYPOINT_BASENAMES = (
    "program.cs", "startup.cs", "app.cs", "global.asax.cs",
    "program.fs", "startup.fs",
    "main.cpp", "main.cc", "main.cxx", "main.c",
)


def _is_entrypoint(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1].lower()
    return base in _ENTRYPOINT_BASENAMES or base.endswith("module.cs")


def _is_readme(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1].lower()
    return base.startswith("readme") or base.endswith(".md")


def select_anchors(member_files: set[str], edges: set[tuple[str, str]], *,
                   max_centrality: int = MAX_CENTRALITY_FILES) -> list[dict[str, str]]:
    """Pure, hashable selection: ``[{path, kind}]`` for an element, deterministically.

    *member_files* is the set of repo-relative source files the element owns; *edges* is the
    file→file dependency set (``file-deps/1``). Returns at most one README, one entry point,
    and the top-*max_centrality* highest in-repo fan-in files (excluding ones already chosen),
    each tagged with its ``kind`` (``readme``/``entrypoint``/``centrality``). Order is stable
    and meaningful: README, entry point, then centrality by descending fan-in then path."""
    chosen: list[dict[str, str]] = []
    used: set[str] = set()

    readme = min((f for f in member_files if _is_readme(f)), default=None)
    if readme:
        chosen.append({"path": readme, "kind": "readme"})
        used.add(readme)

    entry = min((f for f in member_files if _is_entrypoint(f) and f not in used),
                default=None)
    if entry:
        chosen.append({"path": entry, "kind": "entrypoint"})
        used.add(entry)

    # in-repo fan-in WITHIN the element: how many member files import this member file.
    fan_in: dict[str, int] = {}
    for a, b in edges:
        if a in member_files and b in member_files and a != b:
            fan_in[b] = fan_in.get(b, 0) + 1
    ranked = sorted((f for f in fan_in if f not in used),
                    key=lambda f: (-fan_in[f], f))
    for f in ranked[:max_centrality]:
        chosen.append({"path": f, "kind": "centrality"})
        used.add(f)
    return chosen


def read_excerpt(repo: Path, rel: str, *, head_chars: int = HEAD_CHARS) -> str | None:
    """Read the head (``head_chars``) of a repo-relative file, or None if unreadable.

    Deterministic and bounded: never returns more than *head_chars*; truncated at a line
    boundary when possible so a snippet is not cut mid-token."""
    try:
        path = (repo / rel).resolve()
        repo_res = repo.resolve()
        # path-traversal guard: the file must live under the repo root.
        path.relative_to(repo_res)
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    if len(text) <= head_chars:
        return text.strip() or None
    head = text[:head_chars]
    nl = head.rfind("\n")
    if nl > head_chars // 2:
        head = head[:nl]
    return head.strip() or None


def anchor_excerpts(repo: Path, member_files: set[str], edges: set[tuple[str, str]], *,
                    budget_chars: int, head_chars: int = HEAD_CHARS,
                    max_centrality: int = MAX_CENTRALITY_FILES) -> list[dict[str, Any]]:
    """The §2.2 anchor set: ``[{path, kind, text}]`` for an element, under *budget_chars*.

    Excerpts are added in :func:`select_anchors` priority order until the cumulative text
    length would exceed *budget_chars*; an unreadable file is skipped (it never consumes
    budget). Returns ``[]`` when no facts/files support an excerpt — honest absence, never
    filler (plan §4)."""
    out: list[dict[str, Any]] = []
    spent = 0
    for sel in select_anchors(member_files, edges, max_centrality=max_centrality):
        text = read_excerpt(repo, sel["path"], head_chars=head_chars)
        if not text:
            continue
        if spent + len(text) > budget_chars and out:
            break
        out.append({"path": sel["path"], "kind": sel["kind"], "text": text})
        spent += len(text)
    return out
