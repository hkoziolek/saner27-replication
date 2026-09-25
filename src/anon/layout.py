"""Engine §6.9 — the deterministic layout-coordinate export (``arch export --layout``).

The GUI's own-the-canvas decision (GUI plan §3) rests on layout being a *diffable engine
artifact*, not client-side computation: coordinates are computed by this tested stage and
emitted as data (``generated/layout/<view>.json``); the canvas renders them and computes
none (the iron rule, GUI §2.1/§8.1). "Visual drift" is a diff of this artifact, never of
DOM state.

The algorithm is a deliberately simple, **pure-Python deterministic layered (Sugiyama-
style) layout**: longest-path layering over the **weight-aware** cycle-broken graph
(``_feedback_arcs`` — a lone weak edge cannot outvote a heavy dependency bundle and
invert a node's layer, so one incoming edge never sinks a net-source container), fixed
barycenter ordering sweeps, per-layer centered placement with label-fitted node sizes —
overlap-free *by construction* for auto-laid nodes, every tie broken by sorted stable id,
integer coordinates, no randomness, no clock. Same model → byte-identical artifact (the
§6.9 determinism contract, tested in ``tests/test_layout.py``); the weighting is inert on
acyclic views (empty feedback set), so only the cyclic views it was meant to fix move.

**Recorded deviation from the engine plan §6.9 (the open question 11.9 resolution for
this landing):** the plan's preferred engine is a pinned **elkjs** Node sidecar with this
Python layout as the graceful-degradation fallback. The fallback ships *first* — it is
dependency-free, CI-determinism-testable everywhere, and sufficient for the Stage-1/2
render contract at the ≤25-element view budget. The artifact records its producing
``engine`` (``python-layered/1``), which *is* the pinned-version determinism contract;
when the elkjs sidecar lands (the canvas ADR's sub-project), it slots in behind the same
artifact schema with a new engine string, and golden layouts regenerate reviewably.

Hand-pinned positions (GUI §5.10, the unlock→edit→lock workflow) come from the reviewed
``rules/layout-overrides.yaml``::

    views:
      containers:                  # view key, as passed to --view
        "container:web": {x: 40, y: 0}

Pins are consumed as fixed coordinates; the unpinned remainder is auto-laid around them
(unpinned nodes deterministically pushed clear of pins). A pin whose element no longer
exists is reported as ``stale_pins`` in the artifact header — never silently applied or
dropped (§5.10 evolution honesty). Deleting the file returns the view to pure
auto-layout.

Readability metrics (engine §6.9b, GUI §2.9) are emitted in the header — ``node_overlaps``
(0 for pure auto-layout, by construction), ``edge_crossings``, ``label_fit_failures``,
``aspect_ratio`` — so "the picture got worse" can be a failing CI gate, not a vibe.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

import yaml

from .config import load_yaml
from .jsonio import dump_json, load_json
from .model import content_hash
from .paths import Workspace

LAYOUT_SCHEMA = "layout/1"
ENGINE = "python-layered/1"   # the pinned-engine determinism contract (module docstring)

NODE_H = 64          # px; one row of label + one of metadata, the canvas's default chrome
MIN_W = 140
CHAR_W = 8           # label-fitted width: monospace-safe estimate
PAD_W = 24
H_GAP = 48
V_GAP = 96
ROW_TARGET = 1200    # px; a layer wider than this wraps into multiple centered rows —
                     # so an edge-poor view (e.g. N isolated components) becomes a
                     # grid instead of one endless strip (GUI §2.9 readability)
ROW_GAP = 28         # px; between wrapped rows of the SAME layer (< V_GAP, so layers
                     # still read as visually distinct bands)
BARYCENTER_SWEEPS = 4

# deployment view (C4 deployment diagram): per-environment boundary boxes around the
# auto-laid member nodes; images carry the container instances they package as chips
ENV_PAD = 28         # px; padding between an environment boundary and its members
ENV_LABEL_H = 26     # px; space reserved inside the boundary for its label
ENV_GAP = 72         # px; between environment groups
INSTANCE_H = 22      # px; one contained container-instance chip inside an image node

# containers view with a cosmetic parent tier (container-groups plan §5): boundary boxes
# around parent-group members / peeked-container members — the same box grammar as the
# deployment environments above, so the canvas renders both with one node kind.
GROUP_PAD = 28       # px; padding between a group/peek boundary and its members
GROUP_LABEL_H = 26   # px; space reserved inside the boundary for its label

# presentation-only extras a view builder may attach to a node; carried verbatim into
# the artifact so the canvas can render C4 shapes without computing anything (§2.1).
# Views that don't set them (parent-free containers, component:*) emit byte-identical
# artifacts. `collapsed`/`members` decorate a collapsed parent-group quotient node
# (container-groups plan §5.3).
EXTRA_NODE_KEYS = ("kind", "tech", "description", "external", "replicas",
                   "instances", "parent", "collapsed", "members")


class LayoutError(ValueError):
    """A view that cannot be laid out honestly (unknown container, no L2 facts, …)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- core

def _node_size(label: str) -> tuple[int, int]:
    return max(MIN_W, PAD_W + CHAR_W * len(label)), NODE_H


def _feedback_arcs(ids: list[str], pairs: list[tuple[str, str]],
                   weight: dict[tuple[str, str], float]) -> set[tuple[str, str]]:
    """Weighted greedy feedback-arc-set (Eades–Lin–Smyth): arrange the vertices so the
    total *weight* of backward-pointing edges is small, then return those backward edges
    — ignored for *layering* only (they still render).

    This is what stops a single weak edge from sinking a node. The arrangement peels
    off sinks (to the right) and sources (to the left), and breaks any residual cycle at
    the vertex with the greatest weighted *out-minus-in* degree — so an 11-strong
    dependency bundle keeps its direction forward and the lone light edge pointing the
    other way is the one demoted to a back edge. The old DFS-over-sorted-ids breaker was
    weight-blind: whichever edge happened to close a cycle first in *alphabetical* order
    became the back edge, so a group's *name* could decide whether it sank (the §6.9
    layout note).

    Fully deterministic — every choice among ties is by sorted id. On an ACYCLIC graph
    the arrangement is a topological order and the result is empty, so acyclic views keep
    their pure longest-path layering byte-for-byte (the determinism gate): the change is
    inert everywhere except the cyclic views where the old breaker was fragile."""
    succ: dict[str, set[str]] = {u: set() for u in ids}
    pred: dict[str, set[str]] = {u: set() for u in ids}
    for s, t in pairs:
        succ[s].add(t)
        pred[t].add(s)
    remaining = set(ids)

    def _delta(u: str) -> float:  # weighted out-degree minus in-degree among remaining
        out_w = sum(weight.get((u, v), 1.0) for v in succ[u] if v in remaining)
        in_w = sum(weight.get((v, u), 1.0) for v in pred[u] if v in remaining)
        return out_w - in_w

    left: list[str] = []
    right: list[str] = []   # sinks accumulate left-to-right; reversed onto the tail below
    while remaining:
        peeled = True
        while peeled:
            peeled = False
            for u in sorted(remaining):                       # sinks -> right end
                if u in remaining and not any(v in remaining for v in succ[u]):
                    right.append(u)
                    remaining.discard(u)
                    peeled = True
            for u in sorted(remaining):                       # sources -> left end
                if u in remaining and not any(v in remaining for v in pred[u]):
                    left.append(u)
                    remaining.discard(u)
                    peeled = True
        if remaining:                                         # break the residual cycle
            left.append(max(sorted(remaining), key=_delta))   # ties -> smallest id
            remaining.discard(left[-1])
    order = {u: i for i, u in enumerate(left + right[::-1])}
    return {(s, t) for s, t in pairs if order[s] > order[t]}


def _layers(ids: list[str], edges: list[tuple[str, str]],
            weight: dict[tuple[str, str], float] | None = None) -> dict[str, int]:
    """Longest-path layering over the weight-aware cycle-broken edge set (sources at
    layer 0). Cycle breaking follows the heavy dependency directions
    (:func:`_feedback_arcs`), so a lone weak edge can no longer invert a node's layer."""
    back = _feedback_arcs(ids, sorted(set(edges)), weight or {})
    fwd = [(s, t) for s, t in edges if (s, t) not in back]
    layer = {nid: 0 for nid in ids}
    # relaxation over a topological-ish sorted sweep; |V| passes bound is safe and
    # deterministic for the view-budget graph sizes this stage serves. `fwd` is acyclic
    # by construction (all forward in the _feedback_arcs arrangement), so it converges.
    for _ in range(len(ids)):
        changed = False
        for s, t in sorted(set(fwd)):
            if layer[t] < layer[s] + 1:
                layer[t] = layer[s] + 1
                changed = True
        if not changed:
            break
    return layer


