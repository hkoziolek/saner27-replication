"""Eval §14.2 — consolidate the per-system RQ1/RQ2 all-common comparison file.

Writes ``out/<system>/baseline/comparison-<granularity>.json`` — the input contract both
``analysis/aggregate_results.py`` (OSS) and ``analysis/anonymize_results.py`` (Tier-P)
read for the fair fidelity table: every clustering (the pipeline's own plus each SAR
baseline found under ``out/<system>/baseline/``) projected to ONE shared entity set and
scored against the same reference (§6.4 fairness — pairwise ``fidelity.json`` projections
differ per pair; this file is the cross-technique-comparable view)::

    python baselines/make_comparison.py <system> [--granularity file|target]
        [--arch-dir out/<system>/architecture] [--reference references/<system>/reference.rsf]
        [--techniques acdc,arc,wca,limbo,dir,cc]

Prerequisites: ``arch export --graph`` (the pipeline clusters) and ``run_arcade.py``
(the baseline clusters) have run. Techniques whose ``clusters.rsf`` is missing are skipped
with a warning — visible, never silent. Output is deterministic (sorted keys, LF, no
timestamps): rerunning over the same inputs is byte-identical.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from run_arcade import (a2a, ari_of, mojofm, nested_share, read_contains,  # noqa: E402
                        write_contains)

REPO_ROOT = Path(__file__).resolve().parent.parent
TECH_ORDER = ["acdc", "arc", "wca", "limbo", "dir", "cc"]
# A2 curation rungs (baselines/a2_ablation.py persists them under baseline/a2-rungs/) that are
# ALSO scored on the all-common set, under the technique names the paper uses: the no-curation
# floor and the auto-`propose` draft accepted verbatim — the two AUTONOMOUS pipeline rows the RQ2
# comparison must lead with (review response 2026-08-30, items 1.1/5.6). Their a2-ablation.json
# numbers are on the rungs' own common-a2 set; these are on the SAR set, hence comparable.
RUNG_TECH = {"none": "floor", "propose_all": "propose"}


def collect_clusterings(arch: Path, base: Path, g: str,
                        techniques: list[str],
                        rungs: tuple[str, ...] = ()) -> dict[str, dict[str, str]]:
    """name -> {entity: cluster}; ``pipeline`` from the arch workspace, the rest from
    ``out/<system>/baseline/<tech>-<g>/clusters.rsf``; ``rungs`` (A2 rung ids, see
    ``RUNG_TECH``) from ``baseline/a2-rungs/<rung>.rsf`` under their technique names."""
    clusterings: dict[str, dict[str, str]] = {}
    pipeline_rsf = arch / "generated" / "graph" / f"clusters-{g}.rsf"
    if not pipeline_rsf.exists():
        raise FileNotFoundError(
            f"{pipeline_rsf} missing — run `arch export --graph --granularity {g}` first")
    clusterings["pipeline"] = read_contains(pipeline_rsf)
    for tech in techniques:
        rsf = base / f"{tech}-{g}" / "clusters.rsf"
        if rsf.exists():
            clusterings[tech] = read_contains(rsf)
        else:
            print(f"  [skip] {tech}: no {rsf} (run run_arcade.py first)", file=sys.stderr)
    for rung, name in RUNG_TECH.items():
        if rung not in rungs:
            continue
        rsf = base / "a2-rungs" / f"{rung}.rsf"
        if rsf.exists():
            clusterings[name] = read_contains(rsf)
        else:
            print(f"  [skip] {name}: no {rsf} (run a2_ablation.py first)", file=sys.stderr)
    return clusterings


def build_comparison(system: str, g: str, clusterings: dict[str, dict[str, str]],
                     reference: Path, workdir: Path) -> dict:
    """Project reference + every clustering to the shared entity set, score, assemble."""
    ref = read_contains(reference)
    common = set(ref)
    for members in clusterings.values():
        common &= set(members)
    # mojo.MoJo's RSF reader cannot parse whitespace entities even quoted (run_arcade.score) —
    # drop them uniformly so every technique scores the identical set.
    ws_dropped = {e for e in common if any(c.isspace() for c in e)}
    common -= ws_dropped

    entities = {name: len(members) for name, members in clusterings.items()}
    out = {"system": system, "granularity": g,
           "reference_entities": len(ref), "shared_entities": len(common),
           "dropped_whitespace": len(ws_dropped), "entities": entities,
           "projection": "all-common", "results": {}}
    if not common:
        print("  WARNING: empty shared entity set — check granularities/reference projection",
              file=sys.stderr)
        return out

    ref_proj = workdir / "reference-common.rsf"
    ref_common = {e: c for e, c in ref.items() if e in common}
    write_contains(ref_common, ref_proj)
    for name in sorted(clusterings):
        proj = workdir / f"{name}-common.rsf"
        members = {e: c for e, c in clusterings[name].items() if e in common}
        write_contains(members, proj)
        mj, aa = mojofm(proj, ref_proj), a2a(proj, ref_proj)
        # Round to 2 dp so the all-common rows match the pairwise rows' precision in results.csv
        # (aggregate rounds pairwise; the ARCADE a2a impl returns full-precision floats). Display
        # tables/stats round anyway, so this only keeps the raw CSV clean and diff-stable.
        # ARI (x100) and the nested share, added 2026-09-09 (eval plan section 12 (m)): post
        # hoc, descriptive, computed on the identical projected pair as MoJoFM/a2a.
        ar, ns = ari_of(members, ref_common), nested_share(members, ref_common)
        out["results"][name] = {"mojofm": round(mj, 2) if mj is not None else None,
                                "a2a": round(aa, 2) if aa is not None else None,
                                "ari": round(ar, 2) if ar is not None else None,
                                "nested": round(ns, 2) if ns is not None else None,
                                "clusters": len(set(members.values()))}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("system", help="fixture id (out/<system>/) or a label for --arch-dir mode")
    ap.add_argument("--granularity", default="file", choices=["file", "target"])
    ap.add_argument("--arch-dir", help="override: the workspace arch dir "
                                       "(default out/<system>/architecture)")
    ap.add_argument("--reference", help="override: reference clustering RSF "
                                        "(default references/<system>/reference.rsf; a "
                                        "run_arcade file-projected reference is preferred "
                                        "automatically when present)")
    ap.add_argument("--techniques", default=",".join(TECH_ORDER),
                    help=f"comma list of baseline techniques (default: {','.join(TECH_ORDER)})")
    ap.add_argument("--out-root", type=Path, default=REPO_ROOT / "out",
                    help="root of the out/<system>/ tree (default: <kit>/out)")
    ap.add_argument("--rungs", default=",".join(RUNG_TECH),
                    help="A2 rungs to score on the all-common set as well "
                         f"(default: {','.join(RUNG_TECH)}; '' for none)")
    args = ap.parse_args(argv)

    g = args.granularity
    arch = Path(args.arch_dir) if args.arch_dir else args.out_root / args.system / "architecture"
    base = args.out_root / args.system / "baseline"
    # Same target->file reference handling as run_arcade: prefer its projected reference so
    # the comparison scores the identical file-level comparator (C# file granularity).
    projected = base / "reference-file-projected.rsf"
    if args.reference:
        reference = Path(args.reference)
    elif g == "file" and projected.exists():
        reference = projected
    else:
        reference = REPO_ROOT / "references" / args.system / "reference.rsf"
    if not reference.exists():
        print(f"ERROR: reference {reference} missing — build it first (eval §5.3).",
              file=sys.stderr)
        return 2

    techniques = [t.strip() for t in args.techniques.split(",") if t.strip()]
    rungs = tuple(r.strip() for r in args.rungs.split(",") if r.strip())
    try:
        clusterings = collect_clusterings(arch, base, g, techniques, rungs)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    comp = build_comparison(args.system, g, clusterings, reference,
                            base / "comparison-work")
    out_path = base / f"comparison-{g}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(comp, indent=2, sort_keys=True) + "\n")
    scored = ", ".join(f"{t}={v['mojofm']}/{v['a2a']}/{v['ari']}"
                       for t, v in sorted(comp["results"].items()))
    print(f"[comparison-{g}] {args.system}: {comp['shared_entities']} shared entities; {scored}")
    print(f"  -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
