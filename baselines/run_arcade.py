"""Eval §14.1 — baseline runner: SAR techniques + metrics over the canonical RSF export.

Runs the RQ2 baseline techniques over ONE canonical dependency graph (the §6.4 fairness
control — the same ``generated/graph/graph-<granularity>.rsf`` the pipeline's own
decomposition is scored on) and computes the established metrics (MoJoFM, a2a) with the
community's own implementations (ARCADE / Tzerpos's mojo), never re-implementations::

    python baselines/run_arcade.py <system> [--granularity file|target]
        [--techniques acdc,dir,cc] [--reference references/<system>/reference.rsf]
        [--arch-dir out/<system>/architecture]

Outputs (eval §14.2 layout)::

    out/<system>/baseline/<technique>-<granularity>/clusters.rsf
    out/<system>/baseline/<technique>-<granularity>/fidelity.json

Techniques:
  * ``acdc``  — ACDC (Tzerpos & Holt), via the prebuilt ARCADE_Core.jar (v1.2.0 release,
    pinned below; bundles ``mojo.MoJo`` and ``SystemEvo``/a2a). The jar is downloaded to
    ``tools/arcade/`` (gitignored, §22.2) — this script prints the exact URL if missing.
  * ``dir``   — naïve directory-structure clustering (§6.3): cluster = the entity's
    parent directory (file granularity) / its id prefix path (target granularity).
  * ``cc``    — naïve connected-components on the undirected dependency graph (§6.3).

WCA / ARC / LIMBO are also drivable from ARCADE_Core (``clustering.Clusterer`` with
key=value args) but need FeatureVectors / MALLET doc-topics — wired in a follow-up.
Bunch is a separate tool (not in ARCADE_Core).

Each ``fidelity.json`` records MoJoFM + a2a vs the reference (when
``references/<system>/reference.rsf`` exists) and vs the pipeline's own clusters file
(as *agreement*, not fidelity), plus entity counts so an entity-set mismatch between
clusterings is visible, never silent. Metric direction: ``MoJoFM(recovered, reference)``.

File granularity vs a TARGET-granularity reference (the C# case — the GT-doc is project/service
level, eval §5.2): the reference is projected down to files (each file inherits its owning
target's reference cluster, via the ``file-deps/1`` sidecar's file->owner map) so every technique
scores file clusters against a file-level reference. C++ references are already file-level, so the
projection is a no-op there. Needs the file-deps sidecar present under the arch dir (run `arch
extract` so the C# Roslyn helper writes it).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCADE_JAR = REPO_ROOT / "tools" / "arcade" / "ARCADE_Core.jar"
ARCADE_URL = ("https://github.com/usc-softarch/ARCADE_Core/releases/download/"
              "v1.2.0/ARCADE_Core.jar")   # pinned (eval §6.4: one metric impl for all)

NAIVE = {"dir", "cc"}

ARC_DIR = REPO_ROOT / "baselines" / "arc"          # holds the Java driver sources + .class files
ARC_SRCS = [ARC_DIR / "ArcRunner.java", ARC_DIR / "StructuralRunner.java"]
FIXTURES = REPO_ROOT / "fixtures"                  # gitignored shallow clones (manifest.yaml)

# Techniques driven by the bundled ARCADE Clusterer (need k = preselected cluster count).
ARC_DRIVEN = {"arc"}        # topic-based — needs source text (ArcRunner)
STRUCTURAL_DRIVEN = {"wca", "limbo"}   # structural — graph only (StructuralRunner)


# ------------------------------------------------------------------ RSF primitives

def _tokens(line: str) -> list[str]:
    """Split an RSF tuple line, honoring double-quoted entities with spaces."""
    out, cur, quoted = [], [], False
    for ch in line.strip():
        if ch == '"':
            quoted = not quoted
        elif ch.isspace() and not quoted:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def read_depends(path: Path) -> list[tuple[str, str]]:
    edges = []
    for line in path.read_text(encoding="utf-8").splitlines():
        tok = _tokens(line)
        if len(tok) == 3 and tok[0] == "depend":
            edges.append((tok[1], tok[2]))
    return edges


def read_contains(path: Path) -> dict[str, str]:
    """entity -> cluster from `contain <cluster> <entity>` tuples."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        tok = _tokens(line)
        if len(tok) == 3 and tok[0] == "contain":
            out[tok[2]] = tok[1]
    return out


