"""T1.3 — calibrated abstention: continuous confidence + selective prediction.

Proves the deterministic mechanism the eval plan's ECE/AURC measurement rides on:

  1. the continuous per-element confidence is **monotone in grounding** — a declared,
     corroborated, documented L0 element ≫ a lone L2-only element ≫ an isolated singleton;
  2. the **abstention floor** — a ``needs-curation`` element is always scored below τ, so the
     continuous score never disagrees with the discrete §7.3 gate (a structural truth: its
     edges may be solid, but its *placement* is provisional);
  3. the **selective-prediction curve** is well-formed and deterministic — coverage falls
     monotonically as τ rises, the risk proxy is bounded, AURC ∈ [0,1];
  4. CLI wiring (`arch abstain`) runs, is read-only over the facts, and emits text/JSON.
"""
from __future__ import annotations

from pathlib import Path

from anon import cli, confidence
from anon.confidence import DEFAULT_ABSTAIN_THRESHOLD as TAU

from conftest import run_toy_pipeline

STRONG = "csharp:csproj:src/Strong/Strong.csproj"
WEAK = "csharp:csproj:src/Weak/Weak.csproj"
HUB = "csharp:csproj:src/Hub/Hub.csproj"
ISOLATED = "csharp:csproj:src/Iso/Iso.csproj"


def _facts() -> dict:
    """A heterogeneous curated model spanning the confidence spectrum."""
    targets = [
        # strong: declared L0 + documented, two corroborating incident edges.
        {"id": STRONG, "name": "Strong", "container_id": "c", "container_name": "C",
         "tags": [], "responsibilities": ["Owns the ordering workflow."]},
        # hub: a second well-connected declared node (gives STRONG corroboration).
        {"id": HUB, "name": "Hub", "container_id": "c", "container_name": "C", "tags": []},
        # weak: a single L2-only include edge, no docs.
        {"id": WEAK, "name": "Weak", "container_id": "w", "container_name": "W", "tags": []},
        # isolated singleton: no incident edges, provisional (needs-curation).
        {"id": ISOLATED, "name": "Iso", "container_id": ISOLATED, "container_name": "Iso",
         "tags": ["needs-curation"]},
    ]
    rels = [
        {"id": "r1", "source": STRONG, "target": HUB, "confidence": "high",
         "evidence": [{"type": "project_ref", "detail": "<ProjectReference/>"},
                      {"type": "link", "detail": "links"},
                      {"type": "symbol_use", "detail": "Hub", "count": 5}]},
        {"id": "r2", "source": HUB, "target": STRONG, "confidence": "medium",
         "evidence": [{"type": "project_ref", "detail": "<ProjectReference/>"}]},
        {"id": "r3", "source": WEAK, "target": HUB, "confidence": "low",
         "evidence": [{"type": "include", "detail": "hub.h"}]},
    ]
    return {"schema_version": "1.2", "provenance": {}, "targets": targets, "relationships": rels}


# --- the continuous score is monotone in grounding ----------------------------------

def test_confidence_orders_by_grounding() -> None:
    f = _facts()
    by_id = {t["id"]: t for t in f["targets"]}
    c_strong = confidence.element_confidence(by_id[STRONG], f)
    c_weak = confidence.element_confidence(by_id[WEAK], f)
    c_iso = confidence.element_confidence(by_id[ISOLATED], f)
    assert c_strong > c_weak > c_iso, (c_strong, c_weak, c_iso)
    # all scores stay in the unit interval.
    for s in confidence.score_targets(f).values():
        assert 0.0 <= s <= 1.0


def test_strong_declared_element_is_retained() -> None:
    f = _facts()
    by_id = {t["id"]: t for t in f["targets"]}
    assert confidence.element_confidence(by_id[STRONG], f) >= 0.70  # "high" band


def test_needs_curation_is_capped_below_threshold() -> None:
    """A needs-curation element is abstained even if its edges are solid — its *placement* is
    provisional, which is exactly what the gate must reflect."""
    f = _facts()
    # give the isolated singleton a strong declared edge; it must STILL score below τ.
    f["relationships"].append({"id": "r4", "source": ISOLATED, "target": HUB,
                               "confidence": "high",
                               "evidence": [{"type": "project_ref", "detail": "p"},
                                            {"type": "link", "detail": "l"}]})
    by_id = {t["id"]: t for t in f["targets"]}
    assert confidence.element_confidence(by_id[ISOLATED], f) < TAU


def test_isolated_singleton_scores_low() -> None:
    f = _facts()
    by_id = {t["id"]: t for t in f["targets"]}
    assert confidence.element_confidence(by_id[ISOLATED], f) < 0.20


