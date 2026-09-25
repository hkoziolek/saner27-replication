#!/usr/bin/env python3
"""Run the pre-registered statistical analysis (eval plan §12) over results.csv and emit
stats.json (consumed by make_macros.py + make_figures.py).

What runs WITHOUT heavy deps (stdlib + statistics): the descriptive spine the small-N study
leans on — per-RQ medians, per-system paired deltas, win-counts, and the headline
``summary_macros`` map every in-prose number resolves to (§15.5). What is GATED on scipy: the
inferential tests (Friedman omnibus across techniques, Wilcoxon signed-rank for paired
ablations). Without scipy, ``compute_stats`` records them as ``"skipped: scipy absent"`` and
the corresponding macros resolve to TBD — the plan's effect-sizes-over-p-values stance (§12)
means the descriptive output already carries the argument. The CLI, however, REFUSES to write
stats.json in that state unless ``--allow-missing-scipy`` is passed: the committed
stats.json/macros.tex carry the inferential values, and a regeneration under the wrong
interpreter (system python instead of .venv) once silently blanked the paper's p-values.

Pre-registered tests (run when scipy present):
  RQ2 (techniques): Friedman omnibus on MoJoFM/a2a -> post-hoc Nemenyi / pairwise Wilcoxon
                    + Holm-Bonferroni; CD-diagram ranks.
  RQ3 (ablation on/off, paired): Wilcoxon signed-rank + Cliff's delta.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import pathlib
import statistics
import sys

CPP = "cpp"
CS = "cs"
BASELINES = ("acdc", "arc", "wca", "limbo", "dir", "cc")


def load(results_csv: pathlib.Path) -> list[dict]:
    with results_csv.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def vals(rows, *, axis=None, technique=None, metric=None, projection=None, language=None,
         system=None) -> list[float]:
    out = []
    for r in rows:
        if axis is not None and r["axis"] != axis:
            continue
        if technique is not None and r["technique"] != technique:
            continue
        if metric is not None and r["metric"] != metric:
            continue
        if projection is not None and r["projection"] != projection:
            continue
        if language is not None and r["language"] != language:
            continue
        if system is not None and r["system"] != system:
            continue
        try:
            out.append(float(r["value"]))
        except (TypeError, ValueError):
            continue
    return out


def by_system(rows, **kw) -> dict[str, float]:
    """One value per system for a fixed (axis, technique, metric, projection)."""
    out = {}
    for r in rows:
        if all(r[k] == v for k, v in kw.items() if k in r):
            try:
                out[r["system"]] = float(r["value"])
            except (TypeError, ValueError):
                pass
    return out


def med(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 2) if xs else None


def wilson(k: float, n: float, z: float = 1.96) -> list[float] | None:
    """Wilson score interval for a proportion (§12 — RQ6 detection rates)."""
    if not n:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def mcnemar_exact(b: int, c: int) -> float | None:
    """Exact (binomial) McNemar test on the discordant pair counts (§12 — RQ6
    stopping-rules on/off). Stdlib: two-sided sign test on b vs c."""
    import math
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    p = 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, p), 6)


# Nemenyi two-tailed critical values q_alpha (alpha=0.05), infinite df — Demšar (2006) Table 5(a),
# indexed by number of techniques k. Used for the critical-difference diagram (§12, RQ2). This is a
# fixed lookup, so the CD band is computable stdlib-only (no scipy/scikit-posthocs needed here).
NEMENYI_Q05 = {2: 1.960, 3: 2.343, 4: 2.569, 5: 2.728, 6: 2.850, 7: 2.949, 8: 3.031,
               9: 3.102, 10: 3.164, 11: 3.219, 12: 3.268}


def _ranks_desc(values: dict[str, float]) -> dict[str, float]:
    """Average ranks (1 = best) of techniques by value, descending, ties share the mean rank."""
    order = sorted(values, key=lambda t: values[t], reverse=True)
    ranks: dict[str, float] = {}
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2          # mean of the 1-based positions in the tie block
        for t in order[i:j + 1]:
            ranks[t] = avg
        i = j + 1
    return ranks


def cd_diagram(tech_mojo: dict[str, dict[str, float]], systems: list[str],
               focus: str = "pipeline") -> dict:
    """Average-rank + Nemenyi critical difference over the FULL candidate technique set, by MoJoFM,
    on the systems where every candidate technique completed (the largest complete rectangle).

    We keep all candidate techniques (pipeline + the all-common SAR set) and instead drop the
    systems missing any of them — so WCA/LIMBO stay in the diagram over the 5 systems where they
    ran rather than being deleted because ITK's agglomerative clustering does not complete at file
    granularity (§6.1). Lower average rank = better; two techniques differ significantly iff their
    average ranks differ by more than ``cd`` (§12; make_figures renders it). ``excluded_systems``
    records what was dropped, so the omission is visible in the artifact, never silent.
    ``focus`` names the row the pairwise verdicts are taken against (``vs_pipeline`` keeps its key
    for make_figures; review 2026-09-04 §1 reads the verdicts against the autonomous ``floor``)."""
    techs = list(tech_mojo)
    used = [s for s in systems if all(s in tech_mojo[t] for t in techs)]
    excluded = [s for s in systems if s not in used]
    k, n = len(techs), len(used)
    if k < 2 or n < 2:
        return {"techniques": techs, "avg_ranks": {}, "cd": None, "k": k, "n_systems": n,
                "excluded_systems": excluded,
                "note": "insufficient complete technique/system rectangle"}
    acc = {t: 0.0 for t in techs}
    for s in used:
        r = _ranks_desc({t: tech_mojo[t][s] for t in techs})
        for t in techs:
            acc[t] += r[t]
    avg = {t: round(acc[t] / n, 3) for t in techs}
    q = NEMENYI_Q05.get(k)
    cd = round(q * (k * (k + 1) / (6 * n)) ** 0.5, 3) if q else None
    # Post-hoc Nemenyi verdict per pair vs the pipeline row (item 5.4): two techniques differ
    # at alpha=0.05 iff their average ranks differ by more than the critical difference.
    vs_pipe = {}
    if focus in avg and cd is not None:
        for t in techs:
            if t != focus:
                d = round(avg[t] - avg[focus], 3)
                vs_pipe[t] = {"rank_diff": d, "significant": abs(d) > cd}
    return {"techniques": sorted(techs, key=lambda t: avg[t]), "avg_ranks": avg, "cd": cd,
            "k": k, "n_systems": n, "systems": used, "excluded_systems": excluded,
            "alpha": 0.05, "metric": "mojofm", "focus": focus, "vs_pipeline": vs_pipe}


def cliffs_delta(a: list[float], b: list[float]) -> float | None:
    """Non-parametric effect size (paired-friendly here as the dominance of a over b)."""
    if not a or not b:
        return None
    gt = sum(1 for x in a for y in b if x > y)
    lt = sum(1 for x in a for y in b if x < y)
    return round((gt - lt) / (len(a) * len(b)), 3)


def rank_biserial_paired(a: list[float], b: list[float]) -> float | None:
    """Matched-pairs rank-biserial correlation r = (W+ − W−)/(W+ + W−) over the non-zero
    paired differences a−b (Kerby 2014) — the effect size that belongs to the Wilcoxon
    signed-rank test. Cliff's δ is for two INDEPENDENT groups and stays reserved for the
    between-language contrasts (review response 2026-08-30, item 5.5). +1 = a exceeds b on
    every pair, −1 = the reverse, 0 = no paired dominance. None when nothing moved."""
    diffs = [x - y for x, y in zip(a, b) if x != y]
    if not diffs:
        return None
    ranks = _avg_ranks([abs(d) for d in diffs])
    w_pos = sum(r for d, r in zip(diffs, ranks) if d > 0)
    w_neg = sum(r for d, r in zip(diffs, ranks) if d < 0)
    return round((w_pos - w_neg) / (w_pos + w_neg), 3)


def _avg_ranks(xs: list[float]) -> list[float]:
    """1-based average ranks, ascending, ties share the mean rank."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in order[i:j + 1]:
            ranks[k] = (i + 1 + j + 1) / 2
        i = j + 1
    return ranks


def spearman(x: dict[str, float], y: dict[str, float]) -> dict:
    """Spearman ρ (Pearson on average ranks, ties shared) over the keys both carry —
    stdlib, small N, descriptive (the adequacy diagnostic, item 2.1)."""
    keys = sorted(k for k in x if k in y)
    n = len(keys)
    if n < 3:
        return {"rho": None, "n": n}
    rx, ry = _avg_ranks([x[k] for k in keys]), _avg_ranks([y[k] for k in keys])
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    rho = sxy / (sxx * syy) ** 0.5 if sxx and syy else None
    return {"rho": round(rho, 3) if rho is not None else None, "n": n}


def _spearman_lists(xs: list[float], ys: list[float]) -> float | None:
    """``spearman`` over parallel lists (duplicates allowed — the bootstrap resamples)."""
    n = len(xs)
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    return sxy / (sxx * syy) ** 0.5 if sxx and syy else None


def adequacy_robustness(adq: dict[str, dict[str, float]], floor: dict[str, float],
                        floor_ok: float, ratio_max: float, largest_max: float,
                        seed: int = 20260904, reps: int = 2000) -> dict:
    """How much the post-hoc adequacy rule's agreement with the floor means (review 2026-09-04,
    §2). Nine systems cannot validate the rule; these numbers bound the claim. Stdlib, seeded.

    ``interval``      — with the other clause at its published value, the half-open range
                        [lo, hi) of each threshold over which the rule still agrees with
                        floor >= ``floor_ok`` on every scored system: lo = the largest value it
                        must admit, hi = the smallest it must exclude. ``unit_ratio_only`` drops
                        the largest-share clause, which shows which system that clause carries.
    ``loo``           — leave-one-system-out: on the other systems the unit-ratio threshold is
                        re-selected as the midpoint between the largest adequate and the
                        smallest inadequate ratio; the largest-share threshold likewise, over
                        the inadequate systems the ratio clause admits (absent when none); the
                        held-out system is then predicted. The systems it gets wrong are named.
    ``rho_bootstrap`` — percentile bootstrap CI for Spearman rho (unit ratio vs floor MoJoFM);
                        resamples without rank variance are dropped and counted."""
    import random
    systems = sorted(s for s in adq.get("unit_ratio", {}) if s in floor)
    empty_iv = {"lo": None, "hi": None, "separable": None, "lo_system": None, "hi_system": None}
    if len(systems) < 3:   # synthetic / partial corpora: same shape, nothing computed
        return {"interval": {"unit_ratio": dict(empty_iv), "unit_ratio_only": dict(empty_iv),
                             "largest_share": dict(empty_iv)},
                "loo": {"correct": "n/a", "wrong": [], "folds": {},
                        "largest_clause_selected_in_folds": 0},
                "rho_bootstrap": {"reps": reps, "seed": seed, "valid": 0,
                                  "ci95": [None, None], "point": None},
                "note": "fewer than three scored systems with adequacy rows"}
    ratio = {s: adq["unit_ratio"][s] for s in systems}
    largest = {s: adq["largest_share"].get(s, 1.0) for s in systems}
    ok = {s for s in systems if floor[s] >= floor_ok}
    fail = [s for s in systems if s not in ok]

    def _interval(lo_set, hi_set, values):
        lo_set, hi_set = list(lo_set), list(hi_set)
        lo = max(values[s] for s in lo_set) if lo_set else None
        hi = min(values[s] for s in hi_set) if hi_set else None
        return {"lo": round(lo, 4) if lo is not None else None,
                "hi": round(hi, 4) if hi is not None else None,
                "separable": lo is not None and (hi is None or lo < hi),
                "lo_system": max(lo_set, key=lambda s: values[s]) if lo_set else None,
                "hi_system": min(hi_set, key=lambda s: values[s]) if hi_set else None}

    interval = {
        # unit-ratio threshold, largest-share clause fixed: hi = the first inadequate system
        # that clause does NOT catch
        "unit_ratio": _interval(ok, [s for s in fail if largest[s] <= largest_max], ratio),
        "unit_ratio_only": _interval(ok, fail, ratio),
        # largest-share threshold, ratio clause fixed: hi = the first inadequate system the
        # ratio clause admits
        "largest_share": _interval(ok, [s for s in fail if ratio[s] <= ratio_max], largest),
    }

    folds, wrong = {}, []
    for h in systems:
        train = [s for s in systems if s != h]
        ok_t = [s for s in train if s in ok]
        fail_t = [s for s in train if s not in ok]
        if not ok_t or not fail_t:
            folds[h] = {"correct": None, "note": "one class only in the training fold"}
            continue
        lo, hi = max(ratio[s] for s in ok_t), min(ratio[s] for s in fail_t)
        r_thr = (lo + hi) / 2 if lo < hi else lo    # not separable: admit the adequate set
        missed = [s for s in fail_t if ratio[s] <= r_thr]
        l_thr = None
        if missed:
            l_lo, l_hi = max(largest[s] for s in ok_t), min(largest[s] for s in missed)
            l_thr = (l_lo + l_hi) / 2 if l_lo < l_hi else None
        pred = ratio[h] <= r_thr and (l_thr is None or largest[h] <= l_thr)
        correct = pred == (h in ok)
        if not correct:
            wrong.append(h)
        folds[h] = {"ratio_threshold": round(r_thr, 4),
                    "largest_threshold": round(l_thr, 4) if l_thr is not None else None,
                    "predicted_adequate": pred, "actual_adequate": h in ok, "correct": correct}
    n_ok = sum(1 for f in folds.values() if f.get("correct"))
    loo = {"correct": f"{n_ok}/{len(systems)}", "wrong": wrong, "folds": folds,
           "largest_clause_selected_in_folds": sum(
               1 for f in folds.values() if f.get("largest_threshold") is not None)}

    rng = random.Random(seed)
    xs = [ratio[s] for s in systems]
    ys = [floor[s] for s in systems]
    rhos = []
    for _ in range(reps):
        idx = [rng.randrange(len(systems)) for _ in systems]
        r = _spearman_lists([xs[i] for i in idx], [ys[i] for i in idx])
        if r is not None:
            rhos.append(r)
    rhos.sort()

    def _pct(p):
        return round(rhos[min(len(rhos) - 1, int(p * len(rhos)))], 3) if rhos else None

    boot = {"reps": reps, "seed": seed, "valid": len(rhos),
            "ci95": [_pct(0.025), _pct(0.975)],
            "point": spearman(ratio, {s: floor[s] for s in systems})["rho"]}
    return {"interval": interval, "loo": loo, "rho_bootstrap": boot,
            "note": "post-hoc rule on nine systems: these bound the claim, they do not validate it"}


# Systems that joined the fidelity corpus AFTER the RQ2 omnibus test had first been run
# (Squidex, admitted 2026-08-30 — pre-planned in the companion plan §5 step 7, but its arrival
# moves the Friedman p across 0.05). §12 rule: report the statistic with AND without them; the
# significance claim may not rest on the extension alone. Macros: rqTwoFriedmanP (full corpus)
# and rqTwoFriedmanPPreExtension / rqTwoFriedmanNPreExtension (the pre-extension corpus).
CORPUS_EXTENSION = {"squidex"}

