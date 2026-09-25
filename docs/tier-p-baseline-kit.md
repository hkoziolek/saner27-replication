# Tier-P baseline kit — offline SAR baselines + anonymized export

The two Tier-P scripts from the eval plan ([§14.1/§14.3](../plans/2026-06-04-empirical-evaluation-fact-extraction-pipeline.md)):
run the RQ2 SAR baselines on **air-gapped proprietary ([anonymized]) workspaces**, then export the
**only** artifacts allowed to cross the boundary. Everything below is local computation —
**zero network egress at run time**, compatible with the no-public-LLM-endpoint constraint
by construction (the `--no-llm` pipeline + ARCADE are pure Python/JVM).

| Script | Runs on | Purpose |
|---|---|---|
| `baselines/make_bundle.py` | connected side (this repo) | package the offline baseline kit |
| `analysis/anonymize_results.py` | **[anonymized] side** | whitelist-anonymize results for export |

## 1. Build the bundle (connected side)

```powershell
.venv\Scripts\python baselines\make_bundle.py --fetch
# -> out\tier-p\anon-baseline-kit.zip   (~15 MB with the jar)
```

`--fetch` downloads the pinned **ARCADE_Core.jar v1.2.0** into gitignored `tools/arcade/` if
missing — the one-time network access; the bundle itself is then fully offline. The zip is
**deterministic** (fixed timestamps, sorted entries): same inputs → byte-identical archive.
`--without-jar` packages without the jar (CI, or the target already has it).

Contents: `run_arcade.py` + the ARC/WCA/LIMBO Java drivers (with pre-compiled `.class` when
`javac` is available here — then the target only needs a **JRE**), the pinned jar,
`make_comparison.py` (the all-common `comparison-<gran>.json` writer), `determinism_check.py`
(the RQ5 double-run check), `no_curation_floor.py` + `a2_ablation.py` (the RQ1 floor and
curation rungs) and `stability_churn.py` (optional A8), `anonymize_results.py`, this guide +
`tier-p-org-runbook.md` (the per-system protocol/ordering), `sha256sums.txt` +
`BUNDLE-MANIFEST.json` (per-file SHA-256, jar provenance, git commit), and a self-test fixture.

## 2. Transfer, verify, smoke-test ([anonymized] side)

Prereqs: Python 3.10+, Java 17 (JDK only if `.class` files must be rebuilt).

**Where to unpack.** The zip contains a **single top-level folder `anon-baseline-kit/`**
(every entry is prefixed with it). Extract it **as-is** — do *not* strip that prefix, and do
*not* merge it into an [anonymized] source repo. That folder is a **self-contained working root**:
`run_arcade.py` resolves the repo root to it (`…/anon-baseline-kit/`), and `out/`,
`references/`, and `fixtures/` all live under it. `cd` into `anon-baseline-kit/` and run
**every** command below from there. The [anonymized] system's own Anon workspace stays wherever it
already is on the machine and is named with `--arch-dir` (§3) — it is never unpacked into the kit.

```powershell
# after unzipping: verify transfer integrity
sha256sum -c sha256sums.txt        # or the python one-liner in the bundle README

# offline self-test — exercises ACDC + the MoJoFM/a2a metric path end-to-end
python baselines\run_arcade.py smoke --granularity file --techniques acdc,dir,cc `
    --arch-dir smoke\architecture
# expected: 3 result lines, exit 0, dir scores MoJoFM=100.0 (fixture is dir-aligned)
```

## 3. Run the baselines per system ([anonymized] side)

```powershell
# 1) canonical graph from the Anon workspace already on this machine (§6.4 fairness:
#    every technique consumes this ONE export)
arch export --graph --granularity <file|target> --repo <repo> --arch-dir <arch-dir>

# 2) GT-diag reference (plan §5.3) at references\<system>\reference.rsf, then
python baselines\run_arcade.py <system> --granularity <g> `
    --techniques acdc,arc,wca,limbo,dir,cc --arch-dir <arch-dir>
# -> out\<system>\baseline\<tech>-<g>\{clusters.rsf, fidelity.json}
```

### Choosing `--granularity`

Use the **same** value everywhere for a system — the graph export, `reference.rsf`,
`run_arcade.py`, `make_comparison.py`, and the `granularity` field in `tier-p-map.json` — or the
common-entity set is empty and MoJoFM NaNs.

