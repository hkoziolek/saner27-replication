"""§8.1 seeded-mutation harness (RQ6) — apply → re-extract → drift → score.

Per system: copy the pristine fixture to a work area, produce a **baseline** run with the
real pipeline (``arch extract`` + ``arch curate``, in-process), then for every planned
mutation: apply the operator's build-file edit, re-run the pipeline, run
``drift_report.evaluate`` against the baseline facts, score the findings against the
injected label, and revert. Artifacts land in the §14.1 layout::

    out/<system>/mutation/<id>/{label.json, result.json, drift.md}
    out/<system>/mutation/mutation-summary.json      # what aggregate_results.py reads

Runs are **L0-scoped** (``ANON_SKIP_DOXYGEN=1`` + ``ANON_SKIP_ROSLYN=1``): every
§8.1 operator manipulates the declared build graph, and skipping the expensive semantic
layers keeps ~100 pipeline runs tractable without changing what is being tested. The
baseline is produced under the same knobs, so baseline and mutated runs are always
layer-consistent (§11.1).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .common import REPO_ROOT, MutCtx, first_party_ids
from .operators import OPERATORS

OP_ORDER = ["add_forbidden_dep", "remove_dep", "add_target", "remove_target",
            "rename_target", "merge_targets"]

# system -> (idiom, fixture path, rules overlay, language). The mutation subset of the
# corpus: systems whose L0 extractors are pure static parses, so ~25 pipeline runs per
# system stay cheap and deterministic. bash's hand-written idiom collects an executable's
# link refs from EVERY variable mention (extract_autotools B6), so only whole-target
# operators are cleanly expressible there; per-op applicability is recorded, never faked.
SYSTEMS: dict[str, dict[str, str]] = {
    "eshop": {"idiom": "csproj", "fixture": "fixtures/eshop",
              "rules": "overlays/eshop/architecture/rules", "language": "cs"},
    "nopcommerce": {"idiom": "csproj", "fixture": "fixtures/nopcommerce",
                    "rules": "overlays/nopcommerce/architecture/rules", "language": "cs"},
    "orchardcore": {"idiom": "csproj", "fixture": "fixtures/orchardcore",
                    "rules": "overlays/orchardcore/architecture/rules", "language": "cs"},
    "libxml2": {"idiom": "automake", "fixture": "fixtures/libxml2",
                "rules": "overlays/libxml2/architecture/rules", "language": "cpp"},
    "bash": {"idiom": "autotools_hand", "fixture": "fixtures/bash",
             "rules": "overlays/bash/architecture/rules", "language": "cpp"},
    # in-repo deterministic fixture — used by the unit tests, not the corpus results
    "toy": {"idiom": "csproj", "fixture": "tests/fixtures/toy-repo",
            "rules": "tests/fixtures/toy-repo/architecture/rules", "language": "cs"},
}

_COPY_IGNORE = shutil.ignore_patterns(".git", ".vs", "bin", "obj", "node_modules",
                                      "artifacts", "packages")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dumps(obj), encoding="utf-8", newline="")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _arch(argv: list[str]) -> None:
    """Run an ``arch`` subcommand in-process (the tests' pattern); raise on failure."""
    from anon import cli
    rc = cli.main(argv)
    if rc not in (0, None):
        raise RuntimeError(f"arch {' '.join(argv[:1])} failed rc={rc}: {argv}")


def _pipeline(repo: Path, arch_dir: Path, rules: Path) -> dict:
    _arch(["extract", "--repo", str(repo), "--arch-dir", str(arch_dir)])
    _arch(["curate", "--repo", str(repo), "--arch-dir", str(arch_dir),
           "--rules-dir", str(rules)])
    return _load_json(arch_dir / "generated" / "curated-facts.json")


def _curate_only(repo: Path, arch_dir: Path, rules: Path) -> dict:
    _arch(["curate", "--repo", str(repo), "--arch-dir", str(arch_dir),
           "--rules-dir", str(rules)])
    return _load_json(arch_dir / "generated" / "curated-facts.json")


def _prepare_work(system: str, cfg: dict, work_root: Path, fresh: bool) -> tuple[Path, Path]:
    work = work_root / system
    repo_copy = work / "repo"
    rules_copy = work / "rules"
    if fresh and work.exists():
        shutil.rmtree(work)
    if not repo_copy.exists():
        print(f"[mutate] {system}: copying fixture -> {repo_copy} ...", file=sys.stderr)
        shutil.copytree(REPO_ROOT / cfg["fixture"], repo_copy, ignore=_COPY_IGNORE)
    if rules_copy.exists():
        shutil.rmtree(rules_copy)
    shutil.copytree(REPO_ROOT / cfg["rules"], rules_copy)
    return repo_copy, rules_copy


def _write_layering_rules(rules_dir: Path, planned_pairs: list[tuple[str, str]]) -> None:
    """Extend (or create) the work copy's layering-rules.yaml with one ``forbidden`` rule
    per planned add_forbidden_dep pair — present for baseline AND mutated runs, so only
    the injected edge flips a rule OK -> VIOLATION."""
    path = rules_dir / "layering-rules.yaml"
    data: dict = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    forbidden = list(data.get("forbidden") or [])
    for a, b in planned_pairs:
        forbidden.append({"from": a, "to": b})
    data["forbidden"] = forbidden
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8", newline="")


def _with_alias(rules_dir: Path, alias: dict[str, str]):
    """Context: temporarily add id_aliases to the work mapping-rules.yaml (phase 2)."""
    path = rules_dir / "mapping-rules.yaml"
    original = path.read_bytes()
    data = yaml.safe_load(original.decode("utf-8")) or {}
    aliases = dict(data.get("id_aliases") or {})
    aliases.update(alias)
    data["id_aliases"] = aliases
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8", newline="")
    return lambda: path.write_bytes(original)


def _scoped_finding_count(diff: dict, scope: set[str]) -> int:
    n = len([t for t in diff.get("new_targets", []) if t in scope])
    n += len([t for t in diff.get("removed_targets", []) if t in scope])
    n += len([e for e in diff.get("new_edges", []) if e[0] in scope and e[1] in scope])
    n += len([e for e in diff.get("removed_edges", []) if e[0] in scope and e[1] in scope])
    n += len(diff.get("suspected_renames", []))
    return n


def run_system(system: str, ops: list[str] | None = None, instances: int = 4,
               work_root: Path | None = None, fresh: bool = True,
               keep_work: bool = False, out_root: Path | None = None) -> dict | None:
    """Run the seeded-mutation experiment for one system; returns the summary dict."""
    from anon.config import load_yaml
    from anon.paths import resolve_workspace
    from anon.stages import drift_report

    cfg = SYSTEMS.get(system)
    if cfg is None:
        print(f"[mutate] unknown system {system!r}", file=sys.stderr)
        return None
    fixture = REPO_ROOT / cfg["fixture"]
    if not fixture.exists():
        print(f"[mutate] {system}: no fixture at {fixture} — skipping", file=sys.stderr)
        return None
    ops = [o for o in (ops or OP_ORDER) if o in OPERATORS]
    work_root = work_root or (REPO_ROOT / "out" / system / "mutation" / "_work")
    out_dir = (out_root or (REPO_ROOT / "out" / system)) / "mutation"

    os.environ["ANON_SKIP_DOXYGEN"] = "1"
    os.environ["ANON_SKIP_ROSLYN"] = "1"

    repo, rules = _prepare_work(system, cfg, work_root, fresh)
    arch_base = work_root / system / "arch-baseline"
    arch_run = work_root / system / "arch-run"

    t0 = time.monotonic()
    print(f"[mutate] {system}: baseline pipeline run ...", file=sys.stderr)
    baseline = _pipeline(repo, arch_base, rules)
    baseline_path = arch_base / "generated" / "curated-facts.json"
    ctx = MutCtx(system=system, idiom=cfg["idiom"], repo=repo, baseline=baseline)
    print(f"[mutate] {system}: baseline {len(ctx.fp)} first-party targets, "
          f"{len(ctx.l0)} L0 edges ({time.monotonic() - t0:.1f}s)", file=sys.stderr)

    plans = {op: OPERATORS[op].plan(ctx, instances) for op in ops}
    _write_layering_rules(rules, plans.get("add_forbidden_dep", []))
    layering_rules = load_yaml(rules / "layering-rules.yaml")
    layering_base = drift_report.check_layering(baseline, layering_rules)

    per_op: dict[str, dict[str, Any]] = {}
    for op in ops:
        cands = plans[op]
        stats = {"applicable": bool(cands), "n": 0, "tp": 0, "fn": 0, "fp": 0,
                 "detected": 0, "rename_total": 0, "rename_correct": 0,
                 "false_rename_total": 0, "false_rename_clean": 0,
                 "alias_total": 0, "alias_suppressed": 0,
                 "fp_per_run": [], "mutations": []}
        per_op[op] = stats
        for i, cand in enumerate(cands, 1):
            m = OPERATORS[op].apply(ctx, cand)
            if m is None:
                print(f"[mutate] {system}-{op}-{i:02d}: not applicable to candidate "
                      f"{cand!r} — skipped", file=sys.stderr)
                continue
            m.name = f"{system}-{op}-{i:02d}"
            try:
                current = _pipeline(repo, arch_run, rules)
                ws = resolve_workspace(str(repo), str(arch_run), str(rules))
                res = drift_report.evaluate(ws, baseline_path=str(baseline_path),
                                            commit=m.name)
                from .common import score
                sc = score(m.expected, res["diff"], res["layering"], layering_base,
                           baseline, current)
                sc["advisory"] = res["advisory"]
                sc["coverage"] = res["coverage"]
                # phase 2 — pin the rename via id_aliases; the drift must disappear (§6.3)
                if m.alias:
                    restore = _with_alias(rules, m.alias)
                    try:
                        _curate_only(repo, arch_run, rules)
                        res2 = drift_report.evaluate(ws, baseline_path=str(baseline_path),
                                                     commit=m.name + "+alias", write=False)
                        scope = first_party_ids(baseline) | set(m.alias)
                        sc["alias_findings_in_scope"] = _scoped_finding_count(
                            res2["diff"], scope)
                        sc["alias_suppressed"] = sc["alias_findings_in_scope"] == 0
                    finally:
                        restore()
                mdir = out_dir / m.name
                _write_json(mdir / "label.json", {"system": system, **m.label()})
                _write_json(mdir / "result.json",
                            {"diff": res["diff"], "coverage": res["coverage"],
                             "advisory": res["advisory"], "score": sc})
                (mdir / "drift.md").write_text(res["md"], encoding="utf-8", newline="")
            finally:
                m.journal.undo()
            stats["n"] += 1
            stats["tp"] += sc["tp_count"]
            stats["fn"] += sc["fn_count"]
            stats["fp"] += sc["fp_count"]
            stats["detected"] += 1 if sc["detected"] else 0
            stats["fp_per_run"].append(sc["fp_count"])
            if "rename_classified" in sc:
                stats["rename_total"] += 1
                stats["rename_correct"] += 1 if sc["rename_classified"] else 0
            if op == "merge_targets":
                stats["false_rename_total"] += 1
                if not res["diff"].get("suspected_renames"):
                    stats["false_rename_clean"] += 1
            if "alias_suppressed" in sc:
                stats["alias_total"] += 1
                stats["alias_suppressed"] += 1 if sc["alias_suppressed"] else 0
            stats["mutations"].append(m.name)
            print(f"[mutate] {m.name}: tp={sc['tp_count']} fn={sc['fn_count']} "
                  f"fp={sc['fp_count']}"
                  + (f" rename_ok={sc.get('rename_classified')}"
                     if "rename_classified" in sc else "")
                  + (f" alias_suppressed={sc.get('alias_suppressed')}"
                     if "alias_suppressed" in sc else ""), file=sys.stderr)

    totals = {k: sum(s[k] for s in per_op.values()) for k in
              ("n", "tp", "fn", "fp", "detected", "rename_total", "rename_correct",
               "false_rename_total", "false_rename_clean",
               "alias_total", "alias_suppressed")}
    summary = {
        "system": system, "language": cfg["language"], "idiom": cfg["idiom"],
        "instances_requested": instances,
        "l0_scoped": True, "baseline_targets": len(ctx.fp), "baseline_l0_edges": len(ctx.l0),
        "operators": per_op, "totals": totals,
        "schema": "rq6-mutation-summary/1",
    }
    _write_json(out_dir / "mutation-summary.json", summary)
    print(f"[mutate] {system}: {totals['n']} mutations, tp={totals['tp']} "
          f"fn={totals['fn']} fp={totals['fp']} "
          f"({time.monotonic() - t0:.0f}s) -> {out_dir / 'mutation-summary.json'}",
          file=sys.stderr)
    if not keep_work:
        shutil.rmtree(work_root / system, ignore_errors=True)
    return summary
