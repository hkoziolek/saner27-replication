"""Container-groups plan §5 — parent-group boundaries and collapse/peek view states in
the containers layout (`layout._grouped_containers_layout` via `layout.compute`).

What is locked down here:

  * **boundary containment** — every member sits inside its recomputed boundary box,
    boundaries render first (artifact order is render order), outsiders never intersect;
  * **the collapse aggregation** — a collapsed parent becomes one quotient node, edge
    weights sum, ``agg`` counts distinct underlying container pairs, intra-group pairs
    drop (never a self edge);
  * **the peek embedding** — member build targets render inside the container's box,
    the intra edge is un-hidden, outer edges re-anchor to the precise member endpoint
    (the ``component_drilldown`` truth, so peek ≡ the component view's content);
  * **state honesty** — same (facts, state) → identical artifact; unhonourable state
    entries land in ``stale_state``; states are containers-view-only;
  * **the §5.10 pin dance** — a pinned member stretches its boundary, never escapes it.

The parent-FREE byte-identity guard (plan §10) lives in the existing test_layout.py /
golden suite — this file only exercises the grouped path.
"""
from __future__ import annotations

import pytest

from anon import layout
from anon.model import canonicalize
from anon.paths import resolve_workspace


def _parented_facts() -> dict:
    """Synthetic curated facts: PubSub + ReqResp under parent "Middleware", Runtime
    unparented, one needs-curation singleton — the container-groups plan's §2 sketch."""
    def tgt(tid, name, cid=None, cname=None, parent=None):
        t = {"id": tid, "name": name, "type": "cmake_target", "language": "cpp"}
        if cid:
            t["container_id"] = cid
            t["container_name"] = cname
        if parent:
            t["container_parent"] = parent
        return t

    def rel(s, t, w):
        return {"id": f"rel:{s}->{t}", "source": s, "target": t, "weight": w,
                "is_declared_dependency": True,
                "evidence": [{"type": "link", "detail": "x"}]}

    return canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c",
                       "generated_at": "2026-01-01T00:00:00Z", "extractors": []},
        "targets": [
            tgt("cpp:target:a1", "a1", "container:pubsub", "PubSub", "Middleware"),
            tgt("cpp:target:a2", "a2", "container:pubsub", "PubSub", "Middleware"),
            tgt("cpp:target:b1", "b1", "container:reqresp", "ReqResp", "Middleware"),
            tgt("cpp:target:b2", "b2", "container:reqresp", "ReqResp", "Middleware"),
            tgt("cpp:target:r1", "r1", "container:runtime", "Runtime"),
            tgt("cpp:target:r2", "r2", "container:runtime", "Runtime"),
            tgt("cpp:target:solo", "solo"),
        ],
        "relationships": [
            rel("cpp:target:a1", "cpp:target:r1", 3),
            rel("cpp:target:a2", "cpp:target:b1", 2),
            rel("cpp:target:b2", "cpp:target:r2", 5),
            rel("cpp:target:r1", "cpp:target:r2", 4),   # intra Runtime — hidden at
            rel("cpp:target:a1", "cpp:target:a2", 1),   # intra PubSub  — container altitude
        ],
    })


@pytest.fixture()
def pws(tmp_path):
    return resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                             rules_dir=tmp_path / "rules")


def _rect_contains(outer, inner) -> bool:
    return (outer["x"] <= inner["x"] and outer["y"] <= inner["y"]
            and inner["x"] + inner["w"] <= outer["x"] + outer["w"]
            and inner["y"] + inner["h"] <= outer["y"] + outer["h"])


def test_parent_boundary_wraps_members(pws):
    """§5.2: a parent renders as a background boundary node FIRST in artifact order;
    members carry parent= and sit inside the recomputed box; outsiders stay outside."""
    art = layout.compute(pws, "containers", facts=_parented_facts())
    by_id = {n["id"]: n for n in art["nodes"]}
    boundary = art["nodes"][0]
    assert boundary["id"] == "group:Middleware" and boundary["kind"] == "group"
    for cid in ("container:pubsub", "container:reqresp"):
        assert by_id[cid]["parent"] == "group:Middleware"
        assert _rect_contains(boundary, by_id[cid])
    assert "parent" not in by_id["container:runtime"]
    assert not layout._intersects(boundary, by_id["container:runtime"])
    assert not layout._intersects(boundary, by_id["cpp:target:solo"])
    assert "state" not in art and "stale_state" not in art
    # stateless display edges are exactly the plain lifted container pairs
    _nodes, edges = layout.container_view_graph(_parented_facts())
    assert {(e["source"], e["target"]) for e in art["edges"]} == \
        {(e["source"], e["target"]) for e in edges}
    assert art["metrics"]["node_overlaps"] == 0


def test_parent_layout_deterministic(pws):
    a = layout.compute(pws, "containers", facts=_parented_facts(),
                       state={"collapsed": ["Middleware"],
                              "peeked": ["container:runtime"]})
    b = layout.compute(pws, "containers", facts=_parented_facts(),
                       state={"peeked": ["container:runtime"],
                              "collapsed": ["Middleware"]})
    assert a == b   # pure function of (facts, normalized state)


