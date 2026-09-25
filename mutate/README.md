# `mutate/` — RQ6 drift & governance harness (eval plan §8)

Four experiments, three entry points:

```powershell
.venv\Scripts\python -m mutate.run_rq6                    # §8.1 mutations + §8.2 degradation
.venv\Scripts\python -m mutate.run_rq6 eshop --mode mutation --instances 4
.venv\Scripts\python -m mutate.run_rq6 --mode degradation # fact-level, all 8 corpus systems
.venv\Scripts\python -m mutate.combined                   # §8.2a mutation AND degradation at once
.venv\Scripts\python -m mutate.history libxml2           # §8.3 real release pairs, frozen rules
```

## §8.1 Seeded mutation injection (`harness.py` + `operators/*.py`)

Per system: the pristine `fixtures/<system>` clone is **copied** to a work area, a
**baseline** is produced by the real pipeline (`arch extract` + `arch curate`, in-process),
then each operator applies a labelled build-file mutation, the pipeline re-runs, and
`drift_report.evaluate(baseline)` is scored against the injected label. Artifacts
(§14.1): `out/<system>/mutation/<id>/{label.json, result.json, drift.md}` plus the
aggregate `mutation-summary.json` that `analysis/aggregate_results.py` reads.

| Operator | Injected change | Expected drift signal (P§11) |
|---|---|---|
| `add_forbidden_dep` | new ProjectReference across a pre-planted `forbidden:` rule | new edge + layering VIOLATION |
| `remove_dep` | drop one declared reference / `<prog>_LDADD` entry | removed edge |
| `add_target` | new csproj / automake primary / bash sub-lib Makefile | new (unmapped) target |
| `remove_target` | delete the project dir + all repo-wide references + solution entries | removed target + incident removed edges |
| `rename_target` | rename project file, re-point every referrer + solution | **suspected rename** (not delete+add); phase 2 pins `id_aliases {new: old}` → drift disappears (§6.3 partial survival) |
| `merge_targets` | delete B, re-point B's referrers to A | removed target + edge rewiring, **no** false rename pairing |

Design rules (pre-registered):

* **Scope = the first-party L0 build graph.** Expected and actual findings are both
  restricted to first-party targets and edges whose evidence includes `link`/`project_ref`
  — the layer every operator manipulates. Interop/runtime/package edges are excluded from
  both sides; targets touched by interop/runtime evidence are excluded as candidates
  (their identity legitimately survives a build-file edit via another extractor).
* **L0-scoped runs** (`ANON_SKIP_DOXYGEN=1`, `ANON_SKIP_ROSLYN=1`) keep ~25
  pipeline runs per system tractable; baseline and mutated runs always share the knobs, so
  the diff is layer-consistent (§11.1).
* **Determinism:** candidates are sorted and index-spread (`common.spread`, no RNG);
  every edit goes through a `FileJournal` and is reverted byte-exactly.
* **Counting:** every drift finding is expected (TP) or unexpected (FP); every expected
  finding not reported as drift is a miss (FN). A layering VIOLATION whose offending edge
  is an expected new edge of the mutation is *attributed*, never an FP. `coverage_gap_*`
  entries are neither TP nor FP (the report labels them NOT-drift).
* **Applicability is recorded, never faked:** libxml2 (automake) cannot express
  `add_forbidden_dep`/`merge_targets` (single lib, leaf programs); bash (hand-written
  idiom B collects link refs from *every* variable mention) runs whole-target operators
  only. Missing cells are absent from the results, not zero-filled.

Systems: eshop, nopcommerce, orchardcore (csproj); libxml2 (automake); bash
(hand-written autotools); `toy` is reserved for the unit tests
(`tests/test_rq6_rq7.py`).

## §8.2 Degradation-correctness check (`degradation.py`)

Fact-level induced degradation over the committed `out/<sys>/architecture/generated/
curated-facts.json` (the "induced-degradation manifest" is recorded in the artifact),
verifying the P§11.6 stopping rules **downgrade instead of crying wolf, without hiding
erosion**:

* `disable_l2` — every L2-only edge vanishes (toolchain lost) → must reclassify as
  `coverage_gap_edges`;
* `starve_l0_10` / `starve_l0_30` — a spread fraction of first-party targets lose L0
  evidence (targets stay declared) → per-finding reclassification; any drop below the
  baseline coverage makes the run ADVISORY, and below the 80 % floor a synthesized
  `require_layers` rule must answer `UNVERIFIED (degraded)` (never a silent OK) while an
  intact rule stays VIOLATION and gating is off;
* `lose_l0` — the build-graph extractor dies → all disappearances are coverage gaps.

Stopping-rules-**off** is the coverage-naive reading of the same diff (coverage gaps
counted as drift, advisory ignored). FP-suppression rate = suppressed / would-be-false;
stats.py adds Wilson CIs and the exact McNemar on/off test (§12).

Output: `out/<system>/mutation/degradation.json`.

## §8.2a Combined: a real change AND evidence loss at once (`combined.py`)

(Eval plan §8.2a — added 2026-08-30, post hoc; `plans/2026-06-04-empirical-evaluation-fact-extraction-pipeline.md`.)

§8.1 and §8.2 never coincide, so "degradation never hides erosion" was untested against a
real change. `combined.py` replays the identical §8.1 mutation set (same plans, same
95 instances, same L0-scoped knobs): apply → pipeline once → `current`; then, per mode,
a degraded copy **of the mutated run** (never of the baseline) is diffed against the
pristine baseline. Modes: `starve_l0_10` / `starve_l0_30` / `lose_l0` (the §8.2 L0 modes)
plus the adversarial `starve_mutated` — exactly the first-party targets the label names
(new/removed targets, both endpoints of every new/removed edge, old+new of a rename, both
merge members) lose L0. Every injected finding gets exactly one outcome, first match wins:

