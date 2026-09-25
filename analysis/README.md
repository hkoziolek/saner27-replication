# `analysis/` — raw experiment data → paper tables, figures, macros

Implements the data→paper pipeline from the evaluation plan §15. Turns the raw
`out/**` experiment tree (plan §14.2) into the committed-but-generated artifacts
under `../latex/`.

```
out/**  ──aggregate_results.py──▶ results.csv  (tidy long format: one row per
                                                 system × technique/rung × metric)
results.csv ──stats.py──────────▶ stats.json   (descriptive medians + summary_macros;
                                                 Friedman/Wilcoxon GATED on scipy — §12)
results.csv ──make_tables.py────▶ ../latex/tables/*.tex     (booktabs)
                                 + fidelity-by-system.csv   (system x technique x projection pivot: MoJoFM, a2a, ARI + nested share (post hoc, 2026-09-09), k, LLM rerun sd; review item 5.8 — c2c is NOT scored)
results.csv+stats ─make_figures.py▶ ../latex/figures/*.pgf  (matplotlib pgf — STUB, needs matplotlib)
stats.json  ──make_macros.py────▶ ../latex/generated/macros.tex  (every in-prose number)
```

Run order (or `make all` in this directory):
```
python aggregate_results.py     # out/** + references/** -> results.csv
                                #   [--anon export-anon/results-anon.csv]  (Tier-P merge, repeatable)
python stats.py                 # results.csv -> stats.json (+ summary_macros)
                                # needs scipy (the [stats] extra): refuses to write without it
                                # unless --allow-missing-scipy -- use .venv/Scripts/python
python make_tables.py           # results.csv -> ../latex/tables/*.tex
python make_macros.py           # stats.json  -> ../latex/generated/macros.tex
python make_figures.py          # stats.json  -> ../latex/figures/*.pgf  (self-contained TikZ)
```

**Status (2026-08-30): live for RQ1/RQ2/RQ3/RQ5/RQ6/RQ7 + RQ9, RQ3 fully closed (A1–A8).**
RQ9 (blind curation, eval §10a) aggregates from the *tracked* `../references/<sys>/blind-curation/`
(axis `RQ9`; `../out/<sys>/blind-curation/` is only a per-file fallback) — the ceiling row,
each-rater-vs-reference (W3a), partition nesting (`boundary_conflicts`/`merges`/`splits` — the
granularity-vs-structure split MoJoFM cannot make), curator effort, and the four-row table whose
§10a.7 detection/expression label *is* the `projection`. `stats.py` derives the `rq9` block
(four-row numbers, gap-to-ceiling, effort, rater boundary conflicts) and the `\rqNine<System>…`
macros; `make_tables.py` emits `rq9_curation.tex` (the four-row table, + the consensus sensitivity
row where one exists) and `rq9_effort.tex`.
**Squidex admitted to RQ1/RQ2 (2026-08-30)** with the reference-blind RQ9 curation as its
`pipeline` row (`comparison-target.json` rebuilt with `--arch-dir out/squidex-rq9/architecture`;
Table RQ1 marks it `$^\S$`). Its arrival moves the RQ2 Friedman p across 0.05 (0.054 on 8 systems →
0.012 on 9), so `stats.py` reports both (`CORPUS_EXTENSION`; macros `\rqTwoFriedmanP` and
`\rqTwoFriedmanPPreExtension`) — §12 rule: the significance claim never rests on the ninth system.
`RQ9_ONLY` in `aggregate_results.py` is the (now empty) gate for future RQ9-only systems.
`aggregate_results.py`, `stats.py`, `make_tables.py`, `make_macros.py` are real and emit from the
committed `out/**` baseline artifacts (772 rows / 6 fidelity systems). `make_figures.py` renders two
self-contained TikZ figures from `stats.json` — `cd_diagram` (RQ2) and `ablation_tornado` (RQ3);
`drift_pr`/`scaling`/`human` stay scaffolds until RQ6/RQ7/RQ8 land. **RQ3 now carries all eight axes**:
A1 (evidence layers, faithful-path-validated — see `baselines/rq3_a1_faithful_check.py`), A2 (curation
rungs), **A3/A4/A5 with §12-pre-registered reference-edge P/R/F1** (`projection=edge-vs-reference`,
C# target-gran only — C++ file-gran references are skipped, honest boundary), **A6** (LLM-as-structure,
via the §6.2 recovery baseline), **A7** (`baselines/rq3_a7_invariance.py`: enrichment structural
`Δ=0`), A8 (stability/churn).

