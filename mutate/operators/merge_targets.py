"""§8.1 operator — merge target B into target A (B's referrers re-point to A).

Expected drift signal: B's removal + the edge rewiring, and — the classification check —
NO suspected-rename pairing (nothing new appeared; a merge must read as delete+rewire,
not as a rename). The rename-vs-delete+add accuracy metric counts this operator's runs as
correct only when ``suspected_renames`` stays empty.
"""
from __future__ import annotations

from ..common import (FileJournal, MutCtx, Mutation, all_project_referrers, csproj_rel,
                      delete_project, exclusive_project_dir, find_project_reference,
                      has_noisy_evidence, incident_l0, make_expected,
                      referrers_locatable, remove_from_solutions,
                      remove_project_reference, rewrite_project_reference, spread)


def _in_degree(ctx: MutCtx) -> dict[str, int]:
    deg: dict[str, int] = {}
    for _, t in ctx.l0:
        deg[t] = deg.get(t, 0) + 1
    return deg


def plan(ctx: MutCtx, n: int) -> list[tuple[str, str]]:
    """Candidate = (B, A): merge B into A. Only the csproj idiom has re-pointable
    referrers (the automake fixtures' programs have no incoming edges)."""
    if ctx.idiom != "csproj":
        return []
    deg = _in_degree(ctx)
    if not deg:
        return []
    bs = []
    for tid in sorted(ctx.fp):
        rel = csproj_rel(tid)
        if rel and (ctx.repo / rel).is_file() and deg.get(tid, 0) >= 1 \
                and not has_noisy_evidence(ctx.baseline, tid) \
                and exclusive_project_dir(ctx.repo, rel) \
                and referrers_locatable(ctx, tid):
            bs.append(tid)
    out: list[tuple[str, str]] = []
    for b in bs:
        # deterministic partner: the most-referenced other first-party target
        partner = None
        for a in sorted(ctx.fp, key=lambda x: (-deg.get(x, 0), x)):
            if a != b and csproj_rel(a) and (ctx.repo / csproj_rel(a)).is_file():
                partner = a
                break
        if partner:
            out.append((b, partner))
    return spread(out, n)


def apply(ctx: MutCtx, cand: tuple[str, str]) -> Mutation | None:
    b, a = cand
    journal = FileJournal()
    edges = incident_l0(ctx.baseline, b)
    b_rel, a_rel = csproj_rel(b), csproj_rel(a)
    new_edges = []
    curated_referrers = {s for s, t in edges if t == b and s != b}
    try:
        # repo-wide referrer re-pointing (incl. curation-excluded test projects)
        for ref_rel in all_project_referrers(ctx.repo, b_rel):
            path = ctx.repo / ref_rel
            already_has_a = bool(find_project_reference(
                path.read_text(encoding="utf-8"), ref_rel, a_rel))
            if ref_rel == a_rel or already_has_a:
                remove_project_reference(journal, ctx.repo, ref_rel, b_rel)
            else:
                if not rewrite_project_reference(journal, ctx.repo, ref_rel,
                                                 b_rel, a_rel):
                    journal.undo()
                    return None
        for s in sorted(curated_referrers):
            if s != a and (s, a) not in ctx.l0:
                new_edges.append((s, a))
        remove_from_solutions(journal, ctx.repo, b_rel)
        delete_project(journal, ctx.repo, b_rel)
    except OSError:
        journal.undo()
        return None
    return Mutation(
        operator="merge_targets", name="", journal=journal,
        description=f"merge build target {b} into {a}",
        expected=make_expected(removed_targets=[b], removed_edges=sorted(edges),
                               new_edges=sorted(new_edges)))
