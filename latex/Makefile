# Build the paper. Tables/figures/macros are generated from raw experiment data
# by the analysis/ pipeline (plan section 15); `make data` regenerates them.

PAPER   = main
ANALYSIS = ../analysis

.PHONY: all tr data tables figures macros clean

all: $(PAPER).pdf

# Companion technical report (response plan section 13): same macros/tables, distinct title.
tr: tr.pdf

tr.pdf: tr.tex references.bib
	latexmk -pdf -interaction=nonstopmode tr.tex

# Regenerate every committed-but-generated artifact from out/** raw data.
data: tables figures macros

tables:
	python $(ANALYSIS)/make_tables.py  --results $(ANALYSIS)/results.csv --out tables/

figures:
	python $(ANALYSIS)/make_figures.py --results $(ANALYSIS)/results.csv --stats $(ANALYSIS)/stats.json --out figures/

macros:
	python $(ANALYSIS)/make_macros.py  --stats $(ANALYSIS)/stats.json --out generated/macros.tex

$(PAPER).pdf: $(PAPER).tex references.bib
	latexmk -pdf -interaction=nonstopmode $(PAPER).tex

clean:
	latexmk -C $(PAPER).tex
	latexmk -C tr.tex
