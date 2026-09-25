# RQ9 blind-curation experiment — researcher runbook

> **Share this file with every participant.** It is the single instruction set for the
> RQ9 blind-curation experiment (eval plan §10a; companion plan
> `plans/2026-07-13-human-reference-oss-extension.md` §5). Everyone sees every role's
> instructions — that is deliberate, so you understand the whole experiment. What you may
> **not** see is role-specific data (reference mappings, other raters' worksheets, any
> fidelity score) — the blinding rules in §4 are the load-bearing part of this document.
> **Pre-registration record (corrected 2026-08-31).** The roles, blinding rules, stopping
> rules, and measures were pre-registered in the evaluation plan §10a on 2026-07-15
> (`cca8e1a`). The session tooling the attestations cite — the curator GUI launchers
> (`overlays/*/start-gui.ps1`, `ad9d2cf`) and the edit-trace effort logger
> (`src/anon/edit_trace.py`, `0c763fa`) — was committed on 2026-07-21, hours before the
> first eShop session (the curator's edit trace starts 20:50 UTC that day). This runbook
> *operationalizes* that protocol; it was written from §10a during the sessions and committed
> with the executed records on 2026-08-30 (`c1232d8`), so its own commit date does **not**
> establish that it pre-dated the sessions — the plan section and the tooling commits do.

> **Execution status (2026-08-30).** Both systems' core sessions are complete and frozen:
> eShop — raters R-C/R-D 2026-07-21, curator R-A 2026-07-21, reconciliation 2026-08-04;
> Squidex — rater R-A 2026-08-14, rater R-B 2026-08-26, curator R-C 2026-08-26 (freeze),
> reconciliation 2026-08-28 (fine granularity decided). **The two optional arms — the Squidex
> second curator (R-D) and the eShop manual-modeling comparator (R-B, §7) — were dropped and
> will not run**; the executed design is exactly the core (one curator + two raters per
> system). Records: `references/<sys>/blind-curation/` (see §8 "Artifact contract").
> **Phase 2 scored 2026-08-30 for both systems** (`effort-<C>.json`, `fidelity-<C>.json`,
> eShop also `fidelity-R-A-vs-consensus.json`; Squidex SAR baselines run the same day);
> rows are in `analysis/results.csv` (axis `RQ9`). Unblinding (step 7) may proceed.

## 1. What we are measuring and why it needs you

Anon recovers software architecture from build graphs. For C# systems, a human
**curator** groups build targets into containers with the tool's assistance. Scoring a
curation against a reference the curator could see is circular — it proves nothing. This
experiment produces the one **non-circular** number: a curator who has *never seen the
reference* curates a system, and independent raters build the reference; then a neutral
scorer compares them. The headline per system is a four-row table (eval §10a.4):

| Row | What | Kind |
|---|---|---|
| no-curation floor | the build graph alone, zero grouping | detection (context) |
| best autonomous SAR tool | ACDC / ARC / WCA / LIMBO | detection (context) |
| **blind curator via Anon** | assisted human, reference-blind | **expression — the number** |
| **human ceiling (R₁ vs R₂)** | how much two independent humans agree | **ceiling** |

The claim under test: *the blind curator approaches the human ceiling where autonomous
techniques cannot.* You cannot beat the ceiling; closing the gap to it is the result.
We also measure **effort** (how long, how many edit operations, how much of the tool's
auto-proposal survives). *(A manual-modeling comparator on one system was planned as an
optional arm — dropped, see status above; the paper argues that foil qualitatively.)*

## 2. Systems, roles, and who does what

Two systems (the "defensible minimum" of companion plan §2), four researchers
(**R-A…R-D** — the facilitator maps codes to people), plus the **facilitator/neutral
scorer** ([anonymized] — prepares packets, moderates reconciliation, runs all scoring; he is the
only person who ever sees everything, and only after each freeze).

| System | Language / size | Reference situation | Granularity |
|---|---|---|---|
| **eShop** (dotnet/eShop) | C#, 19 projects | committed GT-doc reference exists → two raters **re-derive it independently** (the W3a revalidation: κ against each other *and* the committed reference) | target (project) |
| **Squidex** | C#, ~20 backend projects | no reference → two raters **build it** from code + the project's own docs, incl. the stale-doc delta ledger (companion §5 steps 2–4) | target; file-level split only for units the raters flag as mixed (HG1 decides the scored granularity) |

### Role assignment (proposed — facilitator confirms before sessions start)

| Researcher | eShop | Squidex | Estimated total |
|---|---|---|---|
| **R-A** | Blind curator | Reference rater R₁ | ≈ 6–8 h |
| **R-B** | ~~Manual-modeling comparator (optional arm, §7)~~ **dropped**; **re-assigned 2026-09-04: blind curator #2** (curator-variance arm, response plan 1.5b — packet `out/eshop/packets/curator-R-B.zip`, same tooling as R-A; §8 step 5c) | Reference rater R₂ | ≈ 6–8 h |
| **R-C** | Reference rater R₁ | Blind curator | ≈ 5–6.5 h |
| **R-D** | Reference rater R₂ | ~~Blind curator #2 (optional — a second curator measures curator variance)~~ **dropped** | ≈ 5–6.5 h |

Why this shape: within each system the curator and the two raters are three distinct
people (eval §10a.1 — the separation *is* the experiment). Across systems roles swap, so
everyone rates once and curates (or hand-models) once, and nobody ever holds two roles on
the same system. The facilitator cannot curate eShop (he authored its committed
reference). If a slot must be dropped, drop the optional arms first (Squidex curator #2,
then the manual comparator); the core is one curator + two raters per system.
**Outcome:** both optional arms were dropped (2026-08-30) — curator variance and the
manual foil are therefore *not measured* and must not be claimed in the paper.

## 3. Schedule (target: sessions in the weeks of 2026-07-20 / 07-27)

| Step | Who | When |
|---|---|---|
| 0. Packets prepared, Squidex onboarded (HG1) | facilitator | before any session |
| 1. eShop rating (independent) | R-C, R-D | week 1 |
| 2. eShop reconciliation call | R-C + R-D + facilitator | week 1 |
| 3. eShop blind curation (manual model: dropped) | R-A | week 1 (any time after step 0; independent of steps 1–2) |
| 4. Squidex rating (independent) | R-A, R-B | week 2 (R-A only after finishing step 3) |
| 5. Squidex reconciliation call | R-A + R-B + facilitator | week 2 |
| 6. Squidex blind curation | R-C (+ R-D) | week 2 (any time after step 0) |
| 7. Scoring + unblinding | facilitator | after ALL freezes |

Curation and rating of the *same* system can run in parallel — they don't interact until
scoring. **No score is circulated to anyone until step 7.**

## 4. Blinding rules (read this even if you skip everything else)

| Role | You MAY use | You MUST NOT look at |
|---|---|---|
| Reference rater | the repository (pinned checkout in your packet); the project's own public docs (architecture pages, README, ADRs, e-book) | any Anon output (models, GUI, proposals, exports); the committed `references/` mapping (eShop raters!); the other rater's worksheet — until the reconciliation call |
| Blind curator | the repository; the same public project docs; the Anon GUI, `arch propose` draft, and build facts — the tool is *your instrument* | any `reference.rsf` or rater worksheet; the evaluation repo's `references/` and `overlays/` directories; **any fidelity score** (no score-driven tuning — this is why your stopping rule is your own judgment) |
| Manual comparator | the repository; the public project docs; your modeling tool of choice | same as the blind curator (no Anon output either — your arm is the *unassisted* human) |

Practical enforcement: your packet contains only what your role may see (§5/§6). Don't
clone the evaluation repo; don't browse `references/`, `overlays/`, or teammates'
artifacts. After your session you sign the attestation (§9). If you accidentally see
something forbidden, tell the facilitator immediately — an honest exclusion beats a
silently contaminated number (it becomes a documented threat, not a scandal).

## 5. Instructions — Reference rater (R₁ / R₂)

**Goal:** independently map every first-party entity to a named architectural component,
from code + the project's own docs. **Deliverable:** your filled worksheet CSV + (Squidex
only) delta-ledger notes. **Time: eShop ≈ 2–2.5 h; Squidex ≈ 4–6 h.**

