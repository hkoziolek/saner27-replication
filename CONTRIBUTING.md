# Contributing — keeping the architecture model honest

Anon treats the architecture model as a **governed artifact** (plan §2 principle #7):
people and coding agents *update it through the pipeline* and *review drift* — they never
redraw diagrams by hand. This file is the contract for doing that safely (plan §11.3).

## The split that makes regeneration safe (plan §2 #6)

- **Generated, machine-owned — never hand-edit:** everything under
  `architecture/generated/` (`extracted-facts.json`, `curated-facts.json`,
  `generated-*.dsl`, `run-report.json`, `architecture-drift.md`). Overwritten every run.
- **Hand-curated — never regenerated:** `architecture/rules/*.yaml` (curation, layering,
  redaction), the views & styles in `architecture/views.dsl`, and the committed layout in
  `architecture/workspace.json`. The generator emits *fragments* that `workspace.dsl`
  `!include`s; it never rewrites your views, styles, or layout.

## Coding-agent contract (plan §11.3)

When a change touches architecture-relevant code (build targets, project/package
references, namespaces), the agent (or developer) **must**:

1. **Detect** whether the architecture changed — re-extract facts and diff against the
   committed fact model:
   ```
   arch run --repo . --no-llm        # extract → curate → generate → drift
   ```
2. If it changed, **regenerate fragments only** — never touch hand-curated views/styles.
   `arch run` already does this; do not edit files under `generated/` by hand.
3. Rely on **stable IDs + the committed `workspace.json`** so unchanged in-view elements
   keep their layout; new/regrouped elements get a light re-layout. Preview what a rerun
   would move before committing:
   ```
   arch run --repo . --no-llm --diff      # dry-run: content vs layout-affecting changes
   ```
4. **Never hand-edit generated fragments**, and never regenerate the whole workspace from
   scratch (it destroys layout quality and makes reviews noisy).
5. **Attach the drift report** (`architecture/generated/architecture-drift.md`) to the PR
   for human review. Drift is *advisory* — a human decides whether each item is an intended
   architecture change or a violation to fix in code (plan §11.1/§11.4).

## Drift is advisory; layering rules are fitness functions (plan §11.1/§11.2)

`arch drift` reports three things: new code facts not in the model, model elements no
longer in code, and **layering/fitness violations**. Encode allowed/forbidden
container dependencies in `architecture/rules/layering-rules.yaml`:

```yaml
forbidden:
  - from: "Web App (Blazor Storefront)"   # container name, container id, or target id
    to:   "Event Bus"
    require_layers: ["L2"]                # optional: coverage needed to *verify* the rule
```

Each rule is reported **VIOLATION** (a matching edge exists), **OK** (none, coverage
adequate), or **UNVERIFIED (degraded)** when the required coverage is missing — absence of
evidence is never treated as compliance (plan §11.2). Run it standalone with:

```
arch drift --repo . --rules-dir architecture/rules
```

A degraded run (L0 first-party coverage below 80%, or below the committed baseline) stamps
the report **ADVISORY** and makes every finding non-gating for that run (plan §11.6) — the
pipeline must not cry wolf or hide erosion when extraction is incomplete.

## The regeneration gate runs in CI — humans don't regenerate locally (plan §11.3)

Most developers lack the C++/Roslyn toolchain, so **CI is the regenerator**. CI
re-extracts, canonicalizes the curated fact model (sorted keys; `provenance.generated_at`
/ `commit` and the generated-file banner stripped), and compares its content hash against
the committed copy. The gate fires **only** when that canonicalized model differs *and* no
regenerated commit is present in the PR — not merely because `src/**` changed. The default
is a required PR comment / failing status, never a silent auto-push.

## Running the pipeline

```
arch run --repo <repo> --rules-dir <repo>/architecture/rules --no-llm   # deterministic baseline
arch validate --repo <repo> --which curated --workspace                 # schema + Structurizr DSL gate
arch export  --repo <repo> --format plantuml                            # static views for wikis/slides
```

LLM enrichment is **opt-in** and never runs in CI: `--llm-propose` writes proposed names
into `name-review.tsv` (advisory; not auto-consumed), `--llm-accepted` serves reviewed/
pinned names. It needs Azure AI Foundry config — see `llm.example.yaml`. The `--no-llm`
baseline is byte-identical across reruns and is what the gates assert.
