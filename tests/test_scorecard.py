"""T2.5 — coverage-honest fidelity scorecard.

Proves the reporting instrument that decomposes the model by evidence-coverage stratum:

  1. each element/edge lands in the stratum of its **strongest** incident evidence layer
     (an element with both a declared L0 link and a weak L2 include is an *L0* element);
  2. the **abstention / partial column** counts needs-curation/partial elements and
     suspicious-declared edges — the honest "we don't fully stand behind this" tail;
  3. **edge groundedness** is 1.0 by the referential-integrity invariant (every edge cites
     evidence), and a planted evidence-less edge is flagged, not hidden (T2.2);
  4. CLI wiring (`arch scorecard`) runs and emits text/JSON; the build is deterministic.
"""
from __future__ import annotations

from pathlib import Path

from anon import cli, scorecard

from conftest import run_toy_pipeline

L0 = "el:l0"
INTEROP = "el:interop"
L2 = "el:l2"
NEEDS = "el:needs"


def _facts() -> dict:
    targets = [
        {"id": L0, "name": "L0", "container_id": "a", "container_name": "A", "tags": []},
        {"id": INTEROP, "name": "Native", "container_id": "n", "container_name": "N",
         "tags": ["native"], "external": True},
        {"id": L2, "name": "L2only", "container_id": "x", "container_name": "X", "tags": []},
        {"id": NEEDS, "name": "Needs", "container_id": NEEDS, "container_name": "Needs",
         "tags": ["needs-curation"]},
    ]
    rels = [
        # L0 element also carries a weak L2 include -> still classified L0 (strongest wins).
        {"id": "r1", "source": L0, "target": L2, "confidence": "high",
         "evidence": [{"type": "project_ref", "detail": "p"}, {"type": "include", "detail": "x.h"}]},
        # interop seam edge.
        {"id": "r2", "source": L0, "target": INTEROP, "confidence": "medium",
         "evidence": [{"type": "interop", "detail": "DllImport native"}]},
        # a suspicious-declared edge (declared but flagged).
        {"id": "r3", "source": L2, "target": L0, "confidence": "low", "tags": ["suspicious-declared"],
         "evidence": [{"type": "link", "detail": "l"}]},
    ]
    return {"schema_version": "1.2", "provenance": {}, "targets": targets, "relationships": rels}


def test_strata_partition_by_strongest_layer() -> None:
    card = scorecard.build(_facts())
    by_stratum = {r["stratum"]: r for r in card["strata"]}
    # L0 element has both L0 + L2 incident evidence -> classified by the strongest (L0).
    assert by_stratum["L0"]["elements"] >= 1
    # the L2-only element (only an include incident besides the suspicious link) lands in... it
    # has an incident L0 link (r3 source) + L2 include (r1 target) -> strongest is L0 too.
    # the interop element is interop-stratum; the needs element is isolated (none).
    assert "interop" in by_stratum and by_stratum["interop"]["elements"] == 1
    assert "none" in by_stratum and by_stratum["none"]["elements"] == 1   # NEEDS is isolated
    # strata are emitted best-grounded first.
    order = [r["stratum"] for r in card["strata"]]
    assert order == sorted(order, key=lambda s: scorecard._LAYER_RANK[s])


def test_abstention_and_partial_columns() -> None:
    card = scorecard.build(_facts())
    # the needs-curation element is both partial and abstained.
    assert card["partial"] >= 1 and card["abstained"] >= 1
    none_row = next(r for r in card["strata"] if r["stratum"] == "none")
    assert none_row["partial"] == 1 and none_row["abstained"] == 1
    assert card["suspicious_edges"] == 1


def test_edge_groundedness_invariant() -> None:
    card = scorecard.build(_facts())
    assert card["edge_groundedness"] == 1.0 and card["ungrounded_edges"] == 0


def test_evidence_less_edge_is_flagged() -> None:
    f = _facts()
    f["relationships"].append({"id": "bad", "source": L0, "target": INTEROP, "evidence": []})
    card = scorecard.build(f)
    assert card["edge_groundedness"] < 1.0 and card["ungrounded_edges"] == 1


def test_mean_confidence_present_and_bounded() -> None:
    card = scorecard.build(_facts())
    assert 0.0 <= card["mean_confidence"] <= 1.0
    for row in card["strata"]:
        assert 0.0 <= row["mean_confidence"] <= 1.0


def test_build_is_deterministic() -> None:
    f = _facts()
    assert scorecard.build(f) == scorecard.build(f)


def test_empty_model_is_graceful() -> None:
    card = scorecard.build({"targets": [], "relationships": []})
    assert card["elements"] == 0 and "No curated targets" in scorecard.render(card)


# --- CLI wiring ---------------------------------------------------------------------

def test_cli_scorecard_runs_and_writes_report(tmp_path: Path) -> None:
    tr = run_toy_pipeline(tmp_path / "architecture")
    base = ["scorecard", "--repo", str(tr.ws.repo), "--arch-dir", str(tr.ws.arch_dir),
            "--rules-dir", str(tr.ws.rules_dir)]
    assert cli.main(base) == 0
    assert tr.ws.scorecard_md.exists()
    assert cli.main(base + ["--format", "json"]) == 0


def test_cli_scorecard_run_writes_artifact(tmp_path: Path) -> None:
    """`arch run` writes the coverage scorecard + abstention report as advisory artifacts."""
    from conftest import pristine_toy
    repo = pristine_toy(tmp_path / "toy-src")
    arch = tmp_path / "arch"
    code = cli.main(["run", "--repo", str(repo), "--arch-dir", str(arch),
                     "--rules-dir", str(repo / "architecture" / "rules"), "--no-llm"])
    assert code == 0
    assert (arch / "generated" / "coverage-scorecard.md").exists()
    assert (arch / "generated" / "abstention-report.md").exists()
