"""Eval §7-A8 — clustering stability / CHURN under code change.

Answers the related-work rebuttal *"couldn't you just freeze any SAR clustering and reuse
it, the way Anon locks its curation?"*. The surface form is true — any tool's output
can be dumped to a file — but it collapses the moment the code changes: a global-optimizer
clustering (ACDC, Bunch, WCA) is a function of the *whole* graph, so adding/removing
entities reshuffles the EXISTING ones, whereas Anon re-derives clusters from
*per-entity rules over stable ids* (``members_glob``/``members_tag``), so survivors never
move. This experiment quantifies that churn.

Churn metric: MoJoFM between the clustering of an OLD version and a NEW version, projected
to the entities present in BOTH (the survivors). **100 = the survivors' partition is
identical across versions (zero churn); lower = the version change reshuffled survivors.**
Per-entity methods (Anon rules, ``dir``) are *set-monotone* — removing/adding other
entities cannot move a survivor — so they are 100 by construction; global optimizers churn,
and the load-bearing result is *how much*.

Two modes:

  ``perturb`` (default, always runnable) — simulate a code delta deterministically: the
    full exported graph is the NEW version; the OLD version is the full graph minus a
    seeded, spread-out fraction of entities (and their incident edges). Re-cluster both per
    technique and score survivor churn. Sweeps a few fractions. Needs only the committed
    ``out/<sys>/architecture/generated/graph/`` export + the pinned ARCADE jar (for ACDC).

  ``snapshot`` (realistic; needs full-history clones) — check out two real git refs of the
    OSS repo, extract+export each, cluster both, and report survivor churn PLUS, for
    Anon, how many genuinely-new entities the locked rules place vs leave unmapped (the
    governed cold-spot the ``on_unmapped`` gate surfaces — the non-trivial question a
    synthetic perturbation cannot ask). The bundled fixtures are DEPTH-1 clones (no
    history); enable with::

        git -C fixtures/<sys> fetch --unshallow         # or: fetch a specific old tag
        python baselines/stability_churn.py <sys> --mode snapshot --old-ref <tag>

Usage::

    python baselines/stability_churn.py                 # perturb, all configured systems
    python baselines/stability_churn.py eshop itk       # a subset
    python baselines/stability_churn.py eshop --mode snapshot --old-ref v8.0.0

Output: a per-system table to stdout + ``out/<sys>/baseline/stability-churn.json``.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from run_arcade import (cluster_cc, cluster_dir, mojofm, read_contains,  # noqa: E402
                        read_depends, run_acdc, write_contains)

from a2_ablation import SYSTEMS  # noqa: E402  # system -> (language, granularity)

REPO = Path(__file__).resolve().parent.parent
FRACTIONS = (0.05, 0.10, 0.20)
# Techniques split by KIND: per-entity rules (set-monotone → 0 churn by construction) vs
# global optimizers (graph-wide objective → churn is the thing we measure).
PER_ENTITY = {"anon", "dir"}
GLOBAL = {"acdc", "cc"}
DEFAULT_TECHNIQUES = ["anon", "acdc", "cc", "dir"]


def _graph_dir(system: str) -> Path:
    return REPO / "out" / system / "architecture" / "generated" / "graph"


def _write_graph(edges: list[tuple[str, str]], path: Path) -> None:
    """Write `depend` tuples as an RSF graph (sorted, LF, quoting space-bearing entities)."""
    def q(e: str) -> str:
        return f'"{e}"' if any(c.isspace() for c in e) else e
    lines = sorted(f"depend {q(a)} {q(b)}" for a, b in edges)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="")


def _restrict(clustering: dict[str, str], keep: set[str]) -> dict[str, str]:
    return {m: c for m, c in clustering.items() if m in keep}


def _churn(old_cl: dict[str, str], new_cl: dict[str, str], survivors: set[str],
           tmp: Path, tag: str) -> tuple[float | None, int]:
    """MoJoFM(old, new) over the survivors present in BOTH clusterings (100 = no churn)."""
    common = survivors & set(old_cl) & set(new_cl)
    if len(common) < 2:
        return None, len(common)
    a, b = tmp / f"{tag}-old.rsf", tmp / f"{tag}-new.rsf"
    write_contains(_restrict(old_cl, common), a)
    write_contains(_restrict(new_cl, common), b)
    return mojofm(b, a), len(common)


# --------------------------------------------------------------- per-technique clustering

def _cluster(technique: str, nodes: set[str], edges: list[tuple[str, str]],
             graph_rsf: Path, anon_full: dict[str, str], tmp: Path, tag: str
             ) -> dict[str, str] | None:
    """Cluster `nodes`/`edges` (or the graph file for ACDC) under one technique."""
    if technique == "dir":
        return cluster_dir(nodes)
    if technique == "cc":
        return cluster_cc(nodes, edges)
    if technique == "anon":
        # The locked curation is an id-keyed map: applying it to a node set is a restriction.
        return _restrict(anon_full, nodes)
    if technique == "acdc":
        out = tmp / f"acdc-{tag}.rsf"
        try:
            run_acdc(graph_rsf, out)
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"    acdc failed on {tag}: {exc}", file=sys.stderr)
            return None
        return read_contains(out)
    raise ValueError(f"unknown technique: {technique}")


def _seeded_removed(nodes: set[str], frac: float) -> set[str]:
    """Deterministically pick ~`frac` of nodes, spread across the sorted order (every
    step-th), so the 'old version' loses entities uniformly rather than one contiguous
    region. No RNG — fits the harness determinism gate."""
    if frac <= 0:
        return set()
    step = max(2, round(1 / frac))
    return {n for i, n in enumerate(sorted(nodes)) if i % step == 0}


# --------------------------------------------------------------- perturb mode

def run_perturb(system: str, techniques: list[str], fractions: tuple[float, ...]) -> dict | None:
    lang, gran = SYSTEMS[system]
    gdir = _graph_dir(system)
    graph_full = gdir / f"graph-{gran}.rsf"
    clusters_full = gdir / f"clusters-{gran}.rsf"
    if not graph_full.exists():
        print(f"  {system}: no graph-{gran}.rsf — skipping", file=sys.stderr)
        return None
    edges_full = read_depends(graph_full)
    nodes_full = {n for e in edges_full for n in e}
    anon_full = read_contains(clusters_full) if clusters_full.exists() else {}
    techs = [t for t in techniques
             if t != "anon" or anon_full]  # drop anon if no curation export
    tmp = REPO / "out" / system / "_churn"
    tmp.mkdir(parents=True, exist_ok=True)

    # NEW version = the full graph, clustered once per technique.
    full_cl: dict[str, dict[str, str]] = {}
    for t in techs:
        cl = _cluster(t, nodes_full, edges_full, graph_full, anon_full, tmp, "full")
        if cl is not None:
            full_cl[t] = cl

    res: dict[str, dict] = {t: {} for t in full_cl}
    for frac in fractions:
        removed = _seeded_removed(nodes_full, frac)
        survivors = nodes_full - removed
        edges_old = [(a, b) for a, b in edges_full if a in survivors and b in survivors]
        graph_old = tmp / f"graph-old-{frac}.rsf"
        _write_graph(edges_old, graph_old)
        for t in full_cl:
            old_cl = _cluster(t, survivors, edges_old, graph_old, anon_full,
                              tmp, f"old-{frac}")
            if old_cl is None:
                continue
            churn, scored = _churn(old_cl, full_cl[t], survivors, tmp, f"{t}-{frac}")
            res[t][f"{frac:.2f}"] = {"churn_mojofm": churn, "survivors_scored": scored}

    shutil.rmtree(tmp, ignore_errors=True)
    out = {"system": system, "mode": "perturb", "language": lang, "granularity": gran,
           "entities": len(nodes_full), "fractions": [f"{f:.2f}" for f in fractions],
           "techniques": res}
    (REPO / "out" / system / "baseline" / "stability-churn.json").write_text(
        _dumps(out), encoding="utf-8", newline="")
    return out


# --------------------------------------------------------------- snapshot mode (real refs)

def _arch(*args: str) -> None:
    subprocess.run(["arch", *args], check=True, capture_output=True, text=True)


def _export_version(system: str, repo: Path, ref: str, gran: str, work: Path
                    ) -> tuple[Path, Path] | None:
    """Check out `ref` of `repo` into a worktree, extract + curate + export-graph, and
    return (graph_rsf, anon_clusters_rsf) for that version. Returns None if the clone
    lacks history for `ref`."""
    if subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"],
                      capture_output=True, text=True).stdout.strip() == "true":
        print(f"  {system}: fixtures/{system} is a DEPTH-1 clone — snapshot mode needs "
              f"history.\n    enable: git -C fixtures/{system} fetch --unshallow",
              file=sys.stderr)
        return None
    if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                      capture_output=True, text=True).returncode != 0:
        print(f"  {system}: ref {ref!r} not found in fixtures/{system} "
              f"(fetch the tag/commit first)", file=sys.stderr)
        return None
    wt = work / f"wt-{ref.replace('/', '_')}"
    arch_dir = work / f"arch-{ref.replace('/', '_')}" / "architecture"
    rules_dir = REPO / "overlays" / system / "architecture" / "rules"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), ref],
                   check=True, capture_output=True, text=True)
    try:
        _arch("extract", "--repo", str(wt), "--arch-dir", str(arch_dir))
        _arch("curate", "--repo", str(wt), "--arch-dir", str(arch_dir),
              "--rules-dir", str(rules_dir))
        _arch("export", "--graph", "--granularity", gran,
              "--repo", str(wt), "--arch-dir", str(arch_dir))
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)
    g = arch_dir / "generated" / "graph" / f"graph-{gran}.rsf"
    c = arch_dir / "generated" / "graph" / f"clusters-{gran}.rsf"
    return (g, c) if g.exists() else None


def run_snapshot(system: str, old_ref: str, new_ref: str, techniques: list[str]) -> dict | None:
    lang, gran = SYSTEMS[system]
    repo = REPO / "fixtures" / system
    if not (repo / ".git").exists() and subprocess.run(
            ["git", "-C", str(repo), "rev-parse"], capture_output=True).returncode != 0:
        print(f"  {system}: no fixtures/{system} clone — skipping", file=sys.stderr)
        return None
    work = REPO / "out" / system / "_churn_snap"
    work.mkdir(parents=True, exist_ok=True)
    try:
        old = _export_version(system, repo, old_ref, gran, work)
        new = _export_version(system, repo, new_ref, gran, work)
        if not old or not new:
            return None
        (g_old, c_old_p), (g_new, c_new_p) = old, new
        nodes_old = {n for e in read_depends(g_old) for n in e}
        nodes_new = {n for e in read_depends(g_new) for n in e}
        survivors = nodes_old & nodes_new
        res: dict[str, dict] = {}
        for t in techniques:
            cl_old = _technique_for_graph(t, g_old, c_old_p, work, f"{t}-old")
            cl_new = _technique_for_graph(t, g_new, c_new_p, work, f"{t}-new")
            if cl_old is None or cl_new is None:
                continue
            churn, scored = _churn(cl_old, cl_new, survivors, work, f"{t}")
            res[t] = {"churn_mojofm": churn, "survivors_scored": scored}
        # Anon-specific: how are genuinely-NEW entities handled by the locked rules?
        anon_new = read_contains(c_new_p) if c_new_p.exists() else {}
        new_entities = nodes_new - nodes_old
        placed = sum(1 for n in new_entities if n in anon_new)
        res.setdefault("anon", {})
        res["anon"]["new_entities"] = len(new_entities)
        res["anon"]["new_placed_by_rule"] = placed
        res["anon"]["new_unmapped"] = len(new_entities) - placed
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out = {"system": system, "mode": "snapshot", "old_ref": old_ref, "new_ref": new_ref,
           "language": lang, "granularity": gran, "survivors": len(survivors),
           "techniques": res}
    (REPO / "out" / system / "baseline" / "stability-churn-snapshot.json").write_text(
        _dumps(out), encoding="utf-8", newline="")
    return out


def _technique_for_graph(technique: str, graph_rsf: Path, clusters_rsf: Path,
                         tmp: Path, tag: str) -> dict[str, str] | None:
    edges = read_depends(graph_rsf)
    nodes = {n for e in edges for n in e}
    anon = read_contains(clusters_rsf) if clusters_rsf.exists() else {}
    return _cluster(technique, nodes, edges, graph_rsf, anon, tmp, tag)


# --------------------------------------------------------------- render

def _dumps(obj: dict) -> str:
    import json
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _print_perturb(out: dict) -> None:
    fr = out["fractions"]
    print(f"\n{out['system']} ({out['language']}, {out['granularity']}, "
          f"{out['entities']} entities) — churn MoJoFM (100 = zero churn)")
    print(f"  {'technique':12} " + " ".join(f"{'-'+f:>10}" for f in fr) + "   kind")
    for t in sorted(out["techniques"], key=lambda x: (x not in PER_ENTITY, x)):
        cells = []
        for f in fr:
            v = out["techniques"][t].get(f, {}).get("churn_mojofm")
            cells.append(f"{v:>10.1f}" if isinstance(v, (int, float)) else f"{'—':>10}")
        kind = "per-entity (stable)" if t in PER_ENTITY else "global (churns)"
        print(f"  {t:12} " + " ".join(cells) + f"   {kind}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("systems", nargs="*", default=list(SYSTEMS),
                    help="systems to test (default: all configured)")
    ap.add_argument("--mode", default="perturb", choices=["perturb", "snapshot"])
    ap.add_argument("--techniques", default=",".join(DEFAULT_TECHNIQUES),
                    help="comma list of anon|acdc|cc|dir")
    ap.add_argument("--fractions", default=",".join(f"{f:.2f}" for f in FRACTIONS),
                    help="perturb mode: comma list of removal fractions")
    ap.add_argument("--old-ref", help="snapshot mode: the OLD git ref (tag/commit)")
    ap.add_argument("--new-ref", default="HEAD", help="snapshot mode: the NEW git ref")
    args = ap.parse_args(argv)
    techniques = [t.strip() for t in args.techniques.split(",") if t.strip()]
    fractions = tuple(float(x) for x in args.fractions.split(","))

    if args.mode == "snapshot" and not args.old_ref:
        ap.error("snapshot mode requires --old-ref")

    for s in (args.systems or list(SYSTEMS)):
        if s not in SYSTEMS:
            print(f"  {s}: unknown system", file=sys.stderr)
            continue
        if args.mode == "perturb":
            out = run_perturb(s, techniques, fractions)
            if out:
                _print_perturb(out)
        else:
            out = run_snapshot(s, args.old_ref, args.new_ref, techniques)
            if out:
                r = out["techniques"]
                print(f"\n{s}: snapshot {args.old_ref} → {args.new_ref}, "
                      f"{out['survivors']} survivors")
                for t in sorted(r):
                    print(f"  {t:12} churn MoJoFM = {r[t].get('churn_mojofm')}  "
                          + (f"(new: {r[t]['new_placed_by_rule']}/{r[t]['new_entities']} "
                             f"rule-placed, {r[t]['new_unmapped']} unmapped)"
                             if "new_entities" in r[t] else ""))
    print("\nchurn = MoJoFM(old, new) over surviving entities; 100 = survivors' partition "
          "unchanged.\nper-entity rules (anon/dir) are set-monotone → 100 by "
          "construction; global\noptimizers (acdc/cc) reshuffle survivors when entities "
          "are added/removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
