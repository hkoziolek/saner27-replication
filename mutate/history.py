"""§8.3 real-history mini-arm (RQ6) — the drift detector over REAL release pairs.

The seeded-mutation experiment (``harness.py``) is "true by construction": the label is
injected, so detection can only confirm the operator. This arm runs the same L0-scoped
pipeline + ``drift_report.evaluate`` over consecutive *released* versions of the fixture
repositories with the committed curation rules **frozen** (never re-curated between the
two releases), records every finding, and emits a labelling worksheet for two human
raters. The labelling itself is a later human step (≥2 raters, κ reported — eval §8.3).

Per pair ``A..B`` (both tags exported with ``git archive`` into a work dir; the fixture's
checked-out HEAD is never touched)::

    out/<system>/history/<A>__<B>/drift.md              # the rendered drift report
    out/<system>/history/<A>__<B>/result.json           # rq6-history-pair/1
    out/<system>/history/<A>__<B>/worksheet.csv         # one row per finding, label EMPTY
    out/<system>/history/<A>__<B>/worksheet-R1.csv      # identical copies, one per rater
    out/<system>/history/<A>__<B>/worksheet-R2.csv
    out/<system>/history/history-summary.json           # rq6-history/1 (what analysis/ reads)

Runs are **L0-scoped** exactly like ``harness.run_system`` (``ANON_SKIP_DOXYGEN=1`` +
``ANON_SKIP_ROSLYN=1``) — both releases of a pair share the knobs, so the diff is
layer-consistent (§11.1). Finding counts are restricted to the first-party L0 build graph
the way ``common.score`` does (first-party targets; edges with ``link``/``project_ref``
evidence); the unrestricted ``diff`` dict is kept verbatim in ``result.json``.

Usage (repo root, venv active)::

    python -m mutate.history libxml2
    python -m mutate.history squidex --pairs 7.19.0..7.20.0,7.22.0..7.23.0 --keep-work
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

from .common import (L0_KINDS, REPO_ROOT, csproj_rel, edge_kinds, first_party_ids,
                     rel_index)

# system -> fixture / frozen rules / default release pairs. The rules are the COMMITTED
# curation of the pinned fixture (for squidex the reference-BLIND curator's frozen RQ9
# rules); they are copied once into the work area and never edited between releases.
SYSTEMS: dict[str, dict[str, Any]] = {
    "libxml2": {
        "idiom": "automake", "language": "cpp", "fixture": "fixtures/libxml2",
        "rules": "overlays/libxml2/architecture/rules",
        "rules_origin": "committed libxml2 overlay (GT-pub anchor curation)",
        # libxml2 >= 2.9 also ships a CMakeLists.txt, which would take L0 precedence via a
        # real `cmake` configure (needs a compiler + iconv). The frozen rules were authored
        # for the pinned fixture's automake idiom (2.4.22 has no CMake), so the pure static
        # autotools parse must stay the L0 source across every release studied.
        "knobs": {"ANON_SKIP_CMAKE": "1"},
        # the patch pair v2.15.0..v2.15.3 is the intended negative case
        "pairs": ["v2.13.0..v2.14.0", "v2.14.0..v2.15.0", "v2.15.0..v2.15.3"],
    },
    "squidex": {
        "idiom": "csproj", "language": "cs", "fixture": "fixtures/squidex",
        "rules": "overlays/squidex-blind-R-C",
        "rules_origin": "RQ9 reference-blind curator R-C, frozen at sign-off",
        # Consecutive minor releases with substance. 7.19.0..7.20.0 and 7.22.0..7.23.0 (the
        # original choice) are two-commit intervals with no build-file change — trivially true
        # negatives that inflated the pair count — and were swapped for 7.17..7.18 / 7.18..7.19
        # (audit 2026-08-31). Pairs are fixed consecutive releases, NOT selected by changelog.
        "pairs": ["7.17.0..7.18.0", "7.18.0..7.19.0", "7.20.0..7.21.0", "7.21.0..7.22.0"],
    },
    "nopcommerce": {
        "idiom": "csproj", "language": "cs", "fixture": "fixtures/nopcommerce",
        "rules": "overlays/nopcommerce/architecture/rules",
        "rules_origin": "committed nopCommerce overlay",
        "pairs": ["release-4.70.0..release-4.80.0"],
    },
}

PAIR_SCHEMA = "rq6-history-pair/1"
SUMMARY_SCHEMA = "rq6-history/1"

# What counts as a "build file" for the commit statistics and the rater hints.
BUILD_FILE_NAMES = {"CMakeLists.txt", "Makefile.am", "Makefile.in", "configure.ac",
                    "meson.build"}
BUILD_FILE_SUFFIXES = {".cmake", ".csproj", ".sln", ".slnx", ".props"}

# Finding kinds in worksheet order. The first five are drift; the two coverage_gap_*
# kinds are what the report labels NOT-drift (kept so raters see what was suppressed);
# layering_violation is a §11.2 rule finding new in B (only when the rules ship
# layering-rules.yaml).
FINDING_KINDS = ("new_targets", "removed_targets", "new_edges", "removed_edges",
                 "suspected_renames", "coverage_gap_targets", "coverage_gap_edges",
                 "layering_violations")
KIND_SINGULAR = {"new_targets": "new_target", "removed_targets": "removed_target",
                 "new_edges": "new_edge", "removed_edges": "removed_edge",
                 "suspected_renames": "suspected_rename",
                 "coverage_gap_targets": "coverage_gap_target",
                 "coverage_gap_edges": "coverage_gap_edge",
                 "layering_violations": "layering_violation"}

LABELS = ("architectural", "non-architectural", "extraction-noise")
WORKSHEET_COLUMNS = ("finding_id", "kind", "subject", "counterpart", "hint", "label", "note")
MAX_HINT_COMMITS = 3

CPP_PREFIX = "cpp:target:"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------- small io

def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dumps(obj), encoding="utf-8", newline="")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _log(msg: str) -> None:
    print(f"[history] {msg}", file=sys.stderr)


# --------------------------------------------------------------------- git

def _git(fixture: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", str(fixture), *args], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={out.returncode}: "
                           f"{out.stderr.strip()}")
    return out.stdout


def is_build_file(path: str) -> bool:
    base = posixpath.basename(path)
    return base in BUILD_FILE_NAMES or posixpath.splitext(base)[1].lower() in BUILD_FILE_SUFFIXES


def tag_date(fixture: Path, ref: str) -> str:
    """Committer date (ISO 8601) of the commit a tag points at (annotated tags peeled)."""
    return _git(fixture, "log", "-1", "--format=%cI", f"{ref}^{{commit}}").strip()


def git_pair_stats(fixture: Path, a: str, b: str) -> dict[str, Any]:
    """Commit statistics for ``a..b``: total commits, commits touching build files, the
    build files they touched, the tag dates, and whether the clone is shallow (a shallow
    clone deepened with ``--shallow-exclude=<a>`` counts against the shallow boundary —
    recorded, never hidden)."""
    shallow = _git(fixture, "rev-parse", "--is-shallow-repository").strip() == "true"
    total = int(_git(fixture, "rev-list", "--count", f"{a}..{b}").strip() or 0)
    log = _git(fixture, "log", "--format=%H", "--name-only", f"{a}..{b}")
    build_commits: set[str] = set()
    build_files: set[str] = set()
    current: str | None = None
    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue
        if _HEX40.match(line):
            current = line
            continue
        if current and is_build_file(line):
            build_commits.add(current)
            build_files.add(line)
    return {
        "a": a, "b": b,
        "a_date": tag_date(fixture, a), "b_date": tag_date(fixture, b),
        "commits_total": total,
        "commits_build_files": len(build_commits),
        "build_files_touched": sorted(build_files),
        "shallow_clone": shallow,
        "history_note": ("shallow clone — a..b counted against the shallow boundary of a "
                         "--shallow-exclude=<a> fetch" if shallow else "full clone"),
    }


def commit_hints(fixture: Path, a: str, b: str, files: list[str],
                 needles: list[str] | None = None,
                 limit: int = MAX_HINT_COMMITS) -> list[str]:
    """Up to *limit* ``<short-hash> <subject>`` lines of commits in ``a..b`` that touched any
    of *files* (newest first, as git orders them). When *needles* (target names) are given,
    commits whose diff of those files adds/removes a needle (``git log -S``) are preferred —
    a top-level ``Makefile.am`` is touched by many unrelated commits, the pickaxe finds the
    one that actually declared/undeclared the target; plain file-touch commits are the
    fallback."""
    if not files:
        return []

    def _log_lines(*extra: str) -> list[str]:
        out = _git(fixture, "log", f"--max-count={limit}", "--format=%h %s", *extra,
                   f"{a}..{b}", "--", *files, check=False)
        return [ln.strip()[:120] for ln in out.splitlines() if ln.strip()]

    hints: list[str] = []
    for needle in needles or []:
        for ln in _log_lines("-S" + needle):
            if ln not in hints:
                hints.append(ln)
    if hints:
        return hints[:limit]
    return _log_lines()


def target_needle(tid: str) -> str | None:
    """The build-file token naming a target: the csproj basename (no extension) or the
    automake/CMake target name — what ``git log -S`` should look for."""
    rel = csproj_rel(tid)
    if rel is not None:
        return posixpath.splitext(posixpath.basename(rel))[0]
    if tid.startswith(CPP_PREFIX):
        return tid[len(CPP_PREFIX):]
    return None


def export_release(fixture: Path, ref: str, dest: Path) -> Path:
    """``git archive <ref>`` into *dest* (fresh). The fixture checkout is never modified."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    tar_path = dest.parent / (dest.name + ".tar")
    _git(fixture, "archive", "--format=tar", "-o", str(tar_path), ref)
    try:
        with tarfile.open(tar_path) as tf:
            tf.extractall(dest, filter="data")
    finally:
        tar_path.unlink(missing_ok=True)
    return dest


