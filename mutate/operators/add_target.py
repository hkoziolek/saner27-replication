"""§8.1 operator — add a new target/project.

Expected drift signal: a ``new_target`` (and, via curation, the "new unmapped target"
P§7.3 proposal — the drift report's ``new_targets`` section is what is scored).
"""
from __future__ import annotations

from ..common import (MINIMAL_CSPROJ, FileJournal, MutCtx, Mutation, cpp_id, csproj_id,
                      csproj_source_root, make_expected)


def plan(ctx: MutCtx, n: int) -> list[int]:
    """Candidates are synthetic (a fresh project per instance) — one per index."""
    if ctx.idiom in ("csproj", "automake", "autotools_hand"):
        return list(range(1, n + 1))
    return []


def apply(ctx: MutCtx, k: int) -> Mutation | None:
    journal = FileJournal()
    name = f"AnonMutant{k}"
    if ctx.idiom == "csproj":
        root = csproj_source_root(ctx)
        rel = f"{root}/{name}/{name}.csproj" if root else f"{name}/{name}.csproj"
        journal.write(ctx.repo / rel, MINIMAL_CSPROJ)
        new_id = csproj_id(rel)
    elif ctx.idiom == "automake":
        lib = f"libstrucmut{k}"
        src = f"strucmut{k}.c"
        mk = ctx.repo / "Makefile.am"
        if not mk.is_file():
            return None
        journal.write(ctx.repo / src, f"int anon_mutant_{k}(void) {{ return {k}; }}\n")
        text = mk.read_text(encoding="utf-8")
        block = (f"\nnoinst_LTLIBRARIES = {lib}.la\n"
                 f"{lib}_la_SOURCES = {src}\n")
        journal.write(mk, text + block)
        new_id = cpp_id(lib)
    elif ctx.idiom == "autotools_hand":
        lib = f"libstrucmut{k}"
        d = ctx.repo / "lib" / f"strucmut{k}"
        journal.write(d / f"strucmut{k}.c",
                      f"int anon_mutant_{k}(void) {{ return {k}; }}\n")
        journal.write(d / "Makefile.in",
                      f"LIBRARY_NAME = {lib}.a\n"
                      f"CSOURCES = strucmut{k}.c\n"
                      f"{lib}.a: $(OBJECTS)\n")
        new_id = cpp_id(lib)
    else:
        return None
    return Mutation(
        operator="add_target", name="", journal=journal,
        description=f"add new build target {new_id}",
        expected=make_expected(new_targets=[new_id]))