Your packet (from the facilitator): the pinned repo checkout, links/copies of the
project's public architecture docs, and `worksheet-<you>.csv` — one row per entity
(`entity,cluster,notes`), entity ids are project paths. The worksheet is generated from
the build graph's entity list only; it contains no tool judgment.

1. **Briefing (15 min).** Read this section and §4. Note your start time.
2. **Read the docs (eShop 20–30 min; Squidex 30–45 min).** Treat the doc as *the
   project's documented intent at its date* — for Squidex the authoritative architecture
   page is ~2019 and stale; that mismatch is data, not a nuisance (step 4).
3. **Map (eShop 60–90 min; Squidex stage 1: 60–90 min).** For each worksheet row, fill
   `cluster` with a component name — **use the project's own documented names wherever
   they exist** (κ is computed on names; invented synonyms depress it for no reason).
   Write `EXCLUDE` for out-of-scope entities (tests, samples, build tooling) with a short
   reason in `notes`. Judge from code + docs; when they conflict, map to your best
   architectural judgment and leave a note.
4. **Squidex only — delta ledger.** Whenever you meet something the 2019 doc doesn't
   cover (a component grown since) or a documented concept with no code, add a `notes`
   entry: `DELTA: <what> -> extend|assign|exclude — <one line why>`. These become
   `references/squidex/doc-delta.json` at reconciliation — a measured-staleness result.
   *Outcome (2026-08-28): neither rater wrote a DELTA line — both found the 2019 doc too
   coarse/old to inform target-level clustering and mapped from current code. The ledger is
   closed with no entries **by finding** (staleness not measurable through this instrument
   at target granularity), recorded in `doc-delta.json`; lesson for a re-run: pre-check
   whether the doc even describes the system at the worksheet's granularity.*
