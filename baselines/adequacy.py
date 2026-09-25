"""Build-graph adequacy diagnostic — reference-INDEPENDENT (review response 2026-08-30, item 2.1).

The paper's two-regime finding ("where the build graph carries the architecture, the
no-curation floor recovers it") was diagnosed by consulting the reference, which a reviewer
rightly called retrospective. This script computes, from the extracted facts alone, three
numbers that describe how much grouping the build system's own unit of composition offers
at the scored granularity — the ``none`` rung of the A2 ablation (entity -> owning build
target, zero curation), i.e. exactly the autonomous floor::

    python baselines/adequacy.py                 # fidelity corpus + the reference-less Tier-B pool
    python baselines/adequacy.py itk fmt         # a subset

Per system (``out/<sys>/baseline/adequacy.json``, sorted keys, LF, no timestamps):

  * ``entities``      — nodes at the scored granularity (files for C++, projects for C#);
  * ``units``         — build units the floor partitions them into (clusters of ``none``);
  * ``unit_ratio``    — units / entities. 1.0 = every entity is its own unit (the build
                        system offers NO grouping above the entity — the C# project idiom);
                        small = many entities per unit (files -> targets);
  * ``largest_share`` — share of entities in the largest unit. ~1.0 = one monolithic unit
                        (libxml2's single library) — the build graph exists but says nothing;
  * ``mq``            — TurboMQ of the floor partition over the exported dependency graph
                        (Mitchell & Mancoridis): how well the build units cut the graph.
                        0 by construction when every unit is a singleton;
  * ``edges``, ``intra_edges`` — the graph size and how many edges the floor keeps inside a unit.

None of these consult ``references/``. ``analysis/stats.py`` correlates them (Spearman) with
the all-common floor MoJoFM/a2a over the fidelity systems; the paper reports the diagnostic
as POST HOC (it was not pre-registered) and states the decision rule the data supports.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_arcade import read_contains, read_depends  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# system -> (language, scored granularity). Fidelity corpus first (mirrors a2_ablation.SYSTEMS),
# then the reference-less Tier-B pool (mirrors aggregate_results.PERF_EXTRA minus the toy).
SYSTEMS: dict[str, tuple[str, str]] = {
    "itk": ("cpp", "file"), "abseil": ("cpp", "file"), "opencv": ("cpp", "file"),
    "bash": ("cpp", "file"), "libxml2": ("cpp", "file"),
    "eshop": ("cs", "target"), "orchardcore": ("cs", "target"),
    "nopcommerce": ("cs", "target"), "squidex": ("cs", "target"),
    "fmt": ("cpp", "file"), "terminal": ("cpp", "target"),
    "serilog": ("cs", "target"),
    # orleans deliberately absent: its workspace has only a file-granularity export and no
    # a2-rungs (extractor bug, see memory/plan notes), so it emitted no row — listing it here
    # only produced a skip line (audit 2026-08-31).
}
FIDELITY = ("itk", "abseil", "opencv", "bash", "libxml2",
            "eshop", "orchardcore", "nopcommerce", "squidex")


def floor_partition(system: str, lang: str, gran: str) -> dict[str, str] | None:
    """entity -> build unit, zero curation. Prefers the persisted A2 ``none`` rung (the
    identical partition the ablation scored); falls back to the file-deps sidecar (C++ file
    granularity) or to singletons over the graph nodes (target granularity)."""
    arch = REPO / "out" / system / "architecture"
    rung = REPO / "out" / system / "baseline" / "a2-rungs" / "none.rsf"
    if rung.exists():
        return read_contains(rung)
    graph = arch / "generated" / "graph" / f"graph-{gran}.rsf"
    nodes = {n for e in read_depends(graph) for n in e} if graph.exists() else set()
    if gran == "file":
        for sc in sorted((arch / ".cache" / "fragments").glob("file-deps-*.json")):
            files = json.loads(sc.read_text(encoding="utf-8")).get("files") or {}
            part = {f: t for f, t in files.items() if t}
            if part:
                return part
        return None
    return {n: n for n in nodes} if nodes else None


def turbo_mq(partition: dict[str, str], edges: list[tuple[str, str]]) -> tuple[float, int, int]:
    """TurboMQ = sum over clusters of intra / (intra + 0.5 * inter) — the reference-free
    modularisation quality Bunch optimises. Returns (mq, n_edges_scored, n_intra)."""
    intra: Counter = Counter()
    inter: Counter = Counter()
    scored = 0
    for s, t in edges:
        cs, ct = partition.get(s), partition.get(t)
        if cs is None or ct is None or s == t:
            continue
        scored += 1
        if cs == ct:
            intra[cs] += 1
        else:
            inter[cs] += 1
            inter[ct] += 1
    mq = 0.0
    for c in set(partition.values()):
        i, e = intra[c], inter[c]
        if i + e:
            mq += i / (i + 0.5 * e)
    return round(mq, 3), scored, sum(intra.values())


def diagnose(system: str) -> dict | None:
    lang, gran = SYSTEMS[system]
    part = floor_partition(system, lang, gran)
    if not part:
        print(f"  {system}: no floor partition (no a2-rungs/none.rsf, sidecar, or graph)",
              file=sys.stderr)
        return None
    graph = REPO / "out" / system / "architecture" / "generated" / "graph" / f"graph-{gran}.rsf"
    edges = read_depends(graph) if graph.exists() else []
    sizes = Counter(part.values())
    n = len(part)
    mq, scored, intra = turbo_mq(part, edges)
    return {
        "system": system, "language": lang, "granularity": gran,
        "entities": n, "units": len(sizes),
        "unit_ratio": round(len(sizes) / n, 4),
        "largest_share": round(max(sizes.values()) / n, 4),
        "mq": mq, "mq_per_unit": round(mq / len(sizes), 4),
        "edges": scored, "intra_edges": intra,
        "intra_share": round(intra / scored, 4) if scored else None,
        "fidelity_corpus": system in FIDELITY,
        "source": "a2-rungs/none.rsf" if (REPO / "out" / system / "baseline" / "a2-rungs"
                                          / "none.rsf").exists() else "sidecar-or-singletons",
        "schema": "adequacy/1",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("systems", nargs="*", default=list(SYSTEMS))
    args = ap.parse_args(argv)
    print(f"{'system':12} {'lang':4} {'gran':6} {'entities':>8} {'units':>5} {'ratio':>6} "
          f"{'largest':>7} {'MQ':>7} {'intra%':>6}")
    for s in args.systems:
        if s not in SYSTEMS:
            print(f"  {s}: unknown system", file=sys.stderr)
            continue
        d = diagnose(s)
        if not d:
            continue
        out = REPO / "out" / s / "baseline" / "adequacy.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                       newline="")
        intra = f"{100 * d['intra_share']:.0f}" if d["intra_share"] is not None else "--"
        print(f"{s:12} {d['language']:4} {d['granularity']:6} {d['entities']:>8} {d['units']:>5} "
              f"{d['unit_ratio']:>6.3f} {d['largest_share']:>7.3f} {d['mq']:>7.2f} {intra:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
