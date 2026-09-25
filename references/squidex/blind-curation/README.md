# Squidex — RQ9 blind-curation experiment record (eval §10a; runbook `docs/rq9-blind-curation-runbook.md`)

Tracked home of the experiment's **human-produced, irreplaceable** artifacts (decided
2026-08-30; the in-session working mirror was `out/squidex/blind-curation/`). Participants are
role codes only; the facilitator holds the code→person mapping out-of-tree. Squidex has **no
committed reference**: the raters *build* it — the reconciled consensus becomes
`../reference.rsf` (+ `../provenance.json`, `../doc-delta.json`).

| Role | Code | Session | Wall-clock | Deliverable here |
|---|---|---|---|---|
| Reference rater R₁ | R-A | 2026-08-14 | 60 min | `worksheet-R-A.raw.csv` (as received — Excel-damaged export) → `worksheet-R-A.csv` (repaired deterministically by `repair-worksheet-R-A.py`, labels unchanged; two spliced rows filled from R-A's own confirmation, 2026-08-30) → `rater-R-A.rsf`; `attestation-R-A.md` |
| Reference rater R₂ | R-B | 2026-08-26 | 41 min | `worksheet-R-B.csv` (frozen) → `rater-R-B.rsf`; `attestation-R-B.md` |
| Blind curator | R-C | freeze 2026-08-26 (trace from 2026-08-12) | 210 min (sittings not recorded on the form — take them from the trace) | frozen rules → `../../../overlays/squidex-blind-R-C/mapping-rules.yaml` (verbatim); `edit-trace-R-C.jsonl` (99 events); `proposed-mapping-rules-R-C.yaml` (the `arch propose` draft the curator saw); `attestation-R-C.md` |
| Facilitator / scorer | — | reconciliation 2026-08-28 | — | `worksheet-target.csv` (the 19-entity list the raters received — no tool judgment), `ceiling.json`, `worksheet-consensus.csv` → `consensus.rsf`, this index |

## Status

- **Frozen:** both rater worksheets (R-A: the raw file is the artifact of record, the repaired
  file is its faithful transcription — see the script's docstring), the curator's rules, the
  edit trace, the attestations (names redacted; signed originals out-of-tree).
- **`ceiling.json`** (computed 2026-08-30 from the frozen worksheets): κ = 0.391 on normalized
  labels (4 labels shared), pairwise MoJoFM 54.6 (symmetric), a2a 86.3, on the 14 common
  entities — R-A kept `frontend/generator` as its own cluster, R-B excluded it (only_a = 1);
  both excluded the three TestSuite projects and `GenerateLanguages`. The pre-registered κ<0.6
  flag fired. Structure: R-A's 7 clusters are a strict *coarsening* of R-B's 11 on the common
  set — R-A's `Domain` is the union of six whole R-B clusters (Core, Entities, Events, Users,
  Shared, Application); `Data`, `Web/API`, `Extensions`, `Translator`, `Infrastructure`
  coincide — 0 boundary conflicts. R-B flagged `Squidex.Infrastructure` as `MIXED`.
- **Reconciliation (2026-08-28, R-A + R-B + facilitator): fine granularity decided.**
  Consensus entered 2026-08-30 into `worksheet-consensus.csv` (template from
  `rq9_blind_curation.py consensus`; 10 rows agreed, 9 settled) → `consensus.rsf`: 15 entities
  in 12 clusters, 4 EXCLUDE — R-B's partition on the 14 common entities plus `Generator` kept
  (R-A's position); a strict refinement of R-A, 0 boundary conflicts. **It is the reference:**
  copied verbatim to `../reference.rsf`, with `../provenance.json` (GT-exp; raters, dates, κ,
  decisions, the target-vs-file granularity deviation) and `../doc-delta.json` (closed with no entries by finding — the raters bypassed the
  2019 doc as uninformative at target granularity — plus the one MIXED unit).
- **Dropped arm:** the optional second curator (R-D) did not run — curator variance is unmeasured.
- **Phase 2 scored 2026-08-30** (pipeline re-run over the frozen overlay into
  `out/squidex-rq9/`, graph 26 nodes / 72 edges; SAR baselines run the same day at target
  granularity into `out/squidex/baseline/` — ACDC/cc collapse to 2–3 clusters, ARC/WCA 12,
  LIMBO 12 — and `comparison-target.json` built):

  | row | kind | MoJoFM | a2a | extra |
  |---|---|---|---|---|
  | no-curation floor (`dir`) | detection | 70.0 | 92.0 | 23 singleton clusters |
  | best SAR (LIMBO) | detection | 60.0 | 93.0 | 12 clusters |
  | **blind curator R-C** vs reference | **expression** | **75.0** | **96.4** | edge-F1 0.67 (P 0.68 / R 0.66) |
  | human ceiling R-A vs R-B | ceiling | 54.6 | 86.3 | κ 0.39 (flag) |

  Read with the provenance caveat: the fine-granularity reference is mostly singletons, so
  the directory floor (one project per cluster) is *high* here — the curator's gain over it
  is +5.0 MoJoFM / +4.4 a2a, and the ceiling is again depressed by granularity (R-A coarser),
  not by boundary disagreement. Effort (`effort-R-C.json`): 210 min reported over several
  sittings (the trace *span* is 1350 min = first-to-last timestamp across days, not effort),
  46 curate ops (13 create, 33 move), 59 rule lines / 12 groups, **0 of 12 groups derived
  from the `arch propose` draft** (it offered 7).
- **Decision 2026-08-30 — this blind model is also Squidex's RQ1/RQ2 `pipeline` row** (the
  only curated Squidex model; `comparison-target.json` rebuilt from `out/squidex-rq9/`). On
  the all-common projection shared with the baselines (13 entities) it scores 70.0 MoJoFM /
  95.8 a2a — MoJoFM ties `dir` there, a2a separates them; the 75.0 above is the pairwise RQ9
  projection over all 15 reference entities. Both are labelled by projection in results.csv.

## Known quirks in the frozen curator rules (do not "fix" — they are the artifact)

- Exclusion is modeled as a **container named `EXCLUDE`** holding the ten test projects, with
  `exclude: targets: []` left empty. The target-granularity export will therefore emit an
  `EXCLUDE` cluster; because the reference excludes those entities, the common-entity
  projection drops them before scoring — record it, don't rewrite it.
- No trailing newline at end of file.
- `backend/src/Squidex/Squidex.csproj` (the host/application project) was never assigned to
  a container; with `on_unmapped: annotate` it exports as its own singleton cluster keyed by
  the target id. Since the reference clusters it alone as `Application`, this happens to
  score as a match — record it as an unmapped target the curator's stopping rule missed.