5. **Squidex only — stage 2 (60–120 min).** If a project/namespace is architecturally
   *mixed* (e.g. write-model and read-model code in one project — the CQRS cut), flag it
   in `notes` (`MIXED`). The facilitator will send you a file-level worksheet for exactly
   those units; assign each file. The *fraction of code needing file-level judgment* is
   itself a reported number (how much the architecture fights the file system).
6. **Record your end time** in the worksheet's last-line comment
   (`# wall-clock: <start>–<end>, total <min> min`), and send the CSV to the facilitator
   **only** (never to the other rater or a curator).
7. **Reconciliation call (eShop 30–45 min; Squidex 60–90 min).** Only *after* both
   worksheets are in: the facilitator shows the disagreements; you discuss each to a
   consensus mapping and jointly settle every DELTA disposition. Disagreements are
   resolved by argument, not by seniority; unresolvable ones are recorded as such. The
   consensus becomes `reference.rsf`; your *pre-consensus* worksheets are what κ and the
   ceiling row are computed from — do not edit them afterwards.

**Data collected from you:** the filled worksheet (→ `rater-<you>.rsf`), wall-clock
minutes, delta-ledger notes, the reconciliation outcome.

## 6. Instructions — Blind curator (C)

**Goal:** produce the best `mapping-rules.yaml` you can for the system, using Anon,
stopping when *you* judge the model review-complete. **Deliverable:** your frozen rules +
the automatically-logged edit trace + the attestation. **Time: eShop ≈ 1.5–2 h;
Squidex ≈ 2.5–4 h.**

Your packet: the pinned repo checkout, the public project docs, and a prepared Anon
workspace (the facilitator has already run `arch run --no-llm` and `arch propose` in it —
you never touch the evaluation repo). A `start-gui.ps1` in the packet sets
`ANON_EDIT_TRACE` (every edit you apply is timestamped into
`edit-trace.jsonl` — that's the effort measurement, so **always start the GUI through the
script**) and launches `arch serve`.

1. **Setup (15 min).** Read this section and §4. Run `start-gui.ps1`, open the GUI.
   Note your start time.
2. **Orient (eShop 15–30 min; Squidex 45–60 min).** Browse the repo, the docs, and the
   tool's build facts. You may read the same public architecture docs the raters use —
   both of you transcribing the *project's* intent is the designed setup, not leakage.
3. **Curate (eShop 45–90 min; Squidex 90–150 min).** Review the `arch propose` draft in
   the GUI (accept, reject, or modify its groups), then group the remaining targets via
   the curation tree editor: create containers, drag members, add globs/tags, exclude
   non-production targets, name things properly. Work in as many sittings as you need —
   the trace keeps accumulating; just always start via the script.
4. **Stopping rule (pre-registered):** stop when the model is *review-complete by your
   own architectural judgment* — no unmapped first-party target, every container a
   coherent responsibility you could defend to the project's maintainers. There is no
   score to chase; you couldn't see one if you wanted to.
5. **Freeze (10 min).** Tell the facilitator you're done; note your end time; fill the
   attestation (§9) including your total wall-clock (all sittings). Do not edit the rules
   after freezing.