def _order_layers(ids: list[str], edges: list[tuple[str, str]],
                  layer: dict[str, int]) -> list[list[str]]:
    """Fixed barycenter sweeps for crossing reduction; every tie broken by id."""
    n_layers = max(layer.values(), default=0) + 1
    rows: list[list[str]] = [[] for _ in range(n_layers)]
    for nid in ids:
        rows[layer[nid]].append(nid)
    preds: dict[str, list[str]] = {}
    succs: dict[str, list[str]] = {}
    for s, t in sorted(set(edges)):
        preds.setdefault(t, []).append(s)
        succs.setdefault(s, []).append(t)

    def sweep(row: list[str], anchor_pos: dict[str, int],
              neighbours: dict[str, list[str]]) -> list[str]:
        pos = {nid: i for i, nid in enumerate(row)}

        def bary(nid: str) -> float:
            ns = [anchor_pos[n] for n in neighbours.get(nid, []) if n in anchor_pos]
            return sum(ns) / len(ns) if ns else float(pos[nid])

        return sorted(row, key=lambda nid: (bary(nid), nid))

    for _ in range(BARYCENTER_SWEEPS):
        for i in range(1, n_layers):                       # downward
            anchor = {nid: p for p, nid in enumerate(rows[i - 1])}
            rows[i] = sweep(rows[i], anchor, preds)
        for i in range(n_layers - 2, -1, -1):              # upward
            anchor = {nid: p for p, nid in enumerate(rows[i + 1])}
            rows[i] = sweep(rows[i], anchor, succs)
    return rows


def _intersects(a: dict[str, int], b: dict[str, int]) -> bool:
    return (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
            and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])


