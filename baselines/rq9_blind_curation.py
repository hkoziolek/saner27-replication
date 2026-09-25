"""Eval §10a — RQ9 blind-curation experiment tooling (worksheets, κ ceiling, effort, scoring).

One CLI for every artifact the §10a protocol produces, so the four researcher roles
(reference raters R1/R2, blind curator C, neutral scorer) never hand-compute a number.
Full step-by-step instructions per role live in ``docs/rq9-blind-curation-runbook.md``.

Subcommands::

    worksheet <system>            emit the rater worksheet CSV — the ENTITY LIST ONLY.
                                  Deliberately reads generated/graph/clusters-<g>.rsf but
                                  strips the cluster column, so handing the worksheet to a
                                  rater cannot leak the pipeline's decomposition (§10a.1:
                                  raters are blind to tool output).
    ingest <worksheet.csv>        validate a filled worksheet, convert to a rater RSF.
                                  ``EXCLUDE`` in the cluster column drops the entity
                                  (counted, never silent).
    ceiling <r1.rsf> <r2.rsf>     R1-vs-R2 agreement: Cohen's κ on the common entity set
                                  (normalized labels) + pairwise MoJoFM both directions
                                  (needs tools/arcade; degrades to null with a warning)
                                  -> ceiling.json. ``--reference`` adds each-rater-vs-
                                  committed-reference rows (the W3a eShop revalidation).
    consensus <r1.csv> <r2.csv>   the reconciliation-call template: one row per entity of
                                  the union, ``notes`` pre-seeded with each rater's own label
                                  (``R1=x | R2=y``), ``cluster`` pre-filled only where the
                                  raters already agree (exact label), blank otherwise. The
                                  facilitator fills it live; ``ingest`` turns it into
                                  consensus.rsf (the reference itself where none is committed).
    effort --rules <yaml>         curator effort metrics (§10a.4): rule lines, group/op
                                  counts, wall-clock from the ANON_EDIT_TRACE JSONL,
                                  %-of-groups matching the `arch propose` draft
                                  -> effort.json.
    score <system>                the §10a.4 four-row table (no-curation floor / best
                                  autonomous SAR / blind curator / human ceiling) with
                                  MoJoFM + a2a + container-edge F1 -> fidelity.json.

Determinism: every JSON artifact is written sorted-keys/LF/no-timestamps (the edit-trace
JSONL is the one deliberate exception — it is the raw wall-clock measurement log and
lives under out/<system>/blind-curation/, never under generated/).
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import io
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from run_arcade import (ARCADE_JAR, a2a, mojofm, read_contains,  # noqa: E402
                        read_depends, write_contains)

REPO = Path(__file__).resolve().parent.parent
SAR_TECHNIQUES = ("acdc", "arc", "wca", "limbo")
EXCLUDE_SENTINEL = "EXCLUDE"

WORKSHEET_HEADER = ["entity", "cluster", "notes"]
WORKSHEET_PREAMBLE = [
    "# RQ9 rater worksheet (eval §10a) — assign every entity a cluster name; use the",
    "# project's own documented component names where they exist. Write EXCLUDE to mark an",
    "# entity out-of-scope (tests, samples, dead code) — record why in the notes column.",
    "# Blinding: do NOT consult the tool's output, any reference.rsf, or another rater.",
]


def _write_json(obj: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:  # LF, sorted, no timestamps
        fh.write(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ worksheet

def cmd_worksheet(args: argparse.Namespace) -> int:
    g = args.granularity
    arch = Path(args.arch_dir) if args.arch_dir \
        else REPO / "out" / args.system / "architecture"
    clusters_rsf = arch / "generated" / "graph" / f"clusters-{g}.rsf"
    if not clusters_rsf.exists():
        print(f"ERROR: {clusters_rsf} missing — run "
              f"`arch export --graph --granularity {g}` first", file=sys.stderr)
        return 2
    entities = sorted(read_contains(clusters_rsf))  # cluster column DROPPED (no leak)
    if args.owner:
        from run_arcade import file_owner_map
        owners = file_owner_map(arch)
        entities = [e for e in entities
                    if fnmatch.fnmatch(owners.get(e, ""), args.owner)]
        if not entities:
            print(f"ERROR: --owner {args.owner!r} matched no entities", file=sys.stderr)
            return 2
    out = Path(args.out) if args.out \
        else REPO / "out" / args.system / "blind-curation" / f"worksheet-{g}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as fh:  # LF
        for line in WORKSHEET_PREAMBLE:
            fh.write(line + "\n")
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(WORKSHEET_HEADER)
        for e in entities:
            w.writerow([e, "", ""])
    print(f"worksheet: {len(entities)} entities -> {out}")
    return 0


# ------------------------------------------------------------------ ingest

def parse_worksheet(path: Path) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """-> (entity->cluster, entity->notes, errors). ``EXCLUDE`` rows are dropped but
    returned in notes as ``EXCLUDE: <note>`` so the exclusion count stays visible."""
    mapping: dict[str, str] = {}
    notes: dict[str, str] = {}
    errors: list[str] = []
    text = path.read_text(encoding="utf-8-sig")
    rows = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    reader = csv.reader(io.StringIO("\n".join(rows)))
    header = next(reader, None)
    if not header or [h.strip().lower() for h in header[:2]] != ["entity", "cluster"]:
        return {}, {}, [f"{path}: first non-comment row must be the "
                        f"'entity,cluster,notes' header"]
    for i, row in enumerate(reader, 2):
        if not row or not row[0].strip():
            continue
        entity = row[0].strip()
        cluster = row[1].strip() if len(row) > 1 else ""
        note = row[2].strip() if len(row) > 2 else ""
        if entity in mapping or entity in notes:
            errors.append(f"row {i}: duplicate entity {entity!r}")
            continue
        if not cluster:
            errors.append(f"row {i}: {entity!r} has no cluster — assign or write EXCLUDE")
            continue
        if cluster.upper() == EXCLUDE_SENTINEL:
            notes[entity] = f"EXCLUDE: {note}" if note else "EXCLUDE"
            continue
        mapping[entity] = cluster
        if note:
            notes[entity] = note
    return mapping, notes, errors


def cmd_ingest(args: argparse.Namespace) -> int:
    ws_path = Path(args.worksheet)
    mapping, notes, errors = parse_worksheet(ws_path)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not mapping:
        print("ERROR: worksheet contains no assigned entities", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_contains(mapping, out)
    excluded = sum(1 for n in notes.values() if n.startswith("EXCLUDE"))
    print(f"ingest: {len(mapping)} entities in {len(set(mapping.values()))} clusters "
          f"({excluded} excluded) -> {out}")
    return 0


# ------------------------------------------------------------------ consensus template


def cmd_consensus(args: argparse.Namespace) -> int:
    names = [n.strip() for n in args.names.split(",")] if args.names \
        else [f"R{i + 1}" for i in range(len(args.worksheets))]
    if len(names) != len(args.worksheets):
        print("ERROR: --names must give one label per worksheet", file=sys.stderr)
        return 2
    labels: list[dict[str, str]] = []
    for path in args.worksheets:
        mapping, notes, errors = parse_worksheet(Path(path))
        if errors:
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)
            return 2
        lab = dict(mapping)
        lab.update({e: EXCLUDE_SENTINEL for e, n in notes.items()
                    if n.startswith(EXCLUDE_SENTINEL)})
        labels.append(lab)
    entities = sorted(set().union(*labels))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    agreed = 0
    buf = io.StringIO()
    sysname = f" {args.system}" if args.system else ""
    buf.write(f"# RQ9{sysname} reconciliation — CONSENSUS worksheet (runbook §5 step 7).\n"
              f"# Filled live by {' + '.join(names)} + facilitator. The pre-consensus "
              f"worksheets are FROZEN\n"
              f"# (kappa + the ceiling row come from those) — never edit them.\n"
              f"# notes column pre-seeded with each rater's own label; replace with the "
              f"rationale.\n"
              f"# cluster pre-filled ONLY where the raters already agree (exact label); "
              f"every blank\n# is a disagreement to settle. EXCLUDE is a valid consensus "
              f"(entity out of scope).\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["entity", "cluster", "notes"])
    for e in entities:
        got = [lab.get(e) for lab in labels]
        cluster = got[0] if all(g == got[0] for g in got) and got[0] else ""
        agreed += bool(cluster)
        seed = " | ".join(f"{n}={g if g is not None else '—'}" for n, g in zip(names, got))
        w.writerow([e, cluster, seed])
    out.write_text(buf.getvalue(), encoding="utf-8", newline="\n")
    print(f"consensus template: {len(entities)} entities, {agreed} pre-filled (agreed), "
          f"{len(entities) - agreed} open -> {out}")
    return 0


# ------------------------------------------------------------------ ceiling (κ + MoJoFM)

_NORM_RE = re.compile(r"[\s\-_./]+")


def normalize_label(label: str) -> str:
    """Casefold + collapse separator runs, so 'Building-Blocks' == 'building blocks'.
    Pre-registered: κ is computed on these normalized labels (§10a.6)."""
    return _NORM_RE.sub(" ", label.strip().casefold()).strip()


def _c2(n: int) -> int:
    """Pairs from n items."""
    return n * (n - 1) // 2


def _hungarian(cost: list[list[float]]) -> list[tuple[int, int]]:
    """Minimum-cost assignment (Kuhn–Munkres, O(n²m)), stdlib-only and deterministic.

    Requires ``len(cost) <= len(cost[0])``; callers transpose if needed. Used to find the
    OPTIMAL correspondence between two raters' cluster labels — a greedy match would make
    the aligned agreement below depend on label order, which a reported number must not.
    """
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    return sorted((p[j] - 1, j - 1) for j in range(1, m + 1) if p[j] != 0)


def cohen_kappa(a: dict[str, str], b: dict[str, str]) -> dict:
    """Agreement between two entity->cluster labelings on their common entity set.

    Reports the **pre-registered** Cohen's κ (eval §5.3/§10a.6) *and* three
    label-alignment-invariant companions, because κ alone is the wrong instrument here
    and reporting it alone misleads.

    κ assumes both raters draw from one shared, fixed category set — it scores agreement by
    *exact label match*. Independent raters partitioning a system invent their own cluster
    names, so two raters who produce the SAME partition score κ=0 if they merely worded it
    differently. That is not hypothetical: on eShop the raters shared no label at all
    (``Basket``/``BasketService``, ``Ordering``/``OrderingService``, …), giving
    ``labels_shared=0`` → p_o=0 → p_e=0 → κ=0 exactly, by arithmetic rather than by
    disagreement. The companions below are invariant to what the clusters are *called*:

    * ``ari`` — adjusted Rand index: chance-corrected agreement on which *pairs* of entities
      share a cluster. The standard partition-comparison measure; 0 = chance, 1 = identical.
    * ``pair_agreement`` — the uncorrected Rand index, i.e. the plain fraction of entity
      pairs the two raters treat consistently. Interpretable without a correction model.
    * ``nmi`` — normalized mutual information (arithmetic-mean normalisation): the share of
      partition information the raters have in common, 0 = independent, 1 = identical.
    * ``kappa_aligned`` — κ recomputed after matching the two label vocabularies optimally
      (Hungarian). Directly commensurable with the pre-registered 0.6 threshold.

    The pre-registered κ stays the flagging statistic (``cmd_ceiling``); these are
    descriptive companions, reported so the flag can be *interpreted*, never to relabel a
    reference after the fact.

    κ (either variant) is None when chance agreement is degenerate (a single label used).
    """
    common = sorted(set(a) & set(b))
    n = len(common)
    if n == 0:
        return {"n_common": 0, "kappa": None, "raw_agreement": None,
                "labels_a": 0, "labels_b": 0, "labels_shared": 0,
                "ari": None, "pair_agreement": None, "nmi": None,
                "kappa_aligned": None, "raw_agreement_aligned": None}
    la = {e: normalize_label(a[e]) for e in common}
    lb = {e: normalize_label(b[e]) for e in common}
    po = sum(1 for e in common if la[e] == lb[e]) / n
    pa: dict[str, float] = {}
    pb: dict[str, float] = {}
    for e in common:
        pa[la[e]] = pa.get(la[e], 0) + 1 / n
        pb[lb[e]] = pb.get(lb[e], 0) + 1 / n
    pe = sum(pa[l] * pb.get(l, 0.0) for l in pa)
    kappa = round((po - pe) / (1 - pe), 3) if (1 - pe) > 1e-9 else None

    # --- alignment-invariant companions ------------------------------------------------
    ua, ub = sorted(set(la.values())), sorted(set(lb.values()))
    cont = [[0] * len(ub) for _ in ua]
    for e in common:
        cont[ua.index(la[e])][ub.index(lb[e])] += 1

    # adjusted Rand + plain Rand, both over entity PAIRS (label names never enter)
    s_ij = sum(_c2(c) for row in cont for c in row)
    s_a = sum(_c2(sum(row)) for row in cont)
    s_b = sum(_c2(sum(col)) for col in zip(*cont))
    n_pairs = _c2(n)
    if n_pairs == 0:
        ari = pair_agreement = None
    else:
        expected = s_a * s_b / n_pairs
        max_index = (s_a + s_b) / 2
        # degenerate (both partitions all-singletons or all-one-cluster): identical -> 1
        ari = (round((s_ij - expected) / (max_index - expected), 3)
               if abs(max_index - expected) > 1e-12
               else (1.0 if la == lb else 0.0))
        # Rand: pairs together-in-both + apart-in-both, over all pairs
        pair_agreement = round((n_pairs + 2 * s_ij - s_a - s_b) / n_pairs, 3)

    # κ after the optimal label correspondence (transpose so rows <= cols)
    flip = len(ua) > len(ub)
    matrix = [list(col) for col in zip(*cont)] if flip else cont
    pairs = _hungarian([[-c for c in row] for row in matrix])
    po_al = sum(matrix[i][j] for i, j in pairs) / n
    pe_al = sum((pa[ua[j if flip else i]]) * (pb[ub[i if flip else j]]) for i, j in pairs)
    kappa_aligned = (round((po_al - pe_al) / (1 - pe_al), 3)
                     if (1 - pe_al) > 1e-9 else None)

    # normalized mutual information (arithmetic-mean normalisation), the third label-free
    # companion (review response 2026-08-30, item 3.2): information the two partitions share,
    # 0 = independent, 1 = identical up to relabelling. Computed from the same contingency.
    import math
    h_a = -sum(r / n * math.log(r / n) for r in (sum(row) for row in cont) if r)
    h_b = -sum(c / n * math.log(c / n) for c in (sum(col) for col in zip(*cont)) if c)
    mi = sum(cont[i][j] / n * math.log((cont[i][j] / n)
                                       / ((sum(cont[i]) / n) * (sum(col[j] for col in cont) / n)))
             for i in range(len(ua)) for j in range(len(ub)) if cont[i][j])
    nmi = (round(mi / ((h_a + h_b) / 2), 3) if (h_a + h_b) > 1e-12
           else (1.0 if la == lb else 0.0))

    return {"n_common": n,
            "kappa": kappa,
            "raw_agreement": round(po, 3),
            "chance_agreement": round(pe, 3),
            "labels_a": len(ua), "labels_b": len(ub),
            "labels_shared": len(set(ua) & set(ub)),
            "ari": ari,
            "pair_agreement": pair_agreement,
            "nmi": nmi,
            "kappa_aligned": kappa_aligned,
            "raw_agreement_aligned": round(po_al, 3)}


def _pairwise_mojo(a: dict[str, str], b: dict[str, str], workdir: Path,
                   tag: str) -> dict:
    """MoJoFM both directions + a2a on the common entity set (jar-optional)."""
    common = set(a) & set(b)
    common -= {e for e in common if any(c.isspace() for c in e)}  # mojo RSF limit
    out: dict = {"n_common": len(common)}
    if not common:
        out.update({"mojofm_ab": None, "mojofm_ba": None, "mojofm_mean": None,
                    "a2a": None})
        return out
    if not ARCADE_JAR.exists():
        print(f"  WARNING: {ARCADE_JAR} missing — MoJoFM/a2a for {tag} recorded as null "
              f"(κ is unaffected)", file=sys.stderr)
        out.update({"mojofm_ab": None, "mojofm_ba": None, "mojofm_mean": None,
                    "a2a": None})
        return out
    fa, fb = workdir / f"{tag}-a.rsf", workdir / f"{tag}-b.rsf"
    write_contains({e: c for e, c in a.items() if e in common}, fa)
    write_contains({e: c for e, c in b.items() if e in common}, fb)
    ab, ba = mojofm(fa, fb), mojofm(fb, fa)
    out["mojofm_ab"] = round(ab, 2) if ab is not None else None
    out["mojofm_ba"] = round(ba, 2) if ba is not None else None
    out["mojofm_mean"] = round((ab + ba) / 2, 2) \
        if ab is not None and ba is not None else None
    aa = a2a(fa, fb)
    out["a2a"] = round(aa, 2) if aa is not None else None
    return out


def cmd_ceiling(args: argparse.Namespace) -> int:
    r1 = read_contains(Path(args.rater1))
    r2 = read_contains(Path(args.rater2))
    names = [n.strip() for n in args.names.split(",")] if args.names else ["R1", "R2"]
    workdir = Path(tempfile.mkdtemp(prefix="rq9-ceiling-"))
    try:
        result = {
            "kind": "rq9-ceiling (eval §10a.4 human-ceiling row)",
            "raters": {"a": names[0], "b": names[1] if len(names) > 1 else "R2"},
            "entities": {"a": len(r1), "b": len(r2),
                         "only_a": len(set(r1) - set(r2)),
                         "only_b": len(set(r2) - set(r1))},
            "clusters": {"a": len(set(r1.values())), "b": len(set(r2.values()))},
            "agreement": {**cohen_kappa(r1, r2),
                          **_pairwise_mojo(r1, r2, workdir, "r1r2")},
        }
        if args.reference:
            ref = read_contains(Path(args.reference))
            result["vs_reference"] = {
                "reference": str(Path(args.reference)),
                "a": {**cohen_kappa(r1, ref), **_pairwise_mojo(r1, ref, workdir, "r1ref")},
                "b": {**cohen_kappa(r2, ref), **_pairwise_mojo(r2, ref, workdir, "r2ref")},
            }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    agr = result["agreement"]
    kappa, kappa_al = agr["kappa"], agr["kappa_aligned"]
    if kappa is not None and kappa < 0.6:
        # The flag stays keyed on the PRE-REGISTERED κ — never on the companions, which
        # exist to interpret the flag, not to lift it (eval §20 residual, decided 2026-08-30).
        result["flag"] = ("kappa < 0.6 — lower-confidence reference (eval §5.3/§10a.6); "
                          "reconcile harder or report the disagreement itself")
        if kappa_al is not None and kappa_al >= 0.6:
            result["flag_interpretation"] = (
                f"VOCABULARY-INDUCED: the raters share {agr['labels_shared']} label(s), so the "
                f"raw κ measures wording, not structure. Aligning the two label "
                f"vocabularies optimally gives κ={kappa_al} (>= 0.6), ARI={agr['ari']}, "
                f"pair agreement={agr['pair_agreement']} — the raters agree substantively. "
                f"The flag rule is applied as written in the plan and explained, not lifted.")
        else:
            result["flag_interpretation"] = (
                f"SUBSTANTIVE: κ={kappa_al} even after optimal label alignment "
                f"(ARI={agr['ari']}, pair agreement={agr['pair_agreement']}) — the raters "
                f"genuinely disagree, most often on granularity rather than on boundaries.")
    _write_json(result, Path(args.out))
    print(f"ceiling: kappa={kappa} (aligned {kappa_al}, ARI {agr['ari']}) "
          f"mojofm_mean={agr['mojofm_mean']} -> {args.out}")
    return 0


# ------------------------------------------------------------------ effort

def _group_signature(g: dict) -> tuple:
    """A comparable signature for propose-vs-final group matching."""
    return (normalize_label(str(g.get("container", ""))),
            frozenset(map(str, g.get("members", []) or [])),
            tuple(sorted(map(str, g.get("members_glob", []) or []))),
            tuple(sorted(map(str, g.get("members_tag", []) or []))))


def _matches_proposal(final: dict, proposals: list[dict]) -> bool:
    """A final group counts as propose-derived when a draft group has the same
    (normalized) container name AND either ≥50% explicit-member overlap or an
    identical glob/tag selector (pre-registered matching rule, §10a.4 effort)."""
    fname, fmembers, fglobs, ftags = _group_signature(final)
    for p in proposals:
        pname, pmembers, pglobs, ptags = _group_signature(p)
        if fname != pname:
            continue
        if fglobs and fglobs == pglobs:
            return True
        if ftags and ftags == ptags:
            return True
        if fmembers and pmembers and \
                len(fmembers & pmembers) * 2 >= len(fmembers | pmembers):
            return True
    return False


def cmd_effort(args: argparse.Namespace) -> int:
    rules_path = Path(args.rules)
    if not rules_path.exists():
        print(f"ERROR: {rules_path} missing", file=sys.stderr)
        return 2
    text = rules_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    groups = data.get("group", []) or []
    excludes = (data.get("exclude", {}) or {}).get("targets", []) or []
    result: dict = {
        "kind": "rq9-effort (eval §10a.4 effort row)",
        "rules": {
            "rule_lines": sum(1 for ln in text.splitlines()
                              if ln.strip() and not ln.lstrip().startswith("#")),
            "groups": len(groups),
            "explicit_members": sum(len(g.get("members", []) or []) for g in groups),
            "member_globs": sum(len(g.get("members_glob", []) or []) for g in groups),
            "member_tags": sum(len(g.get("members_tag", []) or []) for g in groups),
            "excludes": len(excludes),
            "name_overrides": len(data.get("name_overrides", {}) or {}),
        },
    }
    if args.proposal:
        prop_path = Path(args.proposal)
        if prop_path.exists():
            drafts = (yaml.safe_load(prop_path.read_text(encoding="utf-8")) or {}) \
                .get("group", []) or []
            derived = sum(1 for g in groups if _matches_proposal(g, drafts))
            result["propose"] = {
                "draft_groups_offered": len(drafts),
                "final_groups_propose_derived": derived,
                "final_groups_hand_authored": len(groups) - derived,
                "propose_derived_fraction": round(derived / len(groups), 3)
                if groups else None,
            }
        else:
            print(f"  WARNING: {prop_path} missing — propose-derived fraction skipped",
                  file=sys.stderr)
    if args.trace:
        trace_path = Path(args.trace)
        if trace_path.exists():
            sys.path.insert(0, str(REPO / "src"))
            from anon.edit_trace import read_trace
            entries = read_trace(trace_path)
            kinds: dict[str, int] = {}
            for e in entries:
                for k in e.get("kinds", []) or []:
                    kinds[k] = kinds.get(k, 0) + 1
            stamps = sorted(e["ts"] for e in entries if e.get("ts"))
            minutes = None
            if len(stamps) >= 2:
                from datetime import datetime
                first = datetime.fromisoformat(stamps[0])
                last = datetime.fromisoformat(stamps[-1])
                minutes = round((last - first).total_seconds() / 60, 1)
            result["trace"] = {
                "events": len(entries),
                "reviewed_writes": sum(1 for e in entries
                                       if e.get("event") == "reviewed_write"),
                "curate_op_batches": sum(1 for e in entries
                                         if e.get("event") == "curate_ops"),
                "ops_by_kind": dict(sorted(kinds.items())),
                "ops_total": sum(kinds.values()),
                "wall_clock_minutes_trace": minutes,
            }
        else:
            print(f"  WARNING: {trace_path} missing — trace metrics skipped",
                  file=sys.stderr)
    if args.wall_clock_minutes is not None:
        # The curator's self-reported session time (attestation form) — kept separate
        # from the trace-derived span, which misses time spent reading code/docs.
        result["wall_clock_minutes_reported"] = args.wall_clock_minutes
    _write_json(result, Path(args.out))
    print(f"effort: {result['rules']['groups']} groups, "
          f"{result['rules']['rule_lines']} rule lines -> {args.out}")
    return 0


# ------------------------------------------------------------------ score (§10a.4 table)

def align_clusters(cand: dict[str, str], ref: dict[str, str]) -> dict[str, str]:
    """Greedy max-overlap one-to-one matching of candidate cluster names onto reference
    cluster names (deterministic: overlap desc, then name asc). Unmatched candidate
    clusters keep a distinct 'unmatched::' name so their lifted edges can never
    accidentally count as reference edges. Pre-registered for the edge-F1 metric."""
    overlap: dict[tuple[str, str], int] = {}
    for e, c in cand.items():
        r = ref.get(e)
        if r is not None:
            overlap[(c, r)] = overlap.get((c, r), 0) + 1
    mapping: dict[str, str] = {}
    used_ref: set[str] = set()
    for (c, r), _n in sorted(overlap.items(), key=lambda kv: (-kv[1], kv[0])):
        if c not in mapping and r not in used_ref:
            mapping[c] = r
            used_ref.add(r)
    return {c: mapping.get(c, f"unmatched::{c}") for c in set(cand.values())}


def lift_edges(edges: list[tuple[str, str]],
               partition: dict[str, str]) -> set[tuple[str, str]]:
    """Entity edges -> directed container edges under *partition* (self-loops dropped)."""
    lifted: set[tuple[str, str]] = set()
    for a, b in edges:
        ca, cb = partition.get(a), partition.get(b)
        if ca is not None and cb is not None and ca != cb:
            lifted.add((ca, cb))
    return lifted


def container_edge_f1(cand: dict[str, str], ref: dict[str, str],
                      edges: list[tuple[str, str]]) -> dict:
    """§10a.4 container-edge F1: restrict to the common entity set, align candidate
    cluster names to reference names by max overlap, lift the SAME entity edge set
    under both partitions, compare the directed container-edge sets."""
    common = set(cand) & set(ref)
    cand_c = {e: c for e, c in cand.items() if e in common}
    ref_c = {e: c for e, c in ref.items() if e in common}
    renames = align_clusters(cand_c, ref_c)
    cand_aligned = {e: renames[c] for e, c in cand_c.items()}
    edges_c = [(a, b) for a, b in edges if a in common and b in common]
    got, want = lift_edges(edges_c, cand_aligned), lift_edges(edges_c, ref_c)
    tp = len(got & want)
    precision = tp / len(got) if got else None
    recall = tp / len(want) if want else None
    f1 = round(2 * precision * recall / (precision + recall), 3) \
        if precision and recall and (precision + recall) else \
        (0.0 if got or want else None)
    return {"n_common_entities": len(common), "entity_edges": len(edges_c),
            "candidate_edges": len(got), "reference_edges": len(want),
            "true_positive_edges": tp,
            "precision": round(precision, 3) if precision is not None else None,
            "recall": round(recall, 3) if recall is not None else None,
            "f1": f1,
            "clusters_aligned": sum(1 for v in renames.values()
                                    if not v.startswith("unmatched::")),
            "clusters_unmatched": sum(1 for v in renames.values()
                                      if v.startswith("unmatched::"))}


def _score_vs_reference(cand: dict[str, str], ref: dict[str, str],
                        workdir: Path, tag: str) -> dict:
    scored = _pairwise_mojo(cand, ref, workdir, tag)
    # Direction convention: MoJoFM(recovered, reference) — the _ab direction.
    return {"mojofm": scored["mojofm_ab"], "a2a": scored["a2a"],
            "n_common": scored["n_common"],
            "clusters": len(set(cand.values()))}


def cmd_score(args: argparse.Namespace) -> int:
    ref = read_contains(Path(args.reference))
    curator = read_contains(Path(args.curator))
    workdir = Path(tempfile.mkdtemp(prefix="rq9-score-"))
    try:
        rows: dict = {}
        # --common-with (review 2026-09-13, response plan §14 F1): score every row on the SAME
        # entity population as the all-common comparison file. A reference entity that no
        # edge-only technique ever sees (a project with no first-party dependency edge is absent
        # from the exported graph) is dropped from the reference before scoring; without this the
        # curator row was scored on the full reference while the floor / best-SAR rows came from
        # the projected comparison, and the paper printed two numbers for one partition (Squidex
        # 75.0/96.4 vs 70.0/95.8). The full-reference score is kept as a labelled sensitivity
        # number, never as the headline.
        projection = "full-reference"
        sensitivity: dict = {}
        dropped: list[str] = []
        if args.common_with:
            common = set(read_contains(Path(args.common_with)))
            dropped = sorted(e for e in ref if e not in common)
            if dropped:
                sensitivity["blind_curator_full_reference"] = {
                    **_score_vs_reference(curator, ref, workdir, "curator-full"),
                    "kind": "expression-full-reference",
                    "note": "the same partition scored on every reference entity, including "
                            "those outside the all-common projection"}
                ref = {e: c for e, c in ref.items() if e in common}
            projection = "all-common"
        # Row 1 — no-curation floor (detection, context).
        if args.floor:
            rows["floor"] = {**_score_vs_reference(read_contains(Path(args.floor)),
                                                   ref, workdir, "floor"),
                             "kind": "detection", "source": str(args.floor)}
        # Rows 1(fallback) + 2 — floor technique + best autonomous SAR from the
        # all-common comparison file (make_comparison.py).
        if args.comparison:
            comp = json.loads(Path(args.comparison).read_text(encoding="utf-8"))
            results = comp.get("results", {})
            if "floor" not in rows and args.floor_technique in results:
                rows["floor"] = {**results[args.floor_technique], "kind": "detection",
                                 "source": f"comparison:{args.floor_technique} "
                                           f"(= the no-curation floor at target "
                                           f"granularity, eval §13)"}
            sar = {t: r for t, r in results.items()
                   if t in SAR_TECHNIQUES and r.get("mojofm") is not None}
            if sar:
                best = max(sar, key=lambda t: sar[t]["mojofm"])
                rows["best_sar"] = {**sar[best], "kind": "detection",
                                    "technique": best,
                                    "candidates": sorted(sar)}
        # Row 3 — the blind curator (expression, THE number).
        rows["blind_curator"] = {**_score_vs_reference(curator, ref, workdir, "curator"),
                                 "kind": "expression", "source": str(args.curator)}
        if args.graph:
            edges = read_depends(Path(args.graph))
            rows["blind_curator"]["container_edge_f1"] = \
                container_edge_f1(curator, ref, edges)
        # Row 4 — the human ceiling (R1-vs-R2).
        if args.ceiling:
            ceiling = json.loads(Path(args.ceiling).read_text(encoding="utf-8"))
            agr = ceiling.get("agreement", {})
            rows["human_ceiling"] = {"kind": "ceiling",
                                     "mojofm": agr.get("mojofm_mean"),
                                     "a2a": agr.get("a2a"),
                                     "kappa": agr.get("kappa"),
                                     "n_common": agr.get("n_common"),
                                     "source": str(args.ceiling)}
        result = {"kind": "rq9-fidelity (eval §10a.4 four-row table)",
                  "system": args.system, "granularity": args.granularity,
                  "reference": str(args.reference), "rows": rows,
                  "projection": projection,
                  "labelling_rule": "detection and expression numbers never cross "
                                    "(eval §10a.7)"}
        if args.common_with:
            result["common_with"] = str(args.common_with)
            result["reference_entities_dropped"] = dropped
        if sensitivity:
            result["sensitivity"] = sensitivity
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    _write_json(result, Path(args.out))
    order = ["floor", "best_sar", "blind_curator", "human_ceiling"]
    print(f"\nRQ9 four-row table — {args.system} ({args.granularity})")
    print(f"{'row':<16} {'kind':<11} {'MoJoFM':>8} {'a2a':>8}  extra")
    for name in order:
        r = rows.get(name)
        if not r:
            print(f"{name:<16} {'—':<11} {'—':>8} {'—':>8}  (not provided)")
            continue
        extra = ""
        if name == "best_sar":
            extra = f"technique={r.get('technique')}"
        elif name == "blind_curator" and "container_edge_f1" in r:
            extra = f"edge-F1={r['container_edge_f1']['f1']}"
        elif name == "human_ceiling":
            extra = f"kappa={r.get('kappa')}"
        mj = r.get("mojofm")
        aa = r.get("a2a")
        print(f"{name:<16} {r['kind']:<11} "
              f"{mj if mj is not None else '—':>8} "
              f"{aa if aa is not None else '—':>8}  {extra}")
    print(f"\n-> {args.out}")
    return 0


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("worksheet", help="emit the rater worksheet CSV (entity list only)")
    w.add_argument("system")
    w.add_argument("--granularity", default="target", choices=["file", "target"])
    w.add_argument("--arch-dir", help="override (default out/<system>/architecture)")
    w.add_argument("--owner", help="file granularity only: keep files whose owning "
                                   "build target matches this glob (two-stage mapping)")
    w.add_argument("--out", help="default out/<system>/blind-curation/worksheet-<g>.csv")
    w.set_defaults(fn=cmd_worksheet)

    i = sub.add_parser("ingest", help="validate a filled worksheet -> rater RSF")
    i.add_argument("worksheet")
    i.add_argument("--out", required=True, help="rater RSF output path")
    i.set_defaults(fn=cmd_ingest)

    c = sub.add_parser("ceiling", help="R1-vs-R2 kappa + pairwise MoJoFM -> ceiling.json")
    c.add_argument("rater1")
    c.add_argument("rater2")
    c.add_argument("--names", help="comma pair of rater labels (default R1,R2)")
    c.add_argument("--reference", help="also score each rater vs this committed "
                                       "reference (the W3a revalidation)")
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_ceiling)

    r = sub.add_parser("consensus", help="reconciliation template from >=2 rater worksheets "
                                         "-> worksheet-consensus.csv")
    r.add_argument("worksheets", nargs="+", help="the frozen rater worksheet CSVs")
    r.add_argument("--names", help="comma list of rater labels, one per worksheet")
    r.add_argument("--system", help="system name for the header comment")
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_consensus)

    e = sub.add_parser("effort", help="curator effort metrics -> effort.json")
    e.add_argument("--rules", required=True, help="the curator's frozen mapping-rules.yaml")
    e.add_argument("--trace", help="the ANON_EDIT_TRACE JSONL")
    e.add_argument("--proposal", help="the arch-propose draft "
                                      "(generated/proposed-mapping-rules.yaml)")
    e.add_argument("--wall-clock-minutes", type=float,
                   help="curator-reported total session minutes (attestation form)")
    e.add_argument("--out", required=True)
    e.set_defaults(fn=cmd_effort)

    s = sub.add_parser("score", help="the §10a.4 four-row table -> fidelity.json")
    s.add_argument("system")
    s.add_argument("--granularity", default="target", choices=["file", "target"])
    s.add_argument("--curator", required=True,
                   help="clusters RSF exported from the curator's frozen rules")
    s.add_argument("--reference", required=True)
    s.add_argument("--graph", help="graph-<g>.rsf for the container-edge F1")
    s.add_argument("--comparison", help="comparison-<g>.json for the floor/best-SAR rows")
    s.add_argument("--floor", help="explicit floor clusters RSF (overrides "
                                   "--floor-technique)")
    s.add_argument("--floor-technique", default="dir",
                   help="comparison technique standing in for the no-curation floor "
                        "(default dir — identical at C# target granularity, eval §13)")
    s.add_argument("--ceiling", help="ceiling.json from the ceiling subcommand")
    s.add_argument("--common-with",
                   help="clusters RSF whose entity set is the all-common projection (e.g. "
                        "out/<sys>/baseline/dir-<g>/clusters.rsf): reference entities outside "
                        "it are dropped before scoring so the curator row shares the population "
                        "of the --comparison rows; the full-reference score is kept under "
                        "`sensitivity` (response plan §14 F1)")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_score)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
