# eShop — RQ9 blind-curation experiment record (eval §10a; runbook `docs/rq9-blind-curation-runbook.md`)

Tracked home of the experiment's **human-produced, irreplaceable** artifacts (decided
2026-08-30; the in-session working mirror was `out/eshop/blind-curation/`). Participants are
role codes only; the facilitator holds the code→person mapping out-of-tree.

| Role | Code | Session | Wall-clock | Deliverable here |
|---|---|---|---|---|
| Reference rater R₁ | R-C | 2026-07-21 | 32 min | `worksheet-R-C.csv` (frozen) → `rater-R-C.rsf`; `attestation-R-C.md` |
| Reference rater R₂ | R-D | 2026-07-21 | 37 min | `worksheet-R-D.csv` (frozen) → `rater-R-D.rsf`; `attestation-R-D.md` |
| Blind curator | R-A | 2026-07-21 | 43 min (1 sitting) | frozen rules → `../../../overlays/eshop-blind-R-A/mapping-rules.yaml` (verbatim); `edit-trace-R-A.jsonl` (89 events); `proposed-mapping-rules-R-A.yaml` (the `arch propose` draft the curator saw); `attestation-R-A.md` |
| Blind curator #2 (curator-variance arm, response plan 1.5b) | R-B | 2026-09-07 | 120 min (2 sittings) | frozen rules → `../../../overlays/eshop-blind-R-B/mapping-rules.yaml` (verbatim); `edit-trace-R-B.jsonl` (78 events); `proposed-mapping-rules-R-B.yaml`; `attestation-R-B.md` |
| Facilitator / scorer | — | — | — | `worksheet-target.csv` (the entity list the raters received — no tool judgment), `ceiling.json`, `worksheet-consensus.csv` → `consensus.rsf`, this index |

## Status

- **Frozen:** both rater worksheets, the curator's rules, the edit trace, the attestations
  (names redacted; signed originals out-of-tree). Never edit — κ and the ceiling row come
  from the pre-consensus worksheets.
- **`ceiling.json`** (computed 2026-08-04): κ = 0.0 on normalized labels — the raters shared
  *no* label string ('Basket' vs 'BasketService', 'View' vs 'WebApp'); the pre-registered
  κ<0.6 flag fired for vocabulary, not structure. Pairwise MoJoFM 66.5 (68.75 / 64.29),
  a2a 89.7. R-D's 13-cluster partition is a strict refinement of R-C's 8-cluster one — zero
  boundary conflicts; all disagreement is granularity, concentrated in the undocumented
  shared-infrastructure tier + the frontend split. Versus the committed reference: R-C
  MoJoFM 93.1 (one merge: AppHost into BuildingBlocks), R-D 74.2 (two splits).
- **Consensus** (reconciliation call R-C + R-D + facilitator on 2026-08-04; the worksheet
  file carries a later timestamp only because it was downloaded on 2026-08-30):
  14 clusters, a strict refinement of the committed 9-cluster reference, 0 conflicts.
- **Scored-reference decision (2026-08-30, before any curator fidelity was computed):** the
  committed `../reference.rsf` stays the scored reference (pre-registered rule); the consensus
  is reported as the W3a re-derivation result and as a labelled sensitivity scoring. Full
  rationale: `../provenance.json` → `independent_raters.scored_reference_decision`.
- **Phase 2 scored 2026-08-30** (pipeline re-run over the frozen overlay into
  `out/eshop-rq9/`, graph 24 nodes / 53 edges, 10 curator clusters over the 19 entities):

  | row | kind | MoJoFM | a2a | extra |
  |---|---|---|---|---|
  | no-curation floor (`dir`) | detection | 33.3 | 80.8 | 19 clusters |
  | best SAR (LIMBO) | detection | 26.7 | 85.9 | 7 clusters |
  | **blind curator R-A** vs committed reference | **expression** | **86.7** | **95.8** | edge-F1 0.63 (P 0.52 / R 0.79) |
  | human ceiling R-C vs R-D | ceiling | 66.5 | 89.7 | κ 0.0 (vocabulary) |
  | *sensitivity:* curator vs raters' consensus | expression-vs-consensus | 75.0 | 92.0 | edge-F1 0.55 |
  | **blind curator #2 R-B** vs committed reference (scored 2026-09-13, `fidelity-R-B.json`) | expression | 66.7 | 90.8 | edge-F1 0.56 (P 0.44 / R 0.79); 13 clusters |
  | curator R-A vs curator R-B (`curator-pair.json`) | curator pair | 80.6 | 93.9 | κ 0.15 raw / 0.82 aligned; ARI 0.75; NMI 0.94 |

  Effort (`effort-R-A.json`): 43 min reported (trace span 40.6), 43 curate ops (12 create,
  23 move, 7 set-parent, 1 remove), 63 rule lines / 11 groups, **0 of 11 groups derived from
  the `arch propose` draft** (it offered 2). Leakage check (eval §10a.6): the blind number
  86.7 sits beside the earlier non-blind 66.7 — the blind curator scored *higher*, so no
  reference leakage inflated the original; the curator exceeds the (granularity-depressed)
  ceiling MoJoFM, which is why the nesting metrics must be reported with it.

  Second curator R-B (`effort-R-B.json`, scored 2026-09-13; session 2026-09-07, same packet
  tooling as R-A): 120 min reported over 2 sittings (trace 11:17–12:17 UTC), 36 curate ops,
  65 rule lines / 13 groups, **0 of 13 groups from the `arch propose` draft**. R-B lands
  exactly on the earlier non-blind 66.7, 20 MoJoFM points below R-A, and the two curators
  agree with each other more than either agrees with the reference (pairwise MoJoFM 80.6,
  aligned κ 0.82). R-A stays the pre-registered headline curator (`stats.py
  RQ9_PRIMARY_CURATOR`); R-B is the curator-variance row beside it, never in its place.

## Known quirks in the frozen curator rules (do not "fix" — they are the artifact)

- A duplicate top-level `group:` key (`group: []` followed by the real list) — PyYAML
  last-key-wins, so the 11 groups load; whether the GUI's rules editor wrote it is a
  separate engine question.
- A `Redis` container with no members (loads as a 0-member group; no effect on the RSF).
- `contract:proto:basket` listed in Basket Service — a contracts-extractor entity outside the
  19-project worksheet universe; target-granularity scoring intersects it away.