# ------------------------------------------------------------ ARI + nestedness (stdlib)
# Added 2026-09-09 (response plan section 12.a, eval plan section 12 amendment (m)): the adjusted
# Rand index Zhang et al. (SARIF, ESEC/FSE 2023, section 4) introduced to SAR evaluation because
# MoJoFM's cheap joins favour over-split partitions and a2a's dynamic range is compressed. POST
# HOC and descriptive: MoJoFM and a2a stay the pre-registered metrics and the inferential tests
# run on them alone. Reported x100 like the other two so the tables read on one scale. ARI is a
# pure function of the two partitions over their common entity set (label names never enter);
# it is chance-corrected, 100 = identical, 0 = what a random relabelling would score, and it can
# go slightly negative. The same contingency-table form as rq9_blind_curation.py's inter-rater
# companions, so the paper's two ARI uses agree by construction.

def _c2(n: int) -> int:
    return n * (n - 1) // 2


def ari_of(rec: dict[str, str], ref: dict[str, str]) -> float | None:
    """Adjusted Rand index x100 of two entity->cluster maps over their common entities."""
    common = sorted(set(rec) & set(ref))
    n = len(common)
    if n < 2:
        return None
    ua = sorted({rec[e] for e in common})
    ub = sorted({ref[e] for e in common})
    ia = {u: i for i, u in enumerate(ua)}
    ib = {u: i for i, u in enumerate(ub)}
    cont = [[0] * len(ub) for _ in ua]
    for e in common:
        cont[ia[rec[e]]][ib[ref[e]]] += 1
    s_ij = sum(_c2(c) for row in cont for c in row)
    s_a = sum(_c2(sum(row)) for row in cont)
    s_b = sum(_c2(sum(col)) for col in zip(*cont))
    n_pairs = _c2(n)
    expected = s_a * s_b / n_pairs
    max_index = (s_a + s_b) / 2
    if abs(max_index - expected) < 1e-12:
        # degenerate (both all-singletons or both one cluster): identical -> 100, else 0
        return 100.0 if all(rec[e] == rec[common[0]] for e in common) == \
            all(ref[e] == ref[common[0]] for e in common) else 0.0
    return 100.0 * (s_ij - expected) / (max_index - expected)


def nested_share(rec: dict[str, str], ref: dict[str, str]) -> float | None:
    """Share (x100) of the recovered clusters whose common-entity members all lie in ONE
    reference cluster — i.e. how much of the recovered partition is a pure refinement of the
    reference. 100 = every recovered cluster nests inside a reference cluster (the partition is
    the reference or a finer cut of it); a low value means clusters straddle reference
    boundaries. Reported beside ARI because ARI charges over-splitting that MoJoFM/a2a forgive,
    and this number says whether a low ARI is over-splitting (nested) or misgrouping (not)."""
    common = set(rec) & set(ref)
    if not common:
        return None
    seen: dict[str, set[str]] = {}
    for e in common:
        seen.setdefault(rec[e], set()).add(ref[e])
    return 100.0 * sum(1 for refs in seen.values() if len(refs) == 1) / len(seen)


def ari(recovered: Path, reference: Path) -> float | None:
    return ari_of(read_contains(recovered), read_contains(reference))


def _rsf_entity(entity: str) -> str:
    return f'"{entity}"' if any(c.isspace() for c in entity) else entity


def write_contains(member_cluster: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = sorted(f"contain {_rsf_entity(c)} {_rsf_entity(m)}"
                   for m, c in member_cluster.items())
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))


# ------------------------------------------------------------ reference projection