def _segments_cross(p1, p2, p3, p4) -> bool:
    """Proper intersection of segments p1p2 / p3p4 (shared endpoints don't count)."""
    def d(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if len({p1, p2, p3, p4}) < 4:
        return False
    d1, d2 = d(p3, p4, p1), d(p3, p4, p2)
    d3, d4 = d(p1, p2, p3), d(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def layout_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *,
                 seed: str = "", pins: dict[str, dict[str, Any]] | None = None,
                 bands: dict[str, int] | None = None
                 ) -> dict[str, Any]:
    """Lay out one node-link view deterministically.

    *nodes*: ``[{"id", "label"}]``; *edges*: ``[{"source", "target"}]``; *pins*:
    ``{id: {"x", "y"}}`` fixed coordinates for the §5.10 lock workflow. *bands*:
    ``{id: layer-index}`` an OPTIONAL declared layer assignment (from the reviewed
    ``layering-rules.yaml`` ``layers:`` whitelist) that REPLACES the structural
    longest-path layering — so a hand-declared "ui over services over data" stack reads
    as horizontal bands, top to bottom, instead of being inferred from edges alone.
    Returns ``{nodes, edges, metrics}`` with integer coordinates, nodes/edges id-sorted.
    The *seed* only labels the artifact (tie-breaks are id-sorted, so the layout is
    already a pure function of the graph — engine §6.9's "a seed only pins tie-breaks").
    """
    pins = pins or {}
    nodes = sorted(nodes, key=lambda n: n["id"])
    by_id = {n["id"]: n for n in nodes}
    ids = [n["id"] for n in nodes]
    id_set = set(ids)
    pairs = sorted({(e["source"], e["target"]) for e in edges
                    if e["source"] in id_set and e["target"] in id_set
                    and e["source"] != e["target"]})
    # optional presentation metadata per merged pair (kind/label) — first edge in
    # deterministic sort order wins; views that don't set them emit identical bytes
    pair_meta: dict[tuple[str, str], dict[str, Any]] = {}
    for e in sorted(edges, key=lambda e: (str(e.get("source")), str(e.get("target")),
                                          str(e.get("kind") or ""),
                                          str(e.get("label") or ""))):
        pair_meta.setdefault((e["source"], e["target"]), e)

    # per-pair edge weight for the weight-aware cycle breaker (`_feedback_arcs`): the
    # summed intensity of every edge merged onto the pair, so the layerer follows the
    # heavy dependency directions. An edge with no `weight` counts as 1.0 (multiplicity),
    # so a weightless view degrades to a plain min-back-edge-COUNT ordering — never zero.
    weight: dict[tuple[str, str], float] = {}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in id_set and t in id_set and s != t:
            weight[(s, t)] = weight.get((s, t), 0.0) + float(e.get("weight") or 1.0)

    # a declared layer stack (layering-rules.yaml `layers:`) wins over the inferred
    # longest-path layering for the nodes it names; unnamed nodes fall to a residual
    # band below the declared stack (never dropped). Otherwise: pure structural layering.
    if bands:
        residual = max(bands.values(), default=-1) + 1
        layer = {nid: bands.get(nid, residual) for nid in ids}
    else:
        layer = _layers(ids, pairs, weight)
    rows = _order_layers(ids, pairs, layer)

    # per-layer centered placement — overlap-free by construction. A layer wider than
    # ROW_TARGET wraps into consecutive centered rows (the barycenter order is kept,
    # so adjacent chunks stay adjacent): an edgeless view lays out as a grid, a wide
    # layer in a real graph stops forcing kilometre-long horizontal panning.
    label = {n["id"]: str(n.get("label") or n["id"]) for n in nodes}

    def _sz(n: dict[str, Any]) -> tuple[int, int]:
        # a view builder may pre-size a node (e.g. an image carrying instance chips);
        # everything else stays label-fitted
        w, h = _node_size(label[n["id"]])
        return int(n.get("w") or w), int(n.get("h") or h)

    size = {nid: _sz(by_id[nid]) for nid in ids}
    visual_rows: list[tuple[list[str], bool]] = []   # (row nodes, starts a new layer)
    for layer_nodes in rows:
        chunk: list[str] = []
        width = 0
        first = True
        for nid in layer_nodes:
            w = size[nid][0]
            if chunk and width + H_GAP + w > ROW_TARGET:
                visual_rows.append((chunk, first))
                first = False
                chunk, width = [], 0
            width += (H_GAP if chunk else 0) + w
            chunk.append(nid)
        if chunk:
            visual_rows.append((chunk, first))
    row_w = [sum(size[nid][0] for nid in row) + H_GAP * max(0, len(row) - 1)
             for row, _ in visual_rows]
    max_w = max(row_w, default=0)
    placed: dict[str, dict[str, Any]] = {}
    y = 0
    prev_row_h = NODE_H
    for (row, new_layer), width in zip(visual_rows, row_w):
        if placed:  # advance from the previous row (no gap above the first)
            y += prev_row_h + (V_GAP if new_layer else ROW_GAP)
        x = (max_w - width) // 2
        for nid in row:
            w, h = size[nid]
            placed[nid] = {"id": nid, "label": label[nid],
                           "x": x, "y": y, "w": w, "h": h,
                           "pinned": nid in pins}
            for key in EXTRA_NODE_KEYS:
                val = by_id[nid].get(key)
                if val not in (None, "", []):
                    placed[nid][key] = val
            x += w + H_GAP
        prev_row_h = max(size[nid][1] for nid in row)

    # the §5.10 lock workflow: pins are fixed coordinates; unpinned nodes are pushed
    # clear of them deterministically (sorted order, rightward shifts)
    for nid, pin in sorted(pins.items()):
        if nid in placed:
            placed[nid]["x"] = int(pin["x"])
            placed[nid]["y"] = int(pin["y"])
    pinned_rects = [placed[nid] for nid in sorted(pins) if nid in placed]
    for nid in ids:
        node = placed[nid]
        if node["pinned"]:
            continue
        for _ in range(len(pinned_rects) + 1):
            hit = next((p for p in pinned_rects if _intersects(node, p)), None)
            if hit is None:
                break
            node["x"] = hit["x"] + hit["w"] + H_GAP

    # straight cited routes: bottom-center -> top-center for downward edges,
    # center -> center otherwise (back edges still render)
    def _anchor(s: str, t: str) -> list[list[int]]:
        a, b = placed[s], placed[t]
        if b["y"] > a["y"]:
            return [[a["x"] + a["w"] // 2, a["y"] + a["h"]],
                    [b["x"] + b["w"] // 2, b["y"]]]
        return [[a["x"] + a["w"] // 2, a["y"] + a["h"] // 2],
                [b["x"] + b["w"] // 2, b["y"] + b["h"] // 2]]

    edges_out = []
    for s, t in pairs:
        edge: dict[str, Any] = {"id": f"{s}->{t}", "source": s, "target": t,
                                "points": _anchor(s, t)}
        meta = pair_meta.get((s, t), {})
        if meta.get("kind"):
            edge["kind"] = meta["kind"]
        if meta.get("label"):
            edge["label"] = meta["label"]
        edges_out.append(edge)

    # readability metrics (engine §6.9b / GUI §2.9) — emitted, gateable
    rects = [placed[nid] for nid in ids]
    overlaps = sum(1 for i in range(len(rects)) for j in range(i + 1, len(rects))
                   if _intersects(rects[i], rects[j]))
    segs = [tuple(map(tuple, e["points"])) for e in edges_out]
    crossings = sum(1 for i in range(len(segs)) for j in range(i + 1, len(segs))
                    if _segments_cross(segs[i][0], segs[i][1], segs[j][0], segs[j][1]))
    xs = [r["x"] for r in rects] + [r["x"] + r["w"] for r in rects]
    ys = [r["y"] for r in rects] + [r["y"] + r["h"] for r in rects]
    bbox_w = (max(xs) - min(xs)) if xs else 0
    bbox_h = (max(ys) - min(ys)) if ys else 0
    metrics = {
        "nodes": len(rects), "edges": len(edges_out),
        "node_overlaps": overlaps,
        "edge_crossings": crossings,
        "label_fit_failures": 0,   # widths are label-fitted by construction
        "aspect_ratio": round(bbox_w / bbox_h, 3) if bbox_h else None,
    }
    return {"nodes": [placed[nid] for nid in ids], "edges": edges_out,
            "metrics": metrics, "seed": seed}


# --------------------------------------------------------------------------- views

def container_view_graph(facts: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """The default view: the engine's two-step lifted container graph (GUI §2.1) —
    never a client-invented node set."""
    from .stages.generate_structurizr import build_container_edges, build_containers
    containers = build_containers(facts)
    t2c = {m: c.cid for c in containers.values() for m in c.members}
    edges = build_container_edges(facts, t2c)
    nodes = [{"id": c.cid, "label": c.name}
             for c in sorted(containers.values(), key=lambda c: c.cid)]
    return nodes, [{"source": s, "target": t, "weight": edges[(s, t)].get("weight", 0)}
                   for (s, t) in sorted(edges)]


def container_parent_map(facts: dict[str, Any]) -> dict[str, str]:
    """Curated container id -> cosmetic parent label (container-groups plan §5.2).

    Empty for a workspace whose rules carry no ``parent:`` — the signal that keeps the
    parent-free containers view on the exact pre-parent code path (byte-identity, §10).
    Every member of a group carries the same curate-stamped label, so last-write is
    identical to first-write."""
    out: dict[str, str] = {}
    for t in facts.get("targets", []) or []:
        parent = t.get("container_parent")
        if parent:
            out[t.get("container_id") or t["id"]] = parent
    return out


def component_drilldown(facts: dict[str, Any],
                        container_id: str) -> tuple[str, list[dict], list[dict]]:
    """The single source of the drill-down dispatch (GUI §5.1; drill-down plan D§2–4).

    Returns ``(kind, items, edges)`` where *kind* is ``"target"`` (the curated container
    drills into its member **build targets** — the plan's primary component altitude,
    D§0.1#2) or ``"namespace"`` (a single build target drills into its L2 namespace tier).

    *items* carry ``id``/``name``/``parent``/``source``/``has_code`` plus, for the
    cross-boundary neighbour boxes, ``external: True`` (a foreign container the shown set
    depends on / is used by). *edges* are ``{source, target, rel}`` — ``rel`` is the
    underlying curated relationship (for citation), ``source``/``target`` the DISPLAY
    endpoints, which differ from ``rel`` for the cross-boundary **half-edges**.

    At the **target** altitude the edge set is the C4-conventional one (D§4 decision 4 /
    §4.1): intra-container target→target edges (un-hidden from the container altitude)
    **plus** cross-boundary half-edges to/from the neighbour *container* — the same
    picture ``generate_structurizr.build_component_edges`` renders in the DSL, so the GUI
    canvas and the DSL component view agree. Every edge is a curated relationship that
    already passed curate's significance rules. Both ``serve`` and
    :func:`component_view_graph` go through here so the two never drift.

    Dispatch (build-graph-as-truth, never a guessed decomposition):
      - an explicit **target id** carrying a namespace tier → that target's namespaces;
      - a curated container with **≥2 members** → its member build targets, intra edges,
        and cross-boundary neighbour boxes;
      - a **singleton** container → its one target's namespaces, or an honest ``no_l2``
        when that target has no L2 tier (nothing deeper to show).
    """
    targets = facts.get("targets", []) or []
    rels_all = facts.get("relationships", []) or []
    self_t = next((t for t in targets if t.get("id") == container_id), None)
    members = [t for t in targets
               if (t.get("container_id") or t.get("id")) == container_id]

    def _namespaces(t: dict[str, Any]) -> tuple[str, list[dict], list[dict]]:
        comps = t.get("components", []) or []
        items = [{"id": c.get("id"), "name": c.get("name", c.get("id")),
                  "parent": t["id"], "source": c.get("source"),
                  "has_code": bool(c.get("code"))}
                 for c in sorted(comps, key=lambda c: c.get("id") or "")]
        cids = {c.get("id") for c in comps}
        edges = [{"source": r["source"], "target": r["target"], "rel": r}
                 for r in rels_all
                 if r.get("source") in cids and r.get("target") in cids
                 and r.get("source") != r.get("target")]
        return "namespace", items, edges

    # an explicit target id with a namespace tier — the second drill hop (target → ns)
    if self_t is not None and self_t.get("components"):
        return _namespaces(self_t)
    if not members:
        raise LayoutError("unknown_id", f"no container '{container_id}'")
    if len(members) == 1:
        if members[0].get("components"):
            return _namespaces(members[0])
        raise LayoutError("no_l2", f"'{container_id}' has no L2 component facts — "
                                   "drill-down unavailable (GUI §5.1)")
    # the curated container drills into its member build targets (D§3.1, Model X)
    member_ids = {t["id"] for t in members}
    items = [{"id": t["id"], "name": t.get("name", t["id"]),
              "parent": container_id,
              "source": (t.get("type") or t.get("language") or None),
              "has_code": bool(t.get("components"))}
             for t in sorted(members, key=lambda t: t.get("id", ""))]
    # every target → its curated container (id + display name) for the neighbour boxes
    t2c = {t["id"]: (t.get("container_id") or t["id"]) for t in targets}
    cname = {(t.get("container_id") or t["id"]):
             (t.get("container_name") or t.get("name") or (t.get("container_id") or t["id"]))
             for t in targets}
    edge_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    neighbours: dict[str, dict[str, Any]] = {}
    for r in rels_all:
        s, t = r.get("source"), r.get("target")
        s_in, t_in = s in member_ids, t in member_ids
        if s_in and t_in:
            if s != t:                                   # intra-container target edge
                edge_by_pair.setdefault((s, t), {"source": s, "target": t, "rel": r})
        elif s_in:                                       # member → foreign container
            ct = t2c.get(t)
            if ct and ct != container_id:
                neighbours.setdefault(ct, {"id": ct, "name": cname.get(ct, ct),
                                           "parent": None, "source": None,
                                           "has_code": False, "external": True})
                edge_by_pair.setdefault((s, ct), {"source": s, "target": ct, "rel": r})
        elif t_in:                                       # foreign container → member
            cs = t2c.get(s)
            if cs and cs != container_id:
                neighbours.setdefault(cs, {"id": cs, "name": cname.get(cs, cs),
                                           "parent": None, "source": None,
                                           "has_code": False, "external": True})
                edge_by_pair.setdefault((cs, t), {"source": cs, "target": t, "rel": r})
    items += [neighbours[k] for k in sorted(neighbours)]
    edges = [edge_by_pair[k] for k in sorted(edge_by_pair)]
    return "target", items, edges


def component_view_graph(facts: dict[str, Any],
                         container_id: str) -> tuple[list[dict], list[dict]]:
    """Component drill-down for one container as a node-link graph — only where a
    drill-down exists; honest refusal otherwise (GUI §5.1), mirroring
    ``GET /model/components``. Delegates the dispatch to :func:`component_drilldown`."""
    _kind, items, edges = component_drilldown(facts, container_id)
    nodes = [{"id": it["id"], "label": it["name"],
              **({"external": True} if it.get("external") else {})}
             for it in items]
    edge_pairs = [{"source": e["source"], "target": e["target"]} for e in edges]
    return nodes, edge_pairs


# --- system-context view (GUI: C4 level 1) -----------------------------------------
# Context-altitude truth has two sources, both reviewed/evidenced, never invented:
# persons + external software systems DECLARED in the hand-owned workspace.dsl (the
# scaffold ships `user = person "Operator"` and hand edits are human truth, the same
# tier as rules/*.yaml), and external infrastructure OBSERVED in the deployment facet
# (extract_deployment, §4.4). The build graph itself carries no context altitude.
_DSL_PERSON = re.compile(
    r'^\s*(\w+)\s*=\s*person\s+"((?:[^"\\]|\\.)*)"(?:\s+"((?:[^"\\]|\\.)*)")?',
    re.MULTILINE)
_DSL_SYSTEM = re.compile(
    r'^\s*(\w+)\s*=\s*softwareSystem\s+"((?:[^"\\]|\\.)*)"', re.MULTILINE)
_DSL_REL = re.compile(
    r'^\s*(\w+)\s*->\s*(\w+)(?:\s+"((?:[^"\\]|\\.)*)")?', re.MULTILINE)


def system_context_view_graph(ws: Workspace,
                              facts: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """C4 system-context view: the software system + the persons/external systems
    declared in the hand-owned ``workspace.dsl`` + the external infrastructure the
    deployment facet observed. A model with none of these refuses honestly
    (``no_context``) — a one-box context diagram is never guessed into existence."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    system_label = ws.repo.name or "System"
    idents: dict[str, str] = {"system": "system"}  # DSL identifier -> node id
    text = ws.workspace_dsl.read_text(encoding="utf-8") \
        if ws.workspace_dsl.exists() else ""
    for ident, name in _DSL_SYSTEM.findall(text):
        if ident == "system":
            system_label = name  # the hand-owned DSL may have renamed the system
        else:
            idents[ident] = f"dsl:system:{ident}"
            nodes.append({"id": idents[ident], "label": name, "kind": "system",
                          "external": True})
    for ident, name, desc in _DSL_PERSON.findall(text):
        idents[ident] = f"dsl:person:{ident}"
        node: dict[str, Any] = {"id": idents[ident], "label": name, "kind": "person"}
        if desc:
            node["description"] = desc
        nodes.append(node)
    nodes.append({"id": "system", "label": system_label, "kind": "system"})
    for a, b, desc in _DSL_REL.findall(text):
        if a in idents and b in idents and a != b:
            edge: dict[str, Any] = {"source": idents[a], "target": idents[b],
                                    "kind": "uses"}
            if desc:
                edge["label"] = desc
            edges.append(edge)
    dep = facts.get("deployment") or {}
    # an external infra node becomes an external system; the system->infra edge is
    # the same evidence lift the generator does (a deploy:image packages our build
    # targets, so its observed runtime call IS the system's) — no edge without it
    called = {e.get("target") for e in dep.get("edges") or []
              if e.get("kind") != "packages"
              and str(e.get("source") or "").startswith("deploy:image:")}
    for n in sorted((n for n in dep.get("nodes") or []
                     if n.get("kind") == "infra" and n.get("external")),
                    key=lambda n: n.get("id") or ""):
        node = {"id": n["id"], "label": n.get("name") or n["id"], "kind": "infra",
                "external": True}
        if n.get("technology") or n.get("subkind"):
            node["tech"] = n.get("technology") or n.get("subkind")
        nodes.append(node)
        if n["id"] in called:
            edges.append({"source": "system", "target": n["id"], "kind": "calls"})
    if len(nodes) < 2:
        raise LayoutError(
            "no_context",
            "no context-level facts: no persons/external systems in workspace.dsl "
            "and no external deployment infrastructure — a one-box context view is "
            "never invented (GUI §5.1)")
    return nodes, edges


def code_view_graph(facts: dict[str, Any],
                    component_id: str) -> tuple[list[dict], list[dict]]:
    """Class-level drill-down for one component — only where the optional code tier
    (schema §6.3 ``component.code[]``) was actually extracted; honest refusal
    (``no_code``) otherwise."""
    comp = None
    for t in facts.get("targets", []) or []:
        for c in t.get("components", []) or []:
            if c.get("id") == component_id:
                comp = c
                break
        if comp is not None:
            break
    if comp is None:
        raise LayoutError("unknown_id", f"no component '{component_id}'")
    code = comp.get("code") or []
    if not code:
        raise LayoutError("no_code", f"'{component_id}' has no code-level facts — "
                                     "class drill-down unavailable (GUI §5.1)")
    nodes = [{"id": c.get("id"), "label": c.get("name", c.get("id")), "kind": "code"}
             for c in sorted(code, key=lambda c: c.get("id") or "")]
    code_ids = {n["id"] for n in nodes}
    edges = []
    for r in facts.get("relationships", []) or []:
        if r.get("source") in code_ids and r.get("target") in code_ids:
            edge: dict[str, Any] = {"source": r["source"], "target": r["target"]}
            if r.get("kind"):
                edge["kind"] = r["kind"]
            edges.append(edge)
    return nodes, edges


def _deployment_layout(ws: Workspace, facts: dict[str, Any],
                       pins_override: dict[str, dict[str, Any]] | None
                       ) -> tuple[dict[str, Any], dict[str, dict], list[str]]:
    """The C4 deployment view: per-environment boundary boxes, image nodes carrying
    the container instances they package (the ``packages`` edges lifted through the
    curated container mapping — the same lift the generator does), external infra,
    and the observed runtime edges. Never an invented topology (§2.5): every node
    and edge is the deployment facet's, verbatim.

    Returns ``(laid, pins, stale)`` where *laid* is ``{nodes, edges, metrics}``;
    boundary nodes (kind ``env``) enclose their members and are excluded from the
    overlap metric (they overlap by design)."""
    dep = facts.get("deployment") or {}
    dnodes = sorted(dep.get("nodes") or [], key=lambda n: n.get("id") or "")
    dedges = dep.get("edges") or []
    members = [n for n in dnodes if n.get("kind") in ("image", "infra")]
    if not members:
        raise LayoutError("no_deployment_facet",
                          "no deployment facts on this run — provide manifests or "
                          "run with --resolve-deployment (§2.5 honest abstention)")
    member_ids = {n["id"] for n in members}

    # container instances per image: packages edges lifted to curated container names
    from .stages.generate_structurizr import build_containers
    t2c = {m: c.name for c in build_containers(facts).values() for m in c.members}
    inst: dict[str, set[str]] = {}
    for e in dedges:
        if e.get("kind") == "packages" and e.get("source") in member_ids:
            tgt = str(e.get("target") or "")
            inst.setdefault(e["source"], set()).add(
                t2c.get(tgt) or tgt.split("/")[-1] or tgt)

    # observed runtime edges among members (everything but packaging), one per pair
    runtime: dict[tuple[str, str], str] = {}
    for e in sorted(dedges, key=lambda e: (str(e.get("source")), str(e.get("target")),
                                           str(e.get("kind") or ""))):
        if e.get("kind") != "packages" and e.get("source") in member_ids \
                and e.get("target") in member_ids and e["source"] != e["target"]:
            runtime.setdefault((e["source"], e["target"]), e.get("kind") or "calls")

    env_names = sorted({n.get("name") or n["id"].split(":", 1)[1]
                        for n in dnodes if n.get("kind") == "env"}
                       | {n["env"] for n in members if n.get("env")})
    groups = env_names + ([""] if any(not n.get("env") for n in members) else [])

    if pins_override is None:
        pins, stale = _pins_for(ws, "deployment", member_ids)
    else:
        pins = {nid: pos for nid, pos in pins_override.items()
                if nid in member_ids and isinstance(pos, dict)
                and "x" in pos and "y" in pos}
        stale = sorted(nid for nid in pins_override if nid not in member_ids)

    placed: dict[str, dict[str, Any]] = {}
    xoff = 0
    for g in groups:
        gm = [n for n in members if (n.get("env") or "") == g]
        if not gm:
            continue
        gids = {n["id"] for n in gm}
        gnodes = []
        for n in gm:
            lab = n.get("name") or n["id"]
            w, h = _node_size(lab)
            chips = sorted(inst.get(n["id"], ()))
            if chips:
                w = max(w, PAD_W + CHAR_W * max(len(c) for c in chips) + 16)
                h = NODE_H + INSTANCE_H * len(chips) + 10
            node: dict[str, Any] = {"id": n["id"], "label": lab, "kind": n["kind"],
                                    "w": w, "h": h}
            if n.get("technology") or n.get("subkind"):
                node["tech"] = n.get("technology") or n.get("subkind")
            if n.get("external"):
                node["external"] = True
            if n.get("replicas") is not None:
                node["replicas"] = n["replicas"]
            if chips:
                node["instances"] = chips
            if g:
                node["parent"] = f"env:{g}"
            gnodes.append(node)
        gedges = [{"source": s, "target": t}
                  for (s, t) in runtime if s in gids and t in gids]
        laid = layout_graph(gnodes, gedges)
        pad_x = xoff + (ENV_PAD if g else 0)
        pad_y = (ENV_LABEL_H + ENV_PAD) if g else 0
        for nd in laid["nodes"]:
            nd["x"] += pad_x
            nd["y"] += pad_y
            placed[nd["id"]] = nd
        xoff = max(nd["x"] + nd["w"] for nd in laid["nodes"]) \
            + (ENV_PAD if g else 0) + ENV_GAP

    # §5.10 pins: applied verbatim to member nodes; the boundary is recomputed from
    # the FINAL member positions, so a pinned node stretches its environment box
    # rather than escaping it silently
    for nid, pin in sorted(pins.items()):
        if nid in placed:
            placed[nid]["x"] = int(pin["x"])
            placed[nid]["y"] = int(pin["y"])
            placed[nid]["pinned"] = True

    boundaries: list[dict[str, Any]] = []
    for g in env_names:
        gm = [placed[n["id"]] for n in members
              if (n.get("env") or "") == g and n["id"] in placed]
        if not gm:
            continue
        x0 = min(nd["x"] for nd in gm) - ENV_PAD
        y0 = min(nd["y"] for nd in gm) - ENV_PAD - ENV_LABEL_H
        x1 = max(nd["x"] + nd["w"] for nd in gm) + ENV_PAD
        y1 = max(nd["y"] + nd["h"] for nd in gm) + ENV_PAD
        boundaries.append({"id": f"env:{g}", "label": g, "x": x0, "y": y0,
                           "w": x1 - x0, "h": y1 - y0, "pinned": False,
                           "kind": "env"})

    def _anchor(s: str, t: str) -> list[list[int]]:
        a, b = placed[s], placed[t]
        if b["y"] > a["y"]:
            return [[a["x"] + a["w"] // 2, a["y"] + a["h"]],
                    [b["x"] + b["w"] // 2, b["y"]]]
        return [[a["x"] + a["w"] // 2, a["y"] + a["h"] // 2],
                [b["x"] + b["w"] // 2, b["y"] + b["h"] // 2]]

    edges_out = [{"id": f"{s}->{t}", "source": s, "target": t, "kind": k,
                  "points": _anchor(s, t)} for (s, t), k in sorted(runtime.items())]

    rects = [placed[nid] for nid in sorted(placed)]
    overlaps = sum(1 for i in range(len(rects)) for j in range(i + 1, len(rects))
                   if _intersects(rects[i], rects[j]))
    segs = [tuple(map(tuple, e["points"])) for e in edges_out]
    crossings = sum(1 for i in range(len(segs)) for j in range(i + 1, len(segs))
                    if _segments_cross(segs[i][0], segs[i][1], segs[j][0], segs[j][1]))
    all_rects = boundaries + rects
    xs = [r["x"] for r in all_rects] + [r["x"] + r["w"] for r in all_rects]
    ys = [r["y"] for r in all_rects] + [r["y"] + r["h"] for r in all_rects]
    bbox_w = (max(xs) - min(xs)) if xs else 0
    bbox_h = (max(ys) - min(ys)) if ys else 0
    metrics = {
        "nodes": len(boundaries) + len(rects), "edges": len(edges_out),
        "node_overlaps": overlaps,   # members only — boundaries overlap by design
        "edge_crossings": crossings,
        "label_fit_failures": 0,
        "aspect_ratio": round(bbox_w / bbox_h, 3) if bbox_h else None,
    }
    laid = {"nodes": boundaries + [placed[nid] for nid in sorted(placed)],
            "edges": edges_out, "metrics": metrics}
    return laid, pins, stale


# --- containers view with parent-group boundaries + view states (plan §5) ----------

def normalized_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Canonical ``{"collapsed": [...], "peeked": [...]}`` (sorted, de-duped, stringly),
    or ``None`` when the state is empty — so "no state" and "{}" are the same picture
    and the response is a pure function of (facts, state)."""
    if not state:
        return None
    collapsed = sorted({str(x) for x in (state.get("collapsed") or [])})
    peeked = sorted({str(x) for x in (state.get("peeked") or [])})
    if not collapsed and not peeked:
        return None
    return {"collapsed": collapsed, "peeked": peeked}


def _grouped_containers_layout(ws: Workspace, facts: dict[str, Any],
                               pins_override: dict[str, dict[str, Any]] | None,
                               state: dict[str, Any] | None
                               ) -> tuple[dict[str, Any], dict, list[str], list[str]]:
    """The containers view when a cosmetic parent tier and/or a §5.3 view state is in
    play — its own pin/boundary dance, mirroring ``_deployment_layout``. The parent-free
    stateless view never enters here (``compute`` routes it to the plain path, §10).

    Three pictures, one mechanism (container-groups plan §0.1#4):

      - a **visible parent group** — member containers laid out together, wrapped in a
        boundary box (a background node, ``kind: "group"``, the deployment-env grammar);
      - a **collapsed parent** (``state.collapsed``) — one quotient node replaces the
        members; edges re-map onto it, weights summed, ``agg`` = the count of distinct
        underlying container pairs;
      - a **peeked container** (``state.peeked``) — its member build targets laid out
        inside its box (``component_drilldown`` is the single drill truth, so peek ≡ the
        component view's content) and every outer edge re-anchored to the precise member
        endpoint the curated relationships name.

    Placement is two-level: an inner ``layout_graph`` per box sizes it, a quotient
    ``layout_graph`` over loose items + boxes places everything, members offset into
    their placed box. Boundaries are recomputed from FINAL member positions, so a §5.10
    pin stretches its box rather than escaping it. Deterministic by composition — both
    passes are the deterministic layered engine, every tie id-sorted.

    Pins apply to top-level items only (containers, peek boxes, collapsed group nodes);
    a pin on anything else (a peeked member, a visible group boundary) is reported in
    ``stale_pins`` for this response, never applied or written back.

    Returns ``(laid, pins, stale_pins, stale_state)`` — *stale_state* names the state
    entries that could not be honoured (unknown label/id, peek of a container with no
    member build targets, peek hidden inside a collapsed parent), never silently dropped.
    """
    state = state or {}
    parent_of = container_parent_map(facts)
    nodes, _edges = container_view_graph(facts)
    by_id = {n["id"]: n for n in nodes}
    labels = set(parent_of.values())
    stale_state: list[str] = []

    # the reviewed layering-rules.yaml `layers:` stack applies on this path exactly as
    # on the plain containers path (compute's else-branch) — one `parent:` key must not
    # silently drop the hand-declared bands. Members band inside their cluster; a group
    # box / collapsed quotient node inherits its members' topmost (minimum) declared
    # band, deterministically; peeked member build targets are below band altitude.
    bands_all = declared_bands(nodes, load_yaml(ws.layering_rules))

    def _member_band(lbl: str) -> int | None:
        got = [bands_all[c] for c in by_id
               if parent_of.get(c) == lbl and c in bands_all]
        return min(got) if got else None

    collapsed = sorted({str(x) for x in (state.get("collapsed") or [])})
    peeked = sorted({str(x) for x in (state.get("peeked") or [])})
    coll = {c for c in collapsed if c in labels}
    stale_state += [f"collapsed:{c} (no such parent group)"
                    for c in collapsed if c not in labels]
    hidden = {cid for cid in by_id if parent_of.get(cid) in coll}

    # --- peek transform: a peeked container becomes a pre-sized box; its member build
    # targets get an inner layout now, offset into the box's final position later.
    peek_member_box: dict[str, str] = {}       # member target id -> owning box id
    peek_inner: dict[str, dict[str, Any]] = {}
    for cid in peeked:
        if cid not in by_id:
            stale_state.append(f"peeked:{cid} (no such container)")
            continue
        if cid in hidden:
            stale_state.append(f"peeked:{cid} (inside a collapsed group)")
            continue
        try:
            kind, items_d, drels = component_drilldown(facts, cid)
        except LayoutError:
            kind, items_d, drels = "", [], []
        if kind != "target":
            stale_state.append(f"peeked:{cid} (no member build targets to show)")
            continue
        members = [it for it in items_d if it.get("parent") == cid]
        member_ids = {it["id"] for it in members}
        inner = layout_graph(
            [{"id": it["id"], "label": it["name"]} for it in members],
            [{"source": e["source"], "target": e["target"],
              "weight": (e.get("rel") or {}).get("weight", 0)} for e in drels
             if e["source"] in member_ids and e["target"] in member_ids])
        xs = [n["x"] for n in inner["nodes"]] + [n["x"] + n["w"] for n in inner["nodes"]]
        ys = [n["y"] for n in inner["nodes"]] + [n["y"] + n["h"] for n in inner["nodes"]]
        peek_inner[cid] = {"laid": inner["nodes"], "x0": min(xs), "y0": min(ys),
                           "w": (max(xs) - min(xs)) + 2 * GROUP_PAD,
                           "h": (max(ys) - min(ys)) + 2 * GROUP_PAD + GROUP_LABEL_H}
        for mid in sorted(member_ids):
            peek_member_box[mid] = cid

    # --- display edges: ONE pass over the curated relationships, each endpoint mapped
    # to its display representative — the member target when its container is peeked,
    # the quotient node when its parent is collapsed, the container otherwise. This is
    # the ``build_container_edges`` lift re-run one altitude up/down as the state asks;
    # stateless it reproduces exactly the container pairs the plain view shows.
    t2c = {t["id"]: (t.get("container_id") or t["id"])
           for t in facts.get("targets", []) or []}

    def _rep(tid: str | None) -> str | None:
        c = t2c.get(tid or "")
        if c is None or c not in by_id:
            return None
        if c in peek_inner:
            return tid
        lbl = parent_of.get(c)
        if lbl in coll:
            return f"group:{lbl}"
        return c

    pair_stats: dict[tuple[str, str], dict[str, Any]] = {}
    for r in facts.get("relationships", []) or []:
        s, t = _rep(r.get("source")), _rep(r.get("target"))
        if s is None or t is None or s == t:
            continue
        st = pair_stats.setdefault((s, t), {"weight": 0, "pairs": set()})
        st["weight"] += r.get("weight", 0)
        st["pairs"].add((t2c.get(r["source"]), t2c.get(r["target"])))

    # --- top-level items (containers, peek boxes, collapsed group nodes) + clusters
    items: dict[str, dict[str, Any]] = {}
    cluster_of: dict[str, str] = {}            # item id -> visible parent label
    for cid in sorted(by_id):
        if cid in hidden:
            continue
        label = str(by_id[cid].get("label") or cid)
        if cid in peek_inner:
            items[cid] = {"id": cid, "label": label, "kind": "peek",
                          "w": peek_inner[cid]["w"], "h": peek_inner[cid]["h"]}
        else:
            items[cid] = {"id": cid, "label": label}
        lbl = parent_of.get(cid)
        if lbl and lbl not in coll:
            cluster_of[cid] = lbl
    for lbl in sorted(coll):
        gid = f"group:{lbl}"
        items[gid] = {"id": gid, "label": lbl, "kind": "group", "collapsed": True,
                      "members": sum(1 for c in by_id if parent_of.get(c) == lbl)}

    def _item(nid: str) -> str:
        return peek_member_box.get(nid, nid)

    item_pairs = sorted({(_item(s), _item(t)) for (s, t) in pair_stats
                         if _item(s) != _item(t)})
    # weight per display pair (summed intensity of every curated edge merged onto it),
    # so the weight-aware layerer follows the heavy directions rather than edge count —
    # a lone weak edge into a peeked/loose member can't sink its box (the §6.9 note).
    item_w: dict[tuple[str, str], float] = {}
    for (s, t), st in pair_stats.items():
        a, b = _item(s), _item(t)
        if a != b:
            item_w[(a, b)] = item_w.get((a, b), 0.0) + st["weight"]

    # --- inner layout per visible parent group sizes its boundary box
    cluster_members: dict[str, list[str]] = {}
    for iid in sorted(cluster_of):
        cluster_members.setdefault(cluster_of[iid], []).append(iid)
    supers: dict[str, dict[str, Any]] = {}
    inner_of_label: dict[str, dict[str, Any]] = {}
    for lbl in sorted(cluster_members):
        mset = set(cluster_members[lbl])
        laid_in = layout_graph(
            [items[i] for i in cluster_members[lbl]],
            [{"source": s, "target": t, "weight": item_w.get((s, t), 0)}
             for (s, t) in item_pairs if s in mset and t in mset],
            bands={i: bands_all[i] for i in cluster_members[lbl]
                   if i in bands_all} or None)
        xs = [n["x"] for n in laid_in["nodes"]] + [n["x"] + n["w"] for n in laid_in["nodes"]]
        ys = [n["y"] for n in laid_in["nodes"]] + [n["y"] + n["h"] for n in laid_in["nodes"]]
        inner_of_label[lbl] = {"nodes": laid_in["nodes"], "x0": min(xs), "y0": min(ys)}
        supers[f"group:{lbl}"] = {
            "id": f"group:{lbl}", "label": lbl,
            "w": (max(xs) - min(xs)) + 2 * GROUP_PAD,
            "h": (max(ys) - min(ys)) + 2 * GROUP_PAD + GROUP_LABEL_H}

    # --- quotient placement over loose items + group boxes
    def _top(iid: str) -> str:
        lbl = cluster_of.get(iid)
        return f"group:{lbl}" if lbl else iid

    q_nodes = ([items[i] for i in sorted(items) if i not in cluster_of]
               + [supers[g] for g in sorted(supers)])
    q_pairs = sorted({(_top(_item(s)), _top(_item(t))) for (s, t) in pair_stats
                      if _top(_item(s)) != _top(_item(t))})
    # quotient-pair weight: the same summed intensity lifted to the top level, so the
    # weight-aware breaker keeps a group's heavy outbound bundle forward instead of
    # letting one incoming edge invert the whole group's band (the reported symptom).
    q_w: dict[tuple[str, str], float] = {}
    for (s, t), st in pair_stats.items():
        a, b = _top(_item(s)), _top(_item(t))
        if a != b:
            q_w[(a, b)] = q_w.get((a, b), 0.0) + st["weight"]
    # quotient bands: a loose container / peek box keeps its own declared band; a
    # group super-node or collapsed quotient node takes its members' topmost band.
    q_bands: dict[str, int] = {}
    for n in q_nodes:
        nid = n["id"]
        band = (_member_band(nid[len("group:"):]) if nid.startswith("group:")
                else bands_all.get(nid))
        if band is not None:
            q_bands[nid] = band
    laid_q = layout_graph(q_nodes,
                          [{"source": s, "target": t, "weight": q_w.get((s, t), 0)}
                           for (s, t) in q_pairs],
                          bands=q_bands or None)
    qpos = {n["id"]: n for n in laid_q["nodes"]}

    placed: dict[str, dict[str, Any]] = {
        n["id"]: dict(n) for n in laid_q["nodes"] if n["id"] not in supers}
    for lbl in sorted(inner_of_label):
        box = qpos[f"group:{lbl}"]
        off_x = box["x"] + GROUP_PAD - inner_of_label[lbl]["x0"]
        off_y = box["y"] + GROUP_LABEL_H + GROUP_PAD - inner_of_label[lbl]["y0"]
        for n in inner_of_label[lbl]["nodes"]:
            nd = dict(n)
            nd["x"] += off_x
            nd["y"] += off_y
            nd["parent"] = f"group:{lbl}"
            placed[nd["id"]] = nd

    # --- §5.10 pins: top-level items only; boundaries recomputed AFTER, so a pinned
    # member stretches its box (the deployment dance)
    pinnable = set(items)
    if pins_override is None:
        pins, stale = _pins_for(ws, "containers", pinnable)
    else:
        pins = {nid: pos for nid, pos in pins_override.items()
                if nid in pinnable and isinstance(pos, dict)
                and "x" in pos and "y" in pos}
        stale = sorted(nid for nid in pins_override if nid not in pinnable)
    for nid, pin in sorted(pins.items()):
        if nid in placed:
            placed[nid]["x"] = int(pin["x"])
            placed[nid]["y"] = int(pin["y"])
            placed[nid]["pinned"] = True

    # --- peek members land inside their box's FINAL rect
    for cid in sorted(peek_inner):
        box = placed.get(cid)
        if box is None:
            continue
        info = peek_inner[cid]
        off_x = box["x"] + GROUP_PAD - info["x0"]
        off_y = box["y"] + GROUP_LABEL_H + GROUP_PAD - info["y0"]
        for n in info["laid"]:
            nd = dict(n)
            nd["x"] += off_x
            nd["y"] += off_y
            nd["parent"] = cid
            placed[nd["id"]] = nd

    # --- boundaries, recomputed from final member positions; artifact order is render
    # order (parent boundaries, then peek boxes, then real nodes — background first)
    boundaries: list[dict[str, Any]] = []
    for lbl in sorted(cluster_members):
        gm = [placed[i] for i in cluster_members[lbl] if i in placed]
        if not gm:
            continue
        x0 = min(nd["x"] for nd in gm) - GROUP_PAD
        y0 = min(nd["y"] for nd in gm) - GROUP_PAD - GROUP_LABEL_H
        x1 = max(nd["x"] + nd["w"] for nd in gm) + GROUP_PAD
        y1 = max(nd["y"] + nd["h"] for nd in gm) + GROUP_PAD
        boundaries.append({"id": f"group:{lbl}", "label": lbl, "x": x0, "y": y0,
                           "w": x1 - x0, "h": y1 - y0, "pinned": False,
                           "kind": "group"})
    for cid in sorted(peek_inner):
        bx = placed.pop(cid, None)   # the box renders as a boundary, not a member node
        if bx is None:
            continue
        b = {"id": cid, "label": bx["label"], "x": bx["x"], "y": bx["y"],
             "w": bx["w"], "h": bx["h"], "pinned": bx.get("pinned", False),
             "kind": "peek"}
        if bx.get("parent"):
            b["parent"] = bx["parent"]
        boundaries.append(b)

    # --- routed display edges (member-level endpoints; every endpoint is placed)
    def _anchor(s: str, t: str) -> list[list[int]]:
        a, b = placed[s], placed[t]
        if b["y"] > a["y"]:
            return [[a["x"] + a["w"] // 2, a["y"] + a["h"]],
                    [b["x"] + b["w"] // 2, b["y"]]]
        return [[a["x"] + a["w"] // 2, a["y"] + a["h"] // 2],
                [b["x"] + b["w"] // 2, b["y"] + b["h"] // 2]]

    edges_out = []
    for (s, t) in sorted(pair_stats):
        if s not in placed or t not in placed:
            continue
        st = pair_stats[(s, t)]
        edge: dict[str, Any] = {"id": f"{s}->{t}", "source": s, "target": t,
                                "points": _anchor(s, t), "weight": st["weight"]}
        n_pairs = len(st["pairs"])
        if n_pairs > 1 or s.startswith("group:") or t.startswith("group:"):
            edge["agg"] = n_pairs   # a state-aggregated edge, not a curated one
        edges_out.append(edge)

    rects = [placed[nid] for nid in sorted(placed)]
    overlaps = sum(1 for i in range(len(rects)) for j in range(i + 1, len(rects))
                   if _intersects(rects[i], rects[j]))
    segs = [tuple(map(tuple, e["points"])) for e in edges_out]
    crossings = sum(1 for i in range(len(segs)) for j in range(i + 1, len(segs))
                    if _segments_cross(segs[i][0], segs[i][1], segs[j][0], segs[j][1]))
    all_rects = boundaries + rects
    xs = [r["x"] for r in all_rects] + [r["x"] + r["w"] for r in all_rects]
    ys = [r["y"] for r in all_rects] + [r["y"] + r["h"] for r in all_rects]
    bbox_w = (max(xs) - min(xs)) if xs else 0
    bbox_h = (max(ys) - min(ys)) if ys else 0
    metrics = {
        "nodes": len(boundaries) + len(rects), "edges": len(edges_out),
        "node_overlaps": overlaps,   # members only — boundaries overlap by design
        "edge_crossings": crossings,
        "label_fit_failures": 0,
        "aspect_ratio": round(bbox_w / bbox_h, 3) if bbox_h else None,
    }
    laid = {"nodes": boundaries + rects, "edges": edges_out, "metrics": metrics}
    return laid, pins, stale, sorted(set(stale_state))


def view_slug(view: str) -> str:
    """Deterministic artifact filename for a view key. ``containers`` stays bare; keys
    carrying ids (slashes, colons) sanitize + hash-suffix so distinct views can never
    collide on disk."""
    slug = re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]+", "-", view)).strip("-").lower()
    if slug == view:
        return slug
    return f"{slug}-{hashlib.sha256(view.encode('utf-8')).hexdigest()[:8]}"


def declared_bands(nodes: list[dict[str, Any]],
                   layering_rules: dict[str, Any] | None) -> dict[str, int]:
    """Map container nodes to declared layer bands from the reviewed
    ``layering-rules.yaml`` optional ``layers:`` whitelist (§11.2 — the
    architect-declared "ui over services over data" stack, top→bottom).

    A container node matches a layer token by its **container id** or its **container
    name** (the node's ``id``/``label``). This is the container-altitude subset of the
    identities ``drift_report._matches_container`` resolves — the container view carries
    no member-target nodes, so a token that is a *raw target id* (which ``forbidden:`` can
    also match) has nothing to bind to here and is simply ignored. First listed layer =
    band 0 (top). Returns ``{}`` when no ``layers:`` are declared, so the caller falls
    back to the structural longest-path layering. Pure + ordered → deterministic.
    """
    layers = (layering_rules or {}).get("layers") or []
    if not isinstance(layers, list) or not layers:
        return {}
    index: dict[str, int] = {}
    for i, token in enumerate(layers):
        index.setdefault(str(token), i)   # first occurrence wins the band
    bands: dict[str, int] = {}
    for n in nodes:
        for key in (n.get("id"), n.get("label")):
            if key in index:
                bands[n["id"]] = index[key]
                break
    return bands


def _pins_for(ws: Workspace, view: str,
              node_ids: set[str]) -> tuple[dict[str, dict], list[str]]:
    """Pins for *view* from the reviewed ``layout-overrides.yaml`` + the stale ones —
    reported, never silently applied or dropped (§5.10)."""
    overrides = (load_yaml(ws.layout_overrides) or {}).get("views") or {}
    raw = overrides.get(view) or {}
    pins = {nid: pos for nid, pos in raw.items()
            if nid in node_ids and isinstance(pos, dict)
            and "x" in pos and "y" in pos}
    stale = sorted(nid for nid in raw if nid not in node_ids)
    return pins, stale


def load_facts(ws: Workspace) -> dict[str, Any] | None:
    """The same facts view the DSL generator and the datasheets consume
    (enriched > curated > extracted), so the picture and the prose agree."""
    for path in (ws.enriched_facts, ws.curated_facts, ws.extracted_facts):
        if path.exists():
            return load_json(path)
    return None


def compute(ws: Workspace, view: str = "containers", *,
            facts: dict[str, Any] | None = None,
            pins_override: dict[str, dict[str, Any]] | None = None,
            state: dict[str, Any] | None = None
            ) -> dict[str, Any] | None:
    """Lay out one view and return the artifact dict WITHOUT writing — the pure core
    :func:`run` persists and the §5.10 preview endpoint calls with *pins_override*
    (the would-be ``layout-overrides.yaml`` content for this view) instead of the
    committed file's pins. ``None`` when no fact model exists.

    *facts* overrides the fact source. The default (``None``) uses :func:`load_facts`
    (enriched > curated > extracted) so the picture matches the DSL/datasheets after a
    full run. The GUI's curate fast-loop passes the freshly-curated facts EXPLICITLY:
    it re-curates without re-enriching, so ``enriched-facts.json`` is a structurally
    stale snapshot and reading it would strand the canvas on the pre-curation grouping
    (the tree reads curated). Enrichment never changes structure, so a curated-based
    layout is node-identical to the enriched-based one a later full run emits — only the
    node labels (which the GUI takes from ``/model/containers``, not the layout) differ."""
    if facts is None:
        facts = load_facts(ws)
    if facts is None:
        return None
    h = content_hash(facts)
    state = normalized_state(state)
    if state and view != "containers":
        raise LayoutError("bad_state",
                          "collapse/peek view states apply to the containers view only "
                          "(container-groups plan §1 non-goals)")
    stale_state: list[str] = []
    if view == "deployment":
        # env-grouped assembly with boundary boxes — its own pin/boundary dance
        laid, pins, stale = _deployment_layout(ws, facts, pins_override)
    elif view == "containers" and (state or container_parent_map(facts)):
        # parent-group boundaries and/or a §5.3 view state — its own pin/boundary
        # dance; the parent-free stateless view stays on the plain path below, so a
        # workspace without `parent:` rules emits byte-identical artifacts (plan §10).
        laid, pins, stale, stale_state = _grouped_containers_layout(
            ws, facts, pins_override, state)
    else:
        if view == "containers":
            nodes, edges = container_view_graph(facts)
        elif view.startswith("component:"):
            nodes, edges = component_view_graph(facts, view.split(":", 1)[1])
        elif view == "system-context":
            nodes, edges = system_context_view_graph(ws, facts)
        elif view.startswith("code:"):
            nodes, edges = code_view_graph(facts, view.split(":", 1)[1])
        else:
            raise LayoutError("bad_view",
                              f"unknown view '{view}' (containers | "
                              "component:<container-id> | system-context | "
                              "deployment | code:<component-id>)")
        ids = {n["id"] for n in nodes}
        if pins_override is None:
            pins, stale = _pins_for(ws, view, ids)
        else:
            pins = {nid: pos for nid, pos in pins_override.items()
                    if nid in ids and isinstance(pos, dict)
                    and "x" in pos and "y" in pos}
            stale = sorted(nid for nid in pins_override if nid not in ids)
        # the container view honours a declared layer stack (layering-rules.yaml
        # `layers:`) as horizontal bands; the other views stay purely structural.
        bands = declared_bands(nodes, load_yaml(ws.layering_rules)) \
            if view == "containers" else {}
        laid = layout_graph(nodes, edges, seed=h, pins=pins, bands=bands or None)
    art = {
        "schema": LAYOUT_SCHEMA,
        "view": view,
        "engine": ENGINE,
        "derived_from_hash": h,
        "seed": h,
        "pinned": sorted(pins),
        "stale_pins": stale,
        "metrics": laid["metrics"],
        "nodes": laid["nodes"],
        "edges": laid["edges"],
    }
    if state:
        # a derived §5.3 state response: record what was asked and what could not be
        # honoured (never silently dropped — the stale_pins convention, plan §5.3).
        art["state"] = state
        art["stale_state"] = stale_state
    return art


def _write_view_index(ws: Workspace, views: list[str]) -> None:
    """Write the sorted view-availability index next to the per-view artifacts. So
    ``GET /model/views`` reads ONE small file instead of parsing every layout artifact
    just for its ``view`` key — on a large workspace (e.g. 1500+ drill-down layouts) that
    is the difference between one open and ~1500 cold file opens. Advisory + deterministic
    (sorted, LF via ``dump_json``); machine-owned, not part of the hashed core (§6.9/§9).
    Filters to truthy strings so a malformed caller / corrupt prior index can never write a
    non-sortable mixed-type list (it would crash the next reader)."""
    dump_json({"schema": "layout-index/1",
               "views": sorted({v for v in views if isinstance(v, str) and v})},
              ws.layout_view_index)


def run(ws: Workspace, view: str = "containers", *,
        facts: dict[str, Any] | None = None,
        update_index: bool = False) -> tuple[dict[str, Any], Any] | None:
    """The ``arch export --layout`` entry point: lay out one view and write the
    deterministic coordinate artifact ``generated/layout/<slug>.json``. Advisory,
    machine-owned, overwritten every run — never part of the determinism-hashed
    extracted core (engine §6.9/§9). Returns ``(artifact, path)``; ``None`` when no
    fact model exists.

    ``update_index`` (the incremental single-view export path) additively folds this
    view into the ``_views.json`` availability index so ``GET /model/views`` stays
    current without a full ``arch run``. It stays *off* for the per-view calls inside
    :func:`run_all_views`, which writes the index once after its loop — otherwise a full
    pass would read-modify-write the index once per view (quadratic).

    *facts* overrides the fact source (see :func:`compute`) — the curate fast-loop passes
    the freshly-curated facts so the canvas doesn't read a stale ``enriched-facts.json``."""
    artifact = compute(ws, view, facts=facts)
    if artifact is None:
        return None
    path = ws.layout_dir / f"{view_slug(view)}.json"
    dump_json(artifact, path)
    if update_index:
        existing: list[str] = []
        if ws.layout_view_index.exists():
            try:
                loaded = load_json(ws.layout_view_index).get("views")
            except (OSError, ValueError):
                loaded = None  # a corrupt index is rebuilt from this view's key
            if isinstance(loaded, list):  # a wrong-type `views` is likewise rebuilt
                existing = loaded
        _write_view_index(ws, [*existing, view])
    return artifact, path


def run_all_views(ws: Workspace, *,
                  facts: dict[str, Any] | None = None) -> list[str]:
    """Emit every layout the GUI can render from this workspace (the `arch run`
    advisory pass): the container view, the system-context and deployment views where
    their facts exist, a component drill-down view for every container that actually
    carries L2 component facts, and a code (class) view for every component carrying
    the optional code tier — anything without facts is skipped, never guessed (§5.1).
    The GUI greys out absent views via `GET /model/views` (an echo of what this
    emitted). Returns the emitted view keys; absent facts → [].

    *facts* overrides the fact source (see :func:`compute`). The default (``None``)
    reads enriched > curated > extracted; the GUI curate fast-loop passes the freshly
    curated facts so every re-emitted view matches the tree, not a stale enriched
    snapshot. The enumeration below must read the SAME facts it lays out from, or a
    container present in curated (but not yet in stale enriched) would be laid out but
    never enumerated (and vice-versa)."""
    if facts is None:
        facts = load_facts(ws)
    if facts is None:
        return []
    emitted: list[str] = []
    if run(ws, view="containers", facts=facts) is not None:
        emitted.append("containers")
    for view in ("system-context", "deployment"):
        try:
            run(ws, view=view, facts=facts)
            emitted.append(view)
        except LayoutError:
            continue  # no context-level / deployment facts — honestly absent (§5.1)
    # a component view exists at two altitudes (drill-down plan D§2–4): a curated
    # container drills into its member build TARGETS (any container with ≥2 members,
    # or a singleton whose one target carries an L2 tier), and each build TARGET with
    # a namespace tier drills into its NAMESPACES. Both are keyed `component:<id>` —
    # the dispatch in `component_drilldown` picks the altitude from the id.
    members_by_container: dict[str, list[dict]] = {}
    for t in facts.get("targets", []) or []:
        members_by_container.setdefault(t.get("container_id") or t.get("id"), []).append(t)
    container_views = {cid for cid, ms in members_by_container.items()
                       if len(ms) >= 2 or (len(ms) == 1 and ms[0].get("components"))}
    # only a MULTI-member container exposes target NODES that drill one hop further
    # into their namespaces; a singleton's namespaces are already its container view.
    target_views = {t["id"] for ms in members_by_container.values() if len(ms) >= 2
                    for t in ms if t.get("components")}
    for cid in sorted(container_views | target_views):
        try:
            run(ws, view=f"component:{cid}", facts=facts)
            emitted.append(f"component:{cid}")
        except LayoutError:
            continue  # e.g. a curation race removed the container — advisory, skip
    with_code = sorted({
        c.get("id")
        for t in facts.get("targets", []) or []
        for c in t.get("components", []) or []
        if c.get("id") and c.get("code")})
    for comp_id in with_code:
        try:
            run(ws, view=f"code:{comp_id}", facts=facts)
            emitted.append(f"code:{comp_id}")
        except LayoutError:
            continue
    # one authoritative write of the availability index after the full pass — the cheap
    # source GET /model/views reads instead of re-parsing every per-view artifact.
    _write_view_index(ws, emitted)
    return emitted


def merge_overrides(ws: Workspace, view: str,
                    positions: dict[str, dict[str, Any]]) -> tuple[str, str, dict]:
    """The §5.10 lock step's file edit: merge *positions* into the per-view pins of
    ``rules/layout-overrides.yaml`` and return ``(old_text, new_text, merged_view_pins)``
    — the caller writes the file and renders the diff (§2.3).

    Unlike ``mapping-rules.yaml`` this file is primarily written THROUGH the governed
    lock endpoint, so a deterministic full re-dump (sorted keys, LF) is the contract;
    hand-added comments do not survive a GUI lock — noted here deliberately. Position
    values are rounded to ints; an explicit ``null`` position removes that pin."""
    old_text = ws.layout_overrides.read_text(encoding="utf-8") \
        if ws.layout_overrides.exists() else ""
    doc = yaml.safe_load(old_text) if old_text else None
    doc = doc if isinstance(doc, dict) else {}
    views = doc.setdefault("views", {}) or {}
    doc["views"] = views
    pins = dict(views.get(view) or {})
    for nid, pos in positions.items():
        if pos is None:
            pins.pop(nid, None)
        else:
            pins[nid] = {"x": int(round(float(pos["x"]))),
                         "y": int(round(float(pos["y"])))}
    if pins:
        views[view] = {k: pins[k] for k in sorted(pins)}
    else:
        views.pop(view, None)
    header = ("# Hand-pinned layout positions (GUI plan §5.10) — rules-tier, per view,\n"
              "# keyed by stable element id; consumed as pinned coordinates by\n"
              "# `arch export --layout`. Written by the GUI's unlock->edit->lock\n"
              "# workflow (full re-dump: hand comments here do not survive a lock).\n")
    new_text = header + yaml.safe_dump(doc, sort_keys=True, default_flow_style=False,
                                       allow_unicode=True)
    return old_text, new_text, pins
