"""Eval §7-A2 — the curation-heuristic ablation.

Tests how much each *grouping heuristic* contributes to fidelity, and — the load-bearing
question after the curation-authorship confound (§13,
``plans/notes/2026-06-14-llm-in-curation-and-no-curation-floor.md``) — separates the
deterministic build-graph contribution (the ``none`` rung = the no-curation floor) from
the (LLM- or human-) authored hand curation (the ``hand_curation`` rung).

For each system the SAME entities are clustered under each heuristic and scored against the
reference with ARCADE's MoJoFM / a2a, all projected to one common entity set so the rungs are
internally comparable::

    python baselines/a2_ablation.py                 # all configured systems
    python baselines/a2_ablation.py itk eshop       # a subset

Heuristics (computed where defined for the system's granularity):
  * ``none``      — build unit, ZERO grouping. C++ file -> owning build target (sidecar);
                    C# project -> itself (singleton). The no-curation floor.
  * ``dir_leaf``  — entity -> its immediate parent directory.
  * ``dir_top``   — entity -> its top-2 path segments (the "architectural directory":
                    Modules/Core, absl/strings, modules/core, src/Libraries, ...).
  * ``name``      — entity -> a token of its name: C++ falls back to the owning target;
                    C# strips the longest common leading dotted-prefix of all project
                    basenames and clusters by the first remaining token (so the heuristic
                    is uniform and reproducible, not hand-tuned per system).
  * ``name_segment`` — (C# only) ``name`` made convention-aware: peel non-discriminating leading
                    namespace markers (a token leading >50% of projects, e.g. ``Plugin``) first,
                    then cluster by the first surviving token. Tests how much of the C# gap is a
                    deterministic naming convention ``name`` misses (the §13 follow-up).
  * ``sdk_role``  — (C# only) cluster by the csproj SDK / target-framework role (Web service /
                    worker / MAUI client / library); read from the pristine ``fixtures/<sys>/``
                    clone. Tests the build-graph-carried deployable-role signal curation ignores.
  * ``edge_cc``   — connected components of the undirected dependency graph (the pure
                    dependency heuristic; what ACDC/cc approximate).
  * ``propose_all`` — the deterministic auto-``propose`` draft ACCEPTED VERBATIM: run
                    :func:`anon.propose.build_proposal` over the system's extracted facts
                    and assign each entity to the container the proposer gives its owning build
                    target (ungrouped targets stay singletons). This is what an architect gets
                    from ``arch propose`` + accept-all WITHOUT authoring an ``hand_curation``
                    overlay — the AUTO-CURATION FLOOR, between the single-signal rungs and the
                    hand curation. Uses the real proposer (solution-folder > directory >
                    namespace > dependency-cluster precedence), not a re-implementation.
  * ``det_curation`` — a curation that ADOPTS a deterministic source signal (a tag an extractor
                    surfaced, or a name convention), exported under its own ``out/<id>`` arch dir
                    (see ``DET_CURATION_ARCH``): orchardcore module ``Manifest.cs`` ``Category``
                    (``overlays/orchardcore-cat``), eshop name globs + ``sdk-role:library`` residual
                    (``overlays/eshop-role``), nopcommerce layer + ``Nop.Plugin.<Type>`` name globs
                    (``overlays/nopcommerce-name``). Omitted until that overlay's ``arch run`` has
                    exported its clusters. CIRCULARITY: each reference is a transcription of the
                    signal the overlay adopts — a reconstruction ceiling, not an independent score;
                    read beside ``hand_curation``.
  * ``hand_curation`` — the hand-authored ``mapping-rules.yaml`` curation (the pipeline's answer).

Output: a per-system table to stdout + ``out/<sys>/baseline/a2-ablation.json``.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

from run_arcade import (  # noqa: E402
    a2a, file_owner_map, mojofm, project_reference_to_files,
    read_contains, read_depends, write_contains,
)

from anon import propose  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# system -> (language, granularity). C# references for nopcommerce are LOCAL-ONLY (license).
SYSTEMS = {
    "itk": ("cpp", "file"),
    "abseil": ("cpp", "file"),
    "opencv": ("cpp", "file"),
    "eshop": ("cs", "target"),
    "orchardcore": ("cs", "target"),
    "nopcommerce": ("cs", "target"),
    "bash": ("cpp", "file"),      # autotools L0 via extract_autotools (former limitation case)
    "libxml2": ("cpp", "file"),   # autotools/automake; monolithic-library contrast case
    "squidex": ("cs", "target"),  # admitted 2026-08-30 (companion plan §5 step 7)
}
# opencv's representative curation is the module-granularity overlay (overlays/opencv-mod).
# squidex's is the reference-blind RQ9 curation (overlays/squidex-blind-R-C, run into
# out/squidex-rq9/) — the only curated Squidex model, non-circular by construction; the
# onboarding overlay under out/squidex/ is a bootstrap floor, never the hand_curation rung.
HAND_CURATION_ARCH = {"opencv": "opencv-mod", "squidex": "squidex-rq9"}

# The `det_curation` rung: a curation that ADOPTS a deterministic source signal (a tag an extractor
# surfaced, or a name convention) — the deterministic counterpart of the (LLM-authored) `hand_curation`.
# Maps system -> the out/<id> arch dir whose `arch run` exported its clusters:
#   orchardcore -> orchardcore-cat  : module `Manifest.cs` Feature `Category` via members_tag
#                                     (extract_orchardcore_manifest)
#   eshop       -> eshop-role        : name globs + `sdk-role:library` residual = BuildingBlocks via
#                                     members_tag (extract_csproj_sdk)
#   nopcommerce -> nopcommerce-name  : pure members_glob over the layer + Nop.Plugin.<Type> convention
# NOTE (circularity, §13 follow-up): each reference is itself a transcription of the signal the overlay
# adopts (orchardcore Category, nopcommerce names; eshop names + the one library-role judgment). So
# `det_curation` is a RECONSTRUCTION ceiling — what a curator reaches by adopting the deterministic
# signal — NOT an independent fidelity result. Read it beside `hand_curation`, never instead of it; the
# honest non-circular number per system stays `hand_curation`.
DET_CURATION_ARCH = {"orchardcore": "orchardcore-cat", "eshop": "eshop-role",
                     "nopcommerce": "nopcommerce-name"}


def _repo_rel(entity: str) -> str:
    """Strip a `lang:kind:` id prefix to the repo-relative path (C# ids); C++ are already rel."""
    return entity.split(":", 2)[2] if entity.startswith(("csharp:", "cpp:")) and entity.count(":") >= 2 else entity


# ------------------------------------------------------------------ heuristics

def cluster_dir_leaf(nodes: set[str]) -> dict[str, str]:
    out = {}
    for n in nodes:
        p = _repo_rel(n)
        out[n] = p.rsplit("/", 1)[0] if "/" in p else "."
    return out


def cluster_dir_top(nodes: set[str], depth: int = 2) -> dict[str, str]:
    out = {}
    for n in nodes:
        parts = _repo_rel(n).split("/")
        out[n] = "/".join(parts[:depth]) if len(parts) > depth else "/".join(parts[:-1]) or "."
    return out


def cluster_name_cs(nodes: set[str]) -> dict[str, str]:
    """C# naming heuristic: strip the longest common leading dotted-token prefix of all
    project basenames, cluster by the first remaining token. Uniform, reproducible."""
    base = {n: _repo_rel(n).rsplit("/", 1)[-1].rsplit(".csproj", 1)[0] for n in nodes}
    tok = {n: b.split(".") for n, b in base.items()}
    # longest common leading token prefix across ALL projects
    common = 0
    if tok:
        seqs = list(tok.values())
        while all(len(s) > common + 1 and s[common] == seqs[0][common] for s in seqs):
            common += 1
    out = {}
    for n, toks in tok.items():
        rest = toks[common:] or toks
        out[n] = rest[0]
    return out


def cluster_name_segment_cs(nodes: set[str], marker_frac: float = 0.5) -> dict[str, str]:
    """Convention-aware C# naming (the §13 follow-up: how much of the C# curation gap is a
    *deterministic* naming convention the plain ``name`` rung misses?). Like ``name``, but first
    peel leading dotted tokens that are non-discriminating STRUCTURAL MARKERS — a token that leads
    more than ``marker_frac`` of all projects (e.g. ``Plugin`` in ``Nop.Plugin.<Category>.*``,
    which 68% of nopcommerce projects share) — then cluster by the first surviving token. The
    marker is auto-detected per system (no hand tuning); the 0.5 cutoff peels the namespace marker
    (``Plugin``) but keeps a genuine category (``Misc``, 38%) intact, recovering the category
    families that ``name`` collapses to a single ``Plugin`` bucket."""
    base = {n: _repo_rel(n).rsplit("/", 1)[-1].rsplit(".csproj", 1)[0] for n in nodes}
    tok = {n: b.split(".") for n, b in base.items()}
    # strip the longest common leading token prefix (same start as `name`)
    common = 0
    if tok:
        seqs = list(tok.values())
        while all(len(s) > common + 1 and s[common] == seqs[0][common] for s in seqs):
            common += 1
    work = {n: (t[common:] or t)[:] for n, t in tok.items()}
    n_total = len(work) or 1
    # iteratively peel a leading token while it is a global marker (recomputed each level)
    for _ in range(4):
        lead = Counter(t[0] for t in work.values())
        markers = {tk for tk, c in lead.items() if c > marker_frac * n_total}
        if not markers:
            break
        changed = False
        for n, t in work.items():
            if len(t) > 1 and t[0] in markers:
                work[n] = t[1:]
                changed = True
        if not changed:
            break
    return {n: t[0] for n, t in work.items()}


def cluster_sdk_role_cs(nodes: set[str], fixtures_root: Path) -> dict[str, str]:
    """Project-role heuristic: cluster by the csproj's SDK / target-framework — the deployable
    role the build graph carries but curation doesn't group on. ``Microsoft.NET.Sdk.Web`` ->
    service, ``.Worker`` -> worker, a MAUI/mobile target-framework -> client app, plain
    ``Microsoft.NET.Sdk`` -> library. Reads the pristine clone under ``fixtures/<system>/`` (the
    SDK attribute is not in the determinism-hashed facts); returns ``{}`` if no csproj resolves so
    the rung degrades out rather than reporting a fake all-``unknown`` clustering."""
    out, resolved = {}, 0
    for n in nodes:
        p = fixtures_root / _repo_rel(n)
        role = "unknown"
        if p.exists():
            resolved += 1
            txt = p.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'<Project\s+Sdk="([^"]+)"', txt)
            sdk = (m.group(1) if m else "").lower()
            t = re.search(r"<TargetFrameworks?>([^<]+)</", txt)
            tfm = t.group(1) if t else ""
            if sdk.endswith(".web"):
                role = "service-web"
            elif sdk.endswith(".worker"):
                role = "service-worker"
            elif "maui" in sdk or any(x in tfm for x in ("android", "ios", "maccatalyst")):
                role = "client-app"
            elif "razor" in sdk:
                role = "ui-library"
            elif sdk == "microsoft.net.sdk":
                role = "library"
            elif sdk:
                role = sdk
        out[n] = role
    return out if resolved else {}