**Data collected from you:** the frozen `mapping-rules.yaml`, `edit-trace.jsonl`
(op counts + timestamps, recorded automatically), self-reported wall-clock, the
attestation. The scorer later computes: fidelity vs the reference, effort metrics, and
the fraction of `propose` suggestions you kept vs hand-authored.

## 7. Instructions — Manual-modeling comparator (optional arm, eShop) — **DROPPED, not executed**

> Kept for the record of what was pre-registered; no manual model was produced and no
> `manual-model/` artifact exists. The paper reports the assisted workflow's governance
> properties (determinism, drift, evidence links) as arguments, not as a measured contrast.

**Goal:** the same decomposition task *without* Anon — the honest foil (eval §10a.5).
**Deliverable:** a hand-made architecture model + wall-clock. **Time ≈ 1.5–2.5 h.**

1. Packet: the pinned eShop checkout + public docs. Same blinding as the curator (§4) —
   and no Anon either.
2. From code + docs, hand-author a container-level architecture model in a tool of your
   choice (hand-written Structurizr DSL, draw.io, PlantUML — your call; pick what you'd
   really use). Every first-party project should be accounted for; draw the container
   dependencies you believe exist.
3. Same stopping rule as the curator; record total wall-clock; send the artifact +
   attestation to the facilitator. The scorer transcribes your grouping to RSF and scores
   it like the curator's — plus we report what the artifact *cannot* do (no determinism,
   no drift check, no evidence links), which is the point of the comparison.

## 8. Instructions — Facilitator / neutral scorer

Everything below runs in the evaluation repo (`.venv` active). Raters and curators never
run these commands.

**Phase 0 — packets (before any session; ≈ 3–5 h incl. Squidex onboarding).**

1. Onboard Squidex per companion plan §5 step 1 (pinned SHA fixture, overlay dir,
   `arch run --no-llm`, `arch export --graph`; HG1: record fileDeps coverage, decide the
   scored granularity).
2. Generate the master worksheets (entity list only — the tool strips the pipeline's
   clusters so a worksheet can't leak):
   ```powershell
   python baselines\rq9_blind_curation.py worksheet eshop   --granularity target
   python baselines\rq9_blind_curation.py worksheet squidex --granularity target
   # Squidex stage 2, per rater-flagged mixed unit:
   python baselines\rq9_blind_curation.py worksheet squidex --granularity file --owner "csharp:csproj:*Squidex.Domain*" --out out\squidex\blind-curation\worksheet-file-domain.csv
   ```
   Copy per rater (`worksheet-R-C.csv`, …). Rater packets: fixture checkout + docs +
   worksheet. **No Anon install, no `references/`, no `overlays/`.**
3. Curator packets: fixture checkout + docs + a fresh workspace (arch dir + an *empty
   default* rules dir — never the committed overlays) with `arch run --no-llm` and
   `arch propose` already executed, plus `start-gui.ps1`:
   ```powershell
   $env:ANON_EDIT_TRACE = "$PSScriptRoot\out\<sys>\blind-curation\edit-trace-<C>.jsonl"
   arch serve --repo <fixture> --arch-dir <arch-dir> --rules-dir <curator-rules-dir>
   ```
4. Store both systems' references (and eShop's committed one) *outside* every packet.

**Phase 1 — during sessions.** Distribute packets; collect worksheets; moderate the
reconciliation calls (project disagreements row by row; record consensus + every DELTA
disposition). After Squidex reconciliation: write `references/squidex/reference.rsf` +
`provenance.json` (raters, dates, κ from below, delta counts) + `doc-delta.json`; post
the maintainer-endorsement question (companion §5 step 5 — non-blocking).

**Phase 2 — scoring (after ALL freezes; ≈ 1–2 h per system).**