* `drift` — reported as drift (a `score()` true positive);
* `gap` — not drift, but its target / either endpoint / the edge itself is a labelled
  `coverage_gap_*` entry;
* `advisory` — absent from both, but the run is ADVISORY (§11.6 global warning);
* `silent` — absent and not advisory: hidden erosion. The claim holds iff no `silent`
  cell exists; the data is reported as-is, never tuned.

Output: `out/<system>/mutation/combined.json` (`rq6-combined/1`; per-mode
`by_operator`/`totals` cells `{n, findings, drift, gap, advisory, silent, fp,
mutations_silent, advisory_runs}`, the `silent_findings` list, and per-mutation
per-finding outcomes). Unit + toy tests: `tests/test_rq6_combined.py`.

## §8.3 Historical real-change analysis — real-history mini-arm (`history.py`)

The seeded arm is "true by construction" (the label is injected). `history.py` runs the
same L0-scoped pipeline + `drift_report.evaluate` over **real release pairs** of the
fixture clones with the committed curation rules **frozen** (never re-curated between A
and B), records every finding, and emits a labelling worksheet per pair for two human
raters (the labelling — ≥2 raters, κ — is a later human step):

```powershell
.venv\Scripts\python -m mutate.history libxml2          # v2.13.0..v2.14.0, v2.14.0..v2.15.0, v2.15.0..v2.15.3
.venv\Scripts\python -m mutate.history squidex          # 7.17->7.18, 7.18->7.19, 7.20->7.21, 7.21->7.22 (the 2-commit 7.19->7.20 and 7.22->7.23 intervals were dropped 2026-08-31; RQ9 blind curator's committed rules)
.venv\Scripts\python -m mutate.history nopcommerce      # release-4.70.0..release-4.80.0 (tags fetched shallowly)
.venv\Scripts\python -m mutate.history libxml2 --pairs v2.13.0..v2.15.3 --keep-work
```

Each release is exported with `git archive` into `out/<sys>/history/_work/<tag>/` (the
fixture's checked-out HEAD is never touched). Output per pair `out/<sys>/history/<A>__<B>/`:
`drift.md`, `result.json` (`rq6-history-pair/1`: the full `diff`, coverage/advisory, the
first-party-L0-scoped `findings`/`counts` — same scope as `common.score` — the
`needs-curation` count of B = targets the frozen rules leave unmapped, the rule-maintenance
signal, and git stats: commits A..B, commits touching build files, tag dates), and
`worksheet.csv` + identical `worksheet-R1.csv`/`worksheet-R2.csv` (columns `finding_id,
kind, subject, counterpart, hint, label, note`; `hint` = up to 3 commits between A and B
that added/removed the target's name in its owning build file(s), `git log -S`; `label` ∈
architectural | non-architectural | extraction-noise, left empty for the rater). Per system
`out/<sys>/history/history-summary.json` (`rq6-history/1`) carries the per-pair counts,
the pinned fixture commit and a `rules_note` stating whether the frozen rules pre- or
post-date the releases studied (libxml2's overlay was authored for the 2.4.22 GT-pub anchor,
i.e. *earlier* than v2.13+).

**Where the raters fill the labels (2026-09-04).** The `out/` copies above are the
regenerable mirror; the copies the raters fill are **tracked** under
`references/<sys>/history/<A>__<B>/worksheet-R{1,2}.csv` (seeded 2026-09-04 from the
2026-08-31 run; un-ignored under the withheld libxml2/nopCommerce references because they
carry only target ids and commit hints, never the reference). `analysis/aggregate_results.py`
reads the tracked copy first, file by file, falls back to `out/`, and warns on stderr when a
labelled `out/` copy differs from its tracked twin — a re-run of `mutate.history` can then
never overwrite a rater's work (the same carve-out as `references/<sys>/blind-curation/`).
The raters' instructions (what a row is, the three labels with their judgement calls, how to
inspect the releases, independence, hand-back) are `docs/rq6-history-labelling-runbook.md`.

**"Frozen" means held constant across the pair, not authored before release A.** The overlays
post-date every studied release for Squidex (`overlays/squidex-blind-R-C`, authored on
7.23.0-7) and nopCommerce (fixture pinned 2026-06-07) and pre-date them for libxml2 — so the
"0 new targets left unmapped" outcome is expected by construction on the two C# systems and a
genuine forward test only on libxml2 (the paper and eval plan §8.3 say so). Rater agreement on
the worksheet labels is reported as raw agreement **and** Cohen's κ
(`analysis/aggregate_results.py`, macros `rqSixHistLabelsAgree` / `rqSixHistLabelsKappa`).

Design rules: **L0-scoped** like §8.1 (both releases share the knobs); libxml2 ≥ 2.9 also
ships a `CMakeLists.txt`, which would take L0 precedence via a real `cmake` configure
(needs a compiler + iconv), so those runs additionally set `ANON_SKIP_CMAKE=1` and
the pure static automake parse — the idiom the frozen rules were authored for — supplies L0
(recorded in `extractor_knobs`). A pair whose release the extractor cannot parse is recorded
as failed / 0-target-warned, never hidden. Tests: `tests/test_rq6_history.py`.