def cluster_cc(nodes: set[str], edges: list[tuple[str, str]]) -> dict[str, str]:
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in edges:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    comp: dict[str, list[str]] = {}
    for n in nodes:
        comp.setdefault(find(n), []).append(n)
    return {n: min(ms) for ms in comp.values() for n in ms}


def cluster_propose_accept_all(nodes: set[str], lang: str, facts: dict,
                               sidecar: dict[str, str]) -> dict[str, str]:
    """The auto-`propose` draft accepted verbatim — the auto-curation floor.

    Runs the REAL :func:`anon.propose.build_proposal` over the system's extracted
    facts (its four-signal precedence: solution-folder > directory > namespace >
    dependency-cluster), then maps every entity to the container the proposer assigns its
    owning build target. Resolution per entity:
      * C# (target granularity) — the entity id IS the build-target id.
      * C++ (file granularity)  — the owning target comes from the file-deps sidecar.
    A target the proposer leaves ungrouped stays a singleton (its own id); an entity whose
    target is unknown is its own singleton. Labels are kind-prefixed (``grp:``/``tgt:``/
    ``ent:``) so a container name can never collide with a fallback target/entity id. Covers
    EVERY node, so adding this rung does not shrink the common entity set (the other rungs'
    numbers are unchanged)."""
    proposal = propose.build_proposal(facts)
    target2container: dict[str, str] = {}
    for g in proposal.get("group", []):
        for m in g.get("members", []):
            target2container[m] = g["container"]
    out: dict[str, str] = {}
    for n in nodes:
        target = n if lang == "cs" else sidecar.get(n)
        if target is not None and target in target2container:
            out[n] = "grp:" + target2container[target]
        elif target is not None:
            out[n] = "tgt:" + target
        else:
            out[n] = "ent:" + n
    return out


