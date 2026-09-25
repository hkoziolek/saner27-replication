"""Tests for §7.5 ID reconciliation: binding hand-crafted ids to extracted stable ids.

Pure tests over :mod:`anon.reconcile` — no I/O beyond a tmp YAML file. They pin the
three §7.5 signals (slug equality strongest, path/namespace overlap, name similarity), the
honest-failure rule (no candidate -> ``extracted_id: None``), the HARD RULE that only
``status: confirmed`` bindings are honored downstream, and determinism of the renderers
(determinism is a hard CI gate, §1.1 #2).
"""
from __future__ import annotations

import yaml

from anon import reconcile


# Two extracted targets used across the suite (fact-model target shape, §6.2).
TARGETS = [
    {
        "id": "cpp:target:OrderProcessor",
        "name": "OrderProcessor",
        "path": "src/order_processor",
        "namespaces": ["op::core"],
    },
    {
        "id": "csharp:csproj:src/order_processor/Op.csproj",
        "name": "Op",
        "path": "src/order_processor",
        "namespaces": ["Op"],
    },
    {
        "id": "cpp:target:BillingService",
        "name": "BillingService",
        "path": "src/billing",
        "namespaces": ["billing"],
    },
]


def _by_human(proposals, human_id):
    return next(p for p in proposals if p["human_id"] == human_id)


def test_exact_slug_match_wins():
    """``orderProcessor`` slug-normalizes to ``orderprocessor`` == target name slug (§7.5 (a))."""
    human = [{"id": "orderProcessor", "name": "Order Processor"}]
    proposals = reconcile.propose_bindings(human, TARGETS)
    p = _by_human(proposals, "orderProcessor")
    assert p["extracted_id"] == "cpp:target:OrderProcessor"
    assert p["status"] == "proposed"
    assert p["score"] == 1.0
    assert "slug-equality" in p["evidence"]


def test_display_name_slug_also_matches():
    """A human element whose *id* is opaque still matches via its display name slug (§7.5 (a))."""
    human = [{"id": "elt-1", "name": "Order Processor"}]
    proposals = reconcile.propose_bindings(human, TARGETS)
    p = _by_human(proposals, "elt-1")
    assert p["extracted_id"] == "cpp:target:OrderProcessor"
    assert p["score"] == 1.0


def test_path_overlap_matches_when_no_slug_equality():
    """``OPSvc`` has no slug/name equality; ``src/order_processor`` grouping overlaps path (§7.5 (b))."""
    human = [{"id": "OPSvc", "name": "OPSvc", "group": "src/order_processor"}]
    # Drop the cpp OrderProcessor whose name would still partially fuzzy-match, to isolate (b);
    # the csharp target shares the path tokens but not the name slug.
    targets = [TARGETS[1], TARGETS[2]]
    proposals = reconcile.propose_bindings(human, targets)
    p = _by_human(proposals, "OPSvc")
    assert p["extracted_id"] == "csharp:csproj:src/order_processor/Op.csproj"
    assert "path/namespace-overlap" in p["evidence"]


def test_no_match_yields_none():
    """A human element with no code counterpart is reported unmatched, never guessed (§4.5.3)."""
    human = [{"id": "loadBalancer", "name": "External Load Balancer"}]
    proposals = reconcile.propose_bindings(human, TARGETS)
    p = _by_human(proposals, "loadBalancer")
    assert p["extracted_id"] is None
    assert p["score"] == 0.0


def test_slug_beats_path_for_same_element():
    """When both signals fire, slug equality (strongest) is the one chosen as evidence (§7.5)."""
    human = [{"id": "orderProcessor", "name": "Order Processor", "group": "src/order_processor"}]
    proposals = reconcile.propose_bindings(human, TARGETS)
    p = _by_human(proposals, "orderProcessor")
    # cpp:target:OrderProcessor wins on slug equality (score 1.0) over the csharp path overlap.
    assert p["extracted_id"] == "cpp:target:OrderProcessor"
    assert p["score"] == 1.0
    assert "slug-equality" in p["evidence"]


