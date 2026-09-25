"""§8.1 operator — add a cross-layer / forbidden dependency.

Expected drift signal: "new code fact" (a ``new_edge``) **plus** a §11.2 layering
VIOLATION. The harness pre-writes a ``forbidden:`` rule for every planned pair into the
work rules dir *before* the baseline layering evaluation, so the rule is OK at baseline
and only the mutated run flips it to VIOLATION (baseline-relative scoring).
"""
from __future__ import annotations

from ..common import (FileJournal, MutCtx, Mutation, add_project_reference, csproj_rel,
                      make_expected, spread)


def plan(ctx: MutCtx, n: int) -> list[tuple[str, str]]:
    """Candidate = an absent first-party pair (A, B) injectable by one build-file edit.
    Only the csproj idiom can declare an arbitrary new target-to-target dependency; the
    automake fixtures (single library + leaf programs) have no absent first-party pair
    expressible without also adding a target — recorded as not-applicable."""
    if ctx.idiom != "csproj":
        return []
    existing = {(s, t) for s, t in ctx.l0}
    referenced = {t for _, t in ctx.l0} | {s for s, _ in ctx.l0}
    out: list[tuple[str, str]] = []
    for a in sorted(ctx.fp):
        for b in sorted(ctx.fp):
            if a == b or (a, b) in existing or (b, a) in existing:
                continue
            if a not in referenced and b not in referenced:
                continue  # prefer pairs that live in the connected build graph
            if csproj_rel(a) and csproj_rel(b) and (ctx.repo / csproj_rel(a)).is_file() \
                    and (ctx.repo / csproj_rel(b)).is_file():
                out.append((a, b))
    return spread(out, n)


def apply(ctx: MutCtx, cand: tuple[str, str]) -> Mutation | None:
    a, b = cand
    journal = FileJournal()
    if not add_project_reference(journal, ctx.repo, csproj_rel(a), csproj_rel(b)):
        return None
    return Mutation(
        operator="add_forbidden_dep", name="", journal=journal,
        description=f"inject forbidden dependency {a} -> {b}",
        expected=make_expected(new_edges=[(a, b)], layering_violations=[(a, b)]))