def file_owner_map(arch: Path) -> dict[str, str]:
    """``file (repo-rel) -> owning target id``, merged from the ``file-deps/1`` sidecars under
    the arch workspace (first sidecar wins on conflict, matching :func:`filedeps.merge`). The
    C# Roslyn helper now emits these (eval §4.1), so this is non-empty for C# workspaces too."""
    owner: dict[str, str] = {}
    for p in sorted((arch / ".cache" / "fragments").glob("file-deps-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for f, tid in (data.get("files") or {}).items():
            if tid and f not in owner:
                owner[f] = tid
    return owner


def project_reference_to_files(ref_targets: dict[str, str],
                               file_owner: dict[str, str]) -> dict[str, str]:
    """Project a TARGET-granularity reference (``csharp:csproj:… -> cluster``) down to FILES:
    each file inherits its owning target's reference cluster (eval §5.2). The C# fidelity
    references are project/service GT-doc, so file-level scoring needs this projection; files
    whose owner is not in the reference (excluded/out-of-reference projects) are simply absent —
    visible in the common-entity counts, never silently relabelled."""
    return {f: ref_targets[owner] for f, owner in file_owner.items() if owner in ref_targets}


# ------------------------------------------------------------------ techniques

def run_acdc(graph_rsf: Path, out_rsf: Path) -> None:
    out_rsf.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["java", "-cp", str(ARCADE_JAR),
         "edu.usc.softarch.arcade.clustering.acdc.ACDC",
         str(graph_rsf), str(out_rsf)],
        check=True, capture_output=True, text=True)


def _ensure_arc_compiled() -> None:
    """Compile the Java drivers against the ARCADE jar if any class is missing/stale."""
    newest_src = max(s.stat().st_mtime for s in ARC_SRCS)
    classes = [ARC_DIR / "ArcRunner.class", ARC_DIR / "StructuralRunner.class"]
    if all(c.exists() for c in classes) and min(c.stat().st_mtime for c in classes) >= newest_src:
        return
    subprocess.run(["javac", "-cp", str(ARCADE_JAR), "-d", str(ARC_DIR), *map(str, ARC_SRCS)],
                   check=True, capture_output=True, text=True)


def run_structural(graph_rsf: Path, out_rsf: Path, *, algo: str, num_clusters: int,
                   system: str) -> None:
    """WCA / LIMBO via ARCADE's own Clusterer (structural — graph only, no source/topics).

    Deterministic (byte-identical reruns), full graph coverage. ``num_clusters`` is the
    preselected agglomerative stopping point (eval §6.4: = the reference cluster count).
    """
    _ensure_arc_compiled()
    out_rsf.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["java", "-cp", os.pathsep.join([str(ARCADE_JAR), str(ARC_DIR)]), "StructuralRunner",
         str(graph_rsf), algo, str(out_rsf), str(num_clusters), system],
        check=True, text=True)


def run_arc(graph_rsf: Path, out_rsf: Path, *, src_root: Path, granularity: str,
            language: str, num_clusters: int, num_topics: int) -> None:
    """ARC (Garcia et al.) via ARCADE's own Clusterer + bundled MALLET LDA (baselines/arc).

    ARC is topic-based, so unlike ACDC/dir/cc it needs the source TEXT of each graph entity
    (resolved under ``src_root`` = the gitignored manifest clone) in addition to the canonical
    graph. ``num_clusters`` is the preselected agglomerative stopping point (eval §6.4: ARC is
    given k = the reference cluster count, the standard SAR protocol; ACDC self-determines k).
    """
    _ensure_arc_compiled()
    out_rsf.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["java", "-cp", os.pathsep.join([str(ARCADE_JAR), str(ARC_DIR)]), "ArcRunner",
         str(graph_rsf), str(src_root), granularity, str(out_rsf),
         language, str(num_topics), str(num_clusters), out_rsf.parent.parent.parent.name],
        check=True, text=True)


def cluster_dir(nodes: set[str]) -> dict[str, str]:
    """Directory-structure clustering: parent path (or '.' at the root)."""
    return {n: (n.rsplit("/", 1)[0] if "/" in n else ".") for n in nodes}


def cluster_cc(nodes: set[str], edges: list[tuple[str, str]]) -> dict[str, str]:
    """Connected components on the undirected graph; component named by its min member."""
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    comp: dict[str, list[str]] = {}
    for n in nodes:
        comp.setdefault(find(n), []).append(n)
    return {n: min(members) for members in comp.values() for n in members}


# ------------------------------------------------------------------ metrics

def _java_metric(cls: str, a: Path, b: Path, extra: list[str] | None = None) -> float | None:
    try:
        proc = subprocess.run(
            ["java", "-cp", str(ARCADE_JAR), cls, str(a), str(b), *(extra or [])],
            check=True, capture_output=True, text=True, timeout=600)
        return float(proc.stdout.strip().splitlines()[-1])
    except (subprocess.SubprocessError, ValueError, IndexError) as exc:
        print(f"  WARNING: {cls} failed on ({a.name}, {b.name}): {exc}", file=sys.stderr)
        return None


def mojofm(recovered: Path, reference: Path) -> float | None:
    return _java_metric("mojo.MoJo", recovered, reference, ["-fm"])


