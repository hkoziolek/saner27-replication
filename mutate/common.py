"""Shared substrate for the §8.1 seeded-mutation operators (RQ6).

Everything here is deterministic: candidate lists are sorted, instance selection is
index-spread (no RNG — the harness determinism gate), and every file edit goes through a
:class:`FileJournal` so a mutation is exactly reversible on the work copy.

Scoring scope (pre-registered, see ``mutate/README.md``): findings are scored over the
**first-party L0 build graph** — first-party targets, and edges between first-party
targets whose evidence includes an L0 kind (``link``/``project_ref``). That is the layer
every §8.1 operator manipulates; interop/runtime/package edges are outside the mutation
surface and are excluded from both the expected and the actual sets (§11.1 already
excludes externals from the target diff).
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

L0_KINDS = {"link", "project_ref"}
NOISY_KINDS = {"interop", "runtime"}   # evidence produced outside the build-file surface

FINDING_CATEGORIES = ("new_targets", "removed_targets", "new_edges", "removed_edges",
                      "suspected_renames", "layering_violations")


# --------------------------------------------------------------------- fact helpers

def first_party_ids(facts: dict[str, Any]) -> set[str]:
    return {t["id"] for t in facts.get("targets", []) if not t.get("external")}


def rel_index(facts: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["source"], r["target"]): r for r in facts.get("relationships", [])}


def edge_kinds(rel: dict[str, Any] | None) -> set[str]:
    if not rel:
        return set()
    return {e.get("type") for e in rel.get("evidence", []) or []}


def l0_edges(facts: dict[str, Any]) -> set[tuple[str, str]]:
    """First-party→first-party edges with at least one L0 evidence kind."""
    fp = first_party_ids(facts)
    return {(s, t) for (s, t), r in rel_index(facts).items()
            if s in fp and t in fp and edge_kinds(r) & L0_KINDS}


def incident_l0(facts: dict[str, Any], tid: str) -> set[tuple[str, str]]:
    return {(s, t) for (s, t) in l0_edges(facts) if s == tid or t == tid}


def has_noisy_evidence(facts: dict[str, Any], tid: str) -> bool:
    """True if any edge touching *tid* carries interop/runtime evidence — such a target's
    identity can survive a build-file removal via another extractor's fragment, so it is
    not a clean mutation candidate."""
    for (s, t), r in rel_index(facts).items():
        if (s == tid or t == tid) and edge_kinds(r) & NOISY_KINDS:
            return True
    return False


# --------------------------------------------------------------------- selection

def spread(items: list, n: int) -> list:
    """Deterministically pick up to *n* items spread across the sorted list (every
    step-th) — the same no-RNG pattern as ``stability_churn._seeded_removed``."""
    if not items or n <= 0:
        return []
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    picked = [items[min(int(i * step), len(items) - 1)] for i in range(n)]
    # de-dup while preserving order (possible when step rounds onto the same index)
    seen: set[int] = set()
    out = []
    for it in picked:
        key = id(it)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


# --------------------------------------------------------------------- file journal

class FileJournal:
    """Records every file operation of one mutation so ``undo()`` restores the work copy
    byte-exactly. Paths are repo-copy-absolute."""

    def __init__(self) -> None:
        self._log: list[tuple[str, Path, bytes | None]] = []
        self.touched: list[str] = []

    def _remember(self, path: Path) -> None:
        original = path.read_bytes() if path.exists() else None
        self._log.append(("file", path, original))

    def write(self, path: Path, text: str) -> None:
        self._remember(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        self.touched.append(str(path))

    def delete(self, path: Path) -> None:
        self._remember(path)
        path.unlink()
        self.touched.append(str(path))

    def rename(self, old: Path, new: Path) -> None:
        self._remember(old)
        self._remember(new)
        old.rename(new)
        self.touched.append(f"{old} -> {new}")

    def undo(self) -> None:
        for _kind, path, original in reversed(self._log):
            if original is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
        self._log.clear()


# --------------------------------------------------------------------- mutation model

@dataclass
class MutCtx:
    """Everything an operator needs: the system, its build idiom, the WORK COPY of the
    checkout, and the baseline curated facts (candidates come from the facts; edits go to
    the copy)."""
    system: str
    idiom: str                       # "csproj" | "automake" | "autotools_hand"
    repo: Path
    baseline: dict[str, Any]
    fp: set[str] = field(default_factory=set)
    rels: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    l0: set[tuple[str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.fp = first_party_ids(self.baseline)
        self.rels = rel_index(self.baseline)
        self.l0 = l0_edges(self.baseline)


@dataclass
class Mutation:
    """One applied, labelled mutation. ``expected`` uses the exact ``diff_facts`` finding
    vocabulary; ``alias`` (rename op) is the ``id_aliases`` entry whose adoption should
    suppress the drift in phase 2 (§7.3/§6.3 partial-survival)."""
    operator: str
    name: str
    description: str
    expected: dict[str, list]
    journal: FileJournal
    alias: dict[str, str] | None = None

    def label(self) -> dict[str, Any]:
        return {"id": self.name, "operator": self.operator, "description": self.description,
                "expected": self.expected,
                "files_touched": [str(p) for p in self.journal.touched],
                "alias": self.alias or {}}


def make_expected(**kw: list) -> dict[str, list]:
    exp: dict[str, list] = {c: [] for c in FINDING_CATEGORIES}
    for k, v in kw.items():
        assert k in exp, k
        exp[k] = sorted(v)
    return exp


# --------------------------------------------------------------------- csproj idiom

CSPROJ_PREFIX = "csharp:csproj:"

PROJECT_REF_RE = re.compile(
    r"[ \t]*<ProjectReference\b[^>]*?Include\s*=\s*\"(?P<inc>[^\"]+)\"[^>]*?"
    r"(?:/>|>.*?</ProjectReference>)[ \t]*\r?\n?", re.DOTALL)


def csproj_rel(tid: str) -> str | None:
    return tid[len(CSPROJ_PREFIX):] if tid.startswith(CSPROJ_PREFIX) else None


def csproj_id(rel: str) -> str:
    return CSPROJ_PREFIX + rel


def resolve_include(owner_rel: str, include: str) -> str:
    """Resolve a ProjectReference Include (backslash-relative) to a repo-relative path."""
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(owner_rel), include.replace("\\", "/")))


def find_project_reference(text: str, owner_rel: str, target_rel: str) -> re.Match | None:
    for m in PROJECT_REF_RE.finditer(text):
        if resolve_include(owner_rel, m.group("inc")) == target_rel:
            return m
    return None


def remove_project_reference(journal: FileJournal, repo: Path,
                             owner_rel: str, target_rel: str) -> bool:
    path = repo / owner_rel
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    m = find_project_reference(text, owner_rel, target_rel)
    if m is None:
        return False
    journal.write(path, text[:m.start()] + text[m.end():])
    return True


def rewrite_project_reference(journal: FileJournal, repo: Path, owner_rel: str,
                              old_target_rel: str, new_target_rel: str) -> bool:
    """Point an existing reference at a different project (relative path recomputed)."""
    path = repo / owner_rel
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    m = find_project_reference(text, owner_rel, old_target_rel)
    if m is None:
        return False
    new_inc = posixpath.relpath(new_target_rel, posixpath.dirname(owner_rel) or ".")
    new_inc = new_inc.replace("/", "\\")
    element = f'    <ProjectReference Include="{new_inc}" />\n'
    journal.write(path, text[:m.start()] + element + text[m.end():])
    return True


def add_project_reference(journal: FileJournal, repo: Path,
                          owner_rel: str, target_rel: str) -> bool:
    path = repo / owner_rel
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    close = text.rfind("</Project>")
    if close < 0:
        return False
    inc = posixpath.relpath(target_rel, posixpath.dirname(owner_rel) or ".").replace("/", "\\")
    block = f'  <ItemGroup>\n    <ProjectReference Include="{inc}" />\n  </ItemGroup>\n'
    journal.write(path, text[:close] + block + text[close:])
    return True


MINIMAL_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
"""