def fixture_head(fixture: Path) -> dict[str, Any]:
    commit = _git(fixture, "rev-parse", "HEAD").strip()
    describe = _git(fixture, "describe", "--tags", "--always", check=False).strip()
    return {"commit": commit, "describe": describe,
            "date": _git(fixture, "log", "-1", "--format=%cI", "HEAD").strip()}


# --------------------------------------------------------------------- pipeline

def _arch(argv: list[str]) -> None:
    """Run an ``arch`` subcommand in-process (the harness pattern); raise on failure."""
    from anon import cli
    rc = cli.main(argv)
    if rc not in (0, None):
        raise RuntimeError(f"arch {argv[0]} failed rc={rc}: {argv}")


def build_release(repo: Path, arch_dir: Path, rules: Path) -> dict:
    """``arch extract`` + ``arch curate`` (L0-scoped by the env knobs set in
    :func:`run_system`); returns the curated facts."""
    _arch(["extract", "--repo", str(repo), "--arch-dir", str(arch_dir)])
    _arch(["curate", "--repo", str(repo), "--arch-dir", str(arch_dir),
           "--rules-dir", str(rules)])
    return _load_json(arch_dir / "generated" / "curated-facts.json")


# --------------------------------------------------------------------- scoping

def needs_curation_ids(facts: dict[str, Any]) -> list[str]:
    """First-party targets the frozen rules did not cover: ``apply_mapping_rules`` leaves
    them as provisional singleton containers tagged ``needs-curation`` (§7.3)."""
    return sorted(t["id"] for t in facts.get("targets", [])
                  if not t.get("external") and "needs-curation" in (t.get("tags") or []))


