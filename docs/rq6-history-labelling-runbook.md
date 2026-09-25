# RQ6 real-history labelling — rater runbook

> **Share this file with both raters.** It is the single instruction set for labelling the
> drift findings of the real-history arm (eval plan §8.3, executed 2026-08-30/31 as a
> post-hoc mini-arm; response plan item 4.2). Paper: §V "Real history" (paper RQ5,
> pre-registered RQ6), Table VII(d) column "Labels". The harness that produced the
> worksheets is described in `mutate/README.md` §8.3; this document is for the two people
> who fill them in.
>
> **Execution status (2026-09-16).** Both worksheets are back and tracked: R1 on 2026-09-08 (`086d66a`), R2 on 2026-09-14 (`7f3abd5`). Aggregated in `be0aebd`: all 26 findings labelled by both, raw agreement 25/26, pooled Cohen's κ 0.922 (14 architectural, 11 non-architectural, 0 extraction noise agreed). The one disagreement — Squidex 7.18.0→7.19.0, the added `frontend/generator/Generator.csproj` (R1 architectural, R2 non-architectural: tooling, not product architecture) — stands as reported; per §1 there is no reconciliation session.

## 1. What we are measuring and why it needs you

The drift detector was run over eight **consecutive release pairs** of three systems with the
committed curation rules held constant across each pair. Every finding it reported is one row
in a worksheet. The detector's precision on *seeded* changes is perfect by construction; what
nobody can compute is whether the findings on **real** history are things a maintainer would
want flagged. That judgement is the label, and it is the paper's only evidence for its
per-pull-request false-finding claim. Two raters label independently; the paper reports the
counts both raters agreed on plus raw agreement and Cohen's κ. Disagreements are reported, not
reconciled, so there is no second session.

## 2. Who, where, how long

- **Two raters.** Anyone who can read a `.csproj` or a `Makefile.am` and a commit. No blinding
  applies: the rows *are* the tool's output, and there is no reference to leak. An author of
  the paper may be a rater; the other should not have written the curation rules of the
  system they label, if that can be arranged (say so in the commit message either way).
- **Files.** Rater 1 edits `worksheet-R1.csv`, rater 2 `worksheet-R2.csv`, in place, under the
  **tracked** `references/<system>/history/<A>__<B>/`. Never the copies under `out/` (they
  are a regenerable mirror; the aggregator reads the tracked file first and warns if the
  mirror carries labels). Five pairs have rows, three have none:

  | Pair                                                               | Rows | Notes                                             |
  | ------------------------------------------------------------------ | ---- | ------------------------------------------------- |
  | `references/libxml2/history/v2.13.0__v2.14.0/`                   | 2    | C, automake; the rules pre-date the releases      |
  | `references/libxml2/history/v2.14.0__v2.15.0/`                   | 0    | nothing to label                                  |
  | `references/libxml2/history/v2.15.0__v2.15.3/`                   | 2    |                                                   |
  | `references/nopcommerce/history/release-4.70.0__release-4.80.0/` | 12   | C#, .NET 8→9, plugin path refactoring in between |
  | `references/squidex/history/7.17.0__7.18.0/`                     | 8    | C#, Entity Framework support arrives              |
  | `references/squidex/history/7.18.0__7.19.0/`                     | 2    |                                                   |
  | `references/squidex/history/7.20.0__7.21.0/`                     | 0    | nothing to label                                  |
  | `references/squidex/history/7.21.0__7.22.0/`                     | 0    | nothing to label                                  |

  26 rows per rater in total, with commit hints already attached: **20–40 minutes**.
  The three worksheets with 0 rows hold only the two header lines because the detector
  reported nothing between those releases. Leave them exactly as they are; they are not an
  error and there is nothing to do in them.
- **Independence.** Do not discuss a row with the other rater until both files are handed
  back. The κ is the point; a reconciled label is worth nothing here.

## 3. Reading a row

Columns: `finding_id, kind, subject, counterpart, hint, label, note`. Edit **only** `label`
and `note`. Do not reorder, add or delete rows, and keep the `#` comment line at the top.