def a2a(recovered: Path, reference: Path) -> float | None:
    return _java_metric("edu.usc.softarch.arcade.metrics.SystemEvo", recovered, reference)


def score(recovered: Path, against: Path | None) -> dict | None:
    """Score with the pre-registered common-entity projection (references/README.md).

    ``mojo.MoJo`` yields NaN when the two clusterings cover different entity sets, and
    the recovered set (first-party build-reachable files) never exactly equals a
    published reference (which may include generated/test/data files). Both clusterings
    are therefore projected to their COMMON entity set before scoring — uniformly for
    every technique (§6.4 fairness), with the overlap counts recorded so the projection
    is visible in the artifact, never silent.
    """
    if against is None or not against.exists():
        return None
    rec, ref = read_contains(recovered), read_contains(against)
    common = set(rec) & set(ref)
    # ``mojo.MoJo``'s RSF reader splits on whitespace and cannot parse an entity containing a
    # space, even quoted (rare but real in C# trees, e.g. a file literally named "Foo .cs").
    # Drop such entities UNIFORMLY (so mojofm and a2a score the same set) and record the count —
    # an honest, visible exclusion rather than a silent metric crash on one pathological filename.
    ws_dropped = {e for e in common if any(c.isspace() for c in e)}
    common -= ws_dropped
    counts = {
        "entities_recovered": len(rec),
        "entities_against": len(ref),
        "entities_common": len(common),
        "entities_dropped_whitespace": len(ws_dropped),
    }
    if not common:
        return {**counts, "mojofm": None, "a2a": None, "ari": None, "nested": None}
    a, b = recovered, against
    if len(common) < len(rec) or len(common) < len(ref):
        a = recovered.parent / f"projected-{recovered.stem}-vs-{against.parent.name}.rsf"
        b = recovered.parent / f"projected-{against.parent.name}-reference.rsf"
        write_contains({m: c for m, c in rec.items() if m in common}, a)
        write_contains({m: c for m, c in ref.items() if m in common}, b)
    rec_c = {m: c for m, c in rec.items() if m in common}
    ref_c = {m: c for m, c in ref.items() if m in common}
    ari_v, nested_v = ari_of(rec_c, ref_c), nested_share(rec_c, ref_c)
    return {**counts, "mojofm": mojofm(a, b), "a2a": a2a(a, b),
            "ari": round(ari_v, 2) if ari_v is not None else None,
            "nested": round(nested_v, 2) if nested_v is not None else None}