# RQ9: the PRE-REGISTERED (first) curator per system — the one every headline "blind curator"
# number and macro refers to. A further curator on the same system (the curator-variance arm,
# response plan 1.5b: eShop R-B) is reported BESIDE it under `curators` / `curator_variance`
# and never replaces it, whatever it scores. make_tables.py mirrors this map.
RQ9_PRIMARY_CURATOR = {"eshop": "R-A", "squidex": "R-C"}


# --- RQ9 (eval §10a): the blind-curation four-row table + effort, per system -------------
def _rq9_block(rows: list[dict], pipe_mojo: dict[str, float]) -> dict:
    """Per-system RQ9 numbers from the axis=RQ9 rows (aggregate_results._rq9_rows). Every
    fidelity value keeps its §10a.7 label (the projection): detection / expression / the
    R1-vs-R2 rater pair (results.csv keeps the pre-registered technique name `ceiling`; the
    derived keys and macros call it what it is — INTER-RATER agreement, which bounds nothing
    about a third partition; review M3, 2026-08-31). `curator_minus_raters` = curator −
    R1-vs-R2 (MoJoFM); a NEGATIVE value is not a failure — rater agreement is depressed
    wherever the raters disagree only on granularity (see `rater_boundary_conflicts`: 0 means
    every rater disagreement was nesting, not structure)."""
    rq9 = [r for r in rows if r["axis"] == "RQ9"]
    per: dict[str, dict] = {}
    for s in sorted({r["system"] for r in rq9}):
        rs = [r for r in rq9 if r["system"] == s]

        def val(tech, metric, projection, *, prefix=False):
            for r in rs:
                ok = r["technique"].startswith(tech) if prefix else r["technique"] == tech
                if ok and r["metric"] == metric and r["projection"] == projection:
                    try:
                        return float(r["value"])
                    except (TypeError, ValueError):
                        return None
            return None

        sar_name = next((r["technique"][4:] for r in rs if r["technique"].startswith("sar:")
                         and r["projection"] == "detection"), None)
        # the pre-registered curator carries every headline number; further curators are the
        # variance arm and stay beside it (RQ9_PRIMARY_CURATOR)
        codes = sorted({r["technique"].split(":", 1)[1] for r in rs
                        if r["technique"].startswith("curator:")})
        plain = any(r["technique"] == "curator" for r in rs)
        primary = (RQ9_PRIMARY_CURATOR.get(s) if RQ9_PRIMARY_CURATOR.get(s) in codes
                   else (codes[0] if codes else None))
        cur = f"curator:{primary}" if primary else ("curator" if plain else None)
        rater_conflicts = [float(r["value"]) for r in rs if r["technique"].startswith("rater:")
                           and r["metric"] == "boundary_conflicts"
                           and r["projection"] == "vs-reference"]

        def curator_block(tech):
            return {"mojofm": val(tech, "mojofm", "expression"),
                    "a2a": val(tech, "a2a", "expression"),
                    "edge_f1": val(tech, "edge_f1", "expression"),
                    "clusters": val(tech, "clusters", "expression"),
                    "minutes": val(tech, "wall_clock_minutes_reported", "effort"),
                    "ops": val(tech, "ops_total", "effort"),
                    "rule_lines": val(tech, "rule_lines", "effort"),
                    "groups": val(tech, "groups", "effort"),
                    "propose_derived_fraction": val(tech, "propose_derived_fraction", "effort")}
        curators = {c: curator_block(f"curator:{c}") for c in codes}
        variance = None
        if len(codes) >= 2:
            def spread(metric):
                xs = [curators[c][metric] for c in codes if curators[c][metric] is not None]
                return ({"min": min(xs), "max": max(xs), "spread": round(max(xs) - min(xs), 2)}
                        if len(xs) >= 2 else None)
            variance = {"n_curators": len(codes), "codes": codes, "primary": primary,
                        "mojofm": spread("mojofm"), "a2a": spread("a2a"),
                        "edge_f1": spread("edge_f1"), "minutes": spread("minutes"),
                        # curator-vs-curator agreement (curator-pair.json, runbook §8 step 5c)
                        "pair": {m: val("curator-pair", m, "curator-pair")
                                 for m in ("mojofm", "a2a", "kappa", "kappa_aligned", "ari",
                                           "nmi", "pair_agreement")}}

        def rater_vs_ref(metric):
            """Each rater independently scored against the COMMITTED reference (the W3a arm —
            present only where `ceiling --reference` ran, i.e. eShop). Reported as a range so
            the claim is 'both raters reach at least X', never a cherry-picked rater."""
            vs = [float(r["value"]) for r in rs if r["technique"].startswith("rater:")
                  and r["metric"] == metric and r["projection"] == "rater-vs-reference"]
            return {"min": min(vs), "max": max(vs), "n": len(vs)} if vs else None
        cur_mojo = val(cur, "mojofm", "expression")
        ceil_mojo = val("ceiling", "mojofm", "rater-pair")
        floor_mojo = val("floor", "mojofm", "detection")
        sar_mojo = val("sar:", "mojofm", "detection", prefix=True)
        d = {
            "curator": cur,
            "curator_code": primary,
            "curators": curators,
            "curator_variance": variance,
            "floor": {"mojofm": floor_mojo, "a2a": val("floor", "a2a", "detection")},
            "best_sar": {"technique": sar_name, "mojofm": sar_mojo,
                         "a2a": val("sar:", "a2a", "detection", prefix=True)},
            "blind_curator": {"mojofm": cur_mojo,
                              "a2a": val(cur, "a2a", "expression"),
                              "edge_f1": val(cur, "edge_f1", "expression"),
                              "clusters": val(cur, "clusters", "expression")},
            # the same partition on the FULL reference (score --common-with sensitivity row,
            # response plan §14 F1): reported as a labelled sensitivity number only.
            "blind_curator_full_reference": {
                "mojofm": val(cur, "mojofm", "expression-full-reference"),
                "a2a": val(cur, "a2a", "expression-full-reference")},
            "blind_curator_vs_consensus": {
                "mojofm": val(cur, "mojofm", "expression-vs-consensus"),
                "a2a": val(cur, "a2a", "expression-vs-consensus"),
                "edge_f1": val(cur, "edge_f1", "expression-vs-consensus")},
            # kappa is the PRE-REGISTERED statistic and the flag key; ari / kappa_aligned are
            # the label-invariant companions that say whether a fired flag is vocabulary or
            # substance (§20 residual, 2026-08-30). Never swap them for kappa.
            "inter_rater": {"mojofm": ceil_mojo, "a2a": val("ceiling", "a2a", "rater-pair"),
                              "kappa": val("ceiling", "kappa", "rater-pair"),
                              "kappa_aligned": val("ceiling", "kappa_aligned", "rater-pair"),
                              "ari": val("ceiling", "ari", "rater-pair"),
                              "pair_agreement": val("ceiling", "pair_agreement", "rater-pair"),
                              "nmi": val("ceiling", "nmi", "rater-pair"),
                              "kappa_flag": val("ceiling", "kappa_flag", "rater-pair"),
                              "labels_shared": val("ceiling", "labels_shared", "rater-pair")},
            "rater_boundary_conflicts": (int(sum(rater_conflicts)) if rater_conflicts else None),
            # W3a: does the COMMITTED reference survive independent human re-derivation?
            "raters_vs_reference": {"kappa": rater_vs_ref("kappa"),
                                    "kappa_aligned": rater_vs_ref("kappa_aligned"),
                                    "ari": rater_vs_ref("ari"),
                                    "nmi": rater_vs_ref("nmi"),
                                    "mojofm": rater_vs_ref("mojofm")},
            "consensus_clusters": val("consensus", "clusters", "partition"),
            "effort": {"minutes": val(cur, "wall_clock_minutes_reported", "effort"),
                       "ops": val(cur, "ops_total", "effort"),
                       "rule_lines": val(cur, "rule_lines", "effort"),
                       "groups": val(cur, "groups", "effort"),
                       "propose_derived_fraction": val(cur, "propose_derived_fraction",
                                                       "effort")},
            "curator_minus_raters_mojofm": (round(cur_mojo - ceil_mojo, 2)
                                            if cur_mojo is not None and ceil_mojo is not None
                                            else None),
            "curator_over_floor_mojofm": (round(cur_mojo - floor_mojo, 2)
                                          if cur_mojo is not None and floor_mojo is not None
                                          else None),
            "curator_over_best_sar_mojofm": (round(cur_mojo - sar_mojo, 2)
                                             if cur_mojo is not None and sar_mojo is not None
                                             else None),
            # RQ1's `pipeline` MoJoFM for the same system: for eShop that is the earlier
            # NON-blind hand curation, so blind − non-blind = the measured leakage (§10a.6).
            # For a system whose RQ1 row IS the blind model (squidex) it is the same model
            # under the all-common projection — a projection delta, not leakage.
            "rq1_pipeline_mojofm": pipe_mojo.get(s),
        }
        per[s] = d
    gaps = [d["curator_minus_raters_mojofm"] for d in per.values()
            if d["curator_minus_raters_mojofm"] is not None]

    def _pdfs(d):   # every curator's propose-derived fraction (the primary's when uncoded)
        return ([c["propose_derived_fraction"] for c in d["curators"].values()]
                or [d["effort"]["propose_derived_fraction"]])
    return {"systems": sorted(per), "n_systems": len(per), "by_system": per,
            "median_curator_minus_raters_mojofm": med(gaps),
            "systems_with_two_curators": [s for s in sorted(per) if per[s]["curator_variance"]],
            "all_curators_propose_derived_fraction_zero": (
                all((x or 0.0) == 0.0 for d in per.values() for x in _pdfs(d))
                if per else None),
            "labelling_rule": "detection and expression numbers never cross (eval §10a.7)"}


def _rq9_macros(rq9: dict, fmt) -> dict:
    """\\rqNine<System><Quantity> per system (macro names carry no digits: EdgeFone), plus
    the corpus-level count. Missing values resolve to TBD, never to a wrong number."""
    def pct(x):
        return fmt(round(100 * x)) if isinstance(x, (int, float)) else "TBD"

    def _rng(d, metric, end):
        """One end of the raters-vs-committed-reference range. Where the arm did not run BY
        DESIGN (Squidex's reference IS the raters' consensus -- references/squidex/reference.rsf
        is byte-identical to blind-curation/consensus.rsf -- so scoring the raters against it
        would score them against themselves) the macro resolves to ``n/a``, not ``TBD``: this
        is a deliberate absence, not missing data, and the prose must never cite it."""
        block = (d.get("raters_vs_reference") or {}).get(metric)
        return fmt(block[end]) if block else "n/a"

    def _mins(v):   # "43", never "43.0", for a self-reported whole-minute wall-clock
        return fmt(int(v) if isinstance(v, float) and v.is_integer() else v)

    two = rq9.get("systems_with_two_curators") or []
    m = {"rqNineSystems": rq9["n_systems"],
         "rqNineMedianCuratorMinusRatersMoJo": fmt(rq9["median_curator_minus_raters_mojofm"]),
         # the curator-variance arm (response plan 1.5b): how many systems have >= 2 curators
         "rqNineSystemsWithTwoCurators": len(two),
         "rqNineTwoCuratorSystemsList": ", ".join(_disp_sys(s) for s in two) or "none"}
    for s, d in rq9["by_system"].items():
        x = s.capitalize()
        m.update({
            f"rqNine{x}CuratorCode": d.get("curator_code") or "TBD",
            f"rqNine{x}CuratorCount": len(d.get("curators") or {}) or (
                1 if d["blind_curator"]["mojofm"] is not None else 0),
            f"rqNine{x}FloorMoJo": fmt(d["floor"]["mojofm"]),
            f"rqNine{x}FloorAtoA": fmt(d["floor"]["a2a"]),
            f"rqNine{x}BestSarName": (d["best_sar"]["technique"] or "TBD").upper(),
            f"rqNine{x}BestSarMoJo": fmt(d["best_sar"]["mojofm"]),
            f"rqNine{x}BestSarAtoA": fmt(d["best_sar"]["a2a"]),
            f"rqNine{x}CuratorMoJo": fmt(d["blind_curator"]["mojofm"]),
            f"rqNine{x}CuratorAtoA": fmt(d["blind_curator"]["a2a"]),
            f"rqNine{x}CuratorEdgeFone": fmt(d["blind_curator"]["edge_f1"]),
            f"rqNine{x}CuratorFullRefMoJo": fmt(d["blind_curator_full_reference"]["mojofm"]),
            f"rqNine{x}CuratorFullRefAtoA": fmt(d["blind_curator_full_reference"]["a2a"]),
            f"rqNine{x}CuratorClusters": fmt(d["blind_curator"]["clusters"]),
            # the R1-vs-R2 pair: "RaterPair", never "Ceiling" (review M3) — the macro names
            # ship in the artifact's macros.tex and must not contradict the paper's wording
            f"rqNine{x}RaterPairMoJo": fmt(d["inter_rater"]["mojofm"]),
            f"rqNine{x}RaterPairAtoA": fmt(d["inter_rater"]["a2a"]),
            f"rqNine{x}Kappa": fmt(d["inter_rater"]["kappa"]),
            f"rqNine{x}KappaAligned": fmt(d["inter_rater"]["kappa_aligned"]),
            # W3a range over the raters vs the committed reference ("both reach at least Min")
            f"rqNine{x}RaterRefKappaAlignedMin": _rng(d, "kappa_aligned", "min"),
            f"rqNine{x}RaterRefKappaAlignedMax": _rng(d, "kappa_aligned", "max"),
            f"rqNine{x}RaterRefAriMin": _rng(d, "ari", "min"),
            f"rqNine{x}RaterRefKappaMin": _rng(d, "kappa", "min"),
            f"rqNine{x}RaterRefMoJoMin": _rng(d, "mojofm", "min"),
            f"rqNine{x}Ari": fmt(d["inter_rater"]["ari"]),
            f"rqNine{x}Nmi": fmt(d["inter_rater"]["nmi"]),
            f"rqNine{x}RaterRefNmiMin": _rng(d, "nmi", "min"),
            f"rqNine{x}PairAgreement": fmt(d["inter_rater"]["pair_agreement"]),
            # a count, not a score — render "0"/"4", never "0.0"/"4.0"
            f"rqNine{x}LabelsShared": (str(int(d["inter_rater"]["labels_shared"]))
                                       if d["inter_rater"]["labels_shared"] is not None
                                       else "TBD"),
            f"rqNine{x}CuratorMinusRatersMoJo": fmt(d["curator_minus_raters_mojofm"]),
            f"rqNine{x}OverFloorMoJo": fmt(d["curator_over_floor_mojofm"]),
            f"rqNine{x}OverBestSarMoJo": fmt(d["curator_over_best_sar_mojofm"]),
            f"rqNine{x}RaterConflicts": fmt(d["rater_boundary_conflicts"]),
            f"rqNine{x}ConsensusClusters": fmt(d["consensus_clusters"]),
            f"rqNine{x}Minutes": _mins(d["effort"]["minutes"]),
            f"rqNine{x}Ops": fmt(d["effort"]["ops"]),
            f"rqNine{x}RuleLines": fmt(d["effort"]["rule_lines"]),
            f"rqNine{x}Groups": fmt(d["effort"]["groups"]),
            f"rqNine{x}ProposeDerivedPct": pct(d["effort"]["propose_derived_fraction"]),
        })
        if d["blind_curator_vs_consensus"]["mojofm"] is not None:
            m[f"rqNine{x}CuratorVsConsensusMoJo"] = fmt(d["blind_curator_vs_consensus"]["mojofm"])
            m[f"rqNine{x}CuratorVsConsensusAtoA"] = fmt(d["blind_curator_vs_consensus"]["a2a"])
        cv = d.get("curator_variance")
        if cv:
            # the second curator beside the pre-registered one, never in its place; the
            # curator-pair cells are TBD until runbook §8 step 5c has run
            second = next(c for c in cv["codes"] if c != cv["primary"])
            sc = d["curators"][second]

            def _sp(metric):
                return fmt(cv[metric]["spread"]) if cv.get(metric) else "TBD"
            m.update({
                f"rqNine{x}SecondCuratorCode": second,
                f"rqNine{x}SecondCuratorMoJo": fmt(sc["mojofm"]),
                f"rqNine{x}SecondCuratorAtoA": fmt(sc["a2a"]),
                f"rqNine{x}SecondCuratorEdgeFone": fmt(sc["edge_f1"]),
                f"rqNine{x}SecondCuratorMinutes": _mins(sc["minutes"]),
                f"rqNine{x}SecondCuratorOps": fmt(sc["ops"]),
                f"rqNine{x}CuratorMoJoSpread": _sp("mojofm"),
                f"rqNine{x}CuratorAtoASpread": _sp("a2a"),
                f"rqNine{x}CuratorEdgeFoneSpread": _sp("edge_f1"),
                f"rqNine{x}CuratorPairMoJo": fmt(cv["pair"]["mojofm"]),
                f"rqNine{x}CuratorPairAtoA": fmt(cv["pair"]["a2a"]),
                f"rqNine{x}CuratorPairAri": fmt(cv["pair"]["ari"]),
                f"rqNine{x}CuratorPairKappaAligned": fmt(cv["pair"]["kappa_aligned"]),
                f"rqNine{x}CuratorPairNmi": fmt(cv["pair"]["nmi"]),
            })
    e = rq9["by_system"].get("eshop")
    if e and e["rq1_pipeline_mojofm"] is not None and e["blind_curator"]["mojofm"] is not None:
        m["rqNineEshopNonBlindMoJo"] = fmt(e["rq1_pipeline_mojofm"])
        m["rqNineEshopLeakageDelta"] = fmt(round(e["blind_curator"]["mojofm"]
                                                 - e["rq1_pipeline_mojofm"], 2))
    return m


