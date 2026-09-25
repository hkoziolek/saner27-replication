"""Stage 1 — OrchardCore module feature-category facts (plan §4.2; a build-graph *refinement*).

OrchardCore apps declare each module's functional family **in source**: every module project
carries a ``Manifest.cs`` with ``[assembly: Feature(... Category = "Content Management" ...)]``.
The empirical evaluation (``plans/notes/2026-06-14-a2-curation-ablation.md`` and
``baselines/probe_orchardcore_manifest.py``) found this ``Category`` is exactly the signal the
reference architecture groups modules by: clustering modules by their manifest category recovers
**97.8%** of the reference's ``mod_*`` families and, as a full clustering, *beats* the
LLM-authored hand curation (MoJoFM 76.0 vs 72.1) — deterministically, with no LLM and no build.
No directory, naming, or dependency heuristic comes within ~35 MoJoFM of it (it is a source-declared
fact none of them can see).

This extractor surfaces that fact as a governed **grouping-hint tag** so curation can group on it
*without* an LLM — the deterministic-first invariant (LLMs touch names/descriptions only, never
structure). For each ``.csproj`` whose directory holds a ``Manifest.cs`` declaring a Feature
``Category``, it emits a ``module-category:<Category>`` tag on the existing
``csharp:csproj:<repo-relative-path>`` target — the SAME id scheme as ``extract_build_graph`` — so
``normalize_facts`` unions the tag onto that target (§6.4c, ``tags`` is a union list field). It is a
sibling of the ``slnfolder:<name>`` hint the build-graph extractor already emits.

It adds **no relationships and no structure** (§4: refine, never replace) and claims no coverage
layer (the manifest is metadata, not dependency evidence). Pure text/XML-free parse — no .NET SDK.
A repo with no OrchardCore manifests (every non-OrchardCore C# repo, the toy fixture, all C++
fixtures) yields an empty fragment: a graceful no-op (§18.4), never a hard failure.
"""
from __future__ import annotations

import collections
import re
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..jsonio import dump_json
from ..paths import Workspace

EXTRACTOR = {"name": "orchardcore-manifest", "version": "0.1.0"}

# Build-output / VCS dirs to skip (mirrors extract_build_graph._SKIP_DIRS) so a Manifest.cs
# copied into bin/obj or vendored under node_modules/.git is not picked up.
_SKIP_DIRS = {"bin", "obj", ".git", "node_modules"}

# A Feature/Module Category assignment: ``Category = "Content Management"``. All 108 OrchardCore
# manifests use string literals (no shared constants), so a literal-only match is complete; a
# constant-valued Category would simply be ignored (honest no-tag rather than a mangled tag).
_CATEGORY = re.compile(r'Category\s*=\s*"([^"]+)"')


def _norm(path: Path, repo: Path) -> str:
    """Repo-relative POSIX path (stable across OSes), matching extract_build_graph._norm."""
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _all_csproj(repo: Path) -> list[Path]:
    """All ``.csproj`` under the repo, excluding build-output dirs (sorted for determinism)."""
    return sorted(p.resolve() for p in repo.rglob("*.csproj")
                  if not ({part.lower() for part in p.parts} & _SKIP_DIRS))


def _primary_category(manifest: Path) -> str | None:
    """The module's primary feature ``Category`` from its ``Manifest.cs``, or ``None``.

    A module may declare several features; the reference assigns one family per project, so we
    take the most frequently declared ``Category`` literal in the file (modules are near-
    homogeneous), resolving ties to first occurrence — ``Counter.most_common`` preserves insertion
    order for equal counts, so the choice is deterministic.
    """
    try:
        cats = _CATEGORY.findall(manifest.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not cats:
        return None
    return collections.Counter(cats).most_common(1)[0][0]


def _tag_value(category: str) -> str:
    """Tag-safe stem for a category: strip non-alphanumerics (``"Content Management"`` ->
    ``ContentManagement``, ``"OpenID Connect"`` -> ``OpenIDConnect``). Matches the reference's
    ``mod_*`` label stems and keeps the tag free of spaces/parens."""
    return re.sub(r"[^A-Za-z0-9]", "", category)


def build_fragment(repo: Path) -> dict[str, Any]:
    """Build the OrchardCore manifest-category fact fragment for *repo*.

    Provenance/coverage stamped here (like extract_build_graph). Empty ``targets`` when the repo
    has no OrchardCore module manifests — a valid, graceful no-op fragment.
    """
    repo = repo.resolve()
    targets: dict[str, dict[str, Any]] = {}
    for csproj in _all_csproj(repo):
        manifest = csproj.parent / "Manifest.cs"
        if not manifest.exists():
            continue
        category = _primary_category(manifest)
        if not category:
            continue
        tid = f"csharp:csproj:{_norm(csproj, repo)}"
        # Emit a full, canonical target row (id scheme + scalars identical to extract_build_graph)
        # so the merged result is byte-identical regardless of which fragment seeds the target;
        # normalize unions the tag and priority-resolves the scalars to the build graph anyway.
        targets[tid] = {
            "id": tid,
            "name": csproj.stem,
            "type": "csproj",
            "language": "csharp",
            "path": _norm(csproj.parent, repo),
            "external": False,
            "tags": [f"module-category:{_tag_value(category)}"],
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR], "coverage": {}},
        "targets": [targets[k] for k in sorted(targets)],
        "relationships": [],
    }


def run(ws: Workspace) -> Path | None:
    """Extract OrchardCore manifest categories and write the fragment, or ``None`` if the repo has
    no module manifests (graceful no-op, §18.4).

    Like the other optional refinement extractors (``extract_csharp_facts``), a repo this extractor
    has nothing to say about writes NO fragment and adds NO entry to ``provenance.extractors`` — so
    a non-OrchardCore run (the toy fixture, every C++ system) is byte-identical to before this
    extractor existed. Never hard-fails.
    """
    fragment = build_fragment(ws.repo)
    if not fragment["targets"]:
        return None
    out = ws.fragments / "orchardcore-manifest.json"
    dump_json(fragment, out)
    return out


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    import argparse

    from ..paths import resolve_workspace

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir)
    print(run(w))