def test_collapsed_group_aggregates_edges(pws):
    """§5.3 collapse: members vanish, one quotient node appears, edges re-map onto it
    with weights summed and agg = distinct underlying container pairs."""
    art = layout.compute(pws, "containers", facts=_parented_facts(),
                         state={"collapsed": ["Middleware"]})
    by_id = {n["id"]: n for n in art["nodes"]}
    assert "container:pubsub" not in by_id and "container:reqresp" not in by_id
    g = by_id["group:Middleware"]
    assert g["kind"] == "group" and g["collapsed"] is True and g["members"] == 2
    agg = next(e for e in art["edges"]
               if (e["source"], e["target"]) == ("group:Middleware", "container:runtime"))
    assert agg["weight"] == 8 and agg["agg"] == 2   # a1->r1 (3) + b2->r2 (5)
    # the intra-group pair (PubSub->ReqResp) collapsed to a self edge and dropped
    assert all(e["source"] != e["target"] for e in art["edges"])
    assert art["state"] == {"collapsed": ["Middleware"], "peeked": []}
    assert art["stale_state"] == []


def test_peeked_container_embeds_members_and_reanchors_edges(pws):
    """§5.3 peek: member build targets render inside the container's boundary box, the
    intra edge is un-hidden, and outer edges re-anchor to the precise member endpoint."""
    art = layout.compute(pws, "containers", facts=_parented_facts(),
                         state={"peeked": ["container:runtime"]})
    by_id = {n["id"]: n for n in art["nodes"]}
    box = by_id["container:runtime"]
    assert box["kind"] == "peek"
    for mid in ("cpp:target:r1", "cpp:target:r2"):
        assert by_id[mid]["parent"] == "container:runtime"
        assert _rect_contains(box, by_id[mid])
    pairs = {(e["source"], e["target"]) for e in art["edges"]}
    assert ("cpp:target:r1", "cpp:target:r2") in pairs          # un-hidden intra edge
    assert ("container:pubsub", "cpp:target:r1") in pairs       # re-anchored precisely
    assert ("container:reqresp", "cpp:target:r2") in pairs
    assert ("container:pubsub", "container:runtime") not in pairs
    # the peek box renders as a boundary: it precedes its members in artifact order
    ids = [n["id"] for n in art["nodes"]]
    assert ids.index("container:runtime") < ids.index("cpp:target:r1")


def test_stale_state_reported_never_dropped(pws):
    """§5.3: unknown labels/ids and un-peekable containers land in stale_state."""
    art = layout.compute(pws, "containers", facts=_parented_facts(),
                         state={"collapsed": ["Nope", "Middleware"],
                                "peeked": ["container:gone", "cpp:target:solo",
                                           "container:pubsub"]})
    assert any(s.startswith("collapsed:Nope") for s in art["stale_state"])
    assert any(s.startswith("peeked:container:gone") for s in art["stale_state"])
    # solo: a singleton with no L2 tier — honest refusal, not a guessed picture
    assert any(s.startswith("peeked:cpp:target:solo") for s in art["stale_state"])
    # PubSub is hidden inside the collapsed Middleware — peek cannot apply
    assert any("inside a collapsed group" in s for s in art["stale_state"])
    assert "group:Middleware" in {n["id"] for n in art["nodes"]}


def test_state_rejected_on_other_views(pws):
    with pytest.raises(layout.LayoutError) as ei:
        layout.compute(pws, "deployment", facts=_parented_facts(),
                       state={"collapsed": ["Middleware"]})
    assert ei.value.code == "bad_state"


def test_declared_bands_apply_on_grouped_path(pws):
    """The reviewed layering-rules.yaml `layers:` stack must not be silently dropped
    the moment a `parent:` key exists: banded containers keep the declared vertical
    order (the structural layering would invert every assertion here — PubSub sources
    the edges), a boundary inherits its members' topmost (minimum) band, and the
    collapse quotient node bands like its members."""
    pws.rules_dir.mkdir(parents=True, exist_ok=True)
    pws.layering_rules.write_text(
        "layers:\n- Runtime\n- ReqResp\n- PubSub\n", encoding="utf-8")
    art = layout.compute(pws, "containers", facts=_parented_facts())
    by_id = {n["id"]: n for n in art["nodes"]}
    # Runtime (band 0) sits ABOVE the Middleware boundary (min member band = 1)
    assert by_id["container:runtime"]["y"] < by_id["group:Middleware"]["y"]
    # inside the boundary the declared order holds too: ReqResp (1) over PubSub (2)
    assert by_id["container:reqresp"]["y"] < by_id["container:pubsub"]["y"]
    # the collapsed quotient node inherits its members' topmost band
    coll = layout.compute(pws, "containers", facts=_parented_facts(),
                          state={"collapsed": ["Middleware"]})
    cby = {n["id"]: n for n in coll["nodes"]}
    assert cby["container:runtime"]["y"] < cby["group:Middleware"]["y"]


def test_pin_stretches_parent_boundary(pws):
    """§5.10 x §5: a pinned member is applied verbatim and its parent boundary is
    recomputed around the FINAL position — the deployment dance."""
    pinned = layout.compute(pws, "containers", facts=_parented_facts(),
                            pins_override={"container:pubsub": {"x": 2000, "y": 900}})
    by_id = {n["id"]: n for n in pinned["nodes"]}
    assert by_id["container:pubsub"]["x"] == 2000 and by_id["container:pubsub"]["pinned"]
    boundary = by_id["group:Middleware"]
    assert _rect_contains(boundary, by_id["container:pubsub"])
    assert _rect_contains(boundary, by_id["container:reqresp"])