```powershell
# 1. Rater RSFs + the ceiling row (κ + pairwise MoJoFM; --reference adds the W3a
#    each-rater-vs-committed-GT-doc check on eShop)
# 0. Move the human artifacts into their TRACKED home first (see "Artifact contract"):
#    references\<sys>\blind-curation\  <- worksheets, attestations (names REDACTED), edit trace
#    overlays\<sys>-blind-<C>\mapping-rules.yaml  <- the curator's frozen rules, verbatim
python baselines\rq9_blind_curation.py ingest references\eshop\blind-curation\worksheet-R-C.csv --out references\eshop\blind-curation\rater-R-C.rsf
python baselines\rq9_blind_curation.py ingest references\eshop\blind-curation\worksheet-R-D.csv --out references\eshop\blind-curation\rater-R-D.rsf
python baselines\rq9_blind_curation.py ceiling references\eshop\blind-curation\rater-R-C.rsf references\eshop\blind-curation\rater-R-D.rsf --names R-C,R-D --reference references\eshop\reference.rsf --out references\eshop\blind-curation\ceiling.json
# after the reconciliation call: the consensus worksheet -> consensus.rsf (same ingest)
python baselines\rq9_blind_curation.py ingest references\eshop\blind-curation\worksheet-consensus.csv --out references\eshop\blind-curation\consensus.rsf

# 2. The curator's model: re-run the pipeline over the FROZEN rules, export clusters
arch run --repo <fixture> --arch-dir out\eshop-rq9\architecture --rules-dir overlays\eshop-blind-R-A --no-llm
arch export --graph --granularity target --repo <fixture> --arch-dir out\eshop-rq9\architecture --rules-dir overlays\eshop-blind-R-A

# 3. Effort (trace + frozen rules + the propose draft + self-reported minutes)
#    (the propose draft is the copy kept beside the trace, proposed-mapping-rules-<C>.yaml —
#     the curator's workspace under out/ is regenerable and not tracked)
python baselines\rq9_blind_curation.py effort --rules overlays\eshop-blind-R-A\mapping-rules.yaml --trace references\eshop\blind-curation\edit-trace-R-A.jsonl --proposal references\eshop\blind-curation\proposed-mapping-rules-R-A.yaml --wall-clock-minutes <N from the attestation> --out references\eshop\blind-curation\effort-R-A.json

# 4. Baselines exist already (run_arcade + make_comparison); else run them now.
# 5. The four-row table (+ container-edge F1)
#    --common-with pins every row to the ALL-COMMON population of the comparison file (response
#    plan §14 F1): a reference project with no first-party dependency is unseen by the edge-only
#    techniques (Squidex: Translator, Generator), and without it the curator row was scored on the
#    full reference while floor / best-SAR came from the projection. The full-reference score is
#    kept under `sensitivity`. Squidex: --common-with out\squidex\baseline\dir-target\clusters.rsf
python baselines\rq9_blind_curation.py score eshop --curator out\eshop-rq9\architecture\generated\graph\clusters-target.rsf --reference references\eshop\reference.rsf --graph out\eshop-rq9\architecture\generated\graph\graph-target.rsf --comparison out\eshop\baseline\comparison-target.json --ceiling references\eshop\blind-curation\ceiling.json --common-with out\eshop\baseline\dir-target\clusters.rsf --out references\eshop\blind-curation\fidelity-R-A.json
# 5b. SENSITIVITY only (never the headline): the same scoring against the raters' consensus.
#     The file name carries the variant; the analysis stage labels the rows from the JSON's
#     own `reference` field (projection "expression-vs-consensus").
python baselines\rq9_blind_curation.py score eshop --curator out\eshop-rq9\architecture\generated\graph\clusters-target.rsf --reference references\eshop\blind-curation\consensus.rsf --graph out\eshop-rq9\architecture\generated\graph\graph-target.rsf --comparison out\eshop\baseline\comparison-target.json --ceiling references\eshop\blind-curation\ceiling.json --common-with out\eshop\baseline\dir-target\clusters.rsf --out references\eshop\blind-curation\fidelity-R-A-vs-consensus.json
# 5c. SECOND CURATOR (curator-variance arm, response plan 1.5b; eShop R-B, packet
#     out\eshop\packets\curator-R-B.zip): repeat 2, 3 and 5 with R-B (overlays\eshop-blind-R-B,
#     out\eshop-rq9-R-B, effort-R-B.json, fidelity-R-B.json; 5b optional), then score the two
#     curators AGAINST EACH OTHER — the `ceiling` subcommand over the two exported cluster RSFs
#     writes the ceiling.json shape, which the analysis stage ingests as curator-pair rows:
python baselines\rq9_blind_curation.py ceiling out\eshop-rq9\architecture\generated\graph\clusters-target.rsf out\eshop-rq9-R-B\architecture\generated\graph\clusters-target.rsf --names R-A,R-B --out references\eshop\blind-curation\curator-pair.json
#     R-A stays the pre-registered headline curator (stats.py RQ9_PRIMARY_CURATOR); R-B is
#     reported beside it (technique curator:R-B; second-curator row + macros), never in its place.
# 6. Aggregate: analysis/aggregate_results.py picks every file above up (axis RQ9) ->
#    results.csv; then stats -> tables -> macros as usual (analysis/README.md).
```

