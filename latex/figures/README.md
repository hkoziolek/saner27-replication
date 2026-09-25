# `latex/figures/` — generated, do not hand-edit

Figures are emitted as `.pgf` (matplotlib pgf backend, so fonts match the paper)
by `analysis/make_figures.py` from `analysis/results.csv` / `analysis/stats.json`.

Planned figures (plan §15.2):

| File | RQ | Content |
|---|---|---|
| `cd_diagram.pgf` | RQ2 | Critical-difference diagram (MoJoFM across techniques) |
| `ablation_tornado.pgf` | RQ3 | ΔMoJoFM per ablated feature |
| `drift_pr.pgf` | RQ6 | Drift precision/recall curves per mutation operator |
| `scaling.pgf` | RQ7 | Wall-clock vs KLOC / #targets (log-log) |
| `human.pgf` | RQ8 | Task time / accuracy / SUS by condition |

Regenerate with `make figures` (see `../README.md`). Committed so the paper
builds without rerunning experiments, but always regenerated from raw data —
never edited by hand.
