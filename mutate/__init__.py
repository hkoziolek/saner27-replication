"""Eval §8 (RQ6) — seeded-mutation drift harness + degradation-correctness check.

``mutate/operators/<operator>.py`` applies a labelled architecture-level mutation to a
checkout copy (eval plan §8.1 / §14.1); ``harness.py`` regenerates facts through the real
pipeline and scores ``drift_report`` detection against the injected label, emitting
``out/<system>/mutation/<id>/{label.json, result.json, drift.md}``. ``degradation.py`` is
the §8.2 induced-degradation / stopping-rules check. Entry point: ``run_rq6.py``.
"""
