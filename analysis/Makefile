# analysis/ — one command from out/** to the paper's tables + macros (eval plan §15.6).
# STDLIB-only stages (aggregate/stats/tables/macros) run anywhere; figures need matplotlib.
#
#   make all      # aggregate -> stats -> tables -> macros  (the live pipeline)
#   make figures  # + make_figures.py (requires matplotlib; stub otherwise)
#   make clean    # remove generated results.csv / stats.json
#
# PY overridable: `make all PY=../.venv/Scripts/python.exe` on Windows.
PY ?= python

# Tier-P (§14.3): the anonymized [anonymized] rows live OUTSIDE out/**, so aggregate only sees them via
# --anon. Without this, `make all` silently drops all Tier-P rows and \tierPSystemCount goes to 0
# — the P-1 row would vanish from the RQ2 table with no error. Wildcard, so a checkout with no
# export (or before the first sign-off) still builds clean.
ANON := $(wildcard ../export-anon/results-anon.csv)
ANON_FLAGS := $(patsubst %,--anon %,$(ANON))

.PHONY: all figures clean
all: ../latex/generated/macros.tex

results.csv: aggregate_results.py $(ANON)
	$(PY) aggregate_results.py $(ANON_FLAGS)

stats.json: stats.py results.csv
	$(PY) stats.py

../latex/tables/rq1_fidelity.tex: make_tables.py results.csv
	$(PY) make_tables.py

../latex/generated/macros.tex: make_macros.py stats.json ../latex/tables/rq1_fidelity.tex
	$(PY) make_macros.py

figures: stats.json results.csv
	$(PY) make_figures.py

clean:
	rm -f results.csv stats.json
