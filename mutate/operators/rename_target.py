"""§8.1 operator — rename/move a target.

Expected drift signal: a **suspected rename** (P§7.3) — i.e. the delete+add pair is
surfaced *as a rename suggestion*, not as independent removal+addition only. Phase 2 of
the harness then pins the rename via ``id_aliases`` (P§6.3) and re-curates: the drift must
disappear (identity survival). ``rename_classified`` in the result is the §8.1
rename-classification-accuracy bit.
"""
from __future__ import annotations

import posixpath
import re

from ..common import (FileJournal, MutCtx, Mutation, all_project_referrers,
                      am_delete_var, am_get_var, am_logical_span, am_set_var, cpp_id,
                      cpp_name, csproj_id, csproj_rel, has_noisy_evidence, incident_l0,
                      make_expected, referrers_locatable, rename_in_solutions,
                      rewrite_project_reference, spread)

_AM_TARGET_SUFFIXES = ("SOURCES", "LDADD", "DEPENDENCIES", "CFLAGS", "LDFLAGS")
_PROGRAM_VARS = ("noinst_PROGRAMS", "bin_PROGRAMS", "check_PROGRAMS")


def plan(ctx: MutCtx, n: int) -> list[str]:
    out: list[str] = []
    if ctx.idiom == "csproj":
        for tid in sorted(ctx.fp):
            rel = csproj_rel(tid)
            if rel and (ctx.repo / rel).is_file() and incident_l0(ctx.baseline, tid) \
                    and not has_noisy_evidence(ctx.baseline, tid) \
                    and referrers_locatable(ctx, tid):
                out.append(tid)
    elif ctx.idiom == "automake":
        mk = ctx.repo / "Makefile.am"
        if mk.is_file():
            declared: set[str] = set()
            for var in _PROGRAM_VARS:
                v = am_get_var(mk, var)
                if v:
                    declared |= set(v.split())
            for tid in sorted(ctx.fp):
                name = cpp_name(tid)
                if name and name in declared and incident_l0(ctx.baseline, tid):
                    out.append(tid)
    return spread(out, n)


def _rewired(old: str, new: str, edges: set[tuple[str, str]]) -> tuple[list, list]:
    removed = sorted(edges)
    added = sorted((new if s == old else s, new if t == old else t) for s, t in edges)
    return removed, added


def apply(ctx: MutCtx, cand: str) -> Mutation | None:
    journal = FileJournal()
    edges = incident_l0(ctx.baseline, cand)
    if ctx.idiom == "csproj":
        old_rel = csproj_rel(cand)
        d, fname = posixpath.split(old_rel)
        new_fname = fname[:-len(".csproj")] + "AnonMut.csproj"
        new_rel = f"{d}/{new_fname}" if d else new_fname
        # repo-wide referrer update (incl. curation-excluded test projects, whose
        # dangling reference would keep the OLD id alive as a missing-ref stub)
        for ref_rel in all_project_referrers(ctx.repo, old_rel):
            if not rewrite_project_reference(journal, ctx.repo, ref_rel,
                                             old_rel, new_rel):
                journal.undo()
                return None
        rename_in_solutions(journal, ctx.repo, old_rel, new_rel)
        journal.rename(ctx.repo / old_rel, ctx.repo / new_rel)
        old_id, new_id = cand, csproj_id(new_rel)
    elif ctx.idiom == "automake":
        name = cpp_name(cand)
        new_name = f"{name}strucmut"
        mk = ctx.repo / "Makefile.am"
        hit = False
        for var in _PROGRAM_VARS:
            span = am_logical_span(mk.read_text(encoding="utf-8"), var)
            if span and name in span[2].split():
                toks = [new_name if t == name else t for t in span[2].split()]
                am_set_var(journal, mk, var, " ".join(toks))
                hit = True
        if not hit:
            journal.undo()
            return None
        canon = re.sub(r"[^A-Za-z0-9_]", "_", name)
        new_canon = re.sub(r"[^A-Za-z0-9_]", "_", new_name)
        tail = []
        for suffix in _AM_TARGET_SUFFIXES:
            v = am_get_var(mk, f"{canon}_{suffix}")
            if v is not None:
                am_delete_var(journal, mk, f"{canon}_{suffix}")
                tail.append(f"{new_canon}_{suffix} = {v}\n")
        if tail:
            text = mk.read_text(encoding="utf-8")
            journal.write(mk, text + "".join(tail))
        old_id, new_id = cand, cpp_id(new_name)
    else:
        return None
    removed_edges, new_edges = _rewired(old_id, new_id, edges)
    return Mutation(
        operator="rename_target", name="", journal=journal,
        description=f"rename build target {old_id} -> {new_id}",
        expected=make_expected(new_targets=[new_id], removed_targets=[old_id],
                               new_edges=new_edges, removed_edges=removed_edges,
                               suspected_renames=[(old_id, new_id)]),
        # phase 2 pins the extracted new id back to the model's old id (§6.3): applied at
        # curate time to the CURRENT facts, so the key must be the id extraction now yields
        alias={new_id: old_id})