_SLN_SKIP = {".git", "bin", "obj", "node_modules", ".vs"}


def _solutions(repo: Path, suffix: str) -> list[Path]:
    return sorted(p for p in repo.rglob(f"*{suffix}")
                  if not ({q.lower() for q in p.parts} & _SLN_SKIP))


def _sln_variants(repo: Path, sln: Path, rel: str) -> set[str]:
    """The path spellings a solution file may use for a repo-relative csproj: solution
    entries are relative TO THE SOLUTION FILE (nopCommerce's ``src/NopCommerce.sln`` lists
    ``Plugins\\...``, not ``src\\Plugins\\...``), in either slash form; keep the
    repo-relative spelling too for root solutions."""
    variants = {rel, rel.replace("/", "\\")}
    try:
        sln_dir = sln.parent.resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        sln_dir = ""
    if sln_dir and sln_dir != ".":
        to_sln = posixpath.relpath(rel, sln_dir)
        variants |= {to_sln, to_sln.replace("/", "\\")}
    return variants


def remove_from_solutions(journal: FileJournal, repo: Path, rel: str) -> None:
    """Drop a project from every ``.sln``/``.slnx`` that lists it — a PR that deletes a
    project deletes its solution entries too (otherwise the target legitimately survives
    via the solution parse)."""
    for sln in _solutions(repo, ".sln"):
        variants = {v.lower() for v in _sln_variants(repo, sln, rel)}
        text = sln.read_text(encoding="utf-8", errors="replace")
        if not any(v in text.lower() for v in variants):
            continue
        lines = text.splitlines(keepends=True)
        out: list[str] = []
        guids: set[str] = set()
        i = 0
        while i < len(lines):
            line = lines[i]
            low = line.lower()
            if low.lstrip().startswith("project(") and any(v in low for v in variants):
                found = re.findall(r"\{[0-9A-Fa-f-]{36}\}", line)
                if found:
                    guids.add(found[-1].upper())          # last brace = the project GUID
                while i < len(lines) and not lines[i].strip().lower() == "endproject":
                    i += 1
                i += 1                                     # skip EndProject
                continue
            out.append(line)
            i += 1
        out = [ln for ln in out
               if not any(g in ln.upper() for g in guids)]
        journal.write(sln, "".join(out))
    for slnx in _solutions(repo, ".slnx"):
        variants = _sln_variants(repo, slnx, rel)
        alts = "|".join(re.escape(v) for v in sorted(variants))
        text = slnx.read_text(encoding="utf-8", errors="replace")
        new = re.sub(rf"[ \t]*<Project\b[^>]*Path\s*=\s*\"(?:{alts})\""
                     rf"[^>]*(?:/>|>.*?</Project>)[ \t]*\r?\n?", "", text,
                     flags=re.DOTALL | re.IGNORECASE)
        if new != text:
            journal.write(slnx, new)