# ------------------------------------------------------------------ driver

def heuristics_for(system: str, lang: str, gran: str):
    arch = REPO / "out" / system / "architecture"
    graph = arch / "generated" / "graph" / f"graph-{gran}.rsf"
    edges = read_depends(graph) if graph.exists() else []
    nodes = {n for e in edges for n in e}
    rungs: dict[str, dict[str, str]] = {}
    sidecar: dict[str, str] = {}  # C++ file -> FULL owning target id (for propose_all)

    # none / build unit
    if lang == "cpp":
        sc = arch / ".cache" / "fragments" / "file-deps-doxygen-xml.json"
        if sc.exists():
            files = json.loads(sc.read_text(encoding="utf-8"))["files"]
            sidecar = {f: t for f, t in files.items() if t}
            bu = {f: t.split(":")[-1] for f, t in files.items() if t}
            if bu:
                rungs["none"] = bu
                nodes |= set(bu)
    else:  # C# project = singleton
        rungs["none"] = {n: n for n in nodes}

    if nodes:
        rungs["dir_leaf"] = cluster_dir_leaf(nodes)
        rungs["dir_top"] = cluster_dir_top(nodes)
        rungs["edge_cc"] = cluster_cc(nodes, edges)
        if lang == "cs":
            rungs["name"] = cluster_name_cs(nodes)
            rungs["name_segment"] = cluster_name_segment_cs(nodes)
            sdk = cluster_sdk_role_cs(nodes, REPO / "fixtures" / system)
            if sdk:
                rungs["sdk_role"] = sdk

    # propose_all = the deterministic auto-propose draft accepted verbatim (auto-curation floor).
    # Uses the system's OWN extracted facts (cold-start: no hand rules), so even for opencv —
    # whose hand_curation rung reads the opencv-mod overlay — the proposer runs over out/opencv.
    facts_path = arch / "generated" / "extracted-facts.json"
    if nodes and facts_path.exists():
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            facts = None
        if isinstance(facts, dict):
            rungs["propose_all"] = cluster_propose_accept_all(nodes, lang, facts, sidecar)

    # det_curation = a 2nd curation adopting a deterministic source signal (tag or name convention),
    # exported under its own out/<id> arch dir. Absent (rung omitted) until that overlay is run.
    if system in DET_CURATION_ARCH:
        mc = (REPO / "out" / DET_CURATION_ARCH[system] / "architecture"
              / "generated" / "graph" / f"clusters-{gran}.rsf")
        if mc.exists():
            rungs["det_curation"] = read_contains(mc)

    # hand_curation = the hand-authored curation
    comb_arch = REPO / "out" / HAND_CURATION_ARCH.get(system, system) / "architecture"
    comb = comb_arch / "generated" / "graph" / f"clusters-{gran}.rsf"
    if comb.exists():
        rungs["hand_curation"] = read_contains(comb)
    return rungs


