"""§8.1 operator — remove a target/project (and, as a real PR would, its references).

Expected drift signal: "element no longer found" — a ``removed_target`` plus the
``removed_edge`` for every incident first-party L0 edge.
"""
from __future__ import annotations

import re

from ..common import (FileJournal, MutCtx, Mutation, all_project_referrers,
                      am_delete_var, am_get_var, am_logical_span, cpp_id, cpp_name,
                      csproj_rel, delete_project, exclusive_project_dir,
                      has_noisy_evidence, incident_l0, make_expected,
                      referrers_locatable, remove_from_solutions,
                      remove_project_reference, spread)

_AM_TARGET_SUFFIXES = ("SOURCES", "LDADD", "DEPENDENCIES", "CFLAGS", "LDFLAGS")
_PROGRAM_VARS = ("noinst_PROGRAMS", "bin_PROGRAMS", "check_PROGRAMS")


def _clean_csproj_candidates(ctx: MutCtx) -> list[str]:
    out = []
    for tid in sorted(ctx.fp):
        rel = csproj_rel(tid)
        if not rel or not (ctx.repo / rel).is_file():
            continue
        if not incident_l0(ctx.baseline, tid):
            continue
        if has_noisy_evidence(ctx.baseline, tid):
            continue  # identity could survive via an interop/runtime fragment
        if not exclusive_project_dir(ctx.repo, rel):
            continue  # whole-directory removal must be clean
        if not referrers_locatable(ctx, tid):
            continue  # shared-props referrers are not editable per-referrer
        out.append(tid)
    return out


def _automake_prog_candidates(ctx: MutCtx) -> list[str]:
    mk = ctx.repo / "Makefile.am"
    if not mk.is_file():
        return []
    declared: set[str] = set()
    for var in _PROGRAM_VARS:
        v = am_get_var(mk, var)
        if v:
            declared |= set(v.split())
    out = []
    for tid in sorted(ctx.fp):
        name = cpp_name(tid)
        if name and name in declared and incident_l0(ctx.baseline, tid) \
                and not has_noisy_evidence(ctx.baseline, tid):
            out.append(tid)
    return out


def _bash_lib_candidates(ctx: MutCtx) -> list[str]:
    """bash idiom: sub-libraries declared by their own ``lib/<x>/Makefile.in``."""
    out = []
    for mk in sorted(ctx.repo.glob("lib/*/Makefile.in")):
        v = am_get_var(mk, "LIBRARY_NAME")
        if not v or not v.endswith(".a"):
            continue
        tid = cpp_id(v[:-2])
        if tid in ctx.fp and incident_l0(ctx.baseline, tid) \
                and not has_noisy_evidence(ctx.baseline, tid):
            out.append(tid)
    return out


def plan(ctx: MutCtx, n: int) -> list[str]:
    if ctx.idiom == "csproj":
        return spread(_clean_csproj_candidates(ctx), n)
    if ctx.idiom == "automake":
        return spread(_automake_prog_candidates(ctx), n)
    if ctx.idiom == "autotools_hand":
        return spread(_bash_lib_candidates(ctx), n)
    return []


def _delete_logical_lines_containing(journal: FileJournal, path, token: str) -> int:
    """Drop every non-recipe logical line (continuations included) that names *token*
    literally. Recipe (tab-indented) lines are never parsed for structure, so they stay."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    dropped = 0
    i = 0
    while i < len(lines):
        group = [lines[i]]
        while group[-1].rstrip("\r\n").rstrip().endswith("\\") and i + len(group) < len(lines):
            group.append(lines[i + len(group)])
        joined = "".join(group)
        if token in joined and not group[0].startswith("\t"):
            dropped += 1
        else:
            out.append(joined)
        i += len(group)
    if dropped:
        journal.write(path, "".join(out))
    return dropped


def apply(ctx: MutCtx, cand: str) -> Mutation | None:
    journal = FileJournal()
    removed_edges = sorted(incident_l0(ctx.baseline, cand))
    try:
        if ctx.idiom == "csproj":
            rel = csproj_rel(cand)
            # referrer cleanup repo-wide, as a real PR would — including curation-excluded
            # test projects, whose dangling reference would re-declare the target
            for ref_rel in all_project_referrers(ctx.repo, rel):
                remove_project_reference(journal, ctx.repo, ref_rel, rel)
            remove_from_solutions(journal, ctx.repo, rel)
            delete_project(journal, ctx.repo, rel)
        elif ctx.idiom == "automake":
            name = cpp_name(cand)
            mk = ctx.repo / "Makefile.am"
            hit = False
            for var in _PROGRAM_VARS:
                span = am_logical_span(mk.read_text(encoding="utf-8"), var)
                if span and name in span[2].split():
                    toks = [t for t in span[2].split() if t != name]
                    from ..common import am_set_var
                    am_set_var(journal, mk, var, " ".join(toks))
                    hit = True
            if not hit:
                journal.undo()
                return None
            for suffix in _AM_TARGET_SUFFIXES:
                canon = re.sub(r"[^A-Za-z0-9_]", "_", name)
                am_delete_var(journal, mk, f"{canon}_{suffix}")
        elif ctx.idiom == "autotools_hand":
            name = cpp_name(cand)                      # e.g. "libglob"
            lib_file = f"{name}.a"
            owner = None
            for mk in sorted(ctx.repo.glob("lib/*/Makefile.in")):
                if am_get_var(mk, "LIBRARY_NAME") == lib_file:
                    owner = mk
                    break
            if owner is None:
                journal.undo()
                return None
            journal.delete(owner)
            # kill the root Makefile's delegation vars/rules naming the archive literally
            _delete_logical_lines_containing(journal, ctx.repo / "Makefile.in", lib_file)
        else:
            return None
    except OSError:
        journal.undo()
        return None
    return Mutation(
        operator="remove_target", name="", journal=journal,
        description=f"remove build target {cand}",
        expected=make_expected(removed_targets=[cand], removed_edges=removed_edges))