# ------------------------------------------------------------------ driver

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("system", help="fixture id (out/<system>/) or a path to an arch dir")
    ap.add_argument("--granularity", default="file", choices=["file", "target"])
    ap.add_argument("--techniques", default="acdc,dir,cc",
                    help="comma list of acdc|arc|wca|limbo|dir|cc (default: acdc,dir,cc)")
    ap.add_argument("--arch-dir", help="override: the workspace arch dir "
                                       "(default out/<system>/architecture)")
    ap.add_argument("--reference", help="override: reference clustering RSF "
                                        "(default references/<system>/reference.rsf)")
    ap.add_argument("--src-root", help="ARC only: source clone for topic modeling "
                                       "(default fixtures/<system>)")
    ap.add_argument("--language", choices=["c", "java"],
                    help="ARC only: MALLET language profile "
                         "(default: c for file granularity, java for target)")
    ap.add_argument("--num-topics", type=int, default=100,
                    help="ARC only: LDA topic count (capped at entities/2; default 100)")
    ap.add_argument("--num-clusters", type=int,
                    help="ARC only: preselected k (default: reference cluster count)")
    args = ap.parse_args(argv)

    arch = Path(args.arch_dir) if args.arch_dir else REPO_ROOT / "out" / args.system / "architecture"
    g = args.granularity
    graph_rsf = arch / "generated" / "graph" / f"graph-{g}.rsf"
    pipeline_rsf = arch / "generated" / "graph" / f"clusters-{g}.rsf"
    if not graph_rsf.exists():
        print(f"ERROR: {graph_rsf} missing — run `arch export --graph --granularity {g}` first.",
              file=sys.stderr)
        return 2
    reference = Path(args.reference) if args.reference else \
        REPO_ROOT / "references" / args.system / "reference.rsf"
    techniques = [t.strip() for t in args.techniques.split(",") if t.strip()]
    if any(t not in NAIVE for t in techniques) and not ARCADE_JAR.exists():
        print(f"ERROR: {ARCADE_JAR} missing. Download the pinned build:\n"
              f"  curl -sL -o \"{ARCADE_JAR}\" {ARCADE_URL}", file=sys.stderr)
        return 2

    edges = read_depends(graph_rsf)
    nodes = {n for e in edges for n in e}
    base_out = REPO_ROOT / "out" / args.system / "baseline"

    # File-granularity vs a TARGET-granularity reference (C#: the GT-doc is project/service
    # level, eval §5.2): project the reference down to files via the file->owning-target sidecar
    # map, so every technique scores file clusters against a file-level reference (else
    # `score()`'s common entity set is empty and C# silently never scores). C++ references are
    # already file-level, so the projection is a no-op there (the reference shares the file nodes).
    if g == "file" and reference.exists():
        ref_targets = read_contains(reference)
        if ref_targets and not (set(ref_targets) & nodes):
            owner = file_owner_map(arch)
            projected = project_reference_to_files(ref_targets, owner)
            if projected:
                proj_path = base_out / "reference-file-projected.rsf"
                write_contains(projected, proj_path)
                reference = proj_path
                print(f"[project] target->file reference: {len(projected)} files from "
                      f"{len(ref_targets)} targets (via {len(owner)} file owners) -> {proj_path}",
                      file=sys.stderr)
            else:
                print(f"[project] WARNING: reference is target-granularity but no file->owner "
                      f"map under {arch}/.cache/fragments (run extract so the file-deps sidecar "
                      f"exists); file scoring will report empty common set.", file=sys.stderr)

    # ARC needs k (preselected agglomerative stopping) and source text; resolve once.
    arc_k = args.num_clusters
    if arc_k is None and reference.exists():
        arc_k = len(set(read_contains(reference).values()))
    elif arc_k is None and pipeline_rsf.exists():
        arc_k = len(set(read_contains(pipeline_rsf).values()))
    arc_src = Path(args.src_root) if args.src_root else FIXTURES / args.system
    arc_lang = args.language or ("c" if g == "file" else "java")

    for tech in techniques:
        out_dir = base_out / f"{tech}-{g}"
        out_rsf = out_dir / "clusters.rsf"
        if tech == "acdc":
            run_acdc(graph_rsf, out_rsf)
        elif tech == "arc":
            if not arc_src.exists():
                print(f"  skipping arc: source clone {arc_src} missing "
                      f"(ARC needs source text; clone via `arch fetch {args.system}`)",
                      file=sys.stderr)
                continue
            if not arc_k:
                print("  skipping arc: no --num-clusters and no reference/pipeline clusters "
                      "to derive k from", file=sys.stderr)
                continue
            run_arc(graph_rsf, out_rsf, src_root=arc_src, granularity=g, language=arc_lang,
                    num_clusters=arc_k, num_topics=args.num_topics)
        elif tech in STRUCTURAL_DRIVEN:
            if not arc_k:
                print(f"  skipping {tech}: no --num-clusters and no reference/pipeline clusters "
                      "to derive k from", file=sys.stderr)
                continue
            run_structural(graph_rsf, out_rsf, algo=tech, num_clusters=arc_k, system=args.system)
        elif tech == "dir":
            write_contains(cluster_dir(nodes), out_rsf)
        elif tech == "cc":
            write_contains(cluster_cc(nodes, edges), out_rsf)
        else:
            print(f"  skipping unknown technique '{tech}'", file=sys.stderr)
            continue
        clusters = read_contains(out_rsf)
        fidelity = {
            "system": args.system,
            "technique": tech,
            "granularity": g,
            "graph": graph_rsf.name,
            "entities_clustered": len(clusters),
            "clusters": len(set(clusters.values())),
            "vs_reference": score(out_rsf, reference if reference.exists() else None),
            "vs_pipeline_agreement": score(out_rsf,
                                           pipeline_rsf if pipeline_rsf.exists() else None),
        }
        fid_path = out_dir / "fidelity.json"
        fid_path.write_text(json.dumps(fidelity, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8", newline="")
        vs_ref = fidelity["vs_reference"]
        ref_note = (f"MoJoFM={vs_ref['mojofm']} a2a={vs_ref['a2a']} vs reference"
                    if vs_ref else "no reference yet")
        print(f"[{tech}-{g}] {fidelity['clusters']} clusters / "
              f"{fidelity['entities_clustered']} entities — {ref_note} -> {fid_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
