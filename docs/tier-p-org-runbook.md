# Tier-P [anonymized]-side execution runbook (eval plan §5.3 / §5.5 / §14.3)

Step-by-step instructions for producing the Tier-P evidence **inside the [anonymized] workspaces**,
per system, ending in the sign-off-gated export of `results-anon.csv` + `manifest-anon.json`
— the only artifacts that cross the boundary. Mechanics of the offline baseline kit
(bundle build/verify, ARCADE invocation details, the anonymizer's whitelist semantics,
the sign-off checklist) live in `tier-p-baseline-kit.md`, which travels with this file;
this runbook is the *ordering and protocol* around it. Everything runs offline
(`--no-llm`, local JVM); no repo content, entity names, or diagrams ever leave the machine.

## Two waves, two deadlines

| Wave | Deadline | Scope | Feeds |
|---|---|---|---|
| **1 — corroboration rows** | **2026-08-20** (paper freeze) | the 3 already-executed systems: refresh runs, GT-diag references, baselines, anonymized export, sign-off | the paper's anonymized Tier-P subsection |
| **2 — per-system depth** | **mid-Oct 2026** (second wave) | +1–3 further systems; full per-system results; delta-ledger drift set finalized | a later industrial write-up |

Wave 1 is the critical path for *both* papers — start the sign-off conversation with the
internal reviewer **before** the artifacts are ready, so the review slot exists when they are.

## Per-system artifact contract (what the anonymizer consumes)

```
references/<sys>/reference.rsf                     # reconciled GT-diag (LOCAL-ONLY)
references/<sys>/provenance.json                   # grade, clusters, entities, raters, kappa, dates
references/<sys>/diagram-delta.json                # the reconciliation ledger (LOCAL-ONLY; counts cross)
out/<sys>/architecture/generated/run-report.json   # RQ7 stage timings + coverage/trust block
out/<sys>/perf/rq7-perf.json                       # RQ7 §10 cold/warm wall-clock + peak RSS (Table VII)
out/<sys>/baseline/<tech>-<gran>/fidelity.json     # per-technique pairwise scores (run_arcade.py)
out/<sys>/baseline/comparison-<gran>.json          # RQ1/RQ2 all-common projection incl. `pipeline`
out/<sys>/baseline/a2-ablation.json                # curation rungs incl. the RQ1 floor (rung `none`)
out/<sys>/baseline/determinism.json                # RQ5 double-run verdict (determinism_check.py)
out/<sys>/baseline/stability-churn.json            # optional: A8 churn (perturb mode)
tier-p-map.json                                    # the LOCAL-ONLY code book (never exported)
```

`<sys>` is the real fixture id locally; it becomes a `P-n` code only inside the exported
files. Anything outside this contract is dropped by the anonymizer with a warning — review
that drop-list, it is the completeness check.

## Phase 0 — prerequisites (once per machine)

1. Transfer + verify + smoke-test the baseline kit (`tier-p-baseline-kit.md` §2).
   Python 3.10+, Java 17 (JRE suffices if the bundle carries `.class` files).
2. **Anon version gate:** the installed Anon must carry **cmake-graph ≥ 0.2.0**
   (CTest dashboard-target filtering). Older exports over-count targets and are not
   comparable — if in doubt, re-run extraction rather than reusing cached fragments.
3. For CMakePresets-based systems set the preset before running, e.g.
   `$env:ANON_CMAKE_PRESET = "Windows-Debug-x86-64"`.
4. Create `tier-p-map.json` (kit §4) **now** and assign the P-codes; per §14.3 the codes are
   used from the first artifact onward, never retrofitted — including cluster codes
   (`P-3/C07`) inside the GT-diag working notes.

## Phase 1 — pipeline runs (RQ5, RQ7, coverage)

```powershell
arch run --repo <repo> --arch-dir out\<sys>\architecture --rules-dir <rules-dir> --no-llm
```

- Keep `generated/run-report.json` — its stage timings are the per-stage RQ7 breakdown and its
  trust block the coverage rows; do not hand-edit anything under `generated/`.
- **RQ7 perf table (Table VII):** the cold/warm end-to-end + peak-RSS row is a *separate*
  measurement from the single-run stage timings. Register the system in
  `baselines/rq7_perf.py`'s `SYSTEMS` dict first (mark it `optin` so the default sweep skips
  it; set `fixture`/`rules` to the paths you passed to `arch run`; `pip install psutil` in the
  venv or the RSS column is blank), then:

  ```powershell
  python baselines\rq7_perf.py <sys>          # cold + warm arch runs -> out\<sys>\perf\rq7-perf.json
  ```

  The anonymizer emits this as `axis=RQ7` rows and Table VII renders them. **Exactness note:**
  cold/warm/peak-RSS **and exact KLOC / #targets** cross here un-bucketed (so the point lands on
  the §10 scaling curve). Exact size is an *identifying* signal even under a P-code, so this is a
  conscious size-disclosure decision the internal reviewer must approve at the Phase 6 sign-off —
  it is not the default bucketed export.
- **Determinism check (RQ5):** run a second time into a fresh arch dir
  (`out\<sys>\architecture-rerun`), then (in the venv where `arch` is installed):

  ```powershell
  python baselines\determinism_check.py out\<sys>\architecture out\<sys>\architecture-rerun --system <sys>
  ```

  This compares the provenance-stripped fact models + byte-compares the generated `.dsl`
  fragments and writes `out\<sys>\baseline\determinism.json` — the whitelisted source of the
  `RQ5 / identical_rate` row in the export. A DIFFER verdict is a finding to investigate,
  never a row to edit.
