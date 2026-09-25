# Protocol history (dated commits)

The paper does not claim pre-registration: no external registry (OSF, AsPredicted) was
used. What it states is the ORDER of events — which protocol sections, thresholds and
metrics were written down before which data existed — and this file is the record of
that order, verifiable against the repository history on acceptance.

This artifact is a snapshot of the evaluation repository at commit `cb987e4e60ba038965e2ee83bcef224c9290716c`
(author date 2026-09-25T13:45:09+02:00). The git history itself is withheld for double-blind review
(it carries author identities) and is released with the named technical report on
acceptance; the hashes below are verifiable against it then.

The protocol documents are (copies in this artifact, scrubbed of identifying strings):

| document | in this artifact |
|---|---|
| evaluation plan §6.4 (fairness controls), §8.2 (RQ6 degradation stopping rules), §8.2a (RQ6 combined arm, a dated post-hoc addition), §10a (blind curation, RQ9), §12 (statistical analysis plan, with its dated 2026-08-30/31 amendments), §13 (threats, planned mitigations) | `protocol/eval-plan-extract.md` |
| RQ9 blind-curation runbook (roles, blinding, stopping rule, attestations) | `docs/rq9-blind-curation-runbook.md` |
| RQ9 session tooling the attestations cite: curator GUI launchers, edit-trace effort logger | `overlays/{eshop,squidex}/start-gui.ps1`, `src/anon/edit_trace.py` (the launchers ship as the dated record of what the curators ran; the `arch serve` layer + GUI they start is pruned from this package — see the README's "Pruned tool surface") |
| RQ6 real-history rater runbook (label set, independence, hand-back) — a post-hoc arm, dated as such | `docs/rq6-history-labelling-runbook.md` |
| RQ9 harness (worksheets, inter-rater agreement, effort, four-row fidelity table) | `baselines/rq9_blind_curation.py` |
| analysis script implementing §12 (Friedman/Nemenyi/Wilcoxon-Holm, rank-biserial / Cliff's δ, Tier-P firewall) | `analysis/stats.py` |

## When each protocol section first entered the repository

| item | first commit (hash, author date) |
|---|---|
| 6.4 Fairness controls (fixed in the plan) | `7db051f8f13d` 2026-06-04T10:59:21+02:00 |
| 8.2 Degradation-correctness check (tests P§11.6 stopping rules) | `7db051f8f13d` 2026-06-04T10:59:21+02:00 |
| 8.2a Combined change-plus-degradation arm (RQ6, added 2026-08-31) | `5915782ce9a4` 2026-09-04T13:59:20+02:00 |
| 10a. Blind-curation experiment (RQ9) | `cca8e1aa0a89` 2026-07-15T14:12:06+02:00 |
| 12. Statistical analysis plan | `7db051f8f13d` 2026-06-04T10:59:21+02:00 |
| 13. Threats to validity | `7db051f8f13d` 2026-06-04T10:59:21+02:00 |
| docs/rq9-blind-curation-runbook.md (file added) | `c1232d82832b` 2026-08-30T15:37:38+02:00 |
| docs/rq6-history-labelling-runbook.md (file added) | `214268b4368f` 2026-09-07T09:47:18+02:00 |
| baselines/rq9_blind_curation.py (file added) | `c1232d82832b` 2026-08-30T15:37:38+02:00 |
| analysis/stats.py (file added) | `7db051f8f13d` 2026-06-04T10:59:21+02:00 |
| src/anon/edit_trace.py (file added) | `0c763fa5d340` 2026-07-21T11:18:59+02:00 |
| overlays/eshop/start-gui.ps1 (file added) | `ad9d2cfc05eb` 2026-07-21T09:10:06+02:00 |
| overlays/squidex/start-gui.ps1 (file added) | `ad9d2cfc05eb` 2026-07-21T09:10:06+02:00 |

## Every commit touching the protocol files

Hash, author date and subject only (no author names). Newest first.

| commit | author date | subject | files |
|---|---|---|---|
| `89be23d912e2` | 2026-09-16T13:37:43+02:00 | RQ6 real-history labels: mark the human item done (eval plan §8.3, response plan 4.2/14.12, runbook status) | rq6-history rater runbook, eval-plan |
| `e67067217bf5` | 2026-09-16T13:14:26+02:00 | Fourth review round (2026-09-13): §14 DO set — one-population RQ9 scoring, P-1 floor, claim rewording | stats.py, rq9-harness, rq9-runbook |
| `1e9d5da5e026` | 2026-09-13T14:37:09+02:00 | RQ7: second eShop blind curator (R-B) scored; drop the "pre-registered" claim | rq9-harness, eval-plan |
| `1f72c967a6fd` | 2026-09-11T13:51:40+03:00 | Paper updates. | stats.py, eval-plan |
| `086d66a8fa54` | 2026-09-08T16:13:14+02:00 | added history lables as rater R1. | rq6-history rater runbook |
| `9b707502703e` | 2026-09-07T14:38:33+02:00 | paper: reframe abstract around erosion and continuous model maintenance; title gains "and Governance" | eval-plan |
| `214268b4368f` | 2026-09-07T09:47:18+02:00 | Presentation pass on the SANER draft + RQ6-hist/RQ9 human-item prep | stats.py, rq6-history rater runbook, rq9-runbook, eval-plan |
| `f94d1ef79474` | 2026-09-04T15:13:14+02:00 | Second review (2026-09-04, Borderline/Weak Accept): every no-human item addressed | stats.py, eval-plan |
| `5915782ce9a4` | 2026-09-04T13:59:20+02:00 | Second audit of the review response + 1.5a dropped, "under four hours" claim removed | stats.py, rq9-runbook, eval-plan |
| `f7fceeab8a83` | 2026-08-31T11:17:58+02:00 | Audit of the 2026-08-30 review response: corrections applied (plan §10 audit table) | stats.py |
| `3a322bc1aefd` | 2026-08-30T20:18:27+02:00 | Review response (SANER, 2026-08-30): reanalysis, two new drift arms, engine coverage fix, paper rewritten to 12 pp | stats.py, rq9-harness |
| `fb0c2ba5dac8` | 2026-08-30T17:30:56+02:00 | stats.py: refuse to write stats.json without scipy; restore "non-replicable" in the Tier-P caption note | stats.py |
| `47d9f67a41bd` | 2026-08-30T17:25:45+02:00 | Macros: n/a for the by-design-absent Squidex rater-vs-reference arm; drop tool name from a macro name | stats.py |
| `0b3f8df7bfa4` | 2026-08-30T17:21:47+02:00 | Paper draft: all eight sections + abstract, TikZ pipeline figure, terse table captions | eval-plan |
| `81228730e4d0` | 2026-08-30T16:43:13+02:00 | Freeze hygiene: anonymity plumbing, RQ9 kappa companions, stale-artifact fixes | stats.py, rq9-harness, eval-plan |
| `c1232d82832b` | 2026-08-30T15:37:38+02:00 | RQ9 blind curation: executed on eShop + Squidex, records tracked, scored, in the paper | stats.py, rq9-harness, rq9-runbook, eval-plan |
| `0c763fa5d340` | 2026-07-21T11:18:59+02:00 | RQ9 serve instrument: edit-trace logging + curate-loop layout fix | edit-trace logger |
| `ecec4ac23799` | 2026-07-21T09:25:42+02:00 | Fix start-gui.ps1 parse failure under Windows PowerShell 5.1 (ASCII-only) | eshop GUI launcher, squidex GUI launcher |
| `ad9d2cfc05eb` | 2026-07-21T09:10:06+02:00 | RQ9 blind-curation packet tooling: curator GUI launchers + prep notes | eshop GUI launcher, squidex GUI launcher |
| `231a042f5932` | 2026-07-16T12:33:44+02:00 | RQ3 updates. | stats.py |
| `ca7e9de6c5c3` | 2026-07-16T08:08:28+02:00 | plan update after ITK + WCA/Limbo memory issues. | eval-plan |
| `5fa20fbe3d3e` | 2026-07-15T15:11:45+02:00 | Update on RQ2. | stats.py, eval-plan |
| `cca8e1aa0a89` | 2026-07-15T14:12:06+02:00 | Add tests for LLM recovery and Tier-P kit functionality | stats.py, eval-plan |
| `7423242e19f8` | 2026-06-21T11:24:04+02:00 | Continued work on the empirical eval. | stats.py, eval-plan |
| `abae53ace527` | 2026-06-16T13:29:28+02:00 | GUI + extraction fixes. | eval-plan |
| `cd93f852e604` | 2026-06-14T10:08:13+02:00 | continued empirical evaluation | eval-plan |
| `fa2ab035b1c3` | 2026-06-14T08:22:47+02:00 | feat(eval): A2 curation-heuristic ablation on all systems | eval-plan |
| `d419968cbe79` | 2026-06-14T07:50:51+02:00 | docs(eval): the curation-authorship confound + the no-curation floor | eval-plan |
| `d6f0bfff7f22` | 2026-06-13T15:56:16+02:00 | feat(eval): opencv module variant + nopCommerce (3rd C#) + bash limitation — G2 MET | eval-plan |
| `5625b17ed342` | 2026-06-13T15:36:34+02:00 | feat(eval): onboard eShop/abseil/opencv fidelity systems; reclassify serilog | eval-plan |
| `59fb17c45b8c` | 2026-06-13T15:18:00+02:00 | feat(eval): onboard OrchardCore — first C# fidelity result (GT-doc) | eval-plan |
| `4185620b23a6` | 2026-06-13T10:04:43+02:00 | docs(eval): first RQ1/RQ2 fidelity numbers on the ITK GT-pub anchor | eval-plan |
| `edc069262177` | 2026-06-12T16:06:14+02:00 | feat: datasheet enrichment, eval harness, GUI bundle + live-narrative fix | eval-plan |
| `5942cf75f0c6` | 2026-06-07T13:02:42+02:00 | Fixes, Structurizr-data, Cheatsheet, etc. | eval-plan |
| `7db051f8f13d` | 2026-06-04T10:59:21+02:00 | First implemenation + empirical evaluation plan. | stats.py, eval-plan |

Notes. (1) Dates are git author dates of the private evaluation repository. Several
July/August work items — the RQ9 runbook and harness among them — were committed in one
batch on 2026-08-30, so for RQ9 the runbook's commit date alone does not establish that
the protocol pre-dated the sessions. What does: the plan section §10a (first commit above,
2026-07-15) and the session tooling the attestations cite — the curator GUI launchers and
the edit-trace effort logger, committed on the morning of 2026-07-21, hours before the
first eShop session (the curator's edit trace under `references/eshop/blind-curation/`
starts 20:50 UTC that day). The dated, signed rater attestations, the worksheet freeze
dates in `references/<system>/provenance.json`, and the runbook's own dated status header
corroborate. (2) §6.4, §8.2, §12 and §13 are in the very first evaluation-plan commit,
before any baseline was run. (3) Everything the paper labels *post hoc* (the §8.2a combined
arm, the §8.3 real-history mini-arm, the lead rule, ARC's exclusion from the paired test,
the recovery criterion, the adequacy diagnostic, rank-biserial in place of Cliff's δ for
paired contrasts, NMI beside the raw κ) entered §8/§10a/§12 as dated amendments
on 2026-08-30/31, after the data freeze; the extract shows them with their dates.
