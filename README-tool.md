# Anon — the pipeline

Anon is a mostly deterministic **fact-extraction pipeline** that turns large **C/C++ and C#**
repositories into governed **C4 / Structurizr DSL** architecture models. Three properties define
it:

- **The build graph is the source of structural truth.** Structure comes from build targets and
  their declared link/reference dependencies (CMake, MSBuild, `.sln`/`.csproj`, autotools),
  refined — never replaced — by source-level evidence (Roslyn, Doxygen, clang-scan-deps).
- **LLMs touch names and descriptions only, never structure.** Enrichment cannot add a
  relationship that has no evidence; it is opt-in, off by default (`--no-llm`), and never runs
  in CI.
- **Output is deterministic and byte-identical.** Same inputs give byte-identical DSL fragments
  and a stable, provenance-stripped fact-model hash; this is a tested gate (`tests/test_golden.py`).

## The pipeline at a glance

```
extract            normalize           curate              enrich (opt.)        generate         validate / govern
build & dep graph  -> Architecture  -> mapping-rules.yaml -> LLM names &     -> Structurizr   -> structurizr validate
(CMake / MSBuild      Fact Model       group / filter /     descriptions        DSL fragments    + drift report in CI
 / Roslyn)            (neutral JSON)    threshold (HITL)     (evidence-cited)    + hand views
```

1. **extract** — independent extractors write JSON fragments (C# build graph from `.sln`/`.csproj`
   by pure parse; optional Roslyn symbol facts; CMake File API; MSBuild `.vcxproj`;
   autotools `Makefile.am`/`.in`; Doxygen and clang-scan-deps include graphs; P/Invoke, C++/CLI
   and COM interop; deployment and runtime topology; shared contracts). Every extractor
   **degrades gracefully**: when its toolchain is absent it writes a coverage warning and a
   valid, possibly empty fragment — never a hard failure.
2. **normalize** — merges the fragments into `extracted-facts.json` deterministically (targets
   merge on id, relationships on (source, target), evidence de-duplicated, derived fields
   recomputed).
3. **curate** — applies the hand-reviewed `rules/mapping-rules.yaml` (grouping into containers,
   exclusions, naming, weight thresholds) to produce `curated-facts.json`.
4. **enrich** (optional) — LLM naming and descriptions through an injectable provider.
5. **generate** — lifts target-to-target edges to container-to-container edges and emits the
   machine-owned DSL fragments; scaffolds the hand-owned `workspace.dsl` / `views.dsl` once.
6. **drift** — an advisory code-versus-model drift report plus layering / fitness-function
   checks; findings downgrade to advisory when first-party L0 coverage is below 80 %, so a thin
   run reports but never cries wolf.

## Install

Python 3.10 or newer. From the package root:

```
python -m venv .venv && .venv/Scripts/activate     # or: source .venv/bin/activate
pip install -e ".[dev]"                             # pipeline + `arch` console script + pytest
```

Extras: `[stats]` (scipy and scikit-posthocs for the inferential statistics in
`analysis/stats.py`). The full tool's `[serve]` extra (the read-only HTTP layer and GUI) is not
in this package — see "Pruned tool surface" in `README.md`.

Optional toolchains, each detected at run time and reported by `arch doctor`: the .NET SDK
(C# fixtures; the Roslyn symbol helper is not part of this snapshot, so C# runs at the
build-graph layer L0), CMake + Ninja and Doxygen or clang-scan-deps (C/C++ beyond L0), Java 17
(the ARCADE baselines under `baselines/`), and a Structurizr CLI or server if you want to
render the DSL. None of them is needed for the toy fixture or the test suite.

## Run

```
arch run --repo tests/fixtures/toy-repo --arch-dir out/toy-run/architecture \
         --rules-dir tests/fixtures/toy-repo/architecture/rules --no-llm
```

This extracts the build graph, normalizes and validates the fact model, applies the curation
rules, generates the DSL fragments (Container view, per-container Component views, deployment
and dynamic views where the inputs exist) and writes the drift report, all under
`out/toy-run/architecture/generated/`. Run it twice into two directories: the `generated-*.dsl`
fragments are byte-identical and the fact models differ only in the volatile `generated_at`
stamp (`arch diff` compares two runs).

`arch run` flags: `--no-llm` (default) / `--llm-propose` / `--llm-accepted` (mutually exclusive),
`--diff` (dry run: content-affecting versus layout-affecting changes), `--strict` (readability
and layering gates become hard failures), `--incremental` (per-extractor caching),
`--resolve-deployment` (opt into Helm/Kustomize rendering; off by default so the hashed core
stays PATH-independent).

## The `arch` CLI

Every subcommand takes `-h`; each stage is also runnable as `python -m anon.stages.<name>`.