def all_project_referrers(repo: Path, target_rel: str) -> list[str]:
    """Repo-relative paths of EVERY csproj holding a ProjectReference to *target_rel* —
    including curation-excluded ones (test projects): a dangling reference re-declares the
    target as a ``missing-ref`` stub, so a realistic removal PR cleans all of them."""
    out = []
    for p in sorted(repo.rglob("*.csproj")):
        if {q.lower() for q in p.parts} & _SLN_SKIP:
            continue
        try:
            rel = p.resolve().relative_to(repo.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        if rel == target_rel:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if find_project_reference(text, rel, target_rel):
            out.append(rel)
    return out


def referrer_index(ctx: "MutCtx") -> dict[str, set[str]]:
    """``target_rel -> {referrer_rel}`` over every csproj's DIRECT ProjectReferences,
    built once per system (cached on the ctx) — the plan()-time filter's hot path."""
    idx = getattr(ctx, "_referrer_index", None)
    if idx is not None:
        return idx
    idx = {}
    for p in sorted(ctx.repo.rglob("*.csproj")):
        if {q.lower() for q in p.parts} & _SLN_SKIP:
            continue
        try:
            rel = p.resolve().relative_to(ctx.repo.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in PROJECT_REF_RE.finditer(text):
            idx.setdefault(resolve_include(rel, m.group("inc")), set()).add(rel)
    ctx._referrer_index = idx
    return idx


def referrers_locatable(ctx: "MutCtx", tid: str) -> bool:
    """True when every baseline L0 referrer of *tid* holds a direct, resolvable
    ProjectReference in its OWN csproj. nopCommerce-style shared ``Directory.Build.props``
    references (every plugin inherits the Nop.Web.Framework edge from one props file) are
    not editable per-referrer, so such targets are excluded as candidates — recorded as
    not-applicable, never mislabeled."""
    rel = csproj_rel(tid)
    if rel is None:
        return False
    located = referrer_index(ctx).get(rel, set())
    for s, t in ctx.l0:
        if t == tid and s != tid:
            s_rel = csproj_rel(s)
            if s_rel is None or s_rel not in located:
                return False
    return True


def exclusive_project_dir(repo: Path, rel: str) -> bool:
    """True when the csproj's directory contains no OTHER project — safe to delete whole
    (a real component-removal PR removes the component's directory, source and all)."""
    d = (repo / rel).parent
    return sum(1 for _ in d.rglob("*.csproj")) == 1


def delete_project(journal: FileJournal, repo: Path, rel: str) -> None:
    """Delete the project's whole directory when exclusive (else just the csproj):
    leftover sources/protos would keep evidence channels attributing to the dead id."""
    proj = repo / rel
    if exclusive_project_dir(repo, rel):
        for p in sorted(proj.parent.rglob("*")):
            if p.is_file():
                journal.delete(p)
    else:
        journal.delete(proj)


def rename_in_solutions(journal: FileJournal, repo: Path, old_rel: str, new_rel: str) -> None:
    """Point every ``.sln``/``.slnx`` entry at the project's new path (each solution's
    entries are relative to the solution file — see :func:`_sln_variants`)."""
    for sln in _solutions(repo, ".sln") + _solutions(repo, ".slnx"):
        old_v = sorted(_sln_variants(repo, sln, old_rel), key=len, reverse=True)
        try:
            sln_dir = sln.parent.resolve().relative_to(repo.resolve()).as_posix()
        except (ValueError, OSError):
            sln_dir = ""
        text = sln.read_text(encoding="utf-8", errors="replace")
        new = text
        for old in old_v:
            uses_bs = "\\" in old
            base = posixpath.relpath(new_rel, sln_dir) \
                if (sln_dir and sln_dir != "." and not _is_repo_rel(old, old_rel)) else new_rel
            repl = base.replace("/", "\\") if uses_bs else base
            new = new.replace(old, repl)
        if new != text:
            journal.write(sln, new)


def _is_repo_rel(variant: str, rel: str) -> bool:
    return variant.replace("\\", "/") == rel


def csproj_source_root(ctx: MutCtx) -> str:
    """Where new mutant projects go: 'src' when the repo has one, else the most common
    top-level segment of existing first-party csproj paths, else the repo root."""
    if (ctx.repo / "src").is_dir():
        return "src"
    tops: dict[str, int] = {}
    for tid in sorted(ctx.fp):
        rel = csproj_rel(tid)
        if rel and "/" in rel:
            tops[rel.split("/", 1)[0]] = tops.get(rel.split("/", 1)[0], 0) + 1
    if tops:
        return max(sorted(tops), key=lambda k: tops[k])
    return ""


# --------------------------------------------------------------------- automake idiom

CPP_PREFIX = "cpp:target:"


def cpp_id(name: str) -> str:
    return CPP_PREFIX + name


def cpp_name(tid: str) -> str | None:
    return tid[len(CPP_PREFIX):] if tid.startswith(CPP_PREFIX) else None


def am_logical_span(text: str, varname: str) -> tuple[int, int, str] | None:
    """Locate the (start, end, joined_value) of a Makefile.am/.in variable assignment,
    following backslash continuations. Returns None if the variable is not assigned."""
    pattern = re.compile(rf"(?m)^{re.escape(varname)}[ \t]*[:+?]?=[ \t]*")
    m = pattern.search(text)
    if m is None:
        return None
    start = m.start()
    pos = m.end()
    value_parts: list[str] = []
    while True:
        eol = text.find("\n", pos)
        line = text[pos:] if eol < 0 else text[pos:eol]
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            value_parts.append(stripped[:-1])
            if eol < 0:
                pos = len(text)
                break
            pos = eol + 1
        else:
            value_parts.append(stripped)
            pos = len(text) if eol < 0 else eol + 1
            break
    return start, pos, " ".join(value_parts)


def am_set_var(journal: FileJournal, path: Path, varname: str, new_value: str) -> bool:
    """Replace a variable assignment (continuations collapsed) with a single line."""
    text = path.read_text(encoding="utf-8")
    span = am_logical_span(text, varname)
    if span is None:
        return False
    start, end, _ = span
    journal.write(path, text[:start] + f"{varname} = {new_value}\n" + text[end:])
    return True


def am_get_var(path: Path, varname: str) -> str | None:
    span = am_logical_span(path.read_text(encoding="utf-8"), varname)
    return span[2] if span else None


def am_delete_var(journal: FileJournal, path: Path, varname: str) -> bool:
    text = path.read_text(encoding="utf-8")
    span = am_logical_span(text, varname)
    if span is None:
        return False
    start, end, _ = span
    journal.write(path, text[:start] + text[end:])
    return True


# --------------------------------------------------------------------- scoring

def _tagged(expected_or_actual: dict[str, Any]) -> set[tuple]:
    out: set[tuple] = set()
    for tid in expected_or_actual.get("new_targets", []):
        out.add(("new_target", tid))
    for tid in expected_or_actual.get("removed_targets", []):
        out.add(("removed_target", tid))
    for s, t in expected_or_actual.get("new_edges", []):
        out.add(("new_edge", s, t))
    for s, t in expected_or_actual.get("removed_edges", []):
        out.add(("removed_edge", s, t))
    for r in expected_or_actual.get("suspected_renames", []):
        pair = (r["old"], r["new"]) if isinstance(r, dict) else tuple(r)
        out.add(("rename", *pair))
    for v in expected_or_actual.get("layering_violations", []):
        out.add(("violation", *tuple(v)))
    return out


def score(expected: dict[str, list], diff: dict[str, Any],
          layering_now: list[dict[str, Any]], layering_base: list[dict[str, Any]],
          baseline_facts: dict[str, Any], current_facts: dict[str, Any]) -> dict[str, Any]:
    """Bucket the drift report's findings against the injected label (§8.1 metrics).

    Scope: first-party L0 build graph (module docstring). ``coverage_gap_*`` findings are
    neither TP nor FP — the report explicitly labels them NOT-drift; an *expected* finding
    that lands there still counts as a miss (FN) because it was not reported as drift.
    """
    scope = first_party_ids(baseline_facts) | first_party_ids(current_facts) \
        | set(expected.get("new_targets", []))
    base_rels = rel_index(baseline_facts)
    cur_rels = rel_index(current_facts)

    def edge_in_scope(s: str, t: str, rels: dict) -> bool:
        return s in scope and t in scope and bool(edge_kinds(rels.get((s, t))) & L0_KINDS)

    base_viol = {(f["from"], f["to"]) for f in layering_base if f["status"] == "VIOLATION"}
    exp_new_edges = {tuple(e) for e in expected.get("new_edges", [])}
    exp_viol = {tuple(v) for v in expected.get("layering_violations", [])}
    new_viol = [f for f in layering_now
                if f["status"] == "VIOLATION" and (f["from"], f["to"]) not in base_viol]
    # a violation whose offending edge IS an expected new edge of this mutation is a
    # correct detection even when the label did not name the rule (e.g. a merge rewiring
    # trips a rule planted for add_forbidden_dep) — attributed, never a false positive
    attributed = sorted({(f["from"], f["to"]) for f in new_viol
                         if (f["from"], f["to"]) not in exp_viol
                         and any(tuple(e) in exp_new_edges for e in f.get("edges", []))})
    actual = {
        "new_targets": [t for t in diff.get("new_targets", [])],
        "removed_targets": [t for t in diff.get("removed_targets", [])],
        "new_edges": [(s, t) for s, t in diff.get("new_edges", [])
                      if edge_in_scope(s, t, cur_rels)],
        "removed_edges": [(s, t) for s, t in diff.get("removed_edges", [])
                          if edge_in_scope(s, t, base_rels)],
        "suspected_renames": diff.get("suspected_renames", []),
        "layering_violations": sorted(
            {(f["from"], f["to"]) for f in new_viol} - set(attributed)),
    }
    exp_set = _tagged(expected)
    act_set = _tagged(actual)
    tp = sorted(exp_set & act_set)
    fn = sorted(exp_set - act_set)
    fp = sorted(act_set - exp_set)

    cg = {"coverage_gap_targets": [t for t in diff.get("coverage_gap_targets", [])
                                   if t in scope],
          "coverage_gap_edges": [(s, t) for s, t in diff.get("coverage_gap_edges", [])
                                 if s in scope and t in scope]}

    result: dict[str, Any] = {
        "attributed_violations": [list(v) for v in attributed],
        "scored": {k: [list(x) if isinstance(x, tuple) else x for x in v]
                   for k, v in actual.items()},
        "coverage_gaps_in_scope": {k: [list(x) if isinstance(x, tuple) else x for x in v]
                                   for k, v in cg.items()},
        "tp": [list(x) for x in tp], "fn": [list(x) for x in fn], "fp": [list(x) for x in fp],
        "tp_count": len(tp), "fn_count": len(fn), "fp_count": len(fp),
        "detected": not fn,          # every injected signal reported as drift
    }
    exp_renames = {(r["old"], r["new"]) if isinstance(r, dict) else tuple(r)
                   for r in expected.get("suspected_renames", [])}
    if exp_renames:
        act_renames = {(r["old"], r["new"]) for r in actual["suspected_renames"]}
        result["rename_classified"] = exp_renames <= act_renames
    return result