- The curated `mapping-rules.yaml` for each system is already done (2026-07 status); re-curate
  only if the refresh run surfaces new targets, and log what changed.

## Phase 2 — canonical graph export (the §6.4 fairness control)

```powershell
arch export --graph --granularity <file|target> --repo <repo> --arch-dir out\<sys>\architecture
```

One export per system; **every** technique below — the pipeline's own clusters
(`generated/graph/clusters-<gran>.rsf`) included — is scored from this single artifact.
Granularity: `file` for C++ (needs the file-deps sidecar from Doxygen/clang), `target` for C#.

## Phase 3 — GT-diag reference construction (§5.3, the scientific core)

Inputs: the inception-era architecture diagram(s) + the current code. Protocol, per system:

1. **Inventory.** Enumerate the diagram's boxes; each becomes a candidate cluster with a
   neutral code (`P-n/C01`, …) assigned immediately.
2. **Independent mapping.** Rater R₁ (ideally the diagram's author or current owner) maps
   every first-party build target (file, at file granularity) to a box. R₂ maps
   independently — no discussion before both are done. Compute pairwise MoJoFM + κ on the
   common set; κ < 0.6 flags the reference low-confidence (it still ships, labelled).
3. **Reconciliation session.** For every unmappable entity (post-diagram components) and
   code-less box (dead/renamed parts), R₁+R₂ jointly decide *extend* / *assign* / *exclude*
   — **every decision gets one ledger line with a one-line rationale.** The ledger is a
   result (the RQ6 real-drift set), not overhead; do not shortcut it.
4. **Outputs.** `reference.rsf` (the reconciled GT — the fidelity comparator);
   `provenance.json` with at least `grade: "GT-diag"`, `clusters`, `entities`,
   `raters`, `kappa`, `diagram_date`, `reconciliation_date`, `convention_derived: false`;
   `diagram-delta.json` (the ledger — only its *counts* ever cross).

Budget ~half a day per system for steps 2–3 with both raters in the room; schedule the
architects now (this, plus sign-off, is the schedule risk — not the compute).

## Phase 4 — SAR baselines

```powershell
python baselines\run_arcade.py <sys> --granularity <g> --techniques acdc,arc,wca,limbo,dir,cc --arch-dir out\<sys>\architecture
```

Caveats (kit §3): `arc` needs `--src-root` and ≥5 reruns (unseeded LDA — report mean ± range);
`wca`/`limbo` hit an O(n²)+ wall near ~8k entities (schedule overnight); k defaults to the
reference cluster count; entity-set mismatches are handled by the pre-registered
common-entity projection built into the runner.

## Phase 5 — floor + the all-common comparison file

1. **Floor + curation rungs** (`baselines/a2_ablation.py` → `a2-ablation.json`; RQ1's
   mandatory floor column is the rung `none` — build-graph-only, zero curation). Register
   the system in the `SYSTEMS` dict at the top of the script first, and run it in the venv
   where `arch` is installed (it imports the anon package). `no_curation_floor.py`
   prints the same floor standalone for a quick sanity check.
2. **Consolidate the all-common comparison** — the RQ1/RQ2 row the anonymizer reads:

```powershell
python baselines\make_comparison.py <sys> --granularity <g>
```

This projects the pipeline clusters + every baseline found under `out\<sys>\baseline\` and
the reference to ONE shared entity set, scores them uniformly, and writes
`comparison-<gran>.json` deterministically. Techniques whose clusters are missing are
skipped with a visible warning — rerun `run_arcade.py` for them first if unintended.

## Phase 6 — anonymize, review, sign off (§14.3 gate)

Exactly as `tier-p-baseline-kit.md` §4: run `anonymize_results.py --map tier-p-map.json`,
walk the **sign-off checklist** with the [anonymized]-internal reviewer, record role + date in the
manifest's `sign_off` block. Two extra points of order:

- Review the **stderr drop-list** against the artifact contract above — a missing
  `comparison-<gran>.json` or `provenance.json` shows up here as silence, not as an error.
- A **REFUSED** export (exit 2) is a bug to fix, never a row to delete by hand.

## Phase 7 — export

Transfer `results-anon.csv` + `manifest-anon.json` only. Connected side ingests via
`aggregate_results.py --anon …` (kit §4); Tier-P rows are firewalled to `tier=P` — they
corroborate, never carry, a claim.

## Definition of done

**Per system:** contract artifacts complete → determinism PASS logged → κ + delta counts in
provenance → anonymizer runs clean (drop-list reviewed) → sign-off recorded.
**Wave 1 (by 08-20):** the above for the 3 executed systems, exports merged connected-side.
**Wave 2 (by mid-Oct):** +1–3 systems; ledger finalized as the RQ6 real-drift set; per-system
depth results retained locally for a later write-up.

## Kit version note

This runbook requires **anon-baseline-kit ≥ 1.1** (`BUNDLE-MANIFEST.json` →
`kit_version`), which ships `make_comparison.py`, `determinism_check.py`,
`no_curation_floor.py`, `a2_ablation.py`, `stability_churn.py`, and this file, and whose
anonymizer whitelists the `RQ5 / identical_rate` row. A 1.0 bundle lacks all of these —
rebuild on the connected side (`python baselines\make_bundle.py --fetch`) before transfer.
