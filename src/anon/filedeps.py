"""Eval §4.1 — the per-file dependency sidecar behind ``arch export --graph --granularity file``.

The evaluation's *primary* RQ2 comparison runs every technique over a **file-level**
dependency graph (SAR ground truths map source files → components, eval §4.1) — but the
fact model deliberately aggregates evidence to target→target edges (a thousand includes
collapse to one weighted edge, P§4.1), so per-file edges do not survive into
``extracted-facts.json`` and never will: the fact model is the *architecture* contract,
not a code-level database.

Per-file data therefore travels as a **sidecar fragment** next to the fact fragments::

    <arch-dir>/.cache/fragments/file-deps-<extractor>.json

    {"schema": "file-deps/1", "extractor": {...}, "language": "cpp",
     "files": {"<repo-rel-posix>": "cpp:target:<name>" | null, ...},   # owning target
     "edges": [["src/a.cpp", "include/b.hpp"], ...]}                   # file -> file

Producers: ``extract_cpp_facts`` (Doxygen include graph — pass 1 already has every
file→file edge before aggregation), ``extract_clang_deps`` (compile-DB-accurate
include graph, repo-internal paths only), and ``extract_csharp_facts`` (the Roslyn helper's
``fileDeps`` block: per-document owning csproj + file→file edges from the semantic model — a
symbol use links the using file to the symbol's in-source defining file, intra- OR
cross-project). C# file granularity now travels the same sidecar path as C++; its coverage
inherits Roslyn's (a project that fails to load — e.g. eShop's net10/Aspire restore, §19.5 —
contributes no file edges, an honest partial graph rather than a wrong one). The C# fidelity
references remain project/service-granularity GT-doc, so file-level scoring projects each
project's reference label down to its files (eval §5.2).

The sidecar is machine-local (gitignored under ``.cache/``), OUTSIDE the determinism-
hashed core and the fact-model schema — adding it bumps nothing and perturbs no golden.
It is still written deterministically (sorted, via ``jsonio``) so the derived
``graph-file.rsf`` is byte-identical across runs.

``files`` keys every located repo file (even edge-less ones — isolated nodes are real
entities for clustering); the value is the owning target id or ``null`` when target
attribution failed (the export *counts* those, never silently drops them).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .jsonio import dump_json
from .paths import Workspace

FILEDEPS_SCHEMA = "file-deps/1"


def make_sidecar(extractor: dict[str, str], language: str,
                 files: dict[str, str | None],
                 edges: set[tuple[str, str]]) -> dict[str, Any]:
    """Assemble a canonical ``file-deps/1`` sidecar (sorted, self-loops dropped)."""
    return {
        "schema": FILEDEPS_SCHEMA,
        "extractor": extractor,
        "language": language,
        "files": dict(sorted(files.items())),
        "edges": sorted([a, b] for a, b in edges if a != b),
    }


def write_sidecar(ws: Workspace, sidecar: dict[str, Any]) -> Path:
    """Write the sidecar as ``file-deps-<extractor-name>.json`` under the fragments dir."""
    out = ws.fragments / f"file-deps-{sidecar['extractor']['name']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_json(sidecar, out)
    return out


def load_sidecars(ws: Workspace) -> list[dict[str, Any]]:
    """All file-deps sidecars, in sorted-filename order (= deterministic merge precedence)."""
    from .jsonio import load_json
    out = []
    for p in sorted(ws.fragments.glob("file-deps-*.json")):
        data = load_json(p)
        if isinstance(data, dict) and data.get("schema") == FILEDEPS_SCHEMA:
            out.append(data)
    return out


def merge(sidecars: list[dict[str, Any]]
          ) -> tuple[dict[str, str | None], set[tuple[str, str]], int]:
    """Union the sidecars into one file graph.

    Returns ``(files, edges, attribution_conflicts)``. On a file claimed by two
    extractors with *different* owning targets the first (sorted-filename order) wins
    and the conflict is counted — corroborating extractors (Doxygen + clang) should
    agree, so a non-zero count is a finding, not noise to hide. A ``null`` attribution
    is filled in by any later sidecar that knows the owner.
    """
    files: dict[str, str | None] = {}
    edges: set[tuple[str, str]] = set()
    conflicts = 0
    for sc in sidecars:
        for f, tid in sc.get("files", {}).items():
            if f not in files:
                files[f] = tid
            elif tid and files[f] is None:
                files[f] = tid
            elif tid and files[f] and tid != files[f]:
                conflicts += 1
        for a, b in sc.get("edges", []):
            if a != b:
                edges.add((a, b))
    return files, edges, conflicts
