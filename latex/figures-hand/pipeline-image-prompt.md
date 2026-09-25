# Pipeline overview figure — image-model prompt (draft for iteration)

**Status:** draft v1, 2026-08-30. Companion to `pipeline.tex` (the TikZ version that the SANER
manuscript actually uses). This file is *not* read by the paper build.

## When to use which

| Variant | Use it for | Why |
|---|---|---|
| `pipeline.tex` (TikZ, vector) | **the paper** | fonts match the body text, scales without blur, tool name goes through `\tool` (anonymity build), editable as text, diff-reviewable, redrawable per venue (plan §1.4 #5) |
| Prompt B below (editable diagram *source*) | README, technical report, talk, a first layout sketch | you get draw.io / SVG / PlantUML you can fix by hand; no text-rendering gamble |
| Prompt A below (rendered raster image) | slides, social/README hero image, a quick "does this layout read?" mock-up | fast and pretty; but see *Known failure modes* — do not put the result in the paper without redrawing |

Recommendation: keep the TikZ figure in the manuscript. Use Prompt A only to explore layout
alternatives quickly, then port the winner back into `pipeline.tex`.

---

## Prompt A — rendered image (ChatGPT / GPT image model)

Paste verbatim. The block is deliberately over-specified: image models drop or invent boxes and
misspell labels unless every box and label is enumerated.

```
Create a clean, flat, technical block diagram for a software-engineering research paper
(IEEE two-column format, wide figure spanning both columns). Landscape, aspect ratio about 16:5,
white background, no gradients, no shadows, no 3D, no icons of people or robots, no logos,
no decorative elements. Sans-serif font, black text, all text perfectly legible and spelled
EXACTLY as given below. Thin black outlines; one light-grey fill for "deterministic" stages;
nothing else coloured.

LAYOUT: one horizontal flow from left to right, three rows.

MIDDLE ROW (the main flow) — seven boxes of equal width connected by right-pointing arrows:
  [0] "Repository" (white fill, rounded) with small text: "CMake, MSBuild, automake build files; C, C++, C# sources"
  [1] "1 Extract" (light-grey fill) with small text: "L0 build graph, L1 package refs, L2 includes & symbols"
  [2] "2 Normalize" (light-grey fill) with small text: "merge fragments; stable ids, weight, confidence"
  [3] "3 Curate" (WHITE fill, DOUBLE outline) with small text: "group, exclude, threshold; declared edges never dropped"
  [4] "4 Enrich" (WHITE fill, DASHED outline) with small text: "names & descriptions; schema + integrity check"
  [5] "5 Generate" (light-grey fill) with small text: "lift target edges to container edges; emit DSL"
  [6] "6 Check" (light-grey fill) with small text: "validate, drift, layering rules, stopping rules"

TOP ROW — one small white document-style box directly above each of boxes [1]–[6], each connected
to its stage by a short upward arrow, monospace text:
  above [1]: "fragments/*.json"  (one per extractor)
  above [2]: "extracted-facts.json"
  above [3]: "mapping-rules.yaml"  (hand-owned)   — this arrow is DOUBLE-HEADED
  above [4]: "enriched-facts.json"
  above [5]: "generated-*.dsl  + workspace.dsl views"
  above [6]: "architecture-drift.md"
Add one long DASHED arc above the top row from the "extracted-facts.json" box to the
"architecture-drift.md" box, labelled in italics: "diff against the committed model".

BOTTOM ROW:
  directly below box [3], a small white box "Architect — reviews the proposal, edits rules
  (YAML or GUI)" connected to box [3] with a double-headed vertical arrow;
  directly below box [4], a small white DASHED box "LLM (optional) — off by default; cannot add
  a relationship" connected to box [4] with a double-headed DASHED vertical arrow;
  under boxes [1]–[2] a thin horizontal bracket labelled in italics "deterministic;
  byte-identical across reruns";
  under boxes [5]–[6] a thin horizontal bracket labelled in italics "deterministic;
  overwritten every run".

Do not add any box, arrow, label, legend, or title that is not listed. Do not draw a tool name
or product name anywhere. Output at high resolution (at least 3000 px wide).
```

### Anonymity variant
The prompt above already contains no tool/author/organisation name. If a *named* version is wanted
for the TR or slides, add after "MIDDLE ROW": `Put the title "Anon" in bold in the top-left
corner.` — and never use that variant in the SANER build.

### Iteration checklist (compare the image with `pipeline.tex`)
- [ ] exactly 7 flow boxes, 6 artifact boxes, 2 actor boxes, 2 brackets, 1 dashed arc
- [ ] every label spelled exactly (image models love "Normalise/Nomalize", "mapping-rule.yml")
- [ ] box [3] double outline, box [4] dashed outline — the two visual codes the caption relies on
- [ ] the arc goes from *extracted-facts* (not curated) to *drift*
- [ ] no invented icons, gears, clouds, robots, "AI" sparkles, or a legend we did not ask for
- [ ] readable when printed in greyscale at 17 cm width
- [ ] nothing that reveals the tool or the organisation

### Known failure modes of image models on diagrams
Mis-spelled or garbled text in small boxes; silently merged or duplicated boxes; arrows that start
or end nowhere; a "legend" or title hallucinated in; inconsistent box widths; and — the reason it is
unsuitable as the paper's source of truth — the result is a raster you cannot edit, anonymise, or
re-caption per venue without regenerating and re-checking everything. Expect 3–6 iterations for a
usable slide image.

---

## Prompt B — editable diagram source (recommended for anything outside the paper)

Same content, but ask for source instead of pixels; then open and fix by hand.

```
Produce the following block diagram as <draw.io XML | SVG | PlantUML | TikZ> source code that I
can paste into a file. Flat style, white background, sans-serif, one light-grey fill for
"deterministic" stages, thin black outlines, no icons. Then the LAYOUT / MIDDLE ROW / TOP ROW /
BOTTOM ROW specification from Prompt A, verbatim.
```

For draw.io: save the answer as `pipeline.drawio`, export to PDF (File → Export as → PDF,
"crop" on), and `\includegraphics` it — vector, and the text stays editable. For the paper,
`pipeline.tex` remains the canonical version.