def test_proposals_are_sorted_deterministically():
    """Output order is independent of input order (sorted by human_id then extracted_id)."""
    human_a = [
        {"id": "billingService", "name": "Billing Service"},
        {"id": "orderProcessor", "name": "Order Processor"},
    ]
    human_b = list(reversed(human_a))
    pa = reconcile.propose_bindings(human_a, TARGETS)
    pb = reconcile.propose_bindings(human_b, TARGETS)
    assert pa == pb
    assert [p["human_id"] for p in pa] == ["billingService", "orderProcessor"]


def test_propose_bindings_is_repeatable():
    """Same inputs -> identical output (no randomness/timestamps; byte-stable, §1.1 #2)."""
    human = [{"id": "orderProcessor", "name": "Order Processor"}]
    assert reconcile.propose_bindings(human, TARGETS) == reconcile.propose_bindings(human, TARGETS)


def test_confirmed_returns_only_confirmed():
    """The HARD RULE: only ``status: confirmed`` is honored; proposed/absent are ignored (§7.5)."""
    bindings = {
        "bindings": {
            "orderProcessor": {
                "extracted_id": "cpp:target:OrderProcessor",
                "status": "confirmed",
                "confidence": "high",
            },
            "billingService": {
                "extracted_id": "cpp:target:BillingService",
                "status": "proposed",  # NOT honored
                "confidence": "medium",
            },
            "ghost": {
                "extracted_id": None,  # confirmed-but-empty -> dropped
                "status": "confirmed",
            },
        }
    }
    result = reconcile.confirmed(bindings)
    assert result == {"orderProcessor": "cpp:target:OrderProcessor"}
    assert "billingService" not in result
    assert "ghost" not in result


def test_confirmed_handles_empty_and_malformed():
    """Empty / missing / non-dict bindings degrade to ``{}`` rather than crashing (§7.5)."""
    assert reconcile.confirmed({}) == {}
    assert reconcile.confirmed({"bindings": {}}) == {}
    assert reconcile.confirmed({"bindings": None}) == {}
    assert reconcile.confirmed({"bindings": {"x": "not-a-dict"}}) == {}


def test_load_bindings_absent_file_is_empty(tmp_path):
    """Absent ``human_id_bindings.yaml`` -> ``{}`` (graceful degradation, §7.5)."""
    assert reconcile.load_bindings(tmp_path / "missing.yaml") == {}