def compute_stats(rows: list[dict]) -> dict:
    # §14.3 iron rule: Tier-P rows CORROBORATE, never carry — every pre-registered statistic
    # and macro below is computed over the replicable OSS rows only; the anonymized industrial
    # rows get their own descriptive block (`tier_p`) and clearly-named macros.
    tier_p_rows = [r for r in rows if r.get("tier") == "P"]
    rows = [r for r in rows if r.get("tier") != "P"]
    # The CORPUS is the fidelity set — RQ7 adds Tier-B perf-only systems (toy/fmt/...)
    # whose rows must not inflate the corpus counts or the RQ2 win denominators.
    systems = sorted({r["system"] for r in rows if r["axis"] == "fidelity"})
    cpp_sys = sorted({r["system"] for r in rows
                      if r["language"] == CPP and r["axis"] == "fidelity"})
    cs_sys = sorted({r["system"] for r in rows
                     if r["language"] == CS and r["axis"] == "fidelity"})

    # --- RQ1: pipeline fidelity. No projection filter on `pipeline` so the opencv module-gran
    # override (technique=pipeline, projection=module-gran) is picked up over the relabelled
    # 4-family presentation row; every system has exactly one `pipeline` row per metric. ---
    pipe_mojo = by_system(rows, axis="fidelity", technique="pipeline", metric="mojofm")
    pipe_a2a = by_system(rows, axis="fidelity", technique="pipeline", metric="a2a")
    # ARI (x100) — post hoc, descriptive (2026-09-09, eval plan section 12 (m)); the inferential
    # tests below stay on the pre-registered MoJoFM and a2a. `nested` = share of a partition's
    # clusters lying inside ONE reference cluster (over-splitting vs misgrouping, see
    # run_arcade.nested_share).
    pipe_ari = by_system(rows, axis="fidelity", technique="pipeline", metric="ari")

    # --- RQ2: per-technique median MoJoFM + pipeline win counts (a2a, the headline metric).
    # ALL_COMMON = the SAR techniques on the pre-registered all-common entity projection (§6.4).
    # WCA/LIMBO joined it in the 2026-07-13 consolidation (they have full graph coverage, so the
    # shared set is unchanged — note 2026-07-13-rq2-projection-consolidation.md); ARC stays pairwise
    # (topic-based partial coverage) and is reported with its coverage, not forced into the set. ---
    ALL_COMMON = ("acdc", "wca", "limbo", "dir", "cc")
    tech_mojo_all = {"pipeline": pipe_mojo,
                     **{t: by_system(rows, axis="fidelity", technique=t, metric="mojofm",
                                     projection="all-common") for t in ALL_COMMON}}
    tech_mojo_pair = {t: by_system(rows, axis="fidelity", technique=t, metric="mojofm",
                                   projection="pairwise") for t in BASELINES}
    a2a_all = {"pipeline": pipe_a2a,
               **{t: by_system(rows, axis="fidelity", technique=t, metric="a2a",
                               projection="all-common") for t in ALL_COMMON}}
    ari_all = {"pipeline": pipe_ari,
               **{t: by_system(rows, axis="fidelity", technique=t, metric="ari",
                               projection="all-common") for t in ALL_COMMON}}
    # --- RQ2 "lead" is DEFINED here (review response 2026-08-30, items 1.1/5.1). Candidate
    # sets: CLASSICAL = the all-common set (ACDC/WCA/LIMBO/dir/cc, identical entity projection);
    # ALL = every displayed method (adds ARC on its own covered subset and the LLM rerun mean).
    # Rules: STRICT (>) and WINS-OR-TIES (>=). The headline macro is strict vs CLASSICAL; the
    # others make the count reproducible from the table. Computed for the curated model
    # (expression) AND the two autonomous rows — the no-curation floor and the auto-propose
    # draft — which the fair detection comparison must lead with. ---
    tech_a2a_pair = {t: by_system(rows, axis="fidelity", technique=t, metric="a2a",
                                  projection="pairwise") for t in BASELINES}
    llm_mojo = by_system(rows, axis="fidelity", technique="llm", metric="mojofm",
                         projection="llm-rerun-mean")
    llm_a2a = by_system(rows, axis="fidelity", technique="llm", metric="a2a",
                        projection="llm-rerun-mean")
    llm_ari = by_system(rows, axis="fidelity", technique="llm", metric="ari",
                        projection="llm-rerun-mean")
    tech_ari_pair = {t: by_system(rows, axis="fidelity", technique=t, metric="ari",
                                  projection="pairwise") for t in BASELINES}
    floor_ari_ac = by_system(rows, axis="fidelity", technique="floor", metric="ari",
                             projection="all-common")
    floor_nested_ac = by_system(rows, axis="fidelity", technique="floor", metric="nested",
                                projection="all-common")
    prop_ari_ac = by_system(rows, axis="fidelity", technique="propose", metric="ari",
                            projection="all-common")
    floor_mojo_ac = by_system(rows, axis="fidelity", technique="floor", metric="mojofm",
                              projection="all-common")
    floor_a2a_ac = by_system(rows, axis="fidelity", technique="floor", metric="a2a",
                             projection="all-common")
    prop_mojo_ac = by_system(rows, axis="fidelity", technique="propose", metric="mojofm",
                             projection="all-common")
    prop_a2a_ac = by_system(rows, axis="fidelity", technique="propose", metric="a2a",
                            projection="all-common")

    def _wins(cand: dict[str, float], others: dict[str, dict[str, float]],
              strict: bool) -> dict:
        w, scored, won = 0, 0, []
        for s in systems:
            if s not in cand:
                continue
            vals = [others[t][s] for t in others if s in others[t]]
            if not vals:
                continue
            scored += 1
            if all((cand[s] > o) if strict else (cand[s] >= o) for o in vals):
                w += 1
                won.append(s)
        return {"wins": w, "scored": scored, "systems": won}

    classical_mojo = {t: tech_mojo_all[t] for t in ALL_COMMON}
    classical_a2a = {t: a2a_all[t] for t in ALL_COMMON}
    all_mojo = {**classical_mojo, "arc": tech_mojo_pair["arc"], "llm": llm_mojo}
    all_a2a = {**classical_a2a, "arc": tech_a2a_pair["arc"], "llm": llm_a2a}
    classical_ari = {t: ari_all[t] for t in ALL_COMMON}
    all_ari = {**classical_ari, "arc": tech_ari_pair["arc"], "llm": llm_ari}
    win_rows = {"pipeline": (pipe_mojo, pipe_a2a, pipe_ari),
                "floor": (floor_mojo_ac, floor_a2a_ac, floor_ari_ac),
                "propose": (prop_mojo_ac, prop_a2a_ac, prop_ari_ac)}
    wins: dict[str, dict] = {}
    for name, (m, a, r) in win_rows.items():
        wins[name] = {
            "mojofm": {"strict_vs_classical": _wins(m, classical_mojo, True),
                       "ties_vs_classical": _wins(m, classical_mojo, False),
                       "strict_vs_all": _wins(m, all_mojo, True)},
            "a2a": {"strict_vs_classical": _wins(a, classical_a2a, True),
                    "ties_vs_classical": _wins(a, classical_a2a, False),
                    "strict_vs_all": _wins(a, all_a2a, True)},
            "ari": {"strict_vs_classical": _wins(r, classical_ari, True),
                    "ties_vs_classical": _wins(r, classical_ari, False),
                    "strict_vs_all": _wins(r, all_ari, True)}}
    # the best ARI any all-common classical technique reaches anywhere (descriptive), and the
    # same over the clustering ALGORITHMS alone (ACDC/WCA/LIMBO, i.e. without the naive dir/cc)
    classical_ari_cells = [(v, t, s) for t, d in classical_ari.items() for s, v in d.items()]
    classical_ari_max = max(classical_ari_cells) if classical_ari_cells else None
    sar_ari_cells = [c for c in classical_ari_cells if c[1] in ("acdc", "wca", "limbo")]
    sar_ari_max = max(sar_ari_cells) if sar_ari_cells else None
    a2a_wins = wins["pipeline"]["a2a"]["strict_vs_classical"]["wins"]

    # --- RQ2 "recovery" / "failure" is DEFINED here as well (review response 2026-08-30,
    # item 2.3; answers reviewer Q6). POST HOC — no threshold was pre-registered and the paper
    # says so. An autonomous partition RECOVERS a system at MoJoFM >= 80 (the same floor
    # threshold the adequacy rule reads, ADQ_FLOOR_OK below) and FAILS below 50 (it recovers
    # less than half); in between it is PARTIAL. Applied per (technique, system) cell to the
    # classical SAR techniques on the C# systems — the cells the "no classical technique
    # recovers a C# system" sentence covers — and, for transparency, to the LLM and the floor. ---
    REC_OK, REC_FAIL = 80.0, 50.0

    def _recovery(v: float) -> str:
        return "recovered" if v >= REC_OK else ("failed" if v < REC_FAIL else "partial")
    _tdisp = {"acdc": "ACDC", "wca": "WCA", "limbo": "LIMBO", "arc": "ARC"}
    _sdisp = {"eshop": "eShop", "orchardcore": "OrchardCore", "nopcommerce": "nopCommerce",
              "squidex": "Squidex", "itk": "ITK", "abseil": "abseil", "opencv": "OpenCV",
              "bash": "bash", "libxml2": "libxml2"}
    _sdisp_b = {"fmt": "fmt", "serilog": "Serilog", "terminal": "Terminal", "orleans": "Orleans"}
    # the best ALL-COMMON technique on the C# systems by median MoJoFM (the §VI regime
    # sentence): "best" is scoped to the all-common set — ARC, on its own subset, is quoted
    # beside it, never folded in (audit 2026-08-31: the old sentence called LIMBO "the best"
    # with a corpus-wide median while ARC's C# median was higher)
    cs_ac_med = {t: med([tech_mojo_all[t][s] for s in cs_sys if s in tech_mojo_all[t]])
                 for t in ALL_COMMON}
    cs_best_ac = max(cs_ac_med, key=lambda t: cs_ac_med[t] if cs_ac_med[t] is not None else -1)
    cs_best_ac_med = cs_ac_med[cs_best_ac]
    classical_sar = {t: tech_mojo_all[t] for t in ("acdc", "wca", "limbo")}
    classical_sar["arc"] = tech_mojo_pair.get("arc", {})      # own covered subset, descriptive
    rec_cells = {f"{t}:{s}": _recovery(v[s]) for t, v in classical_sar.items()
                 for s in cs_sys if s in v}
    rec_counts = {k: sum(1 for c in rec_cells.values() if c == k)
                  for k in ("recovered", "partial", "failed")}
    rec_partial = []      # grouped per system for the prose macro: "Squidex: WCA 50.0, LIMBO 60.0"
    for s in cs_sys:
        items = [f"{_tdisp[t]} {classical_sar[t][s]}" for t in ("acdc", "wca", "limbo", "arc")
                 if rec_cells.get(f"{t}:{s}") == "partial"]
        if items:
            rec_partial.append(f"{_sdisp.get(s, s)}: {', '.join(items)}")
    rec_llm_cs = {s: _recovery(llm_mojo[s]) for s in cs_sys if s in llm_mojo}
    rec_floor_cs = {s: _recovery(floor_mojo_ac[s]) for s in cs_sys if s in floor_mojo_ac}

    def _rec_list(d: dict[str, str], cls: str) -> str:
        hits = [_sdisp.get(s, s) for s in cs_sys if d.get(s) == cls]
        return ", ".join(hits) if hits else "none"
    recovery = {"thresholds": {"recovered_min_mojofm": REC_OK, "failed_below_mojofm": REC_FAIL,
                               "post_hoc": True},
                "cs_classical_cells": rec_cells, "cs_classical_counts": rec_counts,
                "cs_classical_partial": rec_partial,
                "cs_llm": rec_llm_cs, "cs_floor": rec_floor_cs,
                "cpp_classical_cells": {f"{t}:{s}": _recovery(v[s])
                                        for t, v in classical_sar.items()
                                        for s in cpp_sys if s in v},
                "note": "post-hoc criterion (item 2.3): recovered >= 80 MoJoFM, failed < 50, "
                        "else partial; classical = ACDC/WCA/LIMBO (all-common) + ARC (own subset)"}
    # LLM-recovery baseline cost (item 7.3): $ per system over the five reruns at the uncached
    # rate, from the runner's own estimate in llm-recovery-<gran>.json (systems that ran only).
    llm_cost = by_system(rows, axis="fidelity", technique="llm", metric="cost_usd")

    # --- RQ3-A2: the no-curation floor vs the hand curation, by language (§13 confound) ---
    floor = by_system(rows, axis="A2", technique="none", metric="mojofm")
    llm = by_system(rows, axis="A2", technique="hand_curation", metric="mojofm")
    floor_cpp = [floor[s] for s in cpp_sys if s in floor]
    floor_cs = [floor[s] for s in cs_sys if s in floor]
    lift_cpp = [llm[s] - floor[s] for s in cpp_sys if s in llm and s in floor]
    lift_cs = [llm[s] - floor[s] for s in cs_sys if s in llm and s in floor]

    # --- RQ3-A5: edges the never-drop rule saves at the high threshold (load-bearing) ---
    a5_t8 = by_system(rows, axis="A5", technique="t8", metric="edges_saved")
    # A5 PAIRED ablation (§7.2): container edges surviving with the rule ON vs OFF at t8 (all
    # systems), and the §12 reference-edge RECALL on vs off (C# target-gran — the fidelity cost).
    a5_off_t8 = by_system(rows, axis="A5", technique="t8", metric="edges")   # prf recovered = OFF
    a5_on_t8 = {s: a5_off_t8[s] + a5_t8[s] for s in a5_off_t8 if s in a5_t8}
    a5_recall_on = by_system(rows, axis="A5", technique="t8", metric="recall_ref_on")
    a5_recall_off = by_system(rows, axis="A5", technique="t8", metric="recall_ref_off")
    a5_recall_drop = {s: round(a5_recall_on[s] - a5_recall_off[s], 4)
                      for s in a5_recall_on if s in a5_recall_off}
    # No Cliff's δ here: ON vs OFF is a PAIRED contrast (same systems), so the paired
    # rank-biserial r below is the effect size (item 5.5; the δ key was dropped 2026-08-31).
    a5_keys = sorted(s for s in a5_on_t8 if s in a5_off_t8)
    a5_rb_t8 = rank_biserial_paired([a5_on_t8[s] for s in a5_keys],
                                    [a5_off_t8[s] for s in a5_keys])
    # A5 at the OPERATING threshold (min_weight=3, the default) beside the extreme (t=8) — the
    # reviewer's point that "largest effect" must be read at the configuration users run
    # (item 5.7). Rows exist for t in {0,1,3,5,8}; t3 is the shipped default.
    a5_t3 = by_system(rows, axis="A5", technique="t3", metric="edges_saved")
    a5_off_t3 = by_system(rows, axis="A5", technique="t3", metric="edges")
    a5_on_t3 = {s: a5_off_t3[s] + a5_t3[s] for s in a5_off_t3 if s in a5_t3}
    a5_t3_keys = sorted(s for s in a5_on_t3 if s in a5_off_t3)
    a5_rb_t3 = rank_biserial_paired([a5_on_t3[s] for s in a5_t3_keys],
                                    [a5_off_t3[s] for s in a5_t3_keys])
    a5_recall_on_t3 = by_system(rows, axis="A5", technique="t3", metric="recall_ref_on")
    a5_recall_off_t3 = by_system(rows, axis="A5", technique="t3", metric="recall_ref_off")
    a5_recall_drop_t3 = {s: round(a5_recall_on_t3[s] - a5_recall_off_t3[s], 4)
                         for s in a5_recall_on_t3 if s in a5_recall_off_t3}

    # --- build-graph adequacy diagnostic (baselines/adequacy.py, axis=adequacy; item 2.1):
    # reference-free numbers of the floor partition, correlated (Spearman) with the all-common
    # floor fidelity over the scored corpus. The decision rule is POST HOC and says so. ---
    adq = {m: by_system(rows, axis="adequacy", technique="floor", metric=m)
           for m in ("unit_ratio", "largest_share", "mq", "entities", "units", "intra_share")}
    adq_fid = {m: {s: v for s, v in d.items() if s in systems} for m, d in adq.items()}
    ADQ_RATIO_MAX, ADQ_LARGEST_MAX = 0.25, 0.50
    adequate = sorted(s for s in systems if s in adq_fid["unit_ratio"]
                      and adq_fid["unit_ratio"][s] <= ADQ_RATIO_MAX
                      and adq_fid["largest_share"].get(s, 1.0) <= ADQ_LARGEST_MAX)
    inadequate = sorted(s for s in systems if s in adq_fid["unit_ratio"] and s not in adequate)
    ADQ_FLOOR_OK = 80.0
    rule_hits = [s for s in adq_fid["unit_ratio"]
                 if s in floor_mojo_ac
                 and ((s in adequate) == (floor_mojo_ac[s] >= ADQ_FLOOR_OK))]
    # The reference-less Tier-B systems (fmt, serilog, terminal; plan item 2.5 DO-LITE): the
    # diagnostic's range beyond the scored corpus. The rule is APPLIED to them (it needs no
    # reference) but they enter no correlation and no agreement count — there is nothing to
    # agree with. fmt is the informative case: a CMake C++ library the rule calls NOT
    # build-modular, i.e. the in-idiom counterexample the corpus otherwise lacks.
    tier_b = sorted(s for s in adq["unit_ratio"] if s not in systems)
    tier_b_adequate = sorted(s for s in tier_b
                             if adq["unit_ratio"][s] <= ADQ_RATIO_MAX
                             and adq["largest_share"].get(s, 1.0) <= ADQ_LARGEST_MAX)
    adequacy = {
        "by_system": {s: {m: adq[m].get(s) for m in adq} for s in sorted(adq["unit_ratio"])},
        "rule": {"unit_ratio_max": ADQ_RATIO_MAX, "largest_share_max": ADQ_LARGEST_MAX,
                 "floor_mojofm_ok": ADQ_FLOOR_OK, "post_hoc": True,
                 "adequate": adequate, "inadequate": inadequate,
                 "agrees_with_floor": f"{len(rule_hits)}/{len(adq_fid['unit_ratio'])}"},
        "tier_b": {"systems": tier_b, "adequate": tier_b_adequate,
                   "inadequate": sorted(s for s in tier_b if s not in tier_b_adequate),
                   "note": "reference-less systems: the rule applied, no fidelity to agree with"},
        "spearman_vs_floor": {
            "unit_ratio_vs_mojofm": spearman(adq_fid["unit_ratio"], floor_mojo_ac),
            "unit_ratio_vs_a2a": spearman(adq_fid["unit_ratio"], floor_a2a_ac),
            "largest_share_vs_mojofm": spearman(adq_fid["largest_share"], floor_mojo_ac),
            "mq_vs_mojofm": spearman(adq_fid["mq"], floor_mojo_ac),
            "mq_vs_a2a": spearman(adq_fid["mq"], floor_a2a_ac)},
        "note": "reference-independent: computed from the extracted facts + exported graph only "
                "(baselines/adequacy.py); the rule was chosen after seeing the data",
    }
    # Threshold intervals, leave-one-system-out re-selection, bootstrap CI (review 2026-09-04
    # §2; eval plan §12 amendment (h)): what the 9/9 agreement is worth.
    adequacy["robustness"] = adequacy_robustness(adq_fid, floor_mojo_ac, ADQ_FLOOR_OK,
                                                 ADQ_RATIO_MAX, ADQ_LARGEST_MAX)

    # --- RQ3 ablation tornado (§15.4 figure): median edge-set IMPACT (1 - edge_f1 vs the full
    # config) per design choice at its extreme setting — the visual "which knob actually matters".
    # A5 (declared-drop) tops it; A4 (down-weight-private) is ~0 (honest inert result). ---
    def _impact(axis, tech):
        xs = list(by_system(rows, axis=axis, technique=tech, metric="edge_f1",
                            projection="edge-vs-full").values())
        return round(med([1 - x for x in xs]), 4) if xs else None
    ablation_tornado = {"A5 declared-drop": _impact("A5", "t8"), "A1 L0-only": _impact("A1", "L0"),
                        "A3 min-weight=8": _impact("A3", "8"), "A4 no-down-weight": _impact("A4", "off")}

    # --- RQ3-A6: LLM-as-structure (§6.2 recovery) vs the deterministic pipeline. The A6 question
    # "does an LLM grouping *suggestion* help STRUCTURE?" is answered by the §6.2 baseline — hand
    # the LLM the same graph and let it emit the partition (at target gran the nodes ARE the build
    # targets, i.e. the LLM grouping the targets). Reported as the LLM MoJoFM and the paired
    # pipeline−LLM gap; the reproducibility half of the story (rerun stdev) lives in results.csv,
    # and itk's absence is the honest "graph exceeds one context" refusal (§6.2). ---
    a6_llm = by_system(rows, axis="fidelity", technique="llm", metric="mojofm")
    a6_pipe_minus_llm = {s: round(pipe_mojo[s] - a6_llm[s], 2)
                         for s in a6_llm if s in pipe_mojo}
    a6_pipeline_ge = sum(1 for s in a6_pipe_minus_llm if a6_pipe_minus_llm[s] >= 0)

    # --- RQ3-A7: LLM naming enrichment is structurally inert (baselines/rq3_a7_invariance.py). The
    # headline invariant (shared with RQ4): max structural_delta across the corpus is 0 while an
    # adversarial provider changed every name (names_changed > 0), so Δ=0 is not a vacuous no-op. ---
    a7_delta = by_system(rows, axis="A7", technique="enrichment", metric="structural_delta")
    a7_names = by_system(rows, axis="A7", technique="enrichment", metric="names_changed")

    # --- RQ3-A8 / RQ5: churn at 20% turnover — ACDC vs the per-entity (anon/dir) floor ---
    acdc_churn20 = by_system(rows, axis="A8", technique="acdc@0.20", metric="churn_mojofm")
    stru_churn20 = by_system(rows, axis="A8", technique="anon@0.20", metric="churn_mojofm")

    # --- RQ6 §8.1: seeded-mutation detection — precision/recall/F1 per operator (pooled
    # finding counts across systems, Wilson CIs per §12), rename-classification accuracy,
    # and the id_aliases partial-survival rate. ---
    mut_rows = [r for r in rows if r["axis"] == "RQ6-mut"]
    mut_systems = sorted({r["system"] for r in mut_rows})
    operators = sorted({r["technique"] for r in mut_rows})

    def _mut_sum(op: str | None, metric: str) -> int:
        return int(sum(vals(mut_rows, axis="RQ6-mut", technique=op, metric=metric)))

    def _prf(tp: int, fn: int, fp: int) -> dict:
        prec = tp / (tp + fp) if (tp + fp) else None
        rec = tp / (tp + fn) if (tp + fn) else None
        f1 = (2 * prec * rec / (prec + rec)
              if prec is not None and rec is not None and (prec + rec) else None)
        return {"tp": tp, "fn": fn, "fp": fp,
                "precision": round(prec, 4) if prec is not None else None,
                "recall": round(rec, 4) if rec is not None else None,
                "f1": round(f1, 4) if f1 is not None else None,
                "precision_ci95": wilson(tp, tp + fp),
                "recall_ci95": wilson(tp, tp + fn)}

    rq6_by_op = {}
    for op in operators:
        d = _prf(_mut_sum(op, "tp"), _mut_sum(op, "fn"), _mut_sum(op, "fp"))
        d["mutations"] = _mut_sum(op, "n")
        d["detected"] = _mut_sum(op, "detected")
        rq6_by_op[op] = d
    rq6_overall = _prf(_mut_sum(None, "tp"), _mut_sum(None, "fn"), _mut_sum(None, "fp"))
    rq6_n_mut = _mut_sum(None, "n")
    rq6_fp_per_run = round(_mut_sum(None, "fp") / rq6_n_mut, 3) if rq6_n_mut else None
    ren_tot = _mut_sum(None, "rename_total") + _mut_sum(None, "false_rename_total")
    ren_ok = _mut_sum(None, "rename_correct") + _mut_sum(None, "false_rename_clean")
    alias_tot = _mut_sum(None, "alias_total")
    alias_ok = _mut_sum(None, "alias_suppressed")

    # --- RQ6 §8.2: degradation correctness — pooled FP-suppression with the stopping
    # rules ON vs the coverage-naive OFF reading; McNemar on the discordant findings. ---
    deg_rows = [r for r in rows if r["axis"] == "RQ6-deg"]
    deg_systems = sorted({r["system"] for r in deg_rows})
    deg_modes = sorted({r["technique"] for r in deg_rows})
    rq6_deg = {}
    for mode in deg_modes:
        wb = int(sum(vals(deg_rows, technique=mode, metric="would_be_false")))
        sup = int(sum(vals(deg_rows, technique=mode, metric="suppressed")))
        leak = int(sum(vals(deg_rows, technique=mode, metric="leaked")))
        adv = vals(deg_rows, technique=mode, metric="advisory")
        rq6_deg[mode] = {"would_be_false": wb, "suppressed_on": sup, "leaked_on": leak,
                         "fp_suppression_rate": round(sup / wb, 4) if wb else None,
                         "suppression_ci95": wilson(sup, wb) if wb else None,
                         "advisory_runs": f"{int(sum(adv))}/{len(adv)}" if adv else None}
    deg_wb_all = sum(v["would_be_false"] for v in rq6_deg.values())
    deg_sup_all = sum(v["suppressed_on"] for v in rq6_deg.values())
    deg_leak_all = sum(v["leaked_on"] for v in rq6_deg.values())
    # discordant pairs: ON suppresses & OFF flags (b) vs ON flags & OFF suppresses (c=0
    # by construction — the naive reading never suppresses)
    deg_mcnemar_p = mcnemar_exact(deg_sup_all, 0)
    starved_honest = vals(deg_rows, metric="starved_honest")
    intact_viol = vals(deg_rows, metric="intact_still_violation")
    erosion = vals(deg_rows, metric="erosion_visible")

    # --- RQ6 combined (mutate/combined.py; item 4.3): per mode, pooled over systems, how
    # every injected finding surfaced under simultaneous evidence loss. The claim
    # "degradation never hides erosion" holds iff silent == 0 in every mode. ---
    comb_rows = [r for r in rows if r["axis"] == "RQ6-comb" and ":" not in r["technique"]
                 and r["projection"] == "combined"]
    # the same run under the pre-fix coverage convention (projection combined-prefix)
    prefix_rows = [r for r in rows if r["axis"] == "RQ6-comb"
                   and r["projection"] == "combined-prefix"]
    # per-mode totals only (technique without ":<operator>"), never the per-operator rows too
    prefix_silent = int(sum(float(r["value"]) for r in prefix_rows
                            if r["metric"] == "silent" and ":" not in r["technique"]))
    prefix_silent_ops = sorted({r["technique"].split(":", 1)[1] for r in prefix_rows
                                if ":" in r["technique"] and r["metric"] == "silent"
                                and float(r["value"]) > 0})
    comb_systems = sorted({r["system"] for r in comb_rows})
    comb_modes = sorted({r["technique"] for r in comb_rows})
    rq6_comb = {}
    for mode in comb_modes:
        cell = {m: int(sum(vals(comb_rows, technique=mode, metric=m)))
                for m in ("n", "findings", "drift", "gap", "advisory", "silent", "fp",
                          "mutations_silent", "advisory_runs")}
        f = cell["findings"]
        cell["silent_share"] = round(cell["silent"] / f, 4) if f else None
        cell["named_share"] = round((cell["drift"] + cell["gap"]) / f, 4) if f else None
        rq6_comb[mode] = cell
    MODE_STEM = {"lose_l0": "LoseLZero", "starve_l0_10": "StarveTen",
                 "starve_l0_30": "StarveThirty", "starve_mutated": "StarveMutated"}

    def _stem(mode: str) -> str:
        return MODE_STEM.get(mode, "".join(ch for ch in mode.title() if ch.isalpha()))
    comb_silent_total = sum(c["silent"] for c in rq6_comb.values())
    comb_silent_modes = sorted(m for m, c in rq6_comb.items() if c["silent"])
    # per-operator silent cells, for the prose ("which mutations went silent, where")
    comb_op_rows = [r for r in rows if r["axis"] == "RQ6-comb" and ":" in r["technique"]
                    and r["metric"] == "silent" and r["projection"] == "combined"]
    comb_silent_ops = sorted({r["technique"].split(":", 1)[1] for r in comb_op_rows
                              if float(r["value"]) > 0})

    # --- RQ6 real history (mutate/history.py; item 4.2): consecutive release pairs with
    # frozen rules; findings per pair, commit volume, and (once labelled) the raters'
    # verdicts. ---
    hist_rows = [r for r in rows if r["axis"] == "RQ6-hist"]
    hist_pairs = sorted({(r["system"], r["technique"]) for r in hist_rows})
    hist_systems = sorted({s for s, _ in hist_pairs})

    def _hsum(metric):
        return int(sum(vals(hist_rows, metric=metric)))
    hist_pending = any(float(r["value"]) > 0 for r in hist_rows
                       if r["metric"] == "labels_pending")
    HIST_LABELS = ("architectural", "non-architectural", "extraction-noise")
    # per-label counts = findings BOTH raters gave that label (`label_<lab>_agreed`, emitted by
    # the aggregator only once both worksheets carry labels); summing the per-rater rows
    # would double-count every finding
    hist_labels = {lab: _hsum(f"label_{lab}_agreed") for lab in HIST_LABELS}
    # Cohen's kappa on the POOLED 3x3 confusion matrix over all pairs (plan 4.2 promised
    # kappa; a per-pair kappa over 0-12 findings would be noise). None until labels exist or
    # when p_e == 1 (both raters used one label only), which the macro renders as n/a.
    hist_conf = {(la, lb): _hsum(f"labels_conf_{la}_{lb}") for la in HIST_LABELS
                 for lb in HIST_LABELS}
    hist_n = sum(hist_conf.values())
    hist_kappa = None
    if hist_n:
        p_o = sum(hist_conf[(l, l)] for l in HIST_LABELS) / hist_n
        p_e = sum((sum(hist_conf[(la, lb)] for lb in HIST_LABELS) / hist_n)
                  * (sum(hist_conf[(lb, la)] for lb in HIST_LABELS) / hist_n)
                  for la in HIST_LABELS)
        hist_kappa = round((p_o - p_e) / (1 - p_e), 3) if p_e < 1 else None
    hist_zero_pairs = [f"{s}:{t}" for s, t in hist_pairs
                       if sum(vals(hist_rows, system=s, technique=t, metric="drift_total")) == 0]
    # zero-finding pairs that DID touch build files: the informative negatives (a pair with no
    # build-file commit is a trivially true negative and is not counted here)
    hist_zero_build_pairs = [
        p for p in hist_zero_pairs
        if sum(vals(hist_rows, system=p.split(":")[0], technique=p.split(":", 1)[1],
                    metric="commits_build_files")) > 0]
    rq6_hist = {"systems": hist_systems, "n_systems": len(hist_systems),
                "n_pairs": len(hist_pairs),
                "pairs": [f"{s}:{t}" for s, t in hist_pairs],
                "commits_total": _hsum("commits_total"),
                "commits_build_files": _hsum("commits_build_files"),
                "findings": _hsum("drift_total"),
                "coverage_gaps": _hsum("coverage_gap_targets") + _hsum("coverage_gap_edges"),
                "advisory_pairs": _hsum("advisory"),
                "pairs_without_findings": hist_zero_pairs,
                "pairs_without_findings_with_build_churn": hist_zero_build_pairs,
                # rule-maintenance signal (item 4.5-lite): NEW first-party targets at the later
                # release that the frozen rules leave unmapped (needs_curation_b alone also counts
                # rules stale since the fixture, e.g. libxml2's 2.4.22 overlay)
                "new_targets_unmapped": _hsum("needs_curation_new_b"),
                "new_targets": _hsum("new_targets"),
                "labels": hist_labels, "labels_pending": hist_pending,
                "labels_agree": _hsum("labels_agree"),
                "labels_compared": _hsum("labels_compared"),
                "labels_kappa": hist_kappa,
                "labels_confusion": {f"{la}|{lb}": v for (la, lb), v in hist_conf.items()},
                "note": "committed rules held constant across each pair (they post-date the "
                        "studied releases for squidex and nopcommerce, pre-date them for "
                        "libxml2 — history-summary.json rules_note), L0-scoped runs; labels = "
                        "findings both raters gave the label; kappa pooled over all pairs "
                        "(pending until the worksheets are filled)"}

    # --- RQ7 §10: performance & scale — cold/warm, speedup, peak RSS, budgets, and the
    # log-log scaling slope of cold wall-clock vs KLOC (stdlib least squares). ---
    perf_rows = [r for r in rows if r["axis"] == "RQ7"]
    cold = by_system(perf_rows, technique="pipeline", metric="cold_s")
    warm = by_system(perf_rows, technique="pipeline", metric="warm_s")
    speedup = by_system(perf_rows, technique="pipeline", metric="speedup")
    rss_cold = by_system(perf_rows, technique="pipeline", metric="cold_peak_rss_mb")
    kloc = by_system(perf_rows, technique="pipeline", metric="kloc")
    cold_ok = vals(perf_rows, technique="pipeline", metric="cold_within_budget")
    warm_ok = vals(perf_rows, technique="pipeline", metric="warm_within_budget")

    def _share(prows, system, technique) -> float | None:
        """Percentage of a system's cold wall-clock spent in one stage/substep."""
        part = vals(prows, system=system, technique=technique, metric="cold_s")
        total = vals(prows, system=system, technique="pipeline", metric="cold_s")
        return round(100 * part[0] / total[0], 1) if part and total and total[0] else None

    def _loglog_slope(x: dict[str, float], y: dict[str, float]) -> float | None:
        import math
        pts = [(math.log(x[s]), math.log(y[s])) for s in x
               if s in y and x[s] > 0 and y[s] > 0]
        if len(pts) < 3:
            return None
        n = len(pts)
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
        return round(sxy / sxx, 3) if sxx else None

    out: dict = {
        "schema": "stats/v1",
        "corpus": {"systems": systems, "n_systems": len(systems),
                   "cpp": cpp_sys, "cs": cs_sys},
        "rq1": {"pipeline_mojofm_by_system": pipe_mojo, "pipeline_a2a_by_system": pipe_a2a,
                "median_mojofm": med(list(pipe_mojo.values())),
                "median_a2a": med(list(pipe_a2a.values())),
                "median_ari": med(list(pipe_ari.values()))},
        "rq2": {"median_mojofm": {**{t: med(list(v.values())) for t, v in tech_mojo_all.items()},
                                  **{t: med(list(v.values())) for t, v in tech_mojo_pair.items()
                                     if t not in tech_mojo_all}},
                "pipeline_a2a_wins": a2a_wins, "n_baseline_techniques": len(BASELINES),
                "wins": wins,
                "win_rule": "strict = candidate > every baseline in the set on that system; "
                            "ties = >=; classical = all-common ACDC/WCA/LIMBO/dir/cc; "
                            "all = + ARC (own subset) + LLM (rerun mean)",
                "median_ari": {t: med(list(v.values())) for t, v in ari_all.items()},
                "classical_ari_max": ({"value": classical_ari_max[0],
                                       "technique": classical_ari_max[1],
                                       "system": classical_ari_max[2]}
                                      if classical_ari_max else None),
                "floor_all_common": {"mojofm": floor_mojo_ac, "a2a": floor_a2a_ac,
                                     "ari": floor_ari_ac, "nested": floor_nested_ac,
                                     "median_ari_cpp": med([floor_ari_ac[s] for s in cpp_sys
                                                            if s in floor_ari_ac]),
                                     "median_ari_cs": med([floor_ari_ac[s] for s in cs_sys
                                                           if s in floor_ari_ac]),
                                     "median_mojofm_cpp": med([floor_mojo_ac[s] for s in cpp_sys
                                                               if s in floor_mojo_ac]),
                                     "median_mojofm_cs": med([floor_mojo_ac[s] for s in cs_sys
                                                              if s in floor_mojo_ac]),
                                     "median_a2a_cpp": med([floor_a2a_ac[s] for s in cpp_sys
                                                            if s in floor_a2a_ac]),
                                     "median_a2a_cs": med([floor_a2a_ac[s] for s in cs_sys
                                                           if s in floor_a2a_ac]),
                                     # the adequacy rule's build-modular set (item 2.2; audit
                                     # 2026-08-31): the number every "where the build system
                                     # composes many entities into few units" sentence quotes.
                                     # The language medians above include libxml2 and stay for
                                     # RQ1's explicitly labelled "C++ median".
                                     "adequate_systems": adequate,
                                     "median_mojofm_adequate": med([floor_mojo_ac[s] for s in adequate
                                                                    if s in floor_mojo_ac]),
                                     "median_a2a_adequate": med([floor_a2a_ac[s] for s in adequate
                                                                 if s in floor_a2a_ac]),
                                     "median_ari_adequate": med([floor_ari_ac[s] for s in adequate
                                                                 if s in floor_ari_ac])},
                "propose_all_common": {"mojofm": prop_mojo_ac, "a2a": prop_a2a_ac,
                                       "ari": prop_ari_ac,
                                       "median_ari_adequate": med([prop_ari_ac[s] for s in adequate
                                                                   if s in prop_ari_ac]),
                                       "median_mojofm": med(list(prop_mojo_ac.values())),
                                       "median_mojofm_cpp": med([prop_mojo_ac[s] for s in cpp_sys
                                                                 if s in prop_mojo_ac]),
                                       "median_mojofm_cs": med([prop_mojo_ac[s] for s in cs_sys
                                                                if s in prop_mojo_ac])},
                "cd_diagram": cd_diagram(tech_mojo_all, systems),
                # the same rectangle with the autonomous floor in place of the curated row
                # (review 2026-09-04 §1): verdicts read against `floor`
                "cd_diagram_floor": cd_diagram(
                    {**{t: v for t, v in tech_mojo_all.items() if t != "pipeline"},
                     "floor": floor_mojo_ac}, systems, focus="floor")},
        "adequacy": adequacy,
        "recovery": recovery,
        "rq3": {"floor_cpp_median": med(floor_cpp), "floor_cs_median": med(floor_cs),
                "curation_lift_cpp_median": med(lift_cpp), "curation_lift_cs_median": med(lift_cs),
                "a5_edges_saved_t8": a5_t8,
                "a5_edges_saved_t3": a5_t3,
                "a5_paired_t3": {"edges_on": a5_on_t3, "edges_off": a5_off_t3,
                                 "rank_biserial_on_vs_off": a5_rb_t3,
                                 "systems_moved": sorted(s for s in a5_t3 if a5_t3[s] > 0),
                                 "edges_saved_total": int(sum(a5_t3.values())),
                                 "reference_recall_drop": a5_recall_drop_t3,
                                 "note": "the OPERATING threshold (min_weight=3, the default)"},
                "a5_paired_t8": {"edges_on": a5_on_t8, "edges_off": a5_off_t8,
                                 "rank_biserial_on_vs_off": a5_rb_t8,
                                 "reference_recall_on": a5_recall_on,
                                 "reference_recall_off": a5_recall_off,
                                 "reference_recall_drop": a5_recall_drop,
                                 "note": "declared-never-dropped ON vs OFF at min_weight=8; "
                                         "reference_recall_* are the §12 fidelity cost (C# only)"},
                "a1": {"note": "edge/coverage sensitivity — see results.csv axis=A1"},
                "ablation_tornado": ablation_tornado,
                "a6_llm_structure": {"llm_mojofm_by_system": a6_llm,
                                     "pipeline_minus_llm": a6_pipe_minus_llm,
                                     "pipeline_ge_llm": f"{a6_pipeline_ge}/{len(a6_pipe_minus_llm)}",
                                     "median_llm_mojofm": med(list(a6_llm.values())),
                                     "note": "§6.2 LLM-recovery = the A6 'LLM does structure' arm; "
                                             "itk absent = refused-oversize (graph > 1 context)"},
                "a7_enrichment_inert": {
                    "structural_delta_by_system": a7_delta,
                    "max_structural_delta": max(a7_delta.values()) if a7_delta else None,
                    "names_changed_by_system": a7_names,
                    "n_systems": len(a7_delta)}},
        "rq5": {"acdc_churn_20pct": acdc_churn20, "anon_churn_20pct": stru_churn20,
                "acdc_churn_20pct_worst": med([min(acdc_churn20.values())]) if acdc_churn20 else None},
        "rq6": {
            "mutation": {"systems": mut_systems, "n_systems": len(mut_systems),
                         "n_mutations": rq6_n_mut, "by_operator": rq6_by_op,
                         "overall": rq6_overall, "fp_per_simulated_pr": rq6_fp_per_run,
                         "rename_classification": {
                             "correct": ren_ok, "total": ren_tot,
                             "accuracy": round(ren_ok / ren_tot, 4) if ren_tot else None,
                             "ci95": wilson(ren_ok, ren_tot)},
                         "alias_partial_survival": {
                             "suppressed": alias_ok, "total": alias_tot,
                             "rate": round(alias_ok / alias_tot, 4) if alias_tot else None},
                         "note": "seeded §8.1 operators, L0-scoped labels; counts pooled "
                                 "across systems, Wilson 95% CIs (§12)"},
            "degradation": {"systems": deg_systems, "n_systems": len(deg_systems),
                            "by_mode": rq6_deg,
                            "overall": {"would_be_false": deg_wb_all,
                                        "suppressed_on": deg_sup_all,
                                        "leaked_on": deg_leak_all,
                                        "fp_suppression_rate": (round(deg_sup_all / deg_wb_all, 4)
                                                                if deg_wb_all else None),
                                        "mcnemar_on_vs_off_p": deg_mcnemar_p},
                            "layering_starved_honest": (f"{int(sum(starved_honest))}/"
                                                        f"{len(starved_honest)}"
                                                        if starved_honest else None),
                            "layering_intact_still_violation": (f"{int(sum(intact_viol))}/"
                                                                f"{len(intact_viol)}"
                                                                if intact_viol else None),
                            "erosion_visible": (f"{int(sum(erosion))}/{len(erosion)}"
                                                if erosion else None),
                            "note": "§8.2 induced degradation; OFF = the coverage-naive "
                                    "reading of the same diff (counts coverage-gaps as "
                                    "drift, ignores the advisory flag)"}},
        "rq6_combined": {"systems": comb_systems, "n_systems": len(comb_systems),
                         "by_mode": rq6_comb, "silent_total": comb_silent_total,
                         "silent_modes": comb_silent_modes,
                         "silent_operators": comb_silent_ops,
                         "prefix_silent_total": prefix_silent,
                         "prefix_silent_operators": prefix_silent_ops,
                         "note": "mutate/combined.py: the §8.1 mutation set with evidence loss "
                                 "induced on the mutated run; one outcome per injected finding"},
        "rq6_history": rq6_hist,
        "rq7": {"systems": sorted(cold), "n_systems": len(cold),
                "cold_s_by_system": cold, "warm_s_by_system": warm,
                "speedup_by_system": speedup, "peak_rss_mb_by_system": rss_cold,
                "kloc_by_system": kloc,
                "median_speedup": med(list(speedup.values())),
                "max_cold_s": max(cold.values()) if cold else None,
                "max_peak_rss_mb": max(rss_cold.values()) if rss_cold else None,
                "cold_within_budget": (f"{int(sum(cold_ok))}/{len(cold_ok)}"
                                       if cold_ok else None),
                "warm_within_budget": (f"{int(sum(warm_ok))}/{len(warm_ok)}"
                                       if warm_ok else None),
                "scaling_slope_cold_vs_kloc": _loglog_slope(kloc, cold),
                "note": "cold/warm = first/second `arch run --no-llm --incremental` into a "
                        "fresh arch dir (baselines/rq7_perf.py); budgets P§1.1#6 "
                        "(cold L2/L3 30 min, warm 3 min); peak RSS = whole process tree"},
    }

    # --- Tier-P (anonymized industrial rows, §5.5/§14.3): descriptive only, kept apart ---
    p_systems = sorted({r["system"] for r in tier_p_rows})
    p_pipe_mojo = by_system(tier_p_rows, axis="fidelity", technique="pipeline", metric="mojofm")
    p_pipe_a2a = by_system(tier_p_rows, axis="fidelity", technique="pipeline", metric="a2a")
    # The anonymized channel carries the A2 rungs, whose `none` rung IS the no-curation floor and
    # `propose_all` the auto-propose draft (review 2026-09-13: "P-1 has no floor").
    p_floor_mojo = by_system(tier_p_rows, axis="A2", technique="none", metric="mojofm")
    p_floor_a2a = by_system(tier_p_rows, axis="A2", technique="none", metric="a2a")
    p_prop_mojo = by_system(tier_p_rows, axis="A2", technique="propose_all", metric="mojofm")
    out["tier_p"] = {
        "systems": p_systems, "n_systems": len(p_systems),
        "pipeline_mojofm_by_system": p_pipe_mojo, "pipeline_a2a_by_system": p_pipe_a2a,
        "median_mojofm": med(list(p_pipe_mojo.values())),
        "median_a2a": med(list(p_pipe_a2a.values())),
        "floor_mojofm_by_system": p_floor_mojo, "floor_a2a_by_system": p_floor_a2a,
        "propose_mojofm_by_system": p_prop_mojo,
        "median_floor_mojofm": med(list(p_floor_mojo.values())),
        "median_floor_a2a": med(list(p_floor_a2a.values())),
        "median_propose_mojofm": med(list(p_prop_mojo.values())),
        "note": "anonymized proprietary systems (eval §5.5) — corroborating only, "
                "excluded from every RQ statistic above (§14.3)",
    }

    # --- flat macros: every in-prose number (make_macros.py reads this verbatim) ---
    def fmt(x, suffix=""):
        return f"{x}{suffix}" if x is not None else "TBD"

    out["summary_macros"] = {
        "corpusSystemCount": len(systems),
        "corpusReferenceCount": len({r["system"] for r in rows if r["ref_grade"]}),
        "corpusCppCount": len(cpp_sys),
        "corpusCsCount": len(cs_sys),
        "rqOneMedianMoJo": fmt(out["rq1"]["median_mojofm"]),
        "rqOneMedianAtoA": fmt(out["rq1"]["median_a2a"]),
        "rqTwoBaselineCount": len(BASELINES),
        "rqTwoAcdcMoJo": fmt(out["rq2"]["median_mojofm"].get("acdc")),
        "rqTwoArcMoJo": fmt(out["rq2"]["median_mojofm"].get("arc")),
        "rqTwoWcaMoJo": fmt(out["rq2"]["median_mojofm"].get("wca")),
        "rqTwoLimboMoJo": fmt(out["rq2"]["median_mojofm"].get("limbo")),
        "rqTwoPipelineMoJo": fmt(out["rq2"]["median_mojofm"].get("pipeline")),
        # "lead" macros (items 1.1/5.1): <Row><Metric>Wins = STRICT vs the all-common classical
        # set; ...WinsOrTies = >=; ...WinsAll = strict vs every displayed method.
        **{f"rqTwo{row.capitalize()}{mt}{rule_name}":
           f"{wins[row][metric][rule]['wins']}/{wins[row][metric][rule]['scored']}"
           for row in ("pipeline", "floor", "propose")
           for metric, mt in (("a2a", "AtoA"), ("mojofm", "MoJo"), ("ari", "Ari"))
           for rule, rule_name in (("strict_vs_classical", "Wins"),
                                   ("ties_vs_classical", "WinsOrTies"),
                                   ("strict_vs_all", "WinsAll"))},
        "rqOneFloorCppMoJo": fmt(out["rq2"]["floor_all_common"]["median_mojofm_cpp"]),
        "rqOneFloorCsMoJo": fmt(out["rq2"]["floor_all_common"]["median_mojofm_cs"]),
        "rqOneFloorCppAtoA": fmt(out["rq2"]["floor_all_common"]["median_a2a_cpp"]),
        "rqOneFloorCsAtoA": fmt(out["rq2"]["floor_all_common"]["median_a2a_cs"]),
        "rqOneProposeMedianMoJo": fmt(out["rq2"]["propose_all_common"]["median_mojofm"]),
        "rqOneProposeCppMoJo": fmt(out["rq2"]["propose_all_common"]["median_mojofm_cpp"]),
        "rqOneProposeCsMoJo": fmt(out["rq2"]["propose_all_common"]["median_mojofm_cs"]),
        "rqOneProposeEshopMoJo": fmt(prop_mojo_ac.get("eshop")),
        "rqOneProposeOpencvMoJo": fmt(prop_mojo_ac.get("opencv")),
        "rqOneProposeAbseilMoJo": fmt(prop_mojo_ac.get("abseil")),
        "rqOneFloorItkAtoA": fmt(floor_a2a_ac.get("itk")),
        "rqOneLlmEshopMoJo": fmt(llm_mojo.get("eshop")),
        # the build-modular (adequacy-adequate) floor — what the abstract/intro/discussion quote
        "rqOneFloorAdequateMoJo": fmt(out["rq2"]["floor_all_common"]["median_mojofm_adequate"]),
        "rqOneFloorAdequateAtoA": fmt(out["rq2"]["floor_all_common"]["median_a2a_adequate"]),
        # ARI companions (post hoc, 2026-09-09): the floor under a metric that charges
        # over-splitting, the nested share that says it IS over-splitting, and what the
        # propose draft (which merges targets) restores.
        "rqOneFloorAdequateAri": fmt(out["rq2"]["floor_all_common"]["median_ari_adequate"]),
        "rqOneFloorCppAri": fmt(out["rq2"]["floor_all_common"]["median_ari_cpp"]),
        "rqOneFloorCsAri": fmt(out["rq2"]["floor_all_common"]["median_ari_cs"]),
        "rqOneFloorItkAri": fmt(floor_ari_ac.get("itk")),
        "rqOneFloorAbseilAri": fmt(floor_ari_ac.get("abseil")),
        "rqOneFloorBashAri": fmt(floor_ari_ac.get("bash")),
        "rqOneFloorOpencvAri": fmt(floor_ari_ac.get("opencv")),
        "rqOneFloorItkNested": fmt(floor_nested_ac.get("itk")),
        "rqOneFloorAbseilNested": fmt(floor_nested_ac.get("abseil")),
        "rqOneProposeItkAri": fmt(prop_ari_ac.get("itk")),
        "rqOneProposeAbseilAri": fmt(prop_ari_ac.get("abseil")),
        "rqOneProposeAdequateAri": fmt(out["rq2"]["propose_all_common"]["median_ari_adequate"]),
        "rqOneMedianAri": fmt(out["rq1"]["median_ari"]),
        "rqTwoClassicalAriMax": fmt(classical_ari_max[0] if classical_ari_max else None),
        "rqTwoClassicalAriMaxWhere": (f"{classical_ari_max[1]} on {_disp_sys(classical_ari_max[2])}"
                                      if classical_ari_max else "TBD"),
        "rqTwoLlmMedianAri": fmt(med(list(llm_ari.values())) if llm_ari else None),
        "rqTwoSarAriMax": fmt(sar_ari_max[0] if sar_ari_max else None),
        "rqTwoSarAriMaxWhere": (f"{sar_ari_max[1].upper()} on {_disp_sys(sar_ari_max[2])}"
                                if sar_ari_max else "TBD"),
        # recovery / failure criterion (item 2.3, post hoc; answers Q6)
        "rqTwoRecoveredMin": int(REC_OK),
        "rqTwoFailedBelow": int(REC_FAIL),
        "rqTwoCsClassicalCells": len(rec_cells),
        "rqTwoCsClassicalFailed": rec_counts["failed"],
        "rqTwoCsClassicalRecovered": rec_counts["recovered"],
        "rqTwoCsClassicalPartial": rec_counts["partial"],
        "rqTwoCsClassicalPartialList": ("; ".join(rec_partial) if rec_partial else "none"),
        "rqTwoLlmCsRecovered": _rec_list(rec_llm_cs, "recovered"),
        "rqTwoLlmCsPartial": _rec_list(rec_llm_cs, "partial"),
        "rqTwoLlmCsFailed": _rec_list(rec_llm_cs, "failed"),
        "rqTwoFloorCsRecovered": sum(1 for c in rec_floor_cs.values() if c == "recovered"),
        # LLM baseline cost (item 7.3)
        "rqTwoLlmSystemsRan": len(llm_mojo),
        "rqTwoLlmCostMaxUsd": fmt(max(llm_cost.values()) if llm_cost else None),
        "rqTwoLlmCostTotalUsd": fmt(round(sum(llm_cost.values()), 2) if llm_cost else None),
        # adequacy diagnostic (item 2.1)
        "rqAdequacyRuleAgreement": adequacy["rule"]["agrees_with_floor"],
        "rqAdequacyAdequateCount": len(adequate),
        "rqAdequacyInadequateCount": len(inadequate),
        "rqAdequacyRatioMax": ADQ_RATIO_MAX,
        "rqAdequacyLargestMax": ADQ_LARGEST_MAX,
        "rqAdequacyRhoRatioMoJo": fmt(adequacy["spearman_vs_floor"]["unit_ratio_vs_mojofm"]["rho"]),
        "rqAdequacyRhoRatioAtoA": fmt(adequacy["spearman_vs_floor"]["unit_ratio_vs_a2a"]["rho"]),
        "rqAdequacyRhoMqMoJo": fmt(adequacy["spearman_vs_floor"]["mq_vs_mojofm"]["rho"]),
        "rqAdequacyN": adequacy["spearman_vs_floor"]["unit_ratio_vs_mojofm"]["n"],
        # robustness of the post-hoc rule (review 2026-09-04 §2)
        "rqAdequacyRatioIntervalLo": fmt(adequacy["robustness"]["interval"]["unit_ratio"]["lo"]),
        "rqAdequacyRatioIntervalHi": fmt(adequacy["robustness"]["interval"]["unit_ratio"]["hi"]),
        "rqAdequacyRatioOnlyIntervalHi": fmt(
            adequacy["robustness"]["interval"]["unit_ratio_only"]["hi"]),
        "rqAdequacyRatioOnlyHiSystem": _disp_sys(
            adequacy["robustness"]["interval"]["unit_ratio_only"]["hi_system"]),
        "rqAdequacyLargestIntervalLo": fmt(
            adequacy["robustness"]["interval"]["largest_share"]["lo"]),
        "rqAdequacyLargestIntervalHi": fmt(
            adequacy["robustness"]["interval"]["largest_share"]["hi"]),
        "rqAdequacyLooCorrect": adequacy["robustness"]["loo"]["correct"],
        "rqAdequacyLooWrong": ", ".join(_disp_sys(s) for s in adequacy["robustness"]["loo"]["wrong"])
                              or "none",
        "rqAdequacyLooLargestClauseFolds": adequacy["robustness"]["loo"][
            "largest_clause_selected_in_folds"],
        "rqAdequacyRhoCiLo": fmt(adequacy["robustness"]["rho_bootstrap"]["ci95"][0]),
        "rqAdequacyRhoCiHi": fmt(adequacy["robustness"]["rho_bootstrap"]["ci95"][1]),
        "rqAdequacyRhoBootReps": adequacy["robustness"]["rho_bootstrap"]["reps"],
        "rqAdequacyLibxmlLargestShare": fmt(adq["largest_share"].get("libxml2")),
        "rqAdequacyLibxmlRatio": fmt(adq["unit_ratio"].get("libxml2")),
        "rqAdequacyCppRatioMax": fmt(max(adq_fid["unit_ratio"][s] for s in adequate)
                                     if adequate else None),
        "rqAdequacyCppLargestMax": fmt(max(adq_fid["largest_share"][s] for s in adequate)
                                       if adequate else None),
        # the reference-less Tier-B range (item 2.5 DO-LITE): the rule applied beyond the
        # scored corpus; per-system ratio / largest share / unit count for the prose
        "rqAdequacyTierBSystems": len(tier_b),
        "rqAdequacyTierBAdequateList": ", ".join(_sdisp_b.get(s, s) for s in tier_b_adequate)
                                       or "none",
        "rqAdequacyTierBInadequateList": ", ".join(_sdisp_b.get(s, s) for s in tier_b
                                                   if s not in tier_b_adequate) or "none",
        **{f"rqAdequacy{s.capitalize()}Ratio": fmt(adq["unit_ratio"].get(s)) for s in tier_b},
        **{f"rqAdequacy{s.capitalize()}Largest": fmt(adq["largest_share"].get(s))
           for s in tier_b},
        **{f"rqAdequacy{s.capitalize()}Units": fmt(int(adq["units"][s]) if s in adq["units"]
                                                  else None) for s in tier_b},
        **{f"rqAdequacy{s.capitalize()}Entities": fmt(int(adq["entities"][s])
                                                     if s in adq["entities"] else None)
           for s in tier_b},
        # A5 at the operating point (item 5.7)
        "rqThreeAfiveOperatingSystemsMoved": len(out["rq3"]["a5_paired_t3"]["systems_moved"]),
        "rqThreeAfiveOperatingSystemsChecked": len(a5_t3),
        "rqThreeAfiveOperatingEdgesSaved": out["rq3"]["a5_paired_t3"]["edges_saved_total"],
        # n/a, not TBD: nothing moved at t=3, so the paired statistic has no non-zero pairs
        "rqThreeAfiveOperatingRankBiserial": fmt(a5_rb_t3) if a5_rb_t3 is not None else "n/a",
        "rqThreeAfiveRankBiserial": fmt(a5_rb_t8),
        "rqThreeAfiveEshopRefRecallDropOperating": fmt(a5_recall_drop_t3.get("eshop")),
        # the reference-recall cost at t=8 on EVERY system where recall_ref exists (item 5.7
        # asked for all three, not eShop alone): "eShop 1.0, nopCommerce 0.02, OrchardCore 0.41"
        "rqThreeAfiveRefRecallDropList": (", ".join(
            f"{_sdisp.get(s, s)} {fmt(round(a5_recall_drop[s], 2))}"
            for s in sorted(a5_recall_drop, key=lambda s: -a5_recall_drop[s]))
            or "TBD"),
        "rqThreeAfiveRefRecallSystems": len(a5_recall_drop),
        # RQ6 per-mutation unit (item 4.4): mutations detected / injected
        "rqSixMutationsDetected": f"{_mut_sum(None, 'detected')}/{rq6_n_mut}",
        # RQ6 combined (item 4.3)
        "rqSixCombSystems": len(comb_systems),
        "rqSixCombModes": len(comb_modes),
        "rqSixCombMutations": fmt(max((c["n"] for c in rq6_comb.values()), default=None)),
        "rqSixCombFindings": fmt(max((c["findings"] for c in rq6_comb.values()), default=None)),
        "rqSixCombSilentTotal": comb_silent_total,
        "rqSixCombSilentModes": ", ".join(m.replace("_", "\\_") for m in comb_silent_modes) or "none",
        "rqSixCombSilentOps": ", ".join(o.replace("_", "\\_") for o in comb_silent_ops) or "none",
        "rqSixCombFpTotal": sum(c["fp"] for c in rq6_comb.values()),
        "rqSixCombPrefixSilentTotal": prefix_silent,
        "rqSixCombPrefixSilentOps": ", ".join(o.replace("_", "\\_") for o in prefix_silent_ops)
                                    or "none",
        **{f"rqSixComb{_stem(m)}Silent": rq6_comb[m]["silent"] for m in comb_modes},
        **{f"rqSixComb{_stem(m)}Gap": rq6_comb[m]["gap"] for m in comb_modes},
        **{f"rqSixComb{_stem(m)}Drift": rq6_comb[m]["drift"] for m in comb_modes},
        **{f"rqSixComb{_stem(m)}Advisory": rq6_comb[m]["advisory"] for m in comb_modes},
        # RQ6 real history (item 4.2)
        "rqSixHistSystems": len(hist_systems),
        "rqSixHistPairs": len(hist_pairs),
        "rqSixHistCommits": rq6_hist["commits_total"],
        "rqSixHistBuildCommits": rq6_hist["commits_build_files"],
        "rqSixHistFindings": rq6_hist["findings"],
        "rqSixHistCoverageGaps": rq6_hist["coverage_gaps"],
        "rqSixHistPairsZero": len(hist_zero_pairs),
        "rqSixHistPairsZeroBuildChurn": len(hist_zero_build_pairs),
        "rqSixHistNewTargets": rq6_hist["new_targets"],
        "rqSixHistNewUnmapped": rq6_hist["new_targets_unmapped"],
        "rqSixHistArchitectural": hist_labels["architectural"],
        "rqSixHistNonArchitectural": hist_labels["non-architectural"],
        "rqSixHistNoise": hist_labels["extraction-noise"],
        "rqSixHistLabelsAgree": (f"{rq6_hist['labels_agree']}/{rq6_hist['labels_compared']}"
                                 if rq6_hist["labels_compared"] else "TBD"),
        # pooled Cohen's kappa; n/a (not TBD) once labels exist but p_e == 1
        "rqSixHistLabelsKappa": (fmt(hist_kappa) if hist_kappa is not None
                                 else ("n/a" if rq6_hist["labels_compared"] else "TBD")),
        "rqSixHistLabelsPending": "pending" if hist_pending else "done",
        "rqTwoPipelineRank": fmt(out["rq2"]["cd_diagram"]["avg_ranks"].get("pipeline")),
        "rqTwoCriticalDifference": fmt(out["rq2"]["cd_diagram"]["cd"]),
        "rqThreeFloorCppMoJo": fmt(out["rq3"]["floor_cpp_median"]),
        "rqThreeFloorCsMoJo": fmt(out["rq3"]["floor_cs_median"]),
        # curation lift on the ALL-COMMON projection (curated − floor, same entity set as
        # Tables RQ1/RQ2 — item 5.6); the A2-rung medians stay in rq3.* for the artifact
        "rqThreeCurationLiftCpp": fmt(med([pipe_mojo[s] - floor_mojo_ac[s] for s in cpp_sys
                                           if s in pipe_mojo and s in floor_mojo_ac])),
        "rqThreeCurationLiftCs": fmt(med([pipe_mojo[s] - floor_mojo_ac[s] for s in cs_sys
                                          if s in pipe_mojo and s in floor_mojo_ac])),
        "rqFiveAcdcChurnMedian": fmt(med(list(acdc_churn20.values())) if acdc_churn20 else None),
        "rqFiveDirChurnMedian": fmt(med(list(by_system(rows, axis="A8", technique="dir@0.20",
                                                       metric="churn_mojofm").values())) or None),
        "rqThreeAfiveEshopEdgesSaved": fmt(int(a5_t8["eshop"]) if "eshop" in a5_t8 else None),
        "rqThreeSixLlmMedianMoJo": fmt(med(list(a6_llm.values())) if a6_llm else None),
        "rqThreeSixPipelineGeLlm": (f"{a6_pipeline_ge}/{len(a6_pipe_minus_llm)}"
                                    if a6_pipe_minus_llm else "TBD"),
        # the complement, as a count and a named list — never hand-typed in the prose (it
        # moved from 3/7 to 4/8 when the Squidex LLM run landed 2026-08-31)
        "rqThreeSixLlmGtPipeline": (f"{len(a6_pipe_minus_llm) - a6_pipeline_ge}"
                                    if a6_pipe_minus_llm else "TBD"),
        "rqThreeSixLlmSystemsRan": len(a6_pipe_minus_llm),
        "rqThreeSixLlmGtPipelineList": (", ".join(
            _sdisp.get(s, s) for s in sorted(a6_pipe_minus_llm) if a6_pipe_minus_llm[s] < 0)
            or "none"),
        # the C# side of the regime sentence (§VI): the best ALL-COMMON technique's C# median
        # and ARC's (own subset) — LIMBO is "the best" only inside the all-common set
        "rqTwoCsBestAllCommonName": _tdisp.get(cs_best_ac, f"\\texttt{{{cs_best_ac}}}"),
        "rqTwoCsBestAllCommonMoJo": fmt(cs_best_ac_med),
        "rqTwoCsAllCommonMedians": ", ".join(
            f"{_tdisp.get(t, t)} {fmt(cs_ac_med[t])}" for t in
            sorted(ALL_COMMON, key=lambda t: -(cs_ac_med[t] or 0))),
        "rqTwoArcCsMoJo": fmt(med([tech_mojo_pair.get("arc", {})[s] for s in cs_sys
                                   if s in tech_mojo_pair.get("arc", {})])),
        "rqThreeSevenMaxStructuralDelta": fmt(max(a7_delta.values()) if a7_delta else None),
        "rqThreeSevenSystemsChecked": len(a7_delta),
        "rqFiveAcdcChurnWorst": fmt(min(acdc_churn20.values()) if acdc_churn20 else None),
        # macro NAME must not carry the tool name either: the .tex source travels with the
        # anonymized artifact package (main.tex §1.4 #5), so it is the pipeline's churn.
        "rqFivePipelineChurn": fmt(med(list(stru_churn20.values())) if stru_churn20 else None),
        "tierPSystemCount": len(p_systems),
        "tierPMedianMoJo": fmt(out["tier_p"]["median_mojofm"]),
        "tierPMedianAtoA": fmt(out["tier_p"]["median_a2a"]),
        "tierPMedianFloorMoJo": fmt(out["tier_p"]["median_floor_mojofm"]),
        "tierPMedianFloorAtoA": fmt(out["tier_p"]["median_floor_a2a"]),
        "tierPMedianProposeMoJo": fmt(out["tier_p"]["median_propose_mojofm"]),
        # RQ6 (drift/governance)
        "rqSixMutationSystems": len(mut_systems),
        "rqSixMutationCount": rq6_n_mut,
        "rqSixOperatorCount": len(operators),
        "rqSixPrecision": fmt(rq6_overall["precision"]),
        "rqSixRecall": fmt(rq6_overall["recall"]),
        "rqSixFOne": fmt(rq6_overall["f1"]),
        "rqSixFpPerPr": fmt(rq6_fp_per_run),
        "rqSixRenameAccuracy": (fmt(round(ren_ok / ren_tot, 3)) if ren_tot else "TBD"),
        "rqSixAliasSurvival": (f"{alias_ok}/{alias_tot}" if alias_tot else "TBD"),
        "rqSixSuppressionRate": (fmt(round(deg_sup_all / deg_wb_all, 4))
                                 if deg_wb_all else "TBD"),
        "rqSixWouldBeFalse": fmt(deg_wb_all if deg_wb_all else None),
        # carries its own relation (= exact vs < bound) — the prose interpolates it
        # inside $...$ as $p\rqSixMcNemarP$, and an embedded $<$/$=$ would flip OUT
        # of math mode there and typeset < as ¡
        "rqSixMcNemarP": ("TBD" if deg_mcnemar_p is None
                          else "\\ensuremath{<}0.0001" if deg_mcnemar_p < 1e-4
                          else "\\ensuremath{=}" + fmt(deg_mcnemar_p)),
        "rqSixDegradationSystems": len(deg_systems),
        # RQ7 (performance/scale)
        "rqSevenSystems": len(cold),
        "rqSevenMaxColdMin": (fmt(round(max(cold.values()) / 60, 1)) if cold else "TBD"),
        "rqSevenMedianSpeedup": fmt(out["rq7"]["median_speedup"]),
        "rqSevenMaxPeakRssGb": (fmt(round(max(rss_cold.values()) / 1024, 1))
                                if rss_cold else "TBD"),
        "rqSevenColdBudget": fmt(out["rq7"]["cold_within_budget"]),
        "rqSevenWarmBudget": fmt(out["rq7"]["warm_within_budget"]),
        "rqSevenScalingSlope": fmt(out["rq7"]["scaling_slope_cold_vs_kloc"]),
        # the largest system's cold run, broken down (A.4): which substep dominates
        # the KLOC range behind "spanning three orders of magnitude". The minimum keeps one
        # decimal below 10 KLOC: the smallest perf system is the 0.1-KLOC toy fixture, and
        # int-rounding printed the range as "0--825 KLOC" (review 2026-09-07: "zero?").
        "rqSevenMinKloc": fmt((round(min(kloc.values()), 1) if min(kloc.values()) < 10
                               else int(round(min(kloc.values())))) if kloc else None),
        "rqSevenMaxKloc": fmt(int(round(max(kloc.values()))) if kloc else None),
        "rqSevenItkColdMin": (fmt(round(cold["itk"] / 60, 1)) if "itk" in cold else "TBD"),
        "rqSevenItkWarmMin": (fmt(round(warm["itk"] / 60, 1)) if "itk" in warm else "TBD"),
        "rqSevenItkDoxygenSharePct": fmt(_share(perf_rows, "itk", "substep:extract.doxygen-xml")),
        "rqSevenItkCmakeSharePct": fmt(_share(perf_rows, "itk", "substep:extract.cmake-graph")),
        "rqSevenItkClangSharePct": fmt(_share(perf_rows, "itk", "substep:extract.clang-scan-deps")),
        # an ESTIMATE, never measured (audit 2026-08-31): the cold run minus the two
        # source-level extractors (Doxygen XML, clang-scan-deps) — what an L0-only run would
        # take if nothing else changed. The prose must call it an estimate.
        "rqSevenItkLZeroImpliedMin": fmt(round(
            cold["itk"] / 60 * (1 - (
                (_share(perf_rows, "itk", "substep:extract.doxygen-xml") or 0)
                + (_share(perf_rows, "itk", "substep:extract.clang-scan-deps") or 0)) / 100), 1)
            if "itk" in cold else None),
    }

    # --- RQ9 §10a: blind curation (four-row table + effort; labels preserved) ---
    out["rq9"] = _rq9_block(rows, pipe_mojo)
    out["summary_macros"].update(_rq9_macros(out["rq9"], fmt))

    # --- inferential tests (gated on scipy; §12 pre-registration) ---
    if importlib.util.find_spec("scipy") is not None:
        # curated-vs-floor Wilcoxon on the all-common projection (same numbers as Table RQ1;
        # the A2-rung dicts `floor`/`llm` are another projection — audit 2026-08-31)
        out["inferential"] = _scipy_tests(tech_mojo_all, tech_mojo_pair, floor_mojo_ac, pipe_mojo,
                                          systems,
                                          a5_on_t8, a5_off_t8, a2a_all, floor_mojo_ac,
                                          floor_a2a_ac=floor_a2a_ac, propose_ac=prop_mojo_ac,
                                          propose_a2a_ac=prop_a2a_ac)
        ext = sorted(s for s in systems if s in CORPUS_EXTENSION)
        if ext:
            pre = _scipy_tests(tech_mojo_all, tech_mojo_pair, floor_mojo_ac, pipe_mojo,
                               [s for s in systems if s not in CORPUS_EXTENSION],
                               tech_a2a_all=a2a_all, floor_ac=floor_mojo_ac,
                               floor_a2a_ac=floor_a2a_ac, propose_ac=prop_mojo_ac,
                               propose_a2a_ac=prop_a2a_ac)
            out["inferential"]["corpus_extension"] = {
                "systems": ext,
                "friedman_mojofm_pre_extension": pre.get("friedman_mojofm"),
                "friedman_n_systems_pre_extension": pre.get("friedman_n_systems"),
                "friedman_floor_mojofm_pre_extension": pre.get("friedman_floor_mojofm"),
                "friedman_floor_mojofm_n_systems_pre_extension":
                    pre.get("friedman_floor_mojofm_n_systems"),
                "rule": "§12: report both; significance may not rest on the extension alone"}
    else:
        out["inferential"] = {"status": "skipped: scipy absent — install scipy + scikit-posthocs "
                              "for Friedman/Nemenyi/Wilcoxon (§12). Descriptive stats stand alone "
                              "(effect sizes over p-values, small N)."}
    # Effect sizes for the A2 curation contrast (item 5.5), on the SAME all-common projection as
    # Tables RQ1/RQ2 (audit 2026-08-31: the A2-rung values coincide but are another projection).
    # Curated-vs-floor is PAIRED (same systems) -> matched-pairs rank-biserial r. Cliff's delta
    # is kept ONLY for the one between-groups contrast, C++ floor vs C# floor; the former
    # `cliffs_delta_curation_cs` (curated vs floor on the same four systems) was a paired
    # contrast mislabelled as between-groups and is gone.
    cs_paired = [s for s in cs_sys if s in pipe_mojo and s in floor_mojo_ac]
    all_paired = [s for s in systems if s in pipe_mojo and s in floor_mojo_ac]
    out["rq3"]["rank_biserial_curation_cs"] = rank_biserial_paired(
        [pipe_mojo[s] for s in cs_paired], [floor_mojo_ac[s] for s in cs_paired])
    out["rq3"]["rank_biserial_curation_all"] = rank_biserial_paired(
        [pipe_mojo[s] for s in all_paired], [floor_mojo_ac[s] for s in all_paired])
    out["rq3"]["cliffs_delta_floor_cpp_vs_cs"] = cliffs_delta(
        [floor_mojo_ac[s] for s in cpp_sys if s in floor_mojo_ac],
        [floor_mojo_ac[s] for s in cs_sys if s in floor_mojo_ac])

    # --- inferential macros (append after the tests run; graceful TBD when scipy absent) ---
    inf = out["inferential"]
    out["summary_macros"].update({
        "rqTwoFriedmanP": (fmt(round(inf["friedman_mojofm"]["p"], 4))
                           if "friedman_mojofm" in inf else "TBD"),
        "rqTwoFriedmanN": fmt(inf.get("friedman_n_systems", "TBD")),
        "rqTwoFriedmanPPreExtension": (
            fmt(round(inf["corpus_extension"]["friedman_mojofm_pre_extension"]["p"], 4))
            if (inf.get("corpus_extension") or {}).get("friedman_mojofm_pre_extension")
            else "TBD"),
        "rqTwoFriedmanNPreExtension": fmt((inf.get("corpus_extension") or {})
                                          .get("friedman_n_systems_pre_extension", "TBD")),
        "rqThreeCurationWilcoxonP": (fmt(round(inf["wilcoxon_curation_vs_floor"]["p"], 4))
                                     if "wilcoxon_curation_vs_floor" in inf else "TBD"),
        "rqThreeFloorCliffsCppVsCs": fmt(out["rq3"]["cliffs_delta_floor_cpp_vs_cs"]),
        "rqThreeCurationRankBiserialCs": fmt(out["rq3"]["rank_biserial_curation_cs"]),
        "rqThreeCurationRankBiserialAll": fmt(out["rq3"]["rank_biserial_curation_all"]),
        # post-hoc pairs (item 5.4): pipeline vs each all-common technique, Holm-corrected
        "rqTwoPosthocDiffers": _posthoc_list(inf, True),
        "rqTwoPosthocNotDiffers": _posthoc_list(inf, False),
        "rqTwoNemenyiDiffers": _nemenyi_list(out["rq2"]["cd_diagram"], True),
        "rqTwoNemenyiNotDiffers": _nemenyi_list(out["rq2"]["cd_diagram"], False),
        "rqTwoFriedmanAtoAP": (fmt(round(inf["friedman_a2a"]["p"], 4))
                               if "friedman_a2a" in inf else "TBD"),
        # autonomous-row rectangles (review 2026-09-04 §1): the inferential claim rests here;
        # the curated-row Friedman above is descriptive (assisted vs unassisted)
        "rqTwoFriedmanFloorP": _p_macro(inf, "friedman_floor_mojofm", fmt),
        "rqTwoFriedmanFloorAtoAP": _p_macro(inf, "friedman_floor_a2a", fmt),
        "rqTwoFriedmanFloorN": fmt(inf.get("friedman_floor_mojofm_n_systems", "TBD")),
        "rqTwoFriedmanFloorPPreExtension": _p_macro(
            inf.get("corpus_extension") or {}, "friedman_floor_mojofm_pre_extension", fmt),
        "rqTwoFriedmanProposeP": _p_macro(inf, "friedman_propose_mojofm", fmt),
        "rqTwoFriedmanProposeAtoAP": _p_macro(inf, "friedman_propose_a2a", fmt),
        "rqTwoFriedmanProposeN": fmt(inf.get("friedman_propose_mojofm_n_systems", "TBD")),
        "rqTwoFloorPosthocDiffers": _posthoc_list(inf, True, "floor"),
        "rqTwoFloorPosthocNotDiffers": _posthoc_list(inf, False, "floor"),
        "rqTwoProposePosthocDiffers": _posthoc_list(inf, True, "propose"),
        "rqTwoProposePosthocNotDiffers": _posthoc_list(inf, False, "propose"),
        "rqTwoFloorNemenyiDiffers": _nemenyi_list(out["rq2"]["cd_diagram_floor"], True),
        "rqTwoFloorNemenyiNotDiffers": _nemenyi_list(out["rq2"]["cd_diagram_floor"], False),
        "rqThreeAfiveWilcoxonP": (fmt(round(inf["wilcoxon_a5_on_vs_off"]["p"], 4))
                                  if "wilcoxon_a5_on_vs_off" in inf else "TBD"),
        # the A5 test's N: systems whose edge COUNT moved at t8 (review 2026-09-04: the p
        # belongs to this 7-system contrast, not to the 3-system reference-recall drop)
        "rqThreeAfiveWilcoxonN": fmt(inf.get("a5_n_moved", "TBD")),
        "rqThreeAfiveEshopRefRecallDrop": fmt(a5_recall_drop.get("eshop")),
    })
    return out