def run_system(system: str) -> dict | None:
    lang, gran = SYSTEMS[system]
    ref_path = REPO / "references" / system / "reference.rsf"
    if not ref_path.exists():
        print(f"  {system}: no reference (local-only or missing) — skipping", file=sys.stderr)
        return None
    ref = read_contains(ref_path)
    rungs = heuristics_for(system, lang, gran)
    if not rungs:
        print(f"  {system}: no clusterings computed", file=sys.stderr)
        return None
    # Author-effort shortcut (kit §"Building the GT-diag reference.rsf"): a C++ system may ship a
    # TARGET-granularity reference and rely on the file->owning-target sidecar to project it down
    # to files — the same path run_arcade.py takes. If the reference shares nothing with the
    # file-level rung entities, project it first, else the common set is empty and the system drops.
    if gran == "file":
        file_nodes = set().union(*(set(c) for c in rungs.values()))
        if ref and not (set(ref) & file_nodes):
            projected = project_reference_to_files(
                ref, file_owner_map(REPO / "out" / system / "architecture"))
            if projected:
                ref = projected
            else:
                print(f"  {system}: target-granularity reference but no file->owner map "
                      f"(run `arch extract` so the file-deps sidecar exists)", file=sys.stderr)
    # common entity set across the reference AND every rung -> internally comparable
    shared = set(ref)
    for c in rungs.values():
        shared &= set(c)
    if not shared:
        print(f"  {system}: empty shared set", file=sys.stderr)
        return None
    # Persist every rung's FULL (unprojected) partition so the fair all-common comparison
    # (make_comparison.py --rungs) can score the no-curation floor and the auto-propose draft
    # on the identical entity set as the SAR baselines (review response 2026-08-30, items
    # 1.1/5.6): the a2-ablation.json numbers below stay on the rungs' own common-a2 set.
    rung_dir = REPO / "out" / system / "baseline" / "a2-rungs"
    for r, members in rungs.items():
        write_contains(members, rung_dir / f"{r}.rsf")
    tmp = REPO / "out" / system / "_a2"
    tmp.mkdir(parents=True, exist_ok=True)
    write_contains({m: c for m, c in ref.items() if m in shared}, tmp / "ref.rsf")
    order = [r for r in ("none", "dir_leaf", "dir_top", "name", "name_segment", "sdk_role",
                         "edge_cc", "propose_all", "det_curation", "hand_curation") if r in rungs]
    res = {}
    for r in order:
        write_contains({m: cl for m, cl in rungs[r].items() if m in shared}, tmp / f"{r}.rsf")
        res[r] = {"mojofm": mojofm(tmp / f"{r}.rsf", tmp / "ref.rsf"),
                  "a2a": round(a2a(tmp / f"{r}.rsf", tmp / "ref.rsf"), 2),
                  "clusters": len(set(cl for m, cl in rungs[r].items() if m in shared))}
    shutil.rmtree(tmp)
    out = {"system": system, "language": lang, "granularity": gran,
           "reference_clusters": len(set(ref.values())), "shared_entities": len(shared),
           "rungs": res}
    (REPO / "out" / system / "baseline" / "a2-ablation.json").write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("systems", nargs="*", default=list(SYSTEMS),
                    help="systems to ablate (default: all configured)")
    args = ap.parse_args(argv)
    cols = ["none", "dir_leaf", "dir_top", "name", "name_segment", "sdk_role", "edge_cc",
            "propose_all", "det_curation", "hand_curation"]
    print(f"{'system':13} " + " ".join(f"{c:>12}" for c in cols) + f"  {'refΔ(hand-none)':>16}")
    for s in (args.systems or list(SYSTEMS)):
        out = run_system(s)
        if not out:
            continue
        r = out["rungs"]
        cells = []
        for c in cols:
            cells.append(f"{r[c]['mojofm']:>5}/{r[c]['a2a']:>5}" if c in r else f"{'—':>11}")
        lift = (f"{r['hand_curation']['mojofm'] - r['none']['mojofm']:+.1f}"
                if "hand_curation" in r and "none" in r else "—")
        print(f"{s:13} " + " ".join(f"{x:>12}" for x in cells) + f"  {lift:>16}")
    print("\ncells = MoJoFM/a2a over the per-system common entity set; refΔ = hand_curation − none (MoJoFM).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