Then: append κ to `references/<sys>/provenance.json` (`independent_raters` block — the
committed GT-doc stays the scored reference unless κ reveals *real* disagreement,
companion §9 Q4 — and **write the scored-reference decision down there BEFORE computing
any curator fidelity**; a κ that is low only because the raters used different label
vocabularies is not real disagreement — check the `boundary_conflicts` / `merges` /
`splits` rows the analysis stage derives, which separate granularity from structure);
for eShop, report the blind number **beside the existing non-blind 66.7** — the delta
*is* the measured leakage (eval §10a.6). Stability arm (eval §10a.4): freeze the curator's
rules, re-run on a later upstream commit, report churn. Only after all artifacts are
written: unblind everyone (share this table + scores).

**Artifact contract** (eval §14.2, amended 2026-08-30). During a session everything
accumulates under the working mirror `out/<sys>/blind-curation/` (gitignored). **After each
freeze the human-produced artifacts are moved into their TRACKED home** — unlike every other
`out/` artifact they cannot be regenerated:

- `references/<sys>/blind-curation/` — `worksheet-target.csv` (the entity list the raters
  got), `worksheet-<R>.csv` (frozen, with the `# wall-clock:` line), `rater-<R>.rsf`,
  `ceiling.json`, `worksheet-consensus.csv` + `consensus.rsf`, `attestation-<X>.md`
  (**names redacted** — the signed originals stay out-of-tree), `edit-trace-<C>.jsonl`,
  `proposed-mapping-rules-<C>.yaml` (the propose draft the curator saw),
  `effort-<C>.json`, `fidelity-<C>.json` (+ `fidelity-<C>-vs-consensus.json`), and a short
  `README.md` index (roles, dates, what is frozen). (No `manual-model/` — the §7 arm was
  dropped.) A damaged rater export is kept verbatim as `worksheet-<R>.raw.csv` beside the
  repaired `worksheet-<R>.csv` and the deterministic repair script (Squidex R-A precedent).
- `overlays/<sys>-blind-<C>/mapping-rules.yaml` — the curator's frozen rules, byte-for-byte
  (the tracked `--rules-dir` for Phase-2 scoring and the stability arm; same convention as the
  A2 ablation's variant overlays).
- `references/squidex/{reference.rsf, provenance.json, doc-delta.json}` — Squidex only, the
  raters *build* the reference there.

The curator's workspace copy (`ws-<C>/`, MBs) stays under `out/` — Phase 2 step 2 rebuilds
it from the frozen overlay. `analysis/aggregate_results.py` reads the tracked directory
first and falls back to the mirror file by file.

## 9. Attestation (curators, manual comparator; raters sign items 1–2 only)

Copy into `attestation-<you>.md`, tick, sign, date:

```
- [ ] 1. I did not look at any reference.rsf, any rater worksheet, or the evaluation
         repo's references/ or overlays/ directories before my freeze.
- [ ] 2. I saw no fidelity score for this system before my freeze.
- [ ] 3. I started the GUI only via start-gui.ps1 (edit trace active in every sitting).
- [ ] 4. Total wall-clock across all sittings: ___ min (sittings: ___).
- [ ] 5. I stopped by my own review-complete judgment, not by any external signal.
Signed: ____________  Date: ____________  System: ________  Role: ________
```

## 10. FAQ

- **I disagree with the project's docs.** Map by your architectural judgment and leave a
  note; for Squidex, that's exactly what the delta ledger is for.
- **I can't finish in one sitting.** Fine for every role — record each sitting's times;
  curators must relaunch via `start-gui.ps1` each time.
- **The GUI shows me the tool's proposal — is that a blinding violation?** No. As a
  *curator* the tool is your instrument; the proposal is part of the assisted condition.
  Only *raters* must avoid all tool output.
- **Two raters used different names for the same thing.** Expected; κ is computed on
  normalized names, MoJoFM ignores names entirely, and reconciliation settles the label.
  Still, prefer documented names — that's what they're for.
- **I peeked at something forbidden.** Tell the facilitator. Depending on what and when,
  the session is either restarted on the other system, or recorded as a documented
  threat. Silence is the only unrecoverable failure.
- **What happens to my data?** Worksheets/traces/attestations go into the paper's
  replication package (they contain entity ids of open-source projects and your role
  code, not your name). Say so before your session if you object to that.