- **`kind`** — what the detector reports. In these worksheets: `new_target` (a first-party
  build unit present in B, absent in A), `removed_target` (the reverse), `new_edge` (a
  declared dependency `subject → counterpart` present in B only), `removed_edge` (the
  reverse). The harness can also emit `suspected_rename`, `coverage_gap_*` and
  `layering_violation`; none occurred in these pairs.
- **`subject`** / **`counterpart`** — stable identifiers of build units:
  `csharp:csproj:<path>` is a .NET project (the path is its `.csproj`);
  `cpp:target:<name>` is a Makefile/automake target (`libxml2` is the library, `testThreads`
  and `testcatalog` are test executables). An edge means *subject declares a dependency on
  counterpart*: a `<ProjectReference>`, or a Makefile link (`_LDADD`).
- **Scope.** Only first-party, **declared** (L0) build dependencies are findings. NuGet and
  other external references, include or symbol evidence, and anything the run could not
  extract in both releases are out of scope by design and never appear as rows.
- **`hint`** — up to three commits between A and B whose diff added or removed the subject's
  name in its owning build file (`git log -S`). The hint is a lead, not the answer: a
  tree-wide commit such as nopCommerce's "Path refactoring in .csproj files of plugins" or
  "Update to .NET 9" touches every project file and shows up on most rows; look for the
  commit that actually introduced or removed the unit or the reference.

## 4. The three labels

Ask one question first: **did the system's build-graph structure really change between A
and B?** Then, if it did, whether that change matters architecturally.

| Label                 | Use it when                                                                                                                                                                                                                               | Typical shapes                                                                                                                                                                                                                                    |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `architectural`     | A real change of the first-party build-graph structure that a maintainer of the architecture model would want flagged: a production unit appeared or disappeared, or a declared dependency between production units was added or removed. | A new library or service project and the references it introduces; a subsystem gaining a dependency on another it did not have before; a unit deleted or merged away.                                                                             |
| `non-architectural` | A real change in the code, but with no architectural significance: the structure a model would show is unchanged, or only tooling, test, sample, packaging or code-generation scaffolding moved.                                          | A test executable or test project added or removed; a reference added from a test project to what it tests; a generator or build helper project; a unit renamed or moved without changing what depends on what.                                   |
| `extraction-noise`  | Not a real change of the code: the unit or reference exists in both releases and only the extractor's view of it changed.                                                                                                                 | A project that was present in A but not parsed there (solution-format change, conditional`<ProjectReference>`, a build file the static parser could not follow); an identifier that changed because a path moved while the unit itself did not. |

Judgement calls, and how to make them:

- **Test and sample targets** are the common borderline. Decide by the criterion, not by a
  rule of thumb: a new test program is a real change, but is it one the *architecture*
  should reflect? If you would exclude it from a container diagram, it is
  `non-architectural`. Whatever you decide, write "test target" in the note so the two
  raters' reasoning can be compared.
- **A new unit plus its edges** produce several rows (one `new_target`, several
  `new_edge`). Label each row on its own merits; they need not all get the same label — a
  new plugin may be architectural while its reference to a test helper is not.
- **Plugins, extensions, modules**: a new plugin project in a plugin architecture is a new
  unit in that layer. Whether that is architectural depends on whether the model shows
  plugins as units; if in doubt, label it as the model you would draw for the system would
  treat it, and explain in the note.
- **Suspecting noise?** Check that the subject really is absent from release A:
  `git -C fixtures/<sys> show <A>:<path-to-build-file>` (see §5). If it is there, the row is
  `extraction-noise`, and the note should say what the extractor tripped over.

Spelling matters: the aggregator accepts exactly `architectural`, `non-architectural` and
`extraction-noise` (lower case); anything else is reported on stderr and counted nowhere.

## 5. Looking at the code

The subject repositories are **not** in this repository: `fixtures/` is gitignored, and
`arch fetch` would only give you a shallow snapshot at one pinned commit, without the release
tags. You have two ways to see a hint commit or a release, and you need no tooling from this
repository for either.