**STDLIB ONLY by design.** The aggregate/stats/tables/figures stages use only the standard library
(`csv`, `json`, `statistics`; figures are hand-emitted TikZ — **no matplotlib**) so the pipeline runs
in CI and anywhere, deterministically (same `out/**` → byte-identical `results.csv`). The **one**
opt-in heavy piece is `scipy`/`scikit-posthocs` for the §12 inferential tests
(Friedman/Nemenyi/Wilcoxon/Cliff's δ): install `pip install -e ".[stats]"` and run the stages with the
**venv python** to regenerate the committed inferential `stats.json` (`inferential.status="ran"` +
`rqTwoFriedmanP`/`rqThreeAfiveWilcoxonP`/… macros). Without it `stats.py` records
`skipped: scipy absent` and those macros resolve to `TBD` — a graceful, tested degradation.

## Reporting rules baked in (from the 2026-06 findings — plan §1.2a/§4.1/§13)
- **a2a-led.** RQ1 leads with a2a; MoJoFM is secondary (it is beaten by the naïve `dir`
  baseline on directory-aligned C++, and pipeline a2a wins 6/6).
- **no-curation floor shown.** Every fidelity row carries the A2 `none` rung (build graph only)
  beside the curated number, separating *pipeline* from *curator* (the §13 confound).
- **projection labelled, never silent.** Pipeline/ACDC/dir/cc use the pre-registered **all-common**
  entity projection (§6.4); ARC/WCA/LIMBO are currently **pairwise** (the committed
  `comparison-*.json` predates them) — footnoted in `rq2_baselines.tex`. *Open TODO: re-run the
  comparison with all six baselines so the whole RQ2 row shares one projection.*
- **opencv structure-vs-presentation override.** The committed `comparison-*.json` scores opencv's
  deliberately-coarse 4-family GUI curation (MoJoFM 59.6); the structural headline is the 14-module
  recovery (100/100, `overlays/opencv-mod`). `aggregate_results.py` substitutes the module-gran
  number as `pipeline` and keeps the 4-family value as `pipeline_4family` (both in `results.csv`).
- **Tier-P marked, never blended (plan §5.5/§14.3 — landed 2026-07-13).** [anonymized]-side
  `results-anon.csv` exports (produced by `anonymize_results.py`, see
  `../docs/tier-p-baseline-kit.md`) merge via `aggregate_results.py --anon` with `tier=P`;
  `make_tables.py` renders those rows *after* the OSS corpus, name-marked `$^\ddagger$` with a
  non-replicable caption footnote (with no Tier-P rows the tables are byte-identical to before);
  `stats.py` **excludes** `tier=P` from every pre-registered RQ statistic — they get their own
  descriptive `tier_p` block + `tierP*` macros only (corroborate, never carry).

## Tidy schema (`results.csv`)
| column | meaning |
|---|---|
| `system` | subject system id (matches harness manifest) |
| `language` | `cpp` \| `cs` |
| `granularity` | `file` (C++) \| `target` (C#) |
| `ref_grade` / `ref_clusters` / `ref_entities` | reference provenance (from `references/<sys>/provenance.json`) |
| `axis` | `fidelity` (RQ1/RQ2, incl. `technique=llm` = the A6 LLM-as-structure arm, and the autonomous `floor` / `propose` rows on the all-common projection) \| `A1`..`A5` \| `A7` \| `A8` \| `RQ6-mut` \| `RQ6-deg` \| `RQ6-comb` (mutation + evidence loss at once, eval §8.2a; `projection=combined` = current engine, `combined-prefix` = the preserved pre-`_coverage_l0`-fix run) \| `RQ6-hist` (consecutive real release pairs, eval §8.3; `technique=<A>..<B>`; rater labels, agreed counts, and the pooled 3×3 confusion cells `labels_conf_<a>_<b>` for Cohen's κ) \| `RQ7` \| `RQ9` (blind curation, eval §10a) \| `adequacy` (reference-free build-graph diagnostic of the floor, `technique=floor`, `projection=reference-free`; Tier-B systems included) \| `size` (first-party target count from `extracted-facts.json`) |
| `technique` | `pipeline` \| `acdc`/`arc`/`wca`/`limbo`/`dir`/`cc`/`llm` (axis=fidelity); a curation rung (axis=A2); a config label `L0`/`t8`/`off` (axis=A1/A3/A4/A5); `enrichment` (axis=A7); `<tech>@<fraction>` (axis=A8); `ceiling` / `rater:<R>` / `consensus` / `curator[:<C>]` / `floor` / `sar:<tech>` (axis=RQ9) |
| `metric` | `mojofm` \| `a2a` \| `ari` \| `nested` (post hoc 2026-09-09, eval plan §12 (m): adjusted Rand index ×100 and the share of clusters nested in one reference cluster; descriptive, never under an inferential test) \| `clusters` \| `edge_f1` \| `edges` \| `edges_saved` \| `churn_mojofm` \| `entities_common` \| `f1_ref`/`precision_ref`/`recall_ref`/`recall_ref_on`/`recall_ref_off` (A3/A4/A5 vs reference) \| `structural_delta`/`names_changed` (A7) |
| `value` | numeric value |
| `projection` | `all-common` \| `pairwise` \| `common-a2` \| `edge-vs-full` (A1/A3/A4/A5 sensitivity) \| `edge-vs-reference` (§12-pre-registered A3/A4/A5 fidelity, C# target-gran) \| `structural-invariant` (A7) \| `llm-rerun-mean` (A6) \| `perturb` \| `module-gran` \| RQ9: `rater-pair` / `rater-vs-reference` / `partition` / `vs-reference` / `effort` / **`detection` / `expression`** (the §10a.7 label, verbatim from `fidelity.json`; suffixed `-vs-consensus` for the sensitivity scoring) |
| `source` | the raw artifact the row came from (auditability) |
| `tier` | `oss` (replicable corpus) \| `P` (anonymized Tier-P via `--anon`; excluded from RQ stats) |