def test_load_bindings_roundtrip(tmp_path):
    """A written bindings file loads back and gates correctly through :func:`confirmed`."""
    path = tmp_path / "human_id_bindings.yaml"
    doc = {
        "bindings": {
            "orderProcessor": {
                "extracted_id": "cpp:target:OrderProcessor",
                "status": "confirmed",
                "confidence": "high",
            }
        }
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    loaded = reconcile.load_bindings(path)
    assert reconcile.confirmed(loaded) == {"orderProcessor": "cpp:target:OrderProcessor"}


def test_bindings_to_yaml_is_deterministic_and_proposed():
    """``bindings_to_yaml`` is byte-stable and emits every entry as ``status: proposed`` (§7.5)."""
    human = [
        {"id": "orderProcessor", "name": "Order Processor"},
        {"id": "loadBalancer", "name": "External Load Balancer"},
    ]
    proposals = reconcile.propose_bindings(human, TARGETS)
    out1 = reconcile.bindings_to_yaml(proposals)
    out2 = reconcile.bindings_to_yaml(proposals)
    assert out1 == out2

    parsed = yaml.safe_load(out1)
    assert set(parsed["bindings"]) == {"orderProcessor", "loadBalancer"}
    assert all(e["status"] == "proposed" for e in parsed["bindings"].values())
    # Unmatched element carries a null extracted_id so the human sees it.
    assert parsed["bindings"]["loadBalancer"]["extracted_id"] is None
    # Confirmed gate ignores the freshly-proposed file entirely.
    assert reconcile.confirmed(parsed) == {}


# --- T2.7 bulk-accept (`arch reconcile --accept-all >=<band>`) -----------------

def _bulk_bindings() -> dict:
    """Three proposals spanning the bands, plus one already-confirmed and one unmatched."""
    return {
        "bindings": {
            "highOne": {"extracted_id": "cpp:target:H", "status": "proposed",
                        "confidence": "high"},
            "medOne": {"extracted_id": "cpp:target:M", "status": "proposed",
                       "confidence": "medium"},
            "lowOne": {"extracted_id": "cpp:target:L", "status": "proposed",
                       "confidence": "low"},
            "already": {"extracted_id": "cpp:target:A", "status": "confirmed",
                        "confidence": "high"},
            "unmatched": {"extracted_id": None, "status": "proposed", "confidence": "high"},
        }
    }


def _statuses(b: dict) -> dict:
    return {h: e["status"] for h, e in b["bindings"].items()}


def test_bulk_accept_high_promotes_only_high():
    out, n = reconcile.bulk_accept(_bulk_bindings(), "high")
    assert n == 1
    s = _statuses(out)
    assert s["highOne"] == "confirmed"
    assert s["medOne"] == "proposed" and s["lowOne"] == "proposed"
    assert s["already"] == "confirmed"      # untouched
    assert s["unmatched"] == "proposed"     # null extracted_id never auto-confirmed


def test_bulk_accept_medium_promotes_high_and_medium_inclusive():
    out, n = reconcile.bulk_accept(_bulk_bindings(), "medium")
    assert n == 2
    s = _statuses(out)
    assert s["highOne"] == "confirmed" and s["medOne"] == "confirmed"
    assert s["lowOne"] == "proposed"


def test_bulk_accept_low_promotes_all_bands():
    out, n = reconcile.bulk_accept(_bulk_bindings(), "low")
    assert n == 3
    s = _statuses(out)
    assert s["highOne"] == s["medOne"] == s["lowOne"] == "confirmed"


def test_bulk_accept_does_not_mutate_input():
    src = _bulk_bindings()
    reconcile.bulk_accept(src, "low")
    assert src["bindings"]["highOne"]["status"] == "proposed"   # deep-copied, input intact


def test_bulk_accept_is_idempotent():
    once, n1 = reconcile.bulk_accept(_bulk_bindings(), "medium")
    twice, n2 = reconcile.bulk_accept(once, "medium")
    assert n2 == 0                  # nothing left to promote
    assert once == twice            # stable


def test_bulk_accept_unknown_band_promotes_nothing():
    out, n = reconcile.bulk_accept(_bulk_bindings(), "bogus")
    assert n == 0
    assert _statuses(out)["highOne"] == "proposed"


def test_bulk_accept_promoted_entries_pass_confirmed_gate():
    out, _ = reconcile.bulk_accept(_bulk_bindings(), "medium")
    conf = reconcile.confirmed(out)
    assert conf == {"highOne": "cpp:target:H", "medOne": "cpp:target:M",
                    "already": "cpp:target:A"}


def test_bulk_accept_empty_and_malformed():
    assert reconcile.bulk_accept({}, "high") == ({}, 0)
    assert reconcile.bulk_accept({"bindings": None}, "high")[1] == 0


def test_render_reconciliation_table():
    """The Markdown table has the §7.5 columns, an accept checkbox, and ``—`` for no-match."""
    human = [
        {"id": "orderProcessor", "name": "Order Processor"},
        {"id": "loadBalancer", "name": "External Load Balancer"},
    ]
    proposals = reconcile.propose_bindings(human, TARGETS)
    table = reconcile.render_reconciliation_table(proposals)
    assert "| human_id | extracted_id | score | evidence | accept? |" in table
    assert "cpp:target:OrderProcessor" in table
    assert "[ ]" in table
    assert "| — |" in table  # the unmatched loadBalancer row
    # Deterministic.
    assert table == reconcile.render_reconciliation_table(proposals)