| group | subcommands |
|---|---|
| stages | `extract`, `curate` (`--seed-from` adopts a hand-drawn Structurizr model as a one-time seed), `generate`, `validate`, `drift` |
| governance and trust | `abstain` (calibrated per-element confidence), `scorecard` (coverage-honest fidelity by evidence stratum), `explain` (the rule behind a placement, the evidence behind an edge; `--lint` flags shadowed or dead globs) |
| adoption | `discover` (existing C4/Structurizr models and ADRs in a repository), `propose` (advisory draft grouping from source-declared signals), `reconcile` (bulk-confirm proposed id bindings), `diff` (release-to-release diff of two fact snapshots) |
| intelligence | `risk` (cycle / hub / coupling findings), `datasheet` (per-container datasheets), `scenarios` (dynamic views from `rules/scenarios.yaml`), `adr check` / `adr stubs` (ADR conformance and evidence-filled ADR candidates), `query` (the deterministic, cited oracle), `whatif` (simulate extract / merge / move / cut on a copy — a proposal, never applied) |
| ops | `fetch` (clone pinned fixtures from `manifest.yaml`), `export` (PlantUML / Mermaid / DOT, `--graph` for the RSF dependency graph the evaluation scores, `--layout` for layout coordinates), `egress-preview` (the exact post-redaction bytes an LLM run would send, without calling a provider), `doctor` / `bootstrap` (tooling health and gaps) |

The full tool has seven more verbs (`serve` + GUI, `mcp`, `combine`, `fleet`, `timeline`,
`measure`, `verify`) that nothing the paper evaluates depends on; they are pruned from this
package (`README.md`, "Pruned tool surface").

## The fact model is the contract

`schema/fact-model.schema.json` (schema version 1.3, changelog in `schema/CHANGELOG.md`) is the
neutral JSON contract between extractors and the generator. Evidence carries a **layer**
(L0 build graph, L1 declared references, L2 symbols and includes, L3 fine-grained) and a
**kind** (`link`, `project_ref`, `package_ref`, `include`, `symbol_use`, `interop`, `runtime`,
`deploy`, `runtime_config`). Derived quantities the schema cannot express — edge weight,
confidence, canonical ordering, the provenance-stripped content hash — live in
`src/anon/model.py` so every stage computes them the same way. Element ids are stable and
content-independent (`csharp:csproj:src/Foo/Foo.csproj`, `cpp:target:<name>`; multi-repository
ids carry a `<repo>::` prefix), and `src/anon/ids.py` maps them to unique DSL identifiers
order-independently, which is what makes layout-preserving regeneration possible.

## The three-tier file lifecycle

Under a repository's `architecture/` directory:

- `generated/` — machine-owned, overwritten every run, never hand-edited: `extracted-facts.json`,
  `curated-facts.json`, `generated-*.dsl`, `run-report.json`, `architecture-drift.md`.
- `rules/*.yaml` — hand-curated, never regenerated: `mapping-rules.yaml` (curation),
  `layering-rules.yaml` (fitness functions), `payload-redaction.yaml` (LLM egress),
  `scenarios.yaml` (dynamic views). This is where human review happens.
- `workspace.dsl`, `views.dsl`, `workspace.json` — hand-owned, scaffolded once; the generator
  emits fragments these `!include` and never rewrites views, styles or saved layout.

For the evaluated open-source systems the clone stays pristine: artifacts go to `out/<id>/` via
`--arch-dir` and rules come from `overlays/<id>/architecture/rules` via `--rules-dir`.

## Tests

```
python -m pytest -q          # default tier: parallel, real-toolchain 'integration' tests deselected
python -m pytest -q -m ""    # everything (needs cmake / ninja / doxygen / dotnet on PATH)
```

What the suite gates: **determinism** (byte-identical fragments and a stable fact-model hash
against the committed goldens in `tests/golden/`), **readability** (at most 25 elements per
Container view and 20 per Component view), **coverage** (first-party L0 coverage is reported in
`run-report.json`, drift findings are advisory below the 80 % floor), the **schema contract**
(every fact model validates; a `schema_version` bump needs a changelog entry), and the
evaluation harness (`test_tier_p_kit.py`, `test_llm_recovery.py`, `test_rq6_*.py`,
`test_harness.py`). The default tier passes without the .NET SDK or the Roslyn helper.

## LLM enrichment is governed

What may leave the trust boundary is fixed and small: redacted metadata (ids, names, paths after
`rules/payload-redaction.yaml`) plus documentation snippets truncated to 280 characters — never
whole files, never source. `arch egress-preview` writes the exact bytes a live run would send
so they can be reviewed before any provider is called; enrichment is off by default, requires
`--llm-propose` (draft for review) or `--llm-accepted`, and never runs in CI. Structure is
untouched either way: the enrichment stage cannot add, remove or reweight a relationship.

## Where things live in this package

| path | what |
|---|---|
| `src/anon/` | the package: `cli.py`, `stages/` (one module per extractor and stage), `model.py`, `ids.py`, `paths.py` (every artifact location), `config.py` (rule defaults), `jsonio.py` (canonical JSON), `query.py` |
| `schema/` | the fact-model schema and its changelog |
| `tests/` | the suite, the toy fixture (`tests/fixtures/toy-repo`) and the goldens |
| `manifest.yaml`, `overlays/` | the pinned open-source fixtures and their per-system curation rules |
| `CONTRIBUTING.md` | the contract for repositories that adopt the tool: regenerate through the pipeline, never hand-edit `generated/`, attach the drift report to the pull request |