def scope_findings(diff: dict[str, Any], facts_a: dict[str, Any], facts_b: dict[str, Any],
                   layering_a: list[dict[str, Any]] | None = None,
                   layering_b: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Restrict a ``diff_facts`` dict to the first-party L0 scope the way ``common.score``
    does: targets are first-party (A ∪ B), edges need both endpoints in scope AND an L0
    evidence kind (``link``/``project_ref``) on the edge in the run that has it (B for new,
    A for removed). Returns ``{"findings": {kind: [...]}, "counts": {kind: n},
    "out_of_scope": {...}}``; renames are kept whole (they are already first-party)."""
    scope = first_party_ids(facts_a) | first_party_ids(facts_b)
    rels_a = rel_index(facts_a)
    rels_b = rel_index(facts_b)

    def l0_edge(s: str, t: str, rels: dict) -> bool:
        return s in scope and t in scope and bool(edge_kinds(rels.get((s, t))) & L0_KINDS)

    new_edges = [[s, t] for s, t in diff.get("new_edges", []) if l0_edge(s, t, rels_b)]
    removed_edges = [[s, t] for s, t in diff.get("removed_edges", []) if l0_edge(s, t, rels_a)]
    base_viol = {(f["from"], f["to"]) for f in (layering_a or []) if f["status"] == "VIOLATION"}
    new_viol = sorted({(f["from"], f["to"]) for f in (layering_b or [])
                       if f["status"] == "VIOLATION" and (f["from"], f["to"]) not in base_viol})
    findings = {
        "new_targets": sorted(t for t in diff.get("new_targets", []) if t in scope),
        "removed_targets": sorted(t for t in diff.get("removed_targets", []) if t in scope),
        "new_edges": sorted(new_edges),
        "removed_edges": sorted(removed_edges),
        "suspected_renames": [[r["old"], r["new"]] if isinstance(r, dict) else list(r)
                              for r in diff.get("suspected_renames", [])],
        "coverage_gap_targets": sorted(t for t in diff.get("coverage_gap_targets", [])
                                       if t in scope),
        "coverage_gap_edges": sorted([s, t] for s, t in diff.get("coverage_gap_edges", [])
                                     if s in scope and t in scope),
        "layering_violations": [list(v) for v in new_viol],
    }
    counts = {k: len(v) for k, v in findings.items()}
    counts["drift_total"] = sum(counts[k] for k in
                                ("new_targets", "removed_targets", "new_edges",
                                 "removed_edges", "suspected_renames"))
    out_of_scope = {
        "new_edges_non_l0_or_external": len(diff.get("new_edges", [])) - len(new_edges),
        "removed_edges_non_l0_or_external":
            len(diff.get("removed_edges", [])) - len(removed_edges),
    }
    return {"findings": findings, "counts": counts, "out_of_scope": out_of_scope}


# --------------------------------------------------------------------- worksheet

def _target_path(tid: str, *facts: dict[str, Any]) -> str | None:
    for f in facts:
        for t in f.get("targets", []):
            if t["id"] == tid:
                return t.get("path")
    return None


def top_level_build_files(*repos: Path) -> list[str]:
    """Repo-root build files present in any of *repos* (the fallback hint pathspec)."""
    found: set[str] = set()
    for repo in repos:
        if not repo or not repo.is_dir():
            continue
        for p in repo.iterdir():
            if p.is_file() and is_build_file(p.name):
                found.add(p.name)
    return sorted(found)


def owner_build_files(tid: str, facts_a: dict[str, Any], facts_b: dict[str, Any],
                      repo_a: Path | None, repo_b: Path | None) -> list[str]:
    """The build file(s) that declare *tid*: the csproj itself for ``csharp:csproj:<rel>``;
    for ``cpp:target:<name>`` the Makefile.am/.in (or CMakeLists.txt) in the target's
    ``path`` directory when a release still has it; else the top-level build files."""
    rel = csproj_rel(tid)
    if rel is not None:
        return [rel]
    if tid.startswith(CPP_PREFIX):
        d = _target_path(tid, facts_b, facts_a)
        if d is not None:
            cands = [posixpath.join(d, n) if d else n
                     for n in ("Makefile.am", "Makefile.in", "CMakeLists.txt")]
            present = [c for c in cands
                       if any(r and (r / c).is_file() for r in (repo_a, repo_b))]
            if present:
                return present
    return top_level_build_files(*(r for r in (repo_a, repo_b) if r))


def finding_rows(pair_id: str, findings: dict[str, list]) -> list[dict[str, str]]:
    """One worksheet row per finding, deterministic ids ``<pair_id>-NNN`` in kind order."""
    rows: list[dict[str, str]] = []
    n = 0
    for kind in FINDING_KINDS:
        for item in findings.get(kind, []):
            n += 1
            if isinstance(item, (list, tuple)):
                subject, counterpart = str(item[0]), str(item[1])
            else:
                subject, counterpart = str(item), ""
            rows.append({"finding_id": f"{pair_id}-{n:03d}", "kind": KIND_SINGULAR[kind],
                         "subject": subject, "counterpart": counterpart,
                         "hint": "", "label": "", "note": ""})
    return rows


def worksheet_text(system: str, a: str, b: str, rows: list[dict[str, str]]) -> str:
    """The rater worksheet as CSV text (UTF-8, LF): a ``#`` comment row documenting the
    allowed labels, the header, then one row per finding with ``label``/``note`` empty."""
    buf = io.StringIO()
    buf.write(f"# RQ6 real-history worksheet — {system} {a}..{b}. Fill ONLY `label` and "
              f"`note`. label = one of: {' | '.join(LABELS)}. "
              "architectural = a real change of the system's build-graph structure a "
              "maintainer would want flagged; non-architectural = a real change with no "
              "architectural significance (e.g. tooling/test/packaging churn); "
              "extraction-noise = not a real change of the code (extractor artefact). "
              "hint = commits between the releases touching the finding's build file(s). "
              "Rater instructions: docs/rq6-history-labelling-runbook.md.\n")
    w = csv.DictWriter(buf, fieldnames=list(WORKSHEET_COLUMNS), lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in WORKSHEET_COLUMNS})
    return buf.getvalue()


def read_worksheet(path: Path) -> list[dict[str, str]]:
    """Read a worksheet back (skipping the ``#`` comment row) — for the later κ step."""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if not ln.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(lines) + "\n")))


def write_worksheets(pair_dir: Path, text: str) -> None:
    pair_dir.mkdir(parents=True, exist_ok=True)
    for name in ("worksheet.csv", "worksheet-R1.csv", "worksheet-R2.csv"):
        (pair_dir / name).write_text(text, encoding="utf-8", newline="")


# --------------------------------------------------------------------- pair

def evaluate_pair(system: str, a: str, b: str, repo_a: Path, arch_a: Path,
                  repo_b: Path, arch_b: Path, rules: Path,
                  fixture: Path | None = None) -> dict[str, Any]:
    """Drift A→B over two already-built releases; returns ``{"result", "md", "rows"}``.
    *fixture* (the git clone) is optional — without it commit stats and hints are absent
    (the unit-test path)."""
    from anon.config import load_yaml
    from anon.paths import resolve_workspace
    from anon.stages import drift_report

    facts_a = _load_json(arch_a / "generated" / "curated-facts.json")
    facts_b = _load_json(arch_b / "generated" / "curated-facts.json")
    ws_b = resolve_workspace(str(repo_b), str(arch_b), str(rules))
    res = drift_report.evaluate(ws_b, baseline_path=str(arch_a / "generated" /
                                                        "curated-facts.json"),
                                commit=f"{a}..{b}", write=False)
    layering_rules = load_yaml(rules / "layering-rules.yaml")
    layering_a = drift_report.check_layering(facts_a, layering_rules) if layering_rules else []
    scoped = scope_findings(res["diff"], facts_a, facts_b, layering_a, res["layering"])

    pair_id = f"{a}__{b}"
    rows = finding_rows(pair_id, scoped["findings"])
    git: dict[str, Any] | None = None
    if fixture is not None:
        git = git_pair_stats(fixture, a, b)
        hint_cache: dict[tuple, str] = {}
        for row in rows:
            files: list[str] = []
            needles: list[str] = []
            for tid in (row["subject"], row["counterpart"]):
                if tid:
                    files += owner_build_files(tid, facts_a, facts_b, repo_a, repo_b)
                    needle = target_needle(tid)
                    if needle and needle not in needles:
                        needles.append(needle)
            key = (tuple(sorted(set(files))), tuple(needles))
            if key not in hint_cache:
                hints = commit_hints(fixture, a, b, list(key[0]), needles)
                fallback = ""
                if not hints:
                    top = top_level_build_files(repo_a, repo_b)
                    if top and tuple(top) != key[0]:
                        hints = commit_hints(fixture, a, b, top, needles)
                        fallback = "(top-level build files) " if hints else ""
                hint_cache[key] = fallback + " | ".join(hints)
            row["hint"] = hint_cache[key]

    nc_a, nc_b = needs_curation_ids(facts_a), needs_curation_ids(facts_b)
    warnings: list[str] = []
    for label, facts in (("A", facts_a), ("B", facts_b)):
        if not first_party_ids(facts):
            warnings.append(f"{label} extracted 0 first-party targets — the extractor did "
                            f"not parse this release's build files; findings are void")
    result = {
        "schema": PAIR_SCHEMA,
        "system": system,
        "pair": {"a": a, "b": b},
        "l0_scoped": True,
        "diff": res["diff"],
        "coverage": res["coverage"],
        "advisory": res["advisory"],
        "gating": res["gating"],
        "findings": scoped["findings"],
        "counts": scoped["counts"],
        "out_of_scope": scoped["out_of_scope"],
        "targets": {"a_first_party": len(first_party_ids(facts_a)),
                    "b_first_party": len(first_party_ids(facts_b))},
        "needs_curation": {"a": len(nc_a), "b": len(nc_b), "b_ids": nc_b,
                           "new_in_b": sorted(set(nc_b) - set(nc_a))},
        "layering": {"violations_a": len([f for f in layering_a if f["status"] == "VIOLATION"]),
                     "violations_b": len(res["violations"]),
                     "rules_present": bool(layering_rules)},
        "git": git,
        "worksheet_rows": len(rows),
        "warnings": warnings,
    }
    return {"result": result, "md": res["md"], "rows": rows}


def write_pair(pair_dir: Path, system: str, a: str, b: str, ev: dict[str, Any]) -> None:
    pair_dir.mkdir(parents=True, exist_ok=True)
    _write_json(pair_dir / "result.json", ev["result"])
    (pair_dir / "drift.md").write_text(ev["md"], encoding="utf-8", newline="")
    write_worksheets(pair_dir, worksheet_text(system, a, b, ev["rows"]))


def parse_pairs(spec: str | None, default: list[str]) -> list[tuple[str, str]]:
    items = [p.strip() for p in (spec.split(",") if spec else default) if p.strip()]
    pairs = []
    for it in items:
        if ".." not in it:
            raise ValueError(f"pair {it!r} must be <A>..<B>")
        a, b = it.split("..", 1)
        pairs.append((a.strip(), b.strip()))
    return pairs


def rules_note(cfg: dict[str, Any], head: dict[str, Any], pairs: list[dict[str, Any]]) -> str:
    dates = [p.get("b_date") for p in pairs if p.get("b_date")] + \
            [p.get("a_date") for p in pairs if p.get("a_date")]
    relation = "of unknown date relative to"
    if dates and head.get("date"):
        relation = ("later than" if head["date"] > max(dates)
                    else "earlier than" if head["date"] < min(dates)
                    else "within the date range of")
    return (f"Curation rules `{cfg['rules']}` ({cfg['rules_origin']}) were authored against "
            f"the pinned fixture commit {head.get('commit', '?')[:9]} "
            f"({head.get('describe', '?')}, {head.get('date', '?')}), which is {relation} "
            f"every release studied. They were held constant (frozen) across every pair: "
            f"no re-curation between A and B, so drift findings include the rule-maintenance "
            f"effect (needs-curation = first-party targets the frozen rules leave unmapped).")


# --------------------------------------------------------------------- system

def run_system(system: str, pairs_spec: str | None = None, keep_work: bool = False,
               work_root: Path | None = None, out_root: Path | None = None) -> dict | None:
    """Run every release pair of *system*; returns the ``rq6-history/1`` summary dict."""
    cfg = SYSTEMS.get(system)
    if cfg is None:
        _log(f"unknown system {system!r} (known: {', '.join(sorted(SYSTEMS))})")
        return None
    fixture = REPO_ROOT / cfg["fixture"]
    if not (fixture / ".git").exists():
        _log(f"{system}: no git fixture at {fixture} — skipping")
        return None
    rules_src = REPO_ROOT / cfg["rules"]
    if not (rules_src / "mapping-rules.yaml").is_file():
        _log(f"{system}: no mapping-rules.yaml under {rules_src} — skipping")
        return None
    pairs = parse_pairs(pairs_spec, cfg["pairs"])
    out_dir = (out_root or (REPO_ROOT / "out" / system)) / "history"
    work_root = work_root or (out_dir / "_work")

    knobs = {"ANON_SKIP_DOXYGEN": "1", "ANON_SKIP_ROSLYN": "1",
             **cfg.get("knobs", {})}
    os.environ.update(knobs)

    rules = work_root / "rules"
    if rules.exists():
        shutil.rmtree(rules)
    shutil.copytree(rules_src, rules)
    head = fixture_head(fixture)

    t0 = time.monotonic()
    built: dict[str, tuple[Path, Path] | Exception] = {}

    def _release(tag: str) -> tuple[Path, Path]:
        if tag in built:
            r = built[tag]
            if isinstance(r, Exception):
                raise r
            return r
        try:
            _git(fixture, "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}")
            repo = export_release(fixture, tag, work_root / tag / "repo")
            arch = work_root / tag / "arch"
            if arch.exists():
                shutil.rmtree(arch)
            _log(f"{system}: building {tag} ...")
            t1 = time.monotonic()
            facts = build_release(repo, arch, rules)
            _log(f"{system}: {tag} -> {len(first_party_ids(facts))} first-party targets, "
                 f"{len(facts.get('relationships', []))} relationships "
                 f"({time.monotonic() - t1:.1f}s)")
            built[tag] = (repo, arch)
            return built[tag]
        except Exception as exc:  # noqa: BLE001 — recorded per pair, never hidden
            built[tag] = exc
            raise

    summary_pairs: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for a, b in pairs:
        pair_id = f"{a}__{b}"
        pair_dir = out_dir / pair_id
        try:
            repo_a, arch_a = _release(a)
            repo_b, arch_b = _release(b)
            ev = evaluate_pair(system, a, b, repo_a, arch_a, repo_b, arch_b, rules, fixture)
            write_pair(pair_dir, system, a, b, ev)
            r = ev["result"]
            g = r["git"] or {}
            entry = {
                "a": a, "b": b, "status": "ok",
                "a_date": g.get("a_date"), "b_date": g.get("b_date"),
                "commits_total": g.get("commits_total"),
                "commits_build_files": g.get("commits_build_files"),
                "shallow_clone": g.get("shallow_clone"),
                "counts": r["counts"],
                "coverage": r["coverage"], "advisory": r["advisory"],
                "targets": r["targets"],
                "needs_curation_a": r["needs_curation"]["a"],
                "needs_curation_b": r["needs_curation"]["b"],
                "worksheet_rows": r["worksheet_rows"],
                "warnings": r["warnings"],
                "dir": str(pair_dir.relative_to(REPO_ROOT)).replace("\\", "/")
                if pair_dir.is_relative_to(REPO_ROOT) else str(pair_dir),
            }
            summary_pairs.append(entry)
            c = r["counts"]
            _log(f"{system} {a}..{b}: commits={g.get('commits_total')} "
                 f"build-file-commits={g.get('commits_build_files')} "
                 f"new_t={c['new_targets']} rm_t={c['removed_targets']} "
                 f"new_e={c['new_edges']} rm_e={c['removed_edges']} "
                 f"renames={c['suspected_renames']} cg_t={c['coverage_gap_targets']} "
                 f"cg_e={c['coverage_gap_edges']} coverage={r['coverage']:.2f} "
                 f"advisory={r['advisory']} needs_curation_b={r['needs_curation']['b']} "
                 f"rows={r['worksheet_rows']}")
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            _log(f"{system} {a}..{b}: FAILED — {msg}")
            entry = {"a": a, "b": b, "status": "failed", "error": msg}
            try:
                entry["a_date"], entry["b_date"] = tag_date(fixture, a), tag_date(fixture, b)
            except RuntimeError:
                pass
            summary_pairs.append(entry)
            failed.append({"pair": pair_id, "error": msg})

    summary = {
        "schema": SUMMARY_SCHEMA,
        "system": system, "language": cfg["language"], "idiom": cfg["idiom"],
        "l0_scoped": True, "extractor_knobs": knobs,
        "fixture": cfg["fixture"], "fixture_head": head,
        "rules_dir": cfg["rules"], "rules_origin": cfg["rules_origin"],
        "rules_note": rules_note(cfg, head, summary_pairs),
        "finding_kinds": list(FINDING_KINDS),
        "labels": list(LABELS),
        "pairs": summary_pairs,
        "failed": failed,
        "totals": {k: sum(p["counts"][k] for p in summary_pairs if p["status"] == "ok")
                   for k in (*FINDING_KINDS, "drift_total")},
    }
    _write_json(out_dir / "history-summary.json", summary)
    _log(f"{system}: {len(summary_pairs) - len(failed)}/{len(summary_pairs)} pairs ok "
         f"({time.monotonic() - t0:.0f}s) -> {out_dir / 'history-summary.json'}")
    if not keep_work:
        shutil.rmtree(work_root, ignore_errors=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("system", choices=sorted(SYSTEMS))
    ap.add_argument("--pairs", help="comma list of <A>..<B> tag pairs (default per system)")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the exported releases + arch dirs under out/<sys>/history/_work")
    ap.add_argument("--work", help="work-area root (default out/<sys>/history/_work)")
    ap.add_argument("--out", help="output root (default out/<sys>; artifacts go to <out>/history)")
    args = ap.parse_args(argv)
    summary = run_system(args.system, pairs_spec=args.pairs, keep_work=args.keep_work,
                         work_root=Path(args.work) if args.work else None,
                         out_root=Path(args.out) if args.out else None)
    if summary is None:
        return 2
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
