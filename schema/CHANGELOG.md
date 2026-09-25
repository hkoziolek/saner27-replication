# Fact-model schema changelog

Semver per §6.5 of the plan. **MINOR** = additive-only / backward-compatible (old committed
artifacts still validate; new fields optional). **MAJOR** = breaking; ships a new schema file
plus a one-shot `migrate_facts.py`. CI fails if `schema_version` changes without an entry here
(and, for a MAJOR bump, without a migration script).

## 1.3 — 2026-06-12 — datasheet enrichment: source metrics + package fingerprint — ADDITIVE

Additive MINOR bump for the datasheet-enrichment plan (plans/2026-06-12-datasheet-enrichment.md
§3/§6.1). **All changes are backward-compatible: every 1.0–1.2 artifact still validates** (all
new fields are optional properties on already-open objects).

- **Source metrics, `metrics/1` (§3).** The existing optional `targets[].metrics` object gains
  the deterministic cloc-like counts produced by `anon/metrics.py` and folded on post-merge
  by `normalize_facts` (same placement rationale as `interop_resolve` — the deepest-owner file
  attribution needs the merged target paths): `blank` / `comment` / `code` line totals,
  `todo_count` (TODO/FIXME/HACK markers in comments), per-language `by_language[]` rows
  (sorted), and the versioned `engine` tag (`"metrics/1"`) so a counting-heuristic change is a
  reviewable engine bump + golden regen, never silent churn. These are **Class-F facts** (plan
  §2): a pure function of the working tree at a commit, deliberately INSIDE the hashed core —
  a commit that changes code *should* change the fact-model hash. Lines are counted from
  decoded bytes so CRLF and LF working trees count identically (tested).
- **`targets[].package_refs` (§6.1).** DIRECT package references (NuGet) stamped on the
  consuming project target by the build-graph extractor. Needed because curation's
  `drop_external` removes the package *targets* (and their edges) — the datasheet technology
  fingerprint must survive on the project itself. Union-merged across fragments.

## 1.2 — 2026-06-05 — post-v1 backlog (runtime evidence, interop tail, contracts) — ADDITIVE

Additive MINOR bump for the post-v1 backlog items (§1.2 "where we actually are"). **All changes are
backward-compatible: every 1.0/1.1 artifact still validates** (new enum members are additive; the
`runtime` evidence kind already existed in the enum since 1.0).

- **`runtime` evidence is now produced (§5).** Previously defined-but-deferred; the runtime-topology
  extractor (`extract_runtime.py` — .NET Aspire AppHost, DI registration, hosted services) emits
  `runtime` evidence on `deployment.edges` (kind `calls`/`runtime_config`), **never on a build
  relationship**, so it does not feed the §6.5 build-edge weight. No enum change was needed (it was
  reserved in 1.0); recorded here for traceability.
- **`contract` target type + `idl` language (§16#14).** A shared cross-component interface definition
  (`.proto`/`.thrift`/`.fbs`/IDL/WSDL) is modeled as a first-class boundary element
  (`type: "contract"`, `language: "idl"`, `external: true`, id `contract:<kind>:<name>`), with
  `produces`/`consumes` relationships (evidence `package_ref`, tagged `contract`) to the targets that
  generate from / depend on it. Both enum additions are additive.
- **Interop tail (§16#15).** C++/CLI bridges and COM are now detected by `extract_interop.py` and reuse
  the existing `interop` evidence kind + `native` language (boundary ids `native:clr:*` / `com:*`). No
  schema change required — recorded for traceability.

## 1.1 — 2026-06-04 — P7 extension (deployment facet, interop, multi-repo) — ADDITIVE

Additive MINOR bump for the §14 P7 features. **All changes are backward-compatible: every 1.0
artifact still validates** (the new fields/kinds are optional, the new enum members are additive).

- **Deployment facet (§4.4).** New OPTIONAL top-level `deployment` object holding `nodes[]`
  (`deploymentNode`: `deploy:image:<name>` / `infra:<kind>:<name>` / `env:<name>` ids, kept
  language-agnostic and disjoint from `cpp:`/`csharp:`) and `edges[]` (`deploymentEdge`:
  `packages` image→build-target, plus `calls`/`reads`/`publishes`/`runtime_config` runtime edges).
  Strictly distinct from build-time `targets`/`relationships`; runtime edges never feed the §6.5
  build-edge weight. Produced by the deterministic deployment extractor (Dockerfile / compose /
  k8s / Helm), consumed by the §9.3 deployment-dev/prod + container-runtime views.
- **`deploy` / `runtime_config` evidence kinds.** Added to the `evidence.type` enum; only ever
  appear on `deployment.edges`, never on a build relationship.
- **`native` language.** Added to the target `language` enum for the C#-side P/Invoke interop
  scan (§16#15): a `[DllImport("foo")]` yields an `interop` edge to a `native:lib:foo` external
  boundary (`language: native`, `external: true`). The C++ side that would resolve it to a real
  `cpp:shared_lib` target stays deferred.
- **Repo-namespace prefix (§21).** Multi-repo ids gain an OPTIONAL leading `<repo>::` segment
  (`<repo>::csharp:csproj:<path>`), stamped by the `combine` step. The single-repo id is the
  degenerate case (no prefix), so single-repo runs are unchanged and the `target.id` /
  `relationship.source|target` patterns still match. No schema change was required for the prefix
  itself (the `^[^:]+:.+$` pattern already accepts it); documented here for traceability.

## 1.0 — 2026-06-04 — initial draft (v1 pilot, §16 decision #19)

First contract for the C#/.NET pilot. Defines the neutral fact model (Tiers 1–2 of the §6.0
taxonomy): `provenance`, `targets` (with optional child `components` → `code` tiers), and
language-agnostic `relationships` with cited `evidence`.

- **Open objects, closed enums (§6.5a).** Root, `target`, `relationship`, `component`, `code`,
  and `evidence` objects stay open for additive MINOR fields; only the enum-valued fields are
  constrained: target `type`, `level`, `language`, evidence `type`, `confidence`, `visibility`.
- **Stable id contract (§6.3).** `target.id` must be content-independent (C# = `csharp:csproj:<path>`;
  C++ = `cpp:target:<cmake-target-identity>`). Child ids are parent-qualified
  (`…/component:<key>`, `…/code:<key>`). Relationship id = `rel:<source>-><target>` — `kind`
  is curated later and stays out of identity.
- **Evidence required (§5).** `relationship.evidence` has `minItems: 1` — no relationship without
  evidence. v1 evidence kinds: `link`, `project_ref`, `include`, `symbol_use`, `package_ref`,
  and the defined-but-deferred `interop`/`runtime` (§16#15).
- **Optional governance fields.** `confidence`, `evidence_strength`, `visibility`,
  `is_declared_dependency`, and `provenance.coverage` are optional (validated leniently across
  pipeline stages: raw fragments → `extracted-facts.json` → `curated-facts.json`).

### Deliberately out of scope in 1.0 (land as future MINOR bumps)

- **Deployment facet** — top-level `deployment` object, `deploy`/`runtime_config` evidence kinds,
  and `deploy:`/`infra:`/`env:` id forms (§4.4, §16#16) are POST-V1.
- **Repo-namespace prefix** for multi-repo ids (`<repo>::<lang>:target:<key>`, §21) — single-repo
  is the degenerate case, so 1.0 needs no change for it.
- **Additional `language` values** beyond `cpp`/`csharp` — added when new extractors land.