def _p_macro(inf: dict, key: str, fmt) -> str:
    return fmt(round(inf[key]["p"], 4)) if inf.get(key) else "TBD"


def _disp_sys(s: str | None) -> str:
    names = {"itk": "ITK", "opencv": "OpenCV", "eshop": "eShop", "orchardcore": "OrchardCore",
             "nopcommerce": "nopCommerce", "squidex": "Squidex"}
    return names.get(s, s) if s else "none"


def _posthoc_list(inf: dict, significant: bool, row: str = "pipeline") -> str:
    """Comma list of all-common techniques the given pipeline row (curated ``pipeline``, or the
    autonomous ``floor`` / ``propose``) does / does not differ from under the Holm-corrected
    pairwise Wilcoxon (item 5.4); TBD when the tests did not run."""
    pairs = (inf.get("posthoc_wilcoxon_holm") or {}).get(row) or {}
    names = {"acdc": "ACDC", "wca": "WCA", "limbo": "LIMBO", "dir": "dir", "cc": "cc"}
    hits = [names.get(t, t) for t in ("acdc", "wca", "limbo", "dir", "cc")
            if t in pairs and pairs[t]["significant"] == significant]
    return (", ".join(hits) if hits else "none") if pairs else "TBD"


def _nemenyi_list(cd: dict, significant: bool) -> str:
    pairs = cd.get("vs_pipeline") or {}
    names = {"acdc": "ACDC", "wca": "WCA", "limbo": "LIMBO", "dir": "dir", "cc": "cc"}
    hits = [names.get(t, t) for t in ("acdc", "wca", "limbo", "dir", "cc")
            if t in pairs and pairs[t]["significant"] == significant]
    return (", ".join(hits) if hits else "none") if pairs else "TBD"


