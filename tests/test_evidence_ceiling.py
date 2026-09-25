"""Tests for the §7-A1 evidence-layer ceiling (``--max-evidence-layer``).

The ceiling makes the build-graph-first ablation a faithful engine knob: restrict facts to
evidence up to L0/L1/L2/L3, dropping edges that have no surviving evidence and recomputing the
derived fields from the survivors. The default (None) MUST be a strict no-op so the determinism
goldens are untouched (asserted here + by tests/test_golden.py).
"""
from __future__ import annotations

import copy

from anon.model import DECLARED_BASE
from anon.stages.normalize_facts import EVIDENCE_LAYERS, apply_evidence_ceiling


def _facts():
    """Synthetic facts spanning the three strata: an L0 declared link, an L1 package ref, an L2
    include-only edge, and a corroborated L0+L2 edge."""
    return {
        "schema_version": "x",
        "targets": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}],
        "relationships": [
            {"id": "rel:a->b", "source": "a", "target": "b", "weight": DECLARED_BASE,
             "is_declared_dependency": True, "confidence": "low", "evidence_strength": "link only",
             "evidence": [{"type": "link", "detail": "lib", "visibility": "public"}]},
            {"id": "rel:a->c", "source": "a", "target": "c", "weight": 3,
             "is_declared_dependency": False, "confidence": "low", "evidence_strength": "include only",
             "evidence": [{"type": "include", "count": 3, "detail": "#include c.h"}]},
            {"id": "rel:b->d", "source": "b", "target": "d", "weight": 2,
             "is_declared_dependency": False, "confidence": "low", "evidence_strength": "package_ref only",
             "evidence": [{"type": "package_ref", "detail": "Pkg"}]},
            {"id": "rel:a->d", "source": "a", "target": "d", "weight": 99,
             "is_declared_dependency": True, "confidence": "high",
             "evidence_strength": "link + include corroborate",
             "evidence": [{"type": "link", "detail": "lib", "visibility": "public"},
                          {"type": "include", "count": 7, "detail": "#include d.h"}]},
        ],
        "provenance": {},
    }


def _rels(facts):
    return {r["id"] for r in facts["relationships"]}


def test_none_is_strict_noop():
    facts = _facts()
    before = copy.deepcopy(facts)
    out = apply_evidence_ceiling(facts, None)
    assert out is facts                 # identity: the default path does nothing at all
    assert out == before                # and mutates nothing


def test_L0_keeps_only_build_graph_edges():
    out = apply_evidence_ceiling(_facts(), "L0")
    # include-only (a->c) and package_ref-only (b->d, that's L1) drop; the two with a link survive.
    assert _rels(out) == {"rel:a->b", "rel:a->d"}
    a_d = next(r for r in out["relationships"] if r["id"] == "rel:a->d")
    # a->d recomputed from the link alone: declared, weight = the declared base (include stripped).
    assert a_d["is_declared_dependency"] is True
    assert a_d["weight"] == DECLARED_BASE
    assert [e["type"] for e in a_d["evidence"]] == ["link"]


def test_L1_adds_declared_package_refs():
    out = apply_evidence_ceiling(_facts(), "L1")
    assert _rels(out) == {"rel:a->b", "rel:a->d", "rel:b->d"}   # +package_ref edge, still no L2-only


def test_L2_is_the_full_set():
    full = _facts()
    out = apply_evidence_ceiling(_facts(), "L2")
    assert _rels(out) == _rels(full)                            # nothing dropped at the include layer


def test_monotonic_growth_and_provenance_stamp():
    counts = [len(apply_evidence_ceiling(_facts(), lyr)["relationships"]) for lyr in EVIDENCE_LAYERS]
    assert counts == sorted(counts)                             # L0 <= L1 <= L2 <= L3
    out = apply_evidence_ceiling(_facts(), "L0")
    assert out["provenance"]["max_evidence_layer"] == "L0"      # the run is self-describing
