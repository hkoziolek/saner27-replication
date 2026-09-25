# `latex/figures-hand/` — hand-drawn figures (edit these)

The sibling `figures/` directory is **generated** by `analysis/make_figures.py` and must never be
hand-edited. Anything an author draws by hand lives here instead, so the generated/hand split of the
repo (`generated/` vs `rules/`) holds for the paper too.

| File | Section | Content |
|---|---|---|
| `pipeline.drawio` + `pipeline.pdf` | III (Approach) | **In use.** draw.io overview of the pipeline: inputs → six stages → artifacts, with the human gate and the optional LLM stage marked. Icon-led and text-light: GitHub mark for the repository, UML-artifact (`note`) shapes for the files, UML actor for the architect, one icon per stage. Re-export after editing the `.drawio` with `& "C:\Program Files\draw.io\draw.io.exe" -x -f pdf --crop -o pipeline.pdf pipeline.drawio`; included from `sections/03-approach.tex` as `\includegraphics[width=\textwidth]{figures-hand/pipeline.pdf}` inside a `figure*`. Icons are inline SVG data URIs, so the `.drawio` is self-contained and the PDF is fully vector (verified: zero image XObjects). |
| `pipeline.tex` | III (Approach) | **Superseded** by `pipeline.drawio` (2026-09-17). Kept as the TikZ fallback for a venue that forbids external graphics. Core-`tikz` only (no `\usetikzlibrary`), tool name via `\tool`. |
| `model-example.tex` | III / IV (example model) | TikZ transcription of ONE generated container diagram (GNU bash 4.2: 12 containers, all 17 container-level relationships of `out/bash/.../generated-relationships.dsl`), line style = evidence strength, three arrows annotated with their verbatim `curated-facts.json` evidence strings, dashed boxes = `needs-curation`. Core-`tikz` only; drawn ~9.6 × 6.9 cm for `\resizebox{\columnwidth}{!}{...}` inside a single-column `figure`. Header comment lists the source artifacts and every fidelity rule — re-run the pipeline before changing a box or arrow. |
| `pipeline-image-prompt.md` | — | Prompt draft for an image model (raster variant of the same figure, for slides / README / the TR) plus iteration notes. Not used by the paper build. |

Conventions:
- Keep every figure anonymity-safe: no tool, author, or organisation name as a literal (use `\tool`).
- Prefer vector (TikZ / `.pdf` from draw.io) over raster; if a raster is unavoidable, ≥300 dpi at
  final size and legible in greyscale.
- The eval plan (§1.4 #5) requires figures to be *redrawn or re-captioned* per venue; note the venue
  in the file header when a variant is made.