def _holm(pvals: dict[str, float]) -> dict[str, float]:
    """Holm–Bonferroni step-down adjusted p-values (monotone)."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    adj, running = {}, 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        adj[k] = running
    return adj


def _scipy_tests(tech_mojo_all, tech_mojo_pair, floor, llm, systems,
                 a5_on=None, a5_off=None, tech_a2a_all=None,
                 floor_ac=None, floor_a2a_ac=None, propose_ac=None,
                 propose_a2a_ac=None) -> dict:  # pragma: no cover
    from scipy import stats as ss
    res: dict = {"status": "ran"}
    # The Friedman rectangle holds ONLY the all-common techniques (identical entity projection);
    # ARC is scored on its own covered subset and is reported descriptively, never inside the
    # paired test (review response 2026-08-30, item 5.3). tech_mojo_pair is kept in the
    # signature for the descriptive medians only.
    # This first rectangle carries the CURATED row (`pipeline`), three of whose C# members were
    # authored with the reference in view; since the 2026-09-04 review (§1) it is DESCRIPTIVE
    # only (assisted vs unassisted) and the paper's inferential claim rests on the autonomous
    # rectangles below (floor / propose in place of the curated row).
    cols = dict(tech_mojo_all)
    common = [s for s in systems if all(s in v for v in cols.values())]
    if len(common) >= 3 and len(cols) >= 3:
        arrays = [[cols[t][s] for s in common] for t in cols]
        res["friedman_mojofm"] = dict(zip(("stat", "p"), ss.friedmanchisquare(*arrays)))
        res["friedman_n_systems"] = len(common)
        res["friedman_techniques"] = sorted(cols)
    if tech_a2a_all:
        acols = dict(tech_a2a_all)
        acommon = [s for s in systems if all(s in v for v in acols.values())]
        if len(acommon) >= 3 and len(acols) >= 3:
            arrays = [[acols[t][s] for s in acommon] for t in acols]
            res["friedman_a2a"] = dict(zip(("stat", "p"), ss.friedmanchisquare(*arrays)))
            res["friedman_a2a_n_systems"] = len(acommon)
    # Autonomous-row rectangles (review 2026-09-04 §1; eval plan §12 amendment (h)): the
    # all-common classical set plus ONE pipeline row that no human touched — the no-curation
    # floor (every scored system) or the auto-propose draft (the systems it ran on). Keys:
    # friedman_<row>_<metric>[_n_systems|_techniques].
    classical = {t: v for t, v in tech_mojo_all.items() if t != "pipeline"}
    classical_a2a = {t: v for t, v in (tech_a2a_all or {}).items() if t != "pipeline"}
    for row_name, mojo_row, a2a_row in (("floor", floor_ac, floor_a2a_ac),
                                        ("propose", propose_ac, propose_a2a_ac)):
        for metric, row, base in (("mojofm", mojo_row, classical),
                                  ("a2a", a2a_row, classical_a2a)):
            if not row or not base:
                continue
            rect = {**base, row_name: row}
            rcommon = [s for s in systems if all(s in v for v in rect.values())]
            if len(rcommon) >= 3 and len(rect) >= 3:
                arrays = [[rect[t][s] for s in rcommon] for t in rect]
                res[f"friedman_{row_name}_{metric}"] = dict(
                    zip(("stat", "p"), ss.friedmanchisquare(*arrays)))
                res[f"friedman_{row_name}_{metric}_n_systems"] = len(rcommon)
                res[f"friedman_{row_name}_{metric}_techniques"] = sorted(rect)
    # Post-hoc pairwise Wilcoxon (pipeline vs each all-common technique, and the autonomous
    # floor / propose rows vs each) with Holm correction across the family (item 5.4).
    # Zero-difference pairs are dropped by the signed-rank test; N is reported per pair.
    posthoc: dict = {}
    for row_name, row_vals in (("pipeline", tech_mojo_all.get("pipeline")),
                               ("floor", floor_ac), ("propose", propose_ac)):
        if not row_vals:
            continue
        raw, meta = {}, {}
        for t, tv in tech_mojo_all.items():
            if t == "pipeline":
                continue
            pairs = [(row_vals[s], tv[s]) for s in systems
                     if s in row_vals and s in tv and row_vals[s] != tv[s]]
            if len(pairs) < 2:
                continue
            a, b = zip(*pairs)
            try:
                stat, p = ss.wilcoxon(a, b)
            except ValueError:
                continue
            raw[t] = float(p)
            meta[t] = {"n": len(pairs), "stat": float(stat), "p_raw": round(float(p), 4),
                       "rank_biserial": rank_biserial_paired(list(a), list(b))}
        adj = _holm(raw)
        posthoc[row_name] = {t: {**meta[t], "p_holm": round(adj[t], 4),
                                 "significant": adj[t] < 0.05} for t in raw}
    if posthoc:
        res["posthoc_wilcoxon_holm"] = posthoc
    paired = [(llm[s], floor[s]) for s in systems if s in llm and s in floor]
    if len(paired) >= 3:
        a, b = zip(*paired)
        res["wilcoxon_curation_vs_floor"] = dict(zip(("stat", "p"), ss.wilcoxon(a, b)))
    # A5 paired ablation (declared-never-dropped ON vs OFF at t8): Wilcoxon over the systems whose
    # edge count actually moved (zero-diff pairs are dropped by the signed-rank test). Small N —
    # read WITH the per-system deltas + Cliff's δ (§12), never the p-value alone.
    if a5_on and a5_off:
        pairs = [(a5_on[s], a5_off[s]) for s in a5_on if s in a5_off and a5_on[s] != a5_off[s]]
        if len(pairs) >= 1:
            a, b = zip(*pairs)
            res["a5_n_moved"] = len(pairs)
            if len(pairs) >= 2:
                res["wilcoxon_a5_on_vs_off"] = dict(zip(("stat", "p"), ss.wilcoxon(a, b)))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = pathlib.Path(__file__).resolve().parent
    ap.add_argument("--results", type=pathlib.Path, default=here / "results.csv")
    ap.add_argument("--out", type=pathlib.Path, default=here / "stats.json")
    ap.add_argument("--allow-missing-scipy", action="store_true",
                    help="write stats.json even when scipy is absent (inferential block "
                         "'skipped', Friedman/Wilcoxon macros TBD). Off by default: a "
                         "regeneration under the wrong interpreter must fail loudly, not "
                         "silently blank the paper's p-values (happened 2026-08-30).")
    args = ap.parse_args(argv)

    if not args.results.exists():
        print(f"no {args.results} — run aggregate_results.py first", file=sys.stderr)
        return 1
    # Guard: the committed stats.json/macros.tex carry the §12 inferential tests, so the
    # CLI refuses to overwrite them from an interpreter that cannot compute them. Library
    # callers (compute_stats) keep the graceful degradation; only the file-writing path is
    # strict. Check `sys.executable` first — the usual cause is the system python instead
    # of .venv/Scripts/python.
    if importlib.util.find_spec("scipy") is None and not args.allow_missing_scipy:
        print(f"scipy is not importable from {sys.executable}; refusing to write {args.out} "
              "with the inferential tests skipped (Friedman/Wilcoxon macros would become TBD).\n"
              "Run with the project venv (.venv/Scripts/python) or `pip install -e .[stats]`, "
              "or pass --allow-missing-scipy to write the degraded file on purpose.",
              file=sys.stderr)
        return 2
    stats = compute_stats(load(args.results))
    args.out.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                        newline="\n")
    print(f"wrote {args.out} ({len(stats['summary_macros'])} macros)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
