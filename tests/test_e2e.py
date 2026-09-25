"""End-to-end pipeline test + the §1.1 v1 success metrics as CI-gated assertions.

Runs the whole toy pipeline once (via the shared ``toy_run`` fixture) and asserts:

  - every expected artifact is written under the out-of-tree arch dir;
  - **§1.1 metric 1 (Readability GATE)** — Container view ≤25, Component view ≤20
    (``over_budget_views`` empty);
  - **§1.1 metric 2 (Stability GATE)** — the golden harness owns byte-identity; here we
    re-assert the run is deterministic against the committed golden facts hash;
  - **§1.1 metric 3 (Coverage GATE)** — toy L0 first-party coverage == 100%
    (``run-report.json`` ``trust.coverage_pct.L0``);
  - the ``run-report.json`` carries per-stage timings + the §10.2 trust dashboard.

The toy is the smoke fixture (§19.1/§19.3): it proves the v1 gates are *checkable*. The
heavier numeric gates (metric 4 edge-fidelity, metric 6 runtime) are measured on the §19
OSS fixtures once SHAs are pinned (task #5) and are out of scope for this in-repo suite.
"""
from __future__ import annotations

from pathlib import Path

from anon.jsonio import load_json
from anon.model import content_hash

from conftest import GOLDEN_DIR, ToyRun, run_toy_pipeline


# --- artifacts -------------------------------------------------------------------

def test_all_artifacts_written(toy_run: ToyRun) -> None:
    """`arch run` writes the full §12 generated/ set + scaffolds hand-owned files."""
    ws = toy_run.ws
    for p in (ws.extracted_facts, ws.curated_facts, ws.enriched_facts,
              ws.components_dsl, ws.relationships_dsl, ws.drift_md, ws.run_report,
              ws.curation_proposal_md):
        assert p.exists(), f"missing artifact {p}"
    # the machine-readable proposal is emitted at the §12 .yaml path (JSON content is a
    # valid YAML subset; routed through jsonio for byte-stability, curate stage §7.1).
    assert ws.curation_proposal_yaml.exists()
    # scaffold-once hand-owned files land in the (out-of-tree) arch dir, not the fixture.
    assert ws.workspace_dsl.exists()
    assert ws.views_dsl.exists()


def test_pipeline_produces_expected_shape(toy_run: ToyRun) -> None:
    """Toy renders as Model Y (D§2.3 auto-select: N=4 ≤ MODEL_Y_MAX): each build target is
    a container, curated themes become cosmetic group{} labels, and the formerly
    intra-container Domain->Common edge is an honest container edge now."""
    assert toy_run.gen["model_shape"] == "y"
    assert toy_run.gen["targets_n"] == 4
    assert toy_run.gen["containers"] == 4
    assert toy_run.gen["edges"] == 4
    # Model Y carries every edge at container altitude — the component projection is empty.
    assert toy_run.gen["component_edges"] == 0
    # The deterministic L0 run has no namespace tier, so NO Component view is scaffolded —
    # an empty Component view would trip Structurizr's `views.view.empty` inspection. The
    # populated drill-down is exercised by the gated Roslyn path (with_roslyn=True).
    assert toy_run.gen["component_views"] == 0


# --- §1.1 metric 1: Readability GATE ---------------------------------------------

def test_metric1_readability_gate(toy_run: ToyRun) -> None:
    """§1.1 #1: no view exceeds its element budget (Container ≤25, Component ≤20)."""
    assert toy_run.gen["over_budget_views"] == [], \
        f"views over budget: {toy_run.gen['over_budget_views']}"
    # the run-report trust dashboard agrees (the gate a CI step would read, §10.2).
    assert toy_run.report["trust"]["over_budget_views"] == []


# --- §1.1 metric 2: Stability GATE -----------------------------------------------

def test_metric2_stability_gate_against_golden(toy_run: ToyRun) -> None:
    """§1.1 #2: this run's facts content-hash matches the committed golden (byte-stable)."""
    facts = load_json(toy_run.ws.extracted_facts)
    golden = load_json(GOLDEN_DIR / "extracted-facts.canonical.json")
    assert content_hash(facts) == content_hash(golden)


def test_metric2_stability_gate_fresh_rerun(tmp_path: Path, toy_run: ToyRun) -> None:
    """§1.1 #2: a fresh independent run yields byte-identical DSL + facts (re-run half)."""
    other = run_toy_pipeline(tmp_path / "rerun" / "architecture")
    assert other.ws.components_dsl.read_bytes() == toy_run.ws.components_dsl.read_bytes()
    assert other.ws.relationships_dsl.read_bytes() == toy_run.ws.relationships_dsl.read_bytes()
    a = content_hash(load_json(toy_run.ws.extracted_facts))
    b = content_hash(load_json(other.ws.extracted_facts))
    assert a == b


# --- §1.1 metric 3: Coverage GATE ------------------------------------------------

def test_metric3_coverage_gate(toy_run: ToyRun) -> None:
    """§1.1 #3: toy L0 first-party coverage == 100% (read from run-report trust dashboard)."""
    cov = toy_run.report["trust"]["coverage_pct"]["L0"]
    assert cov == 1.0, f"expected 100% L0 coverage, got {cov}"


# --- run-report.json: stage timings + trust dashboard ----------------------------

def test_run_report_has_stage_timings(toy_run: ToyRun) -> None:
    """§10.2: run-report records per-stage wall-clock for every pipeline stage."""
    stages = toy_run.report["stages"]
    for name in ("extract", "normalize", "curate", "enrich", "generate", "drift"):
        assert name in stages, f"run-report missing stage timing for {name}"
        assert isinstance(stages[name], (int, float)) and stages[name] >= 0


def test_run_report_has_trust_dashboard(toy_run: ToyRun) -> None:
    """§10.2 (added v0.5): the model-trust dashboard is present and well-formed."""
    trust = toy_run.report["trust"]
    assert "coverage_pct" in trust and "L0" in trust["coverage_pct"]
    assert "over_budget_views" in trust
    assert "unmapped_targets" in trust
    assert trust["unmapped_targets"] == 0   # toy is fully curated (§7.3)


def test_run_report_written_through_canonical_json(toy_run: ToyRun) -> None:
    """run-report.json is LF-only and re-loadable (written via jsonio.dump_json)."""
    data = toy_run.ws.run_report.read_bytes()
    assert b"\r\n" not in data
    assert isinstance(load_json(toy_run.ws.run_report), dict)
