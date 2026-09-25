#!/usr/bin/env python3
"""Aggregate the raw out/** experiment tree into one tidy long-format results.csv.

This is the single source of truth every downstream table/figure/macro reads (eval plan
§15.1). It walks the committed baseline artifacts and emits one row per
(system, technique/rung, metric) so the table/stats stages never re-parse the raw tree.

STDLIB ONLY (no pandas) — deterministic, CI-runnable, matching the repo's jsonio ethos; the
plan's "deps: pandas" note is deliberately not taken (a tidy CSV needs none). Output rows are
sorted, so two runs over the same out/** are byte-identical (the §15 reproducibility rule).

Sources consumed (eval §14.2), each degrading gracefully when absent:
    out/<sys>/baseline/comparison-<gran>.json      # RQ1/RQ2, the FAIR all-common projection
                                                   #   (pipeline + acdc/cc/dir on one entity set)
    out/<sys>/baseline/<tech>-<gran>/fidelity.json # RQ2, per-technique (incl. arc/wca/limbo);
                                                   #   pairwise projection (tech ∩ reference)
    out/<sys>/baseline/llm-recovery-<gran>.json    # RQ2 §6.2 LLM-recovery baseline: MoJoFM/a2a
                                                   #   MEAN + stdev over reruns (llm-rerun-mean)
    out/<sys>/baseline/a2-ablation.json            # RQ3-A2 curation rungs (none..hand_curation)
    out/<sys>/baseline/rq3-ablation.json           # RQ3 A1/A3/A4/A5 edge ablations
    out/<sys>/baseline/stability-churn.json        # RQ3-A8 churn (+ RQ5 stability contrast)
    out/<sys>/mutation/mutation-summary.json       # RQ6 §8.1 seeded-mutation detection
    out/<sys>/mutation/degradation.json            # RQ6 §8.2 stopping-rule degradation check
    out/<sys>/perf/rq7-perf.json                   # RQ7 §10 cold/warm timing + peak RSS
    references/<sys>/provenance.json               # reference grade + cluster/entity counts
    references/<sys>/blind-curation/               # RQ9 §10a blind-curation experiment — the
        ceiling.json                               #   TRACKED home (2026-08-30): these are
        rater-<R>.rsf, consensus.rsf               #   human-produced, irreplaceable artifacts,
        effort[-<C>].json                          #   unlike everything else under out/**;
        fidelity[-<C>][-<variant>].json            #   out/<sys>/blind-curation/ is consulted
                                                   #   only as a fallback mirror

Tidy schema (one metric per row)::

    system, language, granularity, ref_grade, ref_clusters, ref_entities,
    axis, technique, metric, value, projection, source

  * axis        — "fidelity" (RQ1/RQ2) | "A1".."A5" | "A8" | "RQ6-mut" | "RQ6-deg" | "RQ7"
                  | "RQ9" (blind curation, eval §10a)
  * technique   — pipeline | floor | propose | acdc | arc | wca | limbo | dir | cc  (axis=fidelity;
                  floor/propose = the A2 `none`/`propose_all` rungs re-scored on the SAME
                  all-common set — review response 2026-08-30, items 1.1/5.6)
                  a curation rung (none, dir_top, hand_curation, …)      (axis=A2)
                  a config label (L0, t8, off, …)                       (axis=A1/A3/A4/A5)
                  "<tech>@<fraction>"                                   (axis=A8)
                  ceiling | rater:<R> | consensus | curator[:<C>] | floor | sar:<tech>
                                                                        (axis=RQ9)
  * metric      — mojofm | a2a | clusters | edge_f1 | edges | edges_saved | churn_mojofm
  * projection  — all-common | pairwise | common-a2 | edge-vs-full | perturb
                  RQ9: rater-pair | rater-vs-reference | partition | vs-reference | effort |
                  detection | expression | ceiling (the §10a.7 detection-vs-expression label,
                  carried verbatim from fidelity.json; "-vs-<ref>" suffixed when the scored
                  reference is not the committed reference.rsf, e.g. the consensus sensitivity)
  * tier        — "oss" (this repo's replicable corpus) | "P" (anonymized Tier-P rows
                  ingested via --anon; eval §5.5/§14.3 — corroborating, never load-bearing)

Tier-P ingestion: ``--anon <results-anon.csv>`` (repeatable) merges the [anonymized]-side exports
produced by analysis/anonymize_results.py. Rows are lightly re-validated on this side
(P-coded system, tier=P, numeric value) — the §14.3 whitelist + sign-off already ran
[anonymized]-side; anything malformed here is skipped loudly, never silently.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys

FIELDS = ["system", "language", "granularity", "ref_grade", "ref_clusters", "ref_entities",
          "axis", "technique", "metric", "value", "projection", "source", "tier"]

P_CODE_RE = re.compile(r"^P-\d{1,2}$")

# system -> (language, fidelity granularity). Mirrors baselines/*.py.
SYSTEMS = {
    "itk": ("cpp", "file"), "abseil": ("cpp", "file"), "opencv": ("cpp", "file"),
    "bash": ("cpp", "file"), "libxml2": ("cpp", "file"),
    "eshop": ("cs", "target"), "orchardcore": ("cs", "target"), "nopcommerce": ("cs", "target"),
    "squidex": ("cs", "target"),      # RQ9 system #2 (companion plan §5); RQ1/RQ2 once baselined
}
BASELINE_TECHS = ("acdc", "arc", "wca", "limbo", "dir", "cc")

# Systems whose rows are confined to the RQ9 axis pending a corpus decision. Empty since
# 2026-08-30: Squidex was ADMITTED to RQ1/RQ2 (pre-planned, companion plan §5 step 7) with the
# reference-blind RQ9 curation (overlays/squidex-blind-R-C) as its `pipeline` row — the only
# curated Squidex model, non-circular by construction (comparison-target.json rebuilt with
# --arch-dir out/squidex-rq9/architecture). Its arrival moves the RQ2 Friedman p across 0.05
# (0.054 on 8 systems -> 0.012 on 9), so stats.py reports BOTH (CORPUS_EXTENSION there) and
# the significance claim may not rest on the ninth system alone (eval §12).
RQ9_ONLY: set = set()

# Tier-B systems that carry RQ7 perf runs but no reference (extra §10 scale points).
PERF_EXTRA = {
    "toy": ("cs", "target"), "serilog": ("cs", "target"),
    "fmt": ("cpp", "file"), "terminal": ("cpp", "target"),
}

# OpenCV's committed comparison-file scores the pipeline's deliberately-coarse 4-family GUI
# presentation curation (MoJoFM 59.6) — NOT the structural headline. The build graph recovers all
# 14 documented modules exactly (100.0/100.0, overlays/opencv-mod), the structure-vs-presentation
# result (plan §1.2a; references/README; four-systems-fidelity note). So we override the `pipeline`
# fidelity to the module-granularity number (sourced from the a2-ablation hand_curation rung) and
# keep the 4-family value as `pipeline_4family` — both in results.csv, projection-labelled, never
# silent. Any system here gets its comparison `pipeline` row relabelled and this number substituted.
# `ari`/`nested` (added 2026-09-09, eval plan section 12 (m)): a2a = 100 means zero operations
# separate the two partitions, i.e. they are identical on the shared set, and identical
# partitions have ARI = 1 and every cluster nested by definition — no separate computation.
# `clusters` (2026-09-13, review: "OpenCV's curated k is not recorded although its ARI is"): the
# a2-ablation hand_curation rung's own count, 14 = the reference's 14 documented modules.
PIPELINE_MODULE_GRAN = {"opencv": {"mojofm": 100.0, "a2a": 100.0, "ari": 100.0, "nested": 100.0,
                                   "clusters": 14,
                                   "note": "module-gran (overlays/opencv-mod); 4-family GUI "
                                           "curation = pipeline_4family"}}


def _load(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def iter_rows(out_root: pathlib.Path, refs: pathlib.Path):
    for system, (lang, gran) in SYSTEMS.items():
        base = out_root / system / "baseline"
        prov = _load(refs / system / "provenance.json") or {}
        meta = {"system": system, "language": lang, "granularity": gran,
                "ref_grade": prov.get("grade", ""), "ref_clusters": prov.get("clusters", ""),
                "ref_entities": prov.get("entities", ""), "tier": "oss"}
        if system in RQ9_ONLY:
            yield from _rq9_rows(out_root, refs, system, meta)
            continue

        # --- RQ1/RQ2: the fair all-common projection (comparison-<gran>.json) ---
        comp = _load(base / f"comparison-{gran}.json")
        override = PIPELINE_MODULE_GRAN.get(system)
        if comp:
            for tech, vals in (comp.get("results") or {}).items():
                # Relabel a presentation-curation pipeline row so the headline `pipeline` is the
                # structural (module-granularity) number substituted below.
                out_tech = "pipeline_4family" if (tech == "pipeline" and override) else tech
                # ari / nested: post hoc (2026-09-09, eval plan section 12 (m)); absent from a
                # comparison file written before make_comparison.py emitted them — never silent,
                # the table shows "--".
                for metric in ("mojofm", "a2a", "ari", "nested", "clusters"):
                    if metric in vals and vals[metric] is not None:
                        yield {**meta, "axis": "fidelity", "technique": out_tech, "metric": metric,
                               "value": vals[metric], "projection": "all-common",
                               "source": f"comparison-{gran}.json"}
        if override and comp:
            for metric in ("mojofm", "a2a", "ari", "nested", "clusters"):
                if metric not in override:
                    continue
                yield {**meta, "axis": "fidelity", "technique": "pipeline", "metric": metric,
                       "value": override[metric], "projection": "module-gran",
                       "source": "a2-ablation.json:hand_curation"}

        # --- RQ2: per-technique fidelity.json (captures arc/wca/limbo absent from comparison) ---
        for tech in BASELINE_TECHS:
            fid = _load(base / f"{tech}-{gran}" / "fidelity.json")
            if not fid:
                continue
            vr = fid.get("vs_reference") or {}
            for metric in ("mojofm", "a2a"):
                if vr.get(metric) is not None:
                    yield {**meta, "axis": "fidelity", "technique": tech, "metric": metric,
                           "value": round(float(vr[metric]), 2), "projection": "pairwise",
                           "source": f"{tech}-{gran}/fidelity.json"}
            if vr.get("entities_common") is not None:
                yield {**meta, "axis": "fidelity", "technique": tech, "metric": "entities_common",
                       "value": vr["entities_common"], "projection": "pairwise",
                       "source": f"{tech}-{gran}/fidelity.json"}

        # --- RQ2: LLM-recovery baseline (§6.2, baselines/llm_recovery.py). The reproducibility
        # axis is first-class: we emit the MoJoFM/a2a MEAN over reruns (projection=llm-rerun-mean)
        # plus the stdev as its own metric — the §6.2 story is that the LLM is competitive-but-not-
        # reproducible, so the spread matters as much as the mean. Absent until the baseline runs. ---
        llm = _load(base / f"llm-recovery-{gran}.json")
        agg = (llm or {}).get("aggregate") if isinstance(llm, dict) else None
        if agg:
            for metric in ("mojofm", "a2a", "ari"):
                stat = agg.get(metric) or {}
                if stat.get("mean") is not None:
                    yield {**meta, "axis": "fidelity", "technique": "llm", "metric": metric,
                           "value": stat["mean"], "projection": "llm-rerun-mean",
                           "source": f"llm-recovery-{gran}.json"}
                if stat.get("stdev") is not None:
                    yield {**meta, "axis": "fidelity", "technique": "llm",
                           "metric": f"{metric}_stdev", "value": stat["stdev"],
                           "projection": "llm-rerun-mean", "source": f"llm-recovery-{gran}.json"}
            # $ for the five reruns at the uncached list rate (runner's own estimate from its
            # token counts) — the paper states the cost (review item 7.3); systems that ran only.
            cost = (llm.get("cost_estimate") or {}).get("usd_uncached")
            if cost is not None:
                yield {**meta, "axis": "fidelity", "technique": "llm", "metric": "cost_usd",
                       "value": cost, "projection": "llm-rerun-mean",
                       "source": f"llm-recovery-{gran}.json"}

        # --- RQ3-A2: curation-heuristic rungs ---
        a2 = _load(base / "a2-ablation.json")
        if a2:
            for rung, vals in (a2.get("rungs") or {}).items():
                for metric in ("mojofm", "a2a"):
                    if vals.get(metric) is not None:
                        yield {**meta, "axis": "A2", "technique": rung, "metric": metric,
                               "value": vals[metric], "projection": "common-a2",
                               "source": "a2-ablation.json"}

        # --- RQ3 A1/A3/A4/A5: edge ablations ---
        rq3 = _load(base / "rq3-ablation.json")
        if rq3:
            for axis_key, block in (rq3.get("axes") or {}).items():
                axis = axis_key.split("_", 1)[0]            # "A1_evidence_layers" -> "A1"
                for label, vals in block.items():
                    for metric in ("f1", "edges", "edges_saved_by_rule"):
                        if metric in vals:
                            out_metric = {"f1": "edge_f1",
                                          "edges_saved_by_rule": "edges_saved"}.get(metric, metric)
                            yield {**meta, "axis": axis, "technique": label, "metric": out_metric,
                                   "value": vals[metric], "projection": "edge-vs-full",
                                   "source": "rq3-ablation.json"}
                    # §12-pre-registered reference-edge FIDELITY (C# target-gran only; C++ file-gran
                    # references are skipped upstream -> these keys simply absent). projection
                    # distinguishes them from the edge-vs-full SENSITIVITY rows above.
                    for metric in ("f1_ref", "precision_ref", "recall_ref", "edges_ref",
                                   "recall_ref_on", "recall_ref_off", "f1_ref_on", "f1_ref_off"):
                        if metric in vals:
                            yield {**meta, "axis": axis, "technique": label, "metric": metric,
                                   "value": vals[metric], "projection": "edge-vs-reference",
                                   "source": "rq3-ablation.json"}

        # --- RQ3-A7: LLM naming enrichment is structurally inert (Δ=0 by construction). The
        # headline is structural_delta==0 (partition + edges unmoved) while names_changed>0
        # proves the adversarial provider actually renamed — so Δ=0 is not the vacuous no-op. ---
        a7 = _load(base / "rq3-a7.json")
        if a7:
            for metric in ("structural_delta", "names_changed"):
                if a7.get(metric) is not None:
                    yield {**meta, "axis": "A7", "technique": "enrichment", "metric": metric,
                           "value": a7[metric], "projection": "structural-invariant",
                           "source": "rq3-a7.json"}

        # --- RQ3-A8 / RQ5: stability-churn ---
        churn = _load(base / "stability-churn.json")
        if churn:
            for tech, by_frac in (churn.get("techniques") or {}).items():
                for frac, vals in by_frac.items():
                    if vals.get("churn_mojofm") is not None:
                        yield {**meta, "axis": "A8", "technique": f"{tech}@{frac}",
                               "metric": "churn_mojofm", "value": vals["churn_mojofm"],
                               "projection": churn.get("mode", "perturb"),
                               "source": "stability-churn.json"}

        # --- RQ6 §8.1: seeded-mutation drift detection (mutate/harness.py). One row per
        # (operator, count-metric): tp/fn/fp are raw finding counts (stats.py derives
        # precision/recall/F1 + Wilson CIs from these — never pre-averaged), plus the
        # rename-classification and id_aliases partial-survival tallies. ---
        mut = _load(out_root / system / "mutation" / "mutation-summary.json")
        if mut:
            for op, s in (mut.get("operators") or {}).items():
                if not s.get("applicable") or not s.get("n"):
                    continue        # inexpressible on this idiom — recorded, not faked
                for metric in ("n", "tp", "fn", "fp", "detected", "rename_total",
                               "rename_correct", "false_rename_total",
                               "false_rename_clean", "alias_total", "alias_suppressed"):
                    if s.get(metric) is not None:
                        yield {**meta, "axis": "RQ6-mut", "technique": op,
                               "metric": metric, "value": s[metric],
                               "projection": "seeded",
                               "source": "mutation/mutation-summary.json"}

        # --- RQ6 §8.2: degradation correctness (mutate/degradation.py). ---
        deg = _load(out_root / system / "mutation" / "degradation.json")
        if deg:
            for mode, v in (deg.get("modes") or {}).items():
                rows = {"would_be_false": v.get("would_be_false_findings"),
                        "suppressed": v.get("suppressed_on"),
                        "leaked": v.get("leaked_on"),
                        "fp_suppression_rate": v.get("fp_suppression_rate"),
                        "advisory": (None if v.get("advisory") is None
                                     else int(bool(v["advisory"]))),
                        "erosion_visible": (None if v.get("erosion_visible") is None
                                            else int(bool(v["erosion_visible"]))),
                        "coverage_l0": v.get("coverage_l0")}
                lay = v.get("layering") or {}
                for k in ("starved_honest", "intact_still_violation"):
                    if lay.get(k) is not None:
                        rows[k] = int(bool(lay[k]))
                if v.get("gating") is not None:
                    rows["gating"] = int(bool(v["gating"]))
                for metric, value in rows.items():
                    if value is not None:
                        yield {**meta, "axis": "RQ6-deg", "technique": mode,
                               "metric": metric, "value": value,
                               "projection": "induced",
                               "source": "mutation/degradation.json"}

        # --- RQ6 combined: a real change AND evidence loss at once (mutate/combined.py). ---
        yield from _rq6_combined_rows(out_root, system, meta)

        # --- RQ6 real history: consecutive release pairs (mutate/history.py). ---
        yield from _rq6_history_rows(out_root, refs, system, meta)
        # --- workspace size facts (first-party target count) for Table I, independent of RQ7 ---
        yield from _size_rows(out_root, system, meta)

        # --- RQ7 §10: cold/warm pipeline performance (baselines/rq7_perf.py). ---
        yield from _rq7_rows(out_root, system, meta)

        # --- RQ9 §10a: blind-curation experiment (baselines/rq9_blind_curation.py). ---
        yield from _rq9_rows(out_root, refs, system, meta)

        # --- build-graph adequacy diagnostic (baselines/adequacy.py; reference-free). ---
        yield from _adequacy_rows(out_root, system, meta)

    # RQ7 also runs on Tier-B systems with no reference (scale points for the §10 curve);
    # the adequacy diagnostic covers them too (its range beyond the scored corpus).
    for system, (lang, gran) in PERF_EXTRA.items():
        meta = {"system": system, "language": lang, "granularity": gran,
                "ref_grade": "", "ref_clusters": "", "ref_entities": "", "tier": "oss"}
        yield from _rq7_rows(out_root, system, meta)
        yield from _adequacy_rows(out_root, system, meta)


COMB_METRICS = ("n", "findings", "drift", "gap", "advisory", "silent", "fp",
                "mutations_silent", "advisory_runs")


def _rq6_combined_rows(out_root: pathlib.Path, system: str, meta: dict):
    """axis=RQ6-comb (mutate/combined.py, review response 2026-08-30 item 4.3): the §8.1
    mutation set replayed with evidence loss induced on the MUTATED run. technique=<mode>
    for the per-mode totals, <mode>:<operator> per operator; every injected finding is in
    exactly one of drift / gap / advisory / silent — `silent` is hidden erosion."""
    # combined.json = the current engine; combined-prefix.json = the SAME run before the
    # empty-first-party coverage convention was fixed (the hole this experiment exposed) —
    # kept so the paper can cite the pre-fix silent count, projection-labelled, never blended.
    for fname, projection in (("combined.json", "combined"),
                              ("combined-prefix.json", "combined-prefix")):
        comb = _load(out_root / system / "mutation" / fname)
        if not comb:
            continue
        for mode, block in (comb.get("modes") or {}).items():
            for metric, value in (block.get("totals") or {}).items():
                if metric in COMB_METRICS and value is not None:
                    yield {**meta, "axis": "RQ6-comb", "technique": mode, "metric": metric,
                           "value": value, "projection": projection,
                           "source": f"mutation/{fname}"}
            for op, cell in (block.get("by_operator") or {}).items():
                for metric, value in cell.items():
                    if metric in COMB_METRICS and value is not None:
                        yield {**meta, "axis": "RQ6-comb", "technique": f"{mode}:{op}",
                               "metric": metric, "value": value, "projection": projection,
                               "source": f"mutation/{fname}"}


HIST_COUNTS = ("new_targets", "removed_targets", "new_edges", "removed_edges",
               "suspected_renames", "coverage_gap_targets", "coverage_gap_edges",
               "layering_violations", "drift_total")
HIST_LABELS = ("architectural", "non-architectural", "extraction-noise")


def _read_hist_labels(ws: pathlib.Path) -> dict[str, str] | None:
    """{finding_id: label} of one rater worksheet (empty labels skipped); None when the file
    cannot be read. A label outside HIST_LABELS is a typo, not a fourth category: it is
    reported on stderr and counted in no bucket (audit 2026-08-31 — a silently uncounted
    "noise"/"arch" would have skewed both the counts and the agreement)."""
    labels: dict[str, str] = {}
    try:
        with ws.open(encoding="utf-8") as f:
            rdr = csv.DictReader(line for line in f if not line.startswith("#"))
            for r in rdr:
                lab = (r.get("label") or "").strip().lower()
                if not lab:
                    continue
                if lab not in HIST_LABELS:
                    print(f"[aggregate] {ws}: finding {r.get('finding_id', '?')} has "
                          f"label {lab!r}, not one of {'/'.join(HIST_LABELS)} — "
                          "ignored", file=sys.stderr)
                    continue
                labels[r.get("finding_id", "")] = lab
    except OSError:
        return None
    return labels


def _rq6_history_rows(out_root: pathlib.Path, refs: pathlib.Path, system: str, meta: dict):
    """axis=RQ6-hist (mutate/history.py, item 4.2): the drift detector over consecutive
    REAL release pairs with the committed rules frozen. technique=<A>..<B>; counts per
    finding kind, commit statistics, the rule-maintenance signal (targets the frozen rules
    leave `needs-curation`), and — once the raters have filled the worksheets — the label
    counts per rater plus their agreement. Unlabelled worksheets emit `labels_pending`=1 so
    the paper can say so rather than silently showing zeros.

    The worksheets are read from the TRACKED references/<sys>/history/<A>__<B>/ first, file
    by file, and from the out/ mirror mutate/history.py writes only as a fallback: the filled
    labels are human artifacts under the eval §14.2 carve-out (like references/<sys>/
    blind-curation/) and must survive a harness re-run, which rewrites out/."""
    hist = _load(out_root / system / "history" / "history-summary.json")
    if not hist:
        return
    for pair in hist.get("pairs") or []:
        if pair.get("status") != "ok":
            continue
        tech = f"{pair['a']}..{pair['b']}"

        def row(metric, value, source="history/history-summary.json"):
            return {**meta, "axis": "RQ6-hist", "technique": tech, "metric": metric,
                    "value": value, "projection": "real-history", "source": source}
        for metric in ("commits_total", "commits_build_files", "needs_curation_a",
                       "needs_curation_b", "worksheet_rows", "coverage"):
            if pair.get(metric) is not None:
                yield row(metric, pair[metric])
        # The rule-maintenance signal proper (item 4.5-lite): NEW first-party targets at B that
        # the frozen rules leave unmapped, from the pair's result.json. `needs_curation_b` alone
        # also counts rules stale since the fixture (libxml2's 2.4.22 overlay leaves a constant
        # 20 legacy targets unmapped in every release) and so measures staleness, not maintenance.
        pdir = out_root / system / "history" / f"{pair['a']}__{pair['b']}"
        res = _load(pdir / "result.json") or {}
        new_b = (res.get("needs_curation") or {}).get("new_in_b")
        if new_b is not None:
            yield row("needs_curation_new_b", len(new_b))
        yield row("advisory", int(bool(pair.get("advisory"))))
        for metric in HIST_COUNTS:
            v = (pair.get("counts") or {}).get(metric)
            if v is not None:
                yield row(metric, v)
        # rater labels (human input): the tracked copy wins per file; a labelled out/ mirror
        # beside an unlabelled or different tracked copy is a rater who filled the wrong
        # file — say so loudly rather than silently ignore their work.
        pair_id = f"{pair['a']}__{pair['b']}"
        tracked = refs / system / "history" / pair_id
        sheets: dict[str, pathlib.Path] = {}
        for d in (tracked, pdir):
            if d.is_dir():
                for p in sorted(d.glob("worksheet-R*.csv")):
                    sheets.setdefault(p.name, p)
        labelled_any = False
        # A pair counts as LABELLED only once at least two raters have filled their sheets:
        # the paper's label cells are the findings BOTH raters agreed on, so with one rater in
        # they would print as 0/0/0 (an empty intersection read as "zero agreed findings").
        # Until then the pair stays pending and the table prints "--" (2026-09-13; the R1-only
        # window after commit 086d66a exposed this).
        labelled_raters: set[str] = set()
        per_rater: dict[str, dict[str, str]] = {}
        for name in sorted(sheets):
            ws = sheets[name]
            rater = ws.stem[len("worksheet-"):]
            labels = _read_hist_labels(ws)
            if labels is None:
                continue
            if ws.parent == tracked and (pdir / name).is_file():
                mirror = _read_hist_labels(pdir / name) or {}
                if mirror and mirror != labels:
                    print(f"[aggregate] {pdir / name}: carries labels that differ from the "
                          f"tracked copy {ws}, which wins — move them there",
                          file=sys.stderr)
            per_rater[rater] = labels
            if labels:
                labelled_any = True
                labelled_raters.add(rater)
                where = "references" if ws.parent == tracked else "out"
                src = f"{where}/{system}/history/{pair_id}/{name}"
                for lab in HIST_LABELS:
                    yield row(f"label_{lab}_{rater}", sum(1 for v in labels.values()
                                                             if v == lab), src)
        if len(labelled_raters) >= 2:
            raters = sorted(per_rater)
            a, b = per_rater[raters[0]], per_rater[raters[1]]
            common = [k for k in a if k in b]
            if common:
                yield row("labels_agree", sum(1 for k in common if a[k] == b[k]))
                yield row("labels_compared", len(common))
                # the paper's per-label counts are the findings BOTH raters gave that label
                # (never one rater's view); disagreements are reported through the agreement
                # statistics, not hidden in either rater's column
                for lab in HIST_LABELS:
                    yield row(f"label_{lab}_agreed",
                              sum(1 for k in common if a[k] == lab and b[k] == lab))
                # the 3x3 confusion cells per pair, so stats.py can POOL them over all pairs
                # and compute Cohen's kappa on the whole labelled set (a per-pair kappa over
                # 0-12 findings would be meaningless)
                for la in HIST_LABELS:
                    for lb in HIST_LABELS:
                        yield row(f"labels_conf_{la}_{lb}",
                                  sum(1 for k in common if a[k] == la and b[k] == lb))
        # A pair whose worksheets have no rows (the detector reported nothing) has nothing to
        # label and is never pending — otherwise the three quiet pairs kept the paper's
        # "labelling pending" macro alive after both raters had finished (2026-09-16).
        nothing_to_label = pair.get("worksheet_rows") == 0
        yield row("labels_pending", 0 if len(labelled_raters) >= 2 or nothing_to_label else 1)


def _adequacy_rows(out_root: pathlib.Path, system: str, meta: dict):
    """axis=adequacy: the reference-INDEPENDENT build-graph diagnostic of the no-curation
    floor (baselines/adequacy.py — review response 2026-08-30, item 2.1). technique=floor,
    projection=reference-free: none of these numbers has seen references/**."""
    adq = _load(out_root / system / "baseline" / "adequacy.json")
    if not adq:
        return
    for metric in ("entities", "units", "unit_ratio", "largest_share", "mq", "mq_per_unit",
                   "edges", "intra_edges", "intra_share"):
        if adq.get(metric) is not None:
            yield {**meta, "axis": "adequacy", "technique": "floor", "metric": metric,
                   "value": adq[metric], "projection": "reference-free",
                   "source": "adequacy.json"}


def _size_rows(out_root: pathlib.Path, system: str, meta: dict):
    """axis=size: the first-party build-target count read from the workspace's own
    ``extracted-facts.json`` (same definition as RQ7's ``size.targets``, which only exists for
    systems the perf sweep ran — Squidex did not, so Table I showed ``--`` although the facts
    exist; audit 2026-08-31)."""
    facts = _load(out_root / system / "architecture" / "generated" / "extracted-facts.json")
    if not facts:
        return
    first_party = [t for t in facts.get("targets", []) if not t.get("external")]
    yield {**meta, "axis": "size", "technique": "pipeline", "metric": "first_party_targets",
           "value": len(first_party), "projection": "facts",
           "source": "architecture/generated/extracted-facts.json"}


def _rq7_rows(out_root: pathlib.Path, system: str, meta: dict):
    perf = _load(out_root / system / "perf" / "rq7-perf.json")
    if not perf or not perf.get("success"):
        return
    size = perf.get("size") or {}
    top = {"cold_s": (perf.get("cold") or {}).get("wall_s"),
           "warm_s": (perf.get("warm") or {}).get("wall_s"),
           "cold_peak_rss_mb": (perf.get("cold") or {}).get("peak_rss_mb"),
           "warm_peak_rss_mb": (perf.get("warm") or {}).get("peak_rss_mb"),
           "speedup": perf.get("speedup"),
           "kloc": size.get("kloc"), "targets": size.get("targets"),
           "files": size.get("files"),
           "cold_within_budget": int(bool((perf.get("budget") or {}).get("cold_within"))),
           "warm_within_budget": int(bool((perf.get("budget") or {}).get("warm_within")))}
    for metric, value in top.items():
        if value is not None:
            yield {**meta, "axis": "RQ7", "technique": "pipeline",
                   "metric": metric, "value": value, "projection": "cold-warm",
                   "source": "perf/rq7-perf.json"}
    for phase in ("cold", "warm"):
        for stage, secs in ((perf.get(phase) or {}).get("stages") or {}).items():
            yield {**meta, "axis": "RQ7", "technique": f"stage:{stage}",
                   "metric": f"{phase}_s", "value": secs,
                   "projection": "cold-warm", "source": "perf/rq7-perf.json"}
        # per-extractor substeps (report.substep units): what dominates a cold run —
        # the ITK 410-min breakdown the paper reports (review response 2026-08-30, A.4)
        for stage, subs in ((perf.get(phase) or {}).get("substeps") or {}).items():
            for name, secs in (subs or {}).items():
                yield {**meta, "axis": "RQ7", "technique": f"substep:{stage}.{name}",
                       "metric": f"{phase}_s", "value": secs,
                       "projection": "cold-warm", "source": "perf/rq7-perf.json"}


# --- RQ9 §10a: blind-curation experiment ------------------------------------------------
# The human-produced artifacts (rater worksheets/RSFs, consensus, attestations, edit traces,
# the curator's frozen rules) are IRREPLACEABLE, unlike every other out/** artifact, which
# regenerates from tracked code + pinned SHAs. Their canonical home is therefore the TRACKED
# references/<sys>/blind-curation/ (decided 2026-08-30; the curator's frozen rules live as a
# tracked overlay, overlays/<sys>-blind-<C>/). out/<sys>/blind-curation/ is only the working
# mirror and is consulted as a fallback, file by file, so an experiment mid-flight still
# aggregates. File naming: effort[-<C>].json / fidelity[-<C>][-<variant>].json where <C> is
# the curator's role code (R-A …) and <variant> distinguishes a sensitivity scoring (e.g.
# fidelity-R-A-vs-consensus.json); the projection is derived from the JSON's own `reference`
# field, never from the filename.
RQ9_FILE_RE = re.compile(r"^(?P<what>effort|fidelity)(?:-(?P<code>R-[A-Z]|C\d*))?"
                         r"(?:-(?P<variant>.+))?\.json$")


def _read_contains(path: pathlib.Path):
    """RSF ``contain <cluster> <entity>`` -> {entity: cluster}; None when unreadable."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    out: dict[str, str] = {}
    for ln in lines:
        parts = ln.split()
        if len(parts) >= 3 and parts[0] == "contain":
            out[parts[2]] = parts[1]
    return out


def _partition_relation(cand: dict, ref: dict) -> dict:
    """How ``cand`` relates to ``ref`` over their common entities, cluster by cluster:

      * ``boundary_conflicts`` — cand clusters that CUT across a ref boundary: they partially
        overlap some ref cluster (neither contains the other). This is real structural
        disagreement.
      * ``merges`` — cand clusters that are a union of >=2 WHOLE ref clusters (coarser, nested).
      * ``splits`` — ref clusters that are a union of >=2 whole cand clusters (finer, nested).

    conflicts == 0 means the two partitions are nested wherever they differ — every
    disagreement is granularity ("drew the zoom line elsewhere"), none is structural. This is
    the descriptive complement to MoJoFM the RQ9 write-up needs: MoJoFM alone cannot tell a
    refinement from a re-cut."""
    common = set(cand) & set(ref)
    cm: dict[str, set] = {}
    rm: dict[str, set] = {}
    for e in common:
        cm.setdefault(cand[e], set()).add(e)
        rm.setdefault(ref[e], set()).add(e)
    conflicts = merges = splits = 0
    for ce in cm.values():
        touched = {ref[e] for e in ce}
        if any(not (rm[r] <= ce or ce <= rm[r]) for r in touched):
            conflicts += 1
        elif len(touched) > 1:
            merges += 1
    for re_ in rm.values():
        touched = {cand[e] for e in re_}
        if len(touched) > 1 and all(cm[c] <= re_ for c in touched):
            splits += 1
    return {"boundary_conflicts": conflicts, "merges": merges, "splits": splits}


def _num(v):
    """Numeric-or-None: the tidy CSV carries numbers only (bools excluded — flags are
    emitted explicitly as 0/1 where one is meant)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pair_cells(agr: dict) -> dict:
    """The agreement cells of a two-partition comparison as `rq9_blind_curation.py ceiling`
    writes them (R1-vs-R2, or curator-vs-curator for the variance arm)."""
    return {"kappa": agr.get("kappa"), "raw_agreement": agr.get("raw_agreement"),
            "labels_shared": agr.get("labels_shared"),
            "mojofm": agr.get("mojofm_mean"), "mojofm_ab": agr.get("mojofm_ab"),
            "mojofm_ba": agr.get("mojofm_ba"), "a2a": agr.get("a2a"),
            "entities_common": agr.get("n_common"),
            # Label-alignment-invariant companions to the pre-registered kappa
            # (§20 residual, added 2026-08-30): kappa scores EXACT label match, so two
            # raters who agree on the partition but name clusters differently score 0.
            # These do not replace kappa and never move the flag — they interpret it.
            "ari": agr.get("ari"), "pair_agreement": agr.get("pair_agreement"),
            "nmi": agr.get("nmi"), "kappa_aligned": agr.get("kappa_aligned")}


def _rq9_rows(out_root: pathlib.Path, refs: pathlib.Path, system: str, meta: dict):
    dirs = [refs / system / "blind-curation", out_root / system / "blind-curation"]

    def files(pattern: str) -> list[pathlib.Path]:
        seen: dict[str, pathlib.Path] = {}
        for d in dirs:                       # tracked dir wins, file by file
            if d.is_dir():
                for p in sorted(d.glob(pattern)):
                    seen.setdefault(p.name, p)
        return [seen[k] for k in sorted(seen)]

    def src(path: pathlib.Path) -> str:
        root = "references" if refs in path.parents else "out"
        return f"{root}/{system}/blind-curation/{path.name}"

    def row(technique, metric, value, projection, path):
        return {**meta, "axis": "RQ9", "technique": technique, "metric": metric,
                "value": value, "projection": projection, "source": src(path)}

    # --- the human ceiling (R1 vs R2) + the W3a each-rater-vs-committed-reference cells ---
    for path in files("ceiling.json"):
        ceiling = _load(path) or {}
        names = ceiling.get("raters") or {}
        raters = ((names.get("a", "R1"), "a"), (names.get("b", "R2"), "b"))
        agr = ceiling.get("agreement") or {}
        pair = {**_pair_cells(agr),
                # the pre-registered kappa<0.6 "lower-confidence reference" flag (§10a.6)
                "kappa_flag": int(bool(ceiling.get("flag")))}
        for metric, value in pair.items():
            if _num(value) is not None:
                yield row("ceiling", metric, value, "rater-pair", path)
        vs = ceiling.get("vs_reference") or {}
        for rater, key in raters:
            v = vs.get(key) or {}
            for metric, src_key in (("kappa", "kappa"), ("mojofm", "mojofm_mean"),
                                    ("a2a", "a2a"), ("raw_agreement", "raw_agreement"),
                                    ("ari", "ari"), ("nmi", "nmi"),
                                    ("kappa_aligned", "kappa_aligned")):
                if _num(v.get(src_key)) is not None:
                    yield row(f"rater:{rater}", metric, v[src_key], "rater-vs-reference", path)

    # --- curator-vs-curator agreement (the curator-variance arm, response plan 1.5b):
    # `rq9_blind_curation.py ceiling <C1 clusters>.rsf <C2 clusters>.rsf --names C1,C2 --out
    # curator-pair.json` writes the ceiling.json shape; its cells land under technique
    # `curator-pair` / projection `curator-pair` and never touch the rater rows above. ---
    for path in files("curator-pair*.json"):
        agr = (_load(path) or {}).get("agreement") or {}
        for metric, value in _pair_cells(agr).items():
            if _num(value) is not None:
                yield row("curator-pair", metric, value, "curator-pair", path)

    # --- partitions: each rater's frozen RSF + the reconciled consensus, described by
    # size and by their nesting relation to the committed (scored) reference:
    # boundary_conflicts / merges / splits (see _partition_relation) ---
    ref = _read_contains(refs / system / "reference.rsf")
    parts = [(f"rater:{p.stem[len('rater-'):]}", p) for p in files("rater-*.rsf")]
    parts += [("consensus", p) for p in files("consensus.rsf")]
    for technique, path in parts:
        cand = _read_contains(path)
        if not cand:
            continue
        yield row(technique, "clusters", len(set(cand.values())), "partition", path)
        yield row(technique, "entities", len(cand), "partition", path)
        if ref:
            for metric, value in _partition_relation(cand, ref).items():
                yield row(technique, metric, value, "vs-reference", path)

    # --- curator effort (rules size, propose-derived fraction, edit-trace ops, minutes) ---
    for path in files("effort*.json"):
        m = RQ9_FILE_RE.match(path.name)
        if not m:
            continue
        technique = f"curator:{m.group('code')}" if m.group("code") else "curator"
        eff = _load(path) or {}
        flat = {**(eff.get("rules") or {}), **(eff.get("propose") or {})}
        trace = eff.get("trace") or {}
        flat.update({k: v for k, v in trace.items() if k != "ops_by_kind"})
        flat.update({f"ops:{k}": v for k, v in (trace.get("ops_by_kind") or {}).items()})
        flat["wall_clock_minutes_reported"] = eff.get("wall_clock_minutes_reported")
        for metric, value in flat.items():
            if _num(value) is not None:
                yield row(technique, metric, value, "effort", path)

    # --- the four-row table: every row carries its §10a.7 label (detection / expression /
    # ceiling) as the projection, so no number can cross the line downstream. The
    # human_ceiling row is skipped here — ceiling.json above is its source of truth. ---
    for path in files("fidelity*.json"):
        m = RQ9_FILE_RE.match(path.name)
        if not m:
            continue
        curator = f"curator:{m.group('code')}" if m.group("code") else "curator"
        fid = _load(path) or {}
        ref_name = re.split(r"[\\/]", str(fid.get("reference") or "reference.rsf"))[-1]
        suffix = "" if ref_name == "reference.rsf" else f"-vs-{ref_name.rsplit('.', 1)[0]}"
        # the labelled full-reference sensitivity score (`score --common-with`, response plan
        # §14 F1): the same curator partition on every reference entity, projection-labelled so
        # it can never be mistaken for the all-common headline.
        for r in (fid.get("sensitivity") or {}).values():
            if not isinstance(r, dict):
                continue
            for metric in ("mojofm", "a2a", "clusters"):
                if _num(r.get(metric)) is not None:
                    yield row(curator, metric, r[metric],
                              f"{r.get('kind', 'sensitivity')}{suffix}", path)
        for row_name, r in (fid.get("rows") or {}).items():
            if row_name == "human_ceiling" or not isinstance(r, dict):
                continue
            technique = {"floor": "floor", "blind_curator": curator,
                         "best_sar": f"sar:{r.get('technique', 'best')}"}.get(row_name, row_name)
            projection = f"{r.get('kind', 'unlabelled')}{suffix}"
            for metric, src_key in (("mojofm", "mojofm"), ("a2a", "a2a"),
                                    ("clusters", "clusters"), ("entities_common", "n_common")):
                if _num(r.get(src_key)) is not None:
                    yield row(technique, metric, r[src_key], projection, path)
            edge = r.get("container_edge_f1") or {}
            for metric, src_key in (("edge_precision", "precision"), ("edge_recall", "recall"),
                                    ("edge_f1", "f1"), ("edge_tp", "true_positive_edges"),
                                    ("clusters_aligned", "clusters_aligned"),
                                    ("clusters_unmatched", "clusters_unmatched")):
                if _num(edge.get(src_key)) is not None:
                    yield row(technique, metric, edge[src_key], projection, path)


def iter_anon_rows(paths: list[pathlib.Path]):
    """Ingest Tier-P ``results-anon.csv`` exports (produced [anonymized]-side by
    analysis/anonymize_results.py, eval §14.1/§14.3). The whitelist + sign-off gate already
    ran on the producing side; this side re-checks only the invariants a malformed transfer
    could break (tier=P, P-coded system, numeric value) and skips violations LOUDLY."""
    for path in paths:
        kept = skipped = 0
        try:
            with path.open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    ok = (r.get("tier") == "P" and P_CODE_RE.match(r.get("system") or ""))
                    if ok:
                        try:
                            float(r["value"])
                        except (KeyError, TypeError, ValueError):
                            ok = False
                    if not ok:
                        skipped += 1
                        print(f"  [anon-skip] {path.name}: row with system="
                              f"{r.get('system')!r} tier={r.get('tier')!r} "
                              f"value={r.get('value')!r}", file=sys.stderr)
                        continue
                    kept += 1
                    yield {k: r.get(k, "") for k in FIELDS}
        except OSError as exc:
            print(f"WARNING: cannot read anon export {path}: {exc}", file=sys.stderr)
            continue
        print(f"[anon] {path}: {kept} Tier-P rows ingested"
              + (f", {skipped} skipped" if skipped else ""), file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = pathlib.Path(__file__).resolve().parent
    ap.add_argument("--out-root", type=pathlib.Path, default=here.parent / "out")
    ap.add_argument("--refs", type=pathlib.Path, default=here.parent / "references")
    ap.add_argument("--csv", type=pathlib.Path, default=here / "results.csv")
    ap.add_argument("--anon", type=pathlib.Path, action="append", default=[],
                    help="Tier-P results-anon.csv export to merge (repeatable; §14.3 channel)")
    args = ap.parse_args(argv)

    rows = sorted([*iter_rows(args.out_root, args.refs), *iter_anon_rows(args.anon)],
                  key=lambda r: (r["axis"], r["system"], r["technique"], r["metric"],
                                 r["projection"]))
    # lineterminator="\n": the csv module defaults to CRLF, but the repo pins every tracked text
    # artifact to LF (CLAUDE.md determinism gate) — force LF so results.csv is byte-stable across
    # OSes and a regeneration diffs as content, not an ending flip.
    with args.csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    n_sys = len({r["system"] for r in rows})
    n_p = len({r["system"] for r in rows if r["tier"] == "P"})
    print(f"wrote {len(rows)} rows across {n_sys} systems"
          + (f" (incl. {n_p} Tier-P)" if n_p else "") + f" -> {args.csv}", file=sys.stderr)
    if not rows:
        print("NOTE: no out/** baseline artifacts found.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
