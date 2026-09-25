"""§8.1 operator — remove a real declared dependency.

Expected drift signal: "model element no longer found" — a ``removed_edge`` (coverage-
scoped, §11.1).
"""
from __future__ import annotations

from ..common import (FileJournal, MutCtx, Mutation, am_get_var, am_set_var, cpp_name,
                      csproj_rel, find_project_reference, make_expected,
                      remove_project_reference, spread)


def plan(ctx: MutCtx, n: int) -> list[tuple[str, str]]:
    """Candidate = one first-party L0 edge (source, target) removable by a single
    build-file edit."""
    out: list[tuple[str, str]] = []
    if ctx.idiom == "csproj":
        for s, t in sorted(ctx.l0):
            s_rel, t_rel = csproj_rel(s), csproj_rel(t)
            if not s_rel or not t_rel:
                continue
            path = ctx.repo / s_rel
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if find_project_reference(text, s_rel, t_rel):
                out.append((s, t))
    elif ctx.idiom == "automake":
        # program -> lib edges declared via <prog>_LDADD in the root Makefile.am
        mk = ctx.repo / "Makefile.am"
        if mk.is_file():
            for s, t in sorted(ctx.l0):
                prog = cpp_name(s)
                if prog and am_get_var(mk, f"{prog}_LDADD") is not None:
                    out.append((s, t))
    return spread(out, n)


def apply(ctx: MutCtx, cand: tuple[str, str]) -> Mutation | None:
    s, t = cand
    journal = FileJournal()
    if ctx.idiom == "csproj":
        if not remove_project_reference(journal, ctx.repo, csproj_rel(s), csproj_rel(t)):
            return None
    elif ctx.idiom == "automake":
        prog = cpp_name(s)
        # Empty LDADD: the lib reference disappears; the program itself stays declared.
        if not am_set_var(journal, ctx.repo / "Makefile.am", f"{prog}_LDADD", ""):
            return None
    else:
        return None
    return Mutation(
        operator="remove_dep", name="", journal=journal,
        description=f"remove declared dependency {s} -> {t}",
        expected=make_expected(removed_edges=[(s, t)]))