- **C++ (CMake / MSBuild)** → **`file`** (the SAR primary; matches the ITK/abseil/opencv anchors).
  Needs the per-file sidecar (Doxygen or clang-scan-deps ran during `arch extract`); without it
  `arch export --graph --granularity file` raises `ExportGraphError` naming the gap — fall back to
  **`target`**.
- **C#** → **`target`** (project/service level).

### Building the GT-diag `reference.rsf`

`reference.rsf` is the reference decomposition as flat `contain <cluster> <entity>` tuples — one
line per entity, `<cluster>` = a diagram-box code (`P-n/C01`, …), `<entity>` = the graph node id.
The **full human protocol** (independent rater mapping → κ → reconciliation ledger) is
`tier-p-org-runbook.md` Phase 3; the mechanics that make the file actually score:

- **Entity ids must match `graph-<g>.rsf` verbatim** — repo-relative file paths at
  `--granularity file`, target ids (`cmake:…`, `csharp:csproj:…`) at `--granularity target`. Take
  the entity list straight from the exported `generated/graph/graph-<g>.rsf` `depend` tuples and use
  it as the mapping worksheet, so names match by construction (else the common set is empty).
- **Author-effort shortcut:** you may write the reference at **target** granularity (entities =
  build-target ids, ~one line per target) even when running at `--granularity file` — `run_arcade.py`
  auto-projects a target-level reference down to files via the file→owning-target sidecar (the same
  path the C# references use). Map ~N targets instead of thousands of files.
- **Ignore Structurizr `group{}`s.** They are the cosmetic `container_parent` super-tier and never
  appear in the RSF — `clusters-<g>.rsf` is built from `container_id` only, so the pipeline's own
  decomposition is scored at the **container** level. Build the reference at that same level (one
  diagram box ≈ one container). Folding members up into their parent group would put the reference at
  a coarser granularity than the pipeline output and make MoJoFM compare mismatched partitions.
- **The curated model is *not* the reference.** Emitting `reference.rsf` from your own
  `curated-facts.json` (or reusing `clusters-<g>.rsf`) is **circular** — it scores the pipeline
  against itself (MoJoFM ≈ 100, `convention_derived: true`, scientifically void). GT-diag must be
  anchored to the *inception diagram* and independently rated; the curated groups may **seed** the
  reconciliation as a starting scaffold, but every divergence gets a ledger line and each rater
  verifies independently — only then is `convention_derived: false` honest. `run_arcade.py` already
  reports the curated model separately as `vs_pipeline_agreement` (agreement, not fidelity).

Alongside `reference.rsf`, write `references/<system>/provenance.json` (`grade: "GT-diag"`,
`clusters`, `entities`, `raters`, `kappa`, `diagram_date`, `reconciliation_date`,
`convention_derived: false`) and `references/<system>/diagram-delta.json` (the ledger — only its
*counts* ever cross). Exact fields: `tier-p-org-runbook.md` Phase 3 step 4.

Caveats (from the OSS runs): **arc** needs the source clone (`--src-root`) and is
non-deterministic (unseeded LDA) — rerun **≥5×**, report mean ± range; **arc/wca/limbo**
need k (defaults to the reference cluster count); **wca/limbo** hit an O(n²)+ wall around
~8k entities (plan §14.1 — schedule overnight); MoJoFM NaNs on entity-set mismatch are
handled by the pre-registered common-entity projection built into the runner.

Then consolidate + complete the per-system artifact set (the anonymizer's input contract):

```powershell
# 3) the fair all-common RQ1/RQ2 table row (pipeline + every baseline, ONE shared entity set)
python baselines\make_comparison.py <system> --granularity <g>

# 4) RQ1 floor + curation rungs -> a2-ablation.json, and the RQ5 double-run check
#    (run BOTH in the venv where `arch` is installed — they import the anon package;
#     register the system in a2_ablation.py's SYSTEMS dict first)
python baselines\a2_ablation.py <system>
python baselines\determinism_check.py <arch-dir> <arch-dir-rerun> --system <system>
```

## 4. Anonymize + sign off ([anonymized] side) — the §14.3 gate

Write the **LOCAL-ONLY** code book `tier-p-map.json` (this file *is* the de-anonymization
key — it never leaves the machine and is never committed):

```json
{
  "systems": {
    "<real-fixture-id>": {"code": "P-1", "language": "cpp", "granularity": "file", "kloc": 350}
  },
  "forbid": ["ProductName", "InternalCodename"]
}
```

```powershell
python analysis\anonymize_results.py --map tier-p-map.json --export-dir export-anon
# -> export-anon\results-anon.csv + export-anon\manifest-anon.json
```

`--export-dir` defaults to `<kit-root>\export-anon\` (i.e. `anon-baseline-kit\export-anon\`
when run from the kit root), holding **exactly** the two files that may cross the boundary — nothing
else in the kit does. These two are the entire [anonymized]-side deliverable.

**How the whitelist works:** only numeric metrics, enumerated technique/rung/stage codes,
and **bucketed** size ranges can appear. Anything not on a whitelist is **dropped with a
warning** (it never crosses); if a forbidden token (real system id or `forbid` entry) still
appears anywhere in the serialized output, the script **refuses the whole export** (exit 2,
nothing written) — that path indicates a bug or an enum collision, not a row to skip.
Entity/target/KLOC counts cross **only as buckets**; cluster counts stay exact (needed for
parsimony analysis — the checklist flags them).

**Sign-off checklist** ([anonymized]-internal reviewer; record role + date in the manifest's
`sign_off` block — the recorded approval is itself evidence of process for the paper):

- [ ] `results-anon.csv` + `manifest-anon.json` are the **only** files being exported
- [ ] every `system` value is a P-code; spot-check: no product/system name anywhere
- [ ] size buckets coarse enough that scale + domain do not identify the system
- [ ] exact cluster counts acceptable (else coarsen/remove those rows before export)
- [ ] `diagram_delta_counts` are counts only (never the item names)
- [ ] stderr drop-list reviewed — nothing dropped that should have been whitelisted

Transfer **only** those two files to the connected side (this repo). Drop them into
`export-anon\` at the **repo root** — the location `analysis\README.md` and `aggregate_results.py
--anon` already document — and, since they are anonymized, **commit** them into the replication
package (unlike the LOCAL-ONLY `references\` / `tier-p-map.json`, they are meant to be published).
Then merge them into the analysis pipeline:

```powershell
cd analysis
..\.venv\Scripts\python aggregate_results.py --anon ..\export-anon\results-anon.csv   # repeatable — one --anon per CSV
..\.venv\Scripts\python stats.py; ..\.venv\Scripts\python make_tables.py; ..\.venv\Scripts\python make_macros.py
```

One `results-anon.csv` can carry every P-code system at once (the anonymizer walks all map
entries); `--anon` is repeatable only if you produce separate CSVs (e.g. per wave). `manifest-anon.json`
is the paper's process record — it rides along in `export-anon\` but is not read by `aggregate_results.py`.

Tier-P rows land in `results.csv` with `tier=P`; `make_tables.py` renders them **after** the
OSS corpus, name-marked `$^\ddagger$` with a *non-replicable* caption footnote; `stats.py`
**excludes** them from every pre-registered RQ statistic (own `tier_p` block + `tierP*`
macros only) — the §14.3 rule that Tier-P **corroborates, never carries, a claim** is
enforced in code, not just prose.

## What crosses / what never crosses

| Crosses (after sign-off) | Never crosses |
|---|---|
| `results-anon.csv` (numeric metrics, P-codes, buckets) | system/product names, the code book |
| `manifest-anon.json` (grade, κ, delta **counts**, buckets, sign-off record) | entity ids, paths, cluster/container names |
| the scripts + this protocol (published for reviewer audit) | RSF/facts/DSL files, diagrams, repo content |

## Troubleshooting

- **`ARCADE_Core.jar missing`** — bundle was built `--without-jar`; fetch on a connected
  machine (pinned URL in `BUNDLE-MANIFEST.json`) and place under `tools/arcade/`.
- **`javac` not found on arc/wca/limbo** — the bundle's `.class` files are missing/stale and
  only a JRE is installed; rebundle on a machine with a JDK, or install one.
- **REFUSED — forbidden token** — a `forbid` entry collides with a legit enum (e.g. a
  generic word). Make `forbid` entries specific (product names, codenames), not common words.
- **Empty CSV** — check `--out-root` points at the tree containing `out/<system>/baseline/`
  and that fixture ids in the map match the directory names exactly.
