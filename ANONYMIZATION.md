# Anonymization

This snapshot was produced for double-anonymous review. A text scrub was applied to every
UTF-8 text file (source, docs, YAML/JSON/CSV/RSF data, LaTeX, logs, LLM replies, the built
web bundle) and to file/directory names; binaries (images, fonts) were copied verbatim and
byte-scanned. Replaced categories — the original strings are deliberately not listed:

| category | replacement | notes |
|---|---|---|
| tool name (all case variants, incl. inside identifiers, env-var names, paths) | `Anon` / `anon` / `ANON` | the Python package is `src/anon/`; the `arch` console script keeps its generic name — it is the CLI name the paper uses too (`arch run`, `arch propose`); env vars read `ANON_*` |
| author name(s) (first/last/combined forms), a colleague named as backup approver | `[anonymized]` | one cited third-party author who shares the surname is left intact (bibliography) |
| author e-mail addresses | `[anonymized]` | |
| organisation name (also the hyphenated form inside two file names) | `[anonymized]` / `-org-` | the Tier-P material talks about "the organisation" |
| the proprietary Tier-P system's working name | `[anonymized]` | the system is coded `P-1` everywhere in data |
| local Windows user-profile paths / user ids | `[anonymized]` / `[anonymized]-home` | |
| the private repository's checkout path and name (MSBuild/Roslyn diagnostics in `extracted-facts.json`, ITK/OpenCV baseline logs) and the agent-session scratch path a few `out/` artifacts recorded | `<repo>` / `/<scratch>` | not identity-bearing; scrubbed so the snapshot names no private location |

What is NOT scrubbed: the open-source subject systems and their maintainers, cited authors,
rater codes (`R-A`…`R-D`, already codes in the tracked records), dates. Attestation
signatures were already `REDACTED` in the tracked records.

Git history is not included (author metadata); see `PROTOCOL-HISTORY.md` for the commit
hashes and dates that matter.