def test_detail_explains_the_same_number() -> None:
    """§2.7 single-sourcing for the GUI confidence popover: the breakdown IS the score —
    element/container detail numbers byte-equal the plain functions, and every calc
    string ends in the number it explains (the popover renders, never recomputes)."""
    f = _facts()
    by_id = {t["id"]: t for t in f["targets"]}
    for t in by_id.values():
        d = confidence.element_confidence_detail(t, f)
        assert d["confidence"] == confidence.element_confidence(t, f)
        assert d["calc"]
    strong = confidence.element_confidence_detail(by_id[STRONG], f)
    assert strong["base_layer"] == "L0" and strong["base"] == 0.60
    assert set(strong["bonuses"]) == {"corroboration", "multi_layer", "high_edge", "doc"}
    iso = confidence.element_confidence_detail(by_id[ISOLATED], f)
    assert iso["needs_curation_cap"] is True
    assert iso["base_layer"] == "none"
    cont = confidence.container_confidence_detail(f)
    assert {cid: d["confidence"] for cid, d in cont.items()} == \
        confidence.container_confidence(f)
    for d in cont.values():
        assert d["band"] == confidence.confidence_band(d["confidence"])
        assert d["calc"].endswith(f"= {d['confidence']:.2f}")
        assert 1 <= len(d["examples"]) <= 3
        assert d["formula"] == confidence.CONTAINER_FORMULA


# --- selective partition + risk-coverage curve --------------------------------------

def test_selective_partition_abstains_weak_tail() -> None:
    f = _facts()
    part = confidence.selective_partition(f, TAU)
    assert STRONG in part["retained"] and HUB in part["retained"]
    assert ISOLATED in part["abstained"]
    # lists are disjoint and cover every element, sorted.
    assert sorted(part["retained"] + part["abstained"]) == sorted(t["id"] for t in f["targets"])
    assert part["retained"] == sorted(part["retained"])


def test_risk_coverage_curve_monotone_and_deterministic() -> None:
    f = _facts()
    curve = confidence.risk_coverage_curve(f)
    assert curve and curve[0]["threshold"] == 0.0 and curve[0]["coverage"] == 1.0
    # coverage is non-increasing as τ rises (we abstain on more of the tail).
    covs = [p["coverage"] for p in curve]
    assert covs == sorted(covs, reverse=True)
    # risk proxy bounded; deterministic re-computation.
    assert all(0.0 <= p["selective_risk"] <= 1.0 for p in curve)
    assert confidence.risk_coverage_curve(f) == curve


def test_aurc_in_unit_range() -> None:
    f = _facts()
    a = confidence.aurc(confidence.risk_coverage_curve(f))
    assert 0.0 <= a <= 1.0
    assert confidence.aurc([]) == 0.0  # empty/degenerate curve


def test_confidence_band_thresholds() -> None:
    assert confidence.confidence_band(0.9) == "high"
    assert confidence.confidence_band(TAU) == "medium"
    assert confidence.confidence_band(0.1) == "low"


def test_summarize_shape() -> None:
    f = _facts()
    s = confidence.summarize(f, TAU)
    assert s["elements"] == 4
    assert s["bands"]["high"] + s["bands"]["medium"] + s["bands"]["low"] == 4
    assert s["retained"] + s["abstained"] == 4
    assert ISOLATED in s["abstained_ids"]
    assert 0.0 <= s["mean_confidence"] <= 1.0


# --- CLI wiring (the path humans run) -----------------------------------------------

def test_cli_abstain_runs_and_writes_report(tmp_path: Path) -> None:
    tr = run_toy_pipeline(tmp_path / "architecture")
    base = ["abstain", "--repo", str(tr.ws.repo), "--arch-dir", str(tr.ws.arch_dir),
            "--rules-dir", str(tr.ws.rules_dir)]
    assert cli.main(base) == 0                       # advisory: always exit 0
    assert tr.ws.abstention_report.exists()
    assert cli.main(base + ["--format", "json"]) == 0
    assert cli.main(base + ["--threshold", "0.99"]) == 0   # raising τ abstains more


def test_cli_abstain_is_read_only_over_facts(tmp_path: Path) -> None:
    """`arch abstain` computes confidence on demand — it never mutates curated-facts.json
    (so the §1.1#2 determinism gates are untouched)."""
    tr = run_toy_pipeline(tmp_path / "architecture")
    before = tr.ws.curated_facts.read_bytes()
    cli.main(["abstain", "--repo", str(tr.ws.repo), "--arch-dir", str(tr.ws.arch_dir),
              "--rules-dir", str(tr.ws.rules_dir)])
    assert tr.ws.curated_facts.read_bytes() == before


def test_toy_model_is_confident(tmp_path: Path) -> None:
    """The fully-curated toy (declared deps, documented) scores high and abstains on nothing —
    the mechanism does not cry wolf on a good model."""
    tr = run_toy_pipeline(tmp_path / "architecture")
    from anon.jsonio import load_json
    summary = confidence.summarize(load_json(tr.ws.curated_facts), TAU)
    assert summary["abstained"] == 0
    assert summary["mean_confidence"] >= 0.70
