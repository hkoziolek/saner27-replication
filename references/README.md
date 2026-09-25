# Reference architectures (`references/<system>/reference.rsf`)

The eval plan's §5.3/§14.1 reference store: one directory per corpus system holding

- `reference.rsf` — the reference decomposition as RSF `contain <cluster> <entity>`
  tuples. For **GT-pub** systems this is the published dataset file **verbatim**
  (never edited); for **GT-doc**/**GT-exp** it is the transcribed/consensus mapping.
- `provenance.json` — grade (GT-pub / GT-doc / GT-exp), source citation, exact
  retrieval URL + date, subject version the reference describes, cluster/entity
  counts, and (GT-doc/GT-exp) the rater-agreement record (κ) — for a system that went
  through the RQ9 blind-curation experiment, the `independent_raters` block (raters,
  date, κ / MoJoFM / a2a, the consensus, and the **scored-reference decision**).
- `blind-curation/` (RQ9 systems only; eval §10a, `docs/rq9-blind-curation-runbook.md`)
  — the TRACKED home of the experiment's human-produced artifacts: frozen rater
  worksheets + RSFs, `ceiling.json`, consensus, redacted attestations, the curator's edit
  trace, effort/fidelity JSON. The curator's frozen rules are the tracked overlay
  `overlays/<system>-blind-<C>/`. `analysis/aggregate_results.py` reads these (axis `RQ9`).

`baselines/run_arcade.py <system>` picks `references/<system>/reference.rsf` up
automatically and scores MoJoFM/a2a against it.

Scoring rule (pre-registered, eval §12 + §14.1 MoJo caveat): metrics are computed
after **projection to the common entity set** of the recovered and reference
clusterings at the analysis stage — `mojo.MoJo` returns NaN on entity-set mismatch,
and the recovered set (first-party, build-reachable files) never exactly equals the
reference set (which may include generated/test/data files). The projection is
applied uniformly to *all* techniques, never tuned per run.

Current contents:

| system | grade | subject version | clusters | entities | status |
|---|---|---|---|---|---|
| itk | GT-pub | ITK 4.5.2 (`v4.5.2`) | 13 | 7657 | anchor — active |
| bash | GT-pub | Bash 4.2 (`bash-4.2`) | 14 | 373 | **anchor #2 — active** since 2026-07-16 (autotools L0 via `extract_autotools` static Makefile.in parse; file gran; was the 0-target limitation case) |
| libxml2 | GT-pub | libxml2 2.4.22 (`LIBXML_2_4_22`) | 18 | 82 | **anchor #3 — active** 2026-07-16 (SARIF FSE'23 industry-labeled GT; automake L0; **LOCAL-ONLY** — SARIF repo has no LICENSE, see `libxml2/.gitignore`; the MONOLITHIC-LIBRARY boundary case: 18 semantic clusters inside one `libxml2.la` target) |
| chromium | GT-pub | see provenance | 69 | 18724 | stretch (GN/Ninja out of scope) |
| orchardcore | GT-doc | main `ab28487` | 34 | 207 | C# anchor — active (target granularity; module `Category` manifests + framework roles) |
| eshop | GT-doc | main `9b4f943` | 9 | 19 | C# microservices — active (target gran; Microsoft documented service decomposition). **RQ9 W3a re-derived 2026-07-21** by two blind raters: κ 0.0 on labels (no shared vocabulary), MoJoFM 66.5 / a2a 89.7 pairwise; both partitions nested with this reference (0 boundary conflicts); 14-cluster consensus = strict refinement. **Committed reference stays scored** (decision in `provenance.json`); see `eshop/blind-curation/`. |
| abseil | GT-exp | `20240722.0` | 21 | 809 | C++ — active (file gran; absl/&lt;library&gt; dirs) |
| opencv | GT-doc | `4.9.0` | 14 | 1087 | C++ — active (file gran; documented modules/&lt;module&gt;) |
| squidex | GT-exp | main `9dbc5ed` | 12 | 15 | C# CQRS/ES on Orleans — **active since 2026-08-30** (target gran — a documented deviation from HG1's file-gran verdict, see `provenance.json`); **two-rater blind consensus** (R-A 08-14, R-B 08-26, reconciled 08-28 at fine granularity): κ 0.391 (flag fired), MoJoFM 54.6, a2a 86.3, **0 boundary conflicts** (R-A strictly coarser). Delta ledger CLOSED with no entries **by finding** — both raters judged the 2019 doc uninformative for target-level clustering and mapped from current code (`doc-delta.json`); maintainer endorsement not posted. RQ9 system #2 → `blind-curation/`. |
| serilog | — | `2ef6364` | — | — | NOT a fidelity system: single production assembly (src/Serilog). Reserved for stability/smoke (RQ5/RQ7) + component-naming; target-granularity fidelity is degenerate (1 project). |
| nopcommerce | GT-doc | `e7db8b4` | 17 | 37 | C# layered monolith — **LOCAL-ONLY** (restrictive license, P§19.6): reference/provenance NOT committed (see `nopcommerce/.gitignore`); numbers in the findings note. |
GT-pub files from the Garcia/Lutellier line were recovered from the Wayback Machine
because the original host (`asset.uwaterloo.ca/ArchRecovery`) no longer resolves — they
are committed here precisely so the evaluation does not depend on a dead link. A **live
mirror** exists today at `https://www.cs.purdue.edu/homes/lintan/ArchRecovery/` (verified
2026-07-16) — cite that alongside the Wayback snapshot.

Confirmed negatives (searched 2026-07-16, so nobody re-searches): the Wayback/uwaterloo
dump holds exactly bash/itk/chromium (+ Java hadoop/archstudio) — no Linux, no Mozilla;
the Bowman/Godfrey/Holt Linux and Godfrey/Lee Mozilla architectures were never archived
machine-readable; the USC softarch wiki GT page is dead and the ARCADE repos ship no
ground truths; **no public C# ground-truth decomposition exists anywhere** in the SAR
literature. The SARIF FSE'23 replication package (`github.com/anonymous2f4a9d/SARIF_FSE23`)
is the one newer source: 3 industry-labeled C/C++ GTs (libxml2 — adopted above; two
OpenHarmony subsystems — GN/ninja, out of build-system scope) plus 16 doc-diagram-derived
C/C++ GTs (microsoft/calculator MSBuild, snapcast/audioFlux/KlayGE/... CMake) usable as
GT-doc-grade extensions if the corpus ever needs them.