**On the web (no clone).** Every hint is a commit in the public upstream project; paste the
abbreviated SHA into the project's commit URL, and the tags into its tree URL:

| System      | Commit`<sha>`                                              | Release`<tag>`                                                                                             |
| ----------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| libxml2     | `https://gitlab.gnome.org/GNOME/libxml2/-/commit/<sha>`    | `https://gitlab.gnome.org/GNOME/libxml2/-/tree/<tag>` (`v2.13.0`, `v2.14.0`, `v2.15.0`, `v2.15.3`) |
| Squidex     | `https://github.com/Squidex/squidex/commit/<sha>`          | `https://github.com/Squidex/squidex/tree/<tag>` (`7.17.0` … `7.22.0`)                                 |
| nopCommerce | `https://github.com/nopSolutions/nopCommerce/commit/<sha>` | `https://github.com/nopSolutions/nopCommerce/tree/<tag>` (`release-4.70.0`, `release-4.80.0`)          |

To compare a build file between the two releases on GitHub:
`https://github.com/Squidex/squidex/compare/7.17.0...7.18.0` (then filter to the file), and
on GitLab `https://gitlab.gnome.org/GNOME/libxml2/-/compare/v2.13.0...v2.14.0`.

**With a local clone.** Clone the upstream project once with its tags (the three are 20–90 MB
of history) anywhere you like, then use `git` directly. nopCommerce ships under a restrictive
licence: cloning it to read history is fine, but do not copy its sources into this repository.

```powershell
git clone https://gitlab.gnome.org/GNOME/libxml2.git
git clone https://github.com/Squidex/squidex
git clone https://github.com/nopSolutions/nopCommerce
git -C squidex show 4fe545e8d --stat                                # a hint commit
git -C squidex log --oneline 7.17.0..7.18.0 -- backend/src/Squidex.Data.EntityFramework
git -C nopCommerce diff release-4.70.0 release-4.80.0 -- src/Plugins/Nop.Plugin.Widgets.Swiper/Nop.Plugin.Widgets.Swiper.csproj
git -C libxml2 show v2.13.0:Makefile.am | Select-String testThreads   # is the target in A?
```

(On the machine that ran the harness the same clones sit under `fixtures/<system>` with the
tags already fetched; the commands work there with `-C fixtures\<system>`.)

Optional context: the full drift report of a pair, `out/<sys>/history/<A>__<B>/drift.md`,
lists what the run suppressed as coverage gaps and how many targets the frozen rules left
unmapped. It lives only on the harness machine (regenerable, not tracked); ask the
facilitator for a copy if you want it.

## 6. Filling the file

1. Open the worksheet in a text editor or VS Code, not a spreadsheet, if you can. If you
   must use a spreadsheet, keep the `#` line, save as **CSV UTF-8**, and check afterwards
   that the first two lines are unchanged and no number was reformatted.
2. For each row: read `kind` and `subject`, open the hint commits, diff the owning build
   file between A and B if the hints do not settle it, decide, type the label, and write a
   one-line `note` (required for `architectural` and `extraction-noise`, recommended always:
   the notes are what makes a disagreement interpretable).
3. Every row of every non-empty pair gets a label. Leave nothing blank: an empty label is
   "pending" to the aggregator, not "undecided".
4. Hand the files back (commit them, or send them to the facilitator). Do not touch the
   other rater's file.

## 7. What happens next (facilitator)

`make all` in `analysis/` (with the repo's venv Python and the `--anon` export as usual)
picks the tracked worksheets up: `rqSixHistLabelsPending` flips to "done", per-label counts
are the findings **both** raters gave that label (`rqSixHist{Architectural,NonArchitectural, Noise}`), `rqSixHistLabelsAgree` is the raw agreement and `rqSixHistLabelsKappa` the pooled
Cohen's κ over all pairs; Table VII(d) shows the counts. Record the two raters' roles in the
commit message. The worksheets, once labelled, are irreplaceable human artifacts under the
eval §14.2 carve-out: never regenerate them into `references/`, and never edit them after the
numbers are in the paper.
