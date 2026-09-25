"""T2.5 — coverage-honest fidelity scorecard (`arch scorecard`).

MoJoFM averages fidelity over high- and low-coverage regions of a model and so *structurally
hides* the build-graph advantage: a single number conflates "the part we have declared link-time
evidence for" with "the part we only guessed at." This scorecard is the reporting instrument that
**decomposes the model by evidence-coverage stratum** and adds an **abstention / partial column**,
so a directional superiority claim can be pre-registered on the high-coverage stratum where the
evidence-grounded design *must* win, decoupled from the diluted whole-graph metric that produces
ties (T2.5; it operationalises T1.3's abstention and T2.2's groundedness).

What it reports, per stratum (the strongest evidence layer incident to an element / behind an edge —
L0 build graph → L1 declared refs → L2 symbol/include → L3, plus ``interop`` / ``runtime`` seams,
and ``none`` for an isolated element):

  * **elements / edges** in the stratum;
  * **mean confidence** (the T1.3 continuous score) — how well-grounded that stratum is;
  * **abstained** — elements below the abstention threshold τ (the tool says "needs curation");
  * **partial** — elements carrying a partial/needs-curation tag, plus ``suspicious-declared``
    edges (declared but unused). The honest "we don't fully stand behind this" column.

Plus a **groundedness** line: by the §6/§referential-integrity invariant every edge cites at least
one evidence item, so edge groundedness is **1.0 by construction** (an LLM baseline measures it
empirically < 1.0 — T2.2). The scorecard reports the invariant and flags any violation rather than
asserting it blindly.

Pure + deterministic (facts only; everything sorted): no model call, no randomness, computed on
demand — the §1.1#2 byte-identity gates are untouched.
"""
from __future__ import annotations

from typing import Any

from . import confidence
from .jsonio import load_json
from .paths import Workspace
from .stages.enrich_llm import EVIDENCE_STRENGTH_K

# Stratum display order: best-grounded first (the high-coverage stratum the claim leads with).
_STRATA_ORDER = ["L0", "L1", "L2", "L3", "interop", "runtime", "none"]
_PARTIAL_TAGS = ("needs-curation", "partial:unrendered", "llm-enrich-failed", "partial:codegen")

# Rank used to pick the *strongest* layer when an element's incident edges span several layers
# (e.g. an element with both a declared L0 link and a weak L2 include is an L0-stratum element).
_LAYER_RANK = {l: i for i, l in enumerate(_STRATA_ORDER)}


def _edge_layers(rel: dict[str, Any]) -> set[str]:
    return {confidence._LAYER_OF.get(e.get("type")) for e in rel.get("evidence", []) or []
            if confidence._LAYER_OF.get(e.get("type"))}


def _strongest(layers: set[str]) -> str:
    """The best-grounded layer in *layers* (lowest rank), or ``"none"`` when empty."""
    return min(layers, key=lambda l: _LAYER_RANK.get(l, len(_STRATA_ORDER)), default="none") \
        if layers else "none"


def element_strata(facts: dict[str, Any]) -> dict[str, str]:
    """Per-element evidence stratum (the strongest layer incident to each target).

    The same stratification :func:`build` aggregates, exposed per element so overlays
    (the GUI confidence heatmap, §5.9) consume the identical assignment — two screens
    must never disagree about a target's stratum (GUI §2.7 single-sourced honesty).
    """
    incident_by_target: dict[str, set[str]] = {}
    for r in facts.get("relationships", []) or []:
        layers = _edge_layers(r)
        for endpoint in (r.get("source"), r.get("target")):
            if endpoint:
                incident_by_target.setdefault(endpoint, set()).update(layers)
    return {t["id"]: _strongest(incident_by_target.get(t["id"], set()))
            for t in facts.get("targets", []) if t.get("id")}


def container_strata(facts: dict[str, Any]) -> dict[str, str]:
    """Per-container coverage stratum: the STRONGEST stratum among member targets.

    The container-level twin of :func:`element_strata`, single-sourced here so the
    datasheet health block and the GUI's container overlay can never disagree about a
    container's coverage (GUI §2.7). Falls back to the target id as the container for
    ungrouped targets — the same rule as the generator's ``build_containers``.
    """
    by_element = element_strata(facts)
    out: dict[str, str] = {}
    for t in facts.get("targets", []):
        tid = t.get("id")
        if not tid:
            continue
        cid = t.get("container_id") or tid
        stratum = by_element.get(tid, "none")
        best = out.get(cid)
        if best is None or _LAYER_RANK.get(stratum, 99) < _LAYER_RANK.get(best, 99):
            out[cid] = stratum
    return out


def _is_partial_target(t: dict[str, Any]) -> bool:
    tags = t.get("tags", []) or []
    return any(tag in tags for tag in _PARTIAL_TAGS)


def build(facts: dict[str, Any], threshold: float = confidence.DEFAULT_ABSTAIN_THRESHOLD,
          *, k: int = EVIDENCE_STRENGTH_K) -> dict[str, Any]:
    """Compute the coverage-stratified scorecard (T2.5). Pure, deterministic, facts-only."""
    targets = [t for t in facts.get("targets", []) if t.get("id")]
    # the scorecard is a BUILD-TARGET-altitude metric; component-altitude edges
    # (`…/component:` endpoints, e.g. the C# namespace→namespace symbol_use edges) live
    # one zoom below the curation grid and must not dilute the edge counts/groundedness.
    rels = [r for r in (facts.get("relationships", []) or [])
            if "/component:" not in (r.get("source") or "")
            and "/component:" not in (r.get("target") or "")]
    scores = confidence.score_targets(facts, k=k)
    strata_by_id = element_strata(facts)

    # accumulate per-stratum element + edge stats.
    strata: dict[str, dict[str, Any]] = {
        s: {"stratum": s, "elements": 0, "edges": 0, "abstained": 0, "partial": 0,
            "_conf_sum": 0.0} for s in _STRATA_ORDER}
    for t in targets:
        s = strata_by_id[t["id"]]
        row = strata[s]
        row["elements"] += 1
        row["_conf_sum"] += scores.get(t["id"], 0.0)
        if scores.get(t["id"], 0.0) < threshold:
            row["abstained"] += 1
        if _is_partial_target(t):
            row["partial"] += 1

    grounded_edges = 0
    ungrounded_edges = 0
    suspicious_edges = 0
    for r in rels:
        ev = r.get("evidence", []) or []
        if ev:
            grounded_edges += 1
        else:
            ungrounded_edges += 1
        if "suspicious-declared" in (r.get("tags", []) or []):
            suspicious_edges += 1
        s = _strongest(_edge_layers(r))
        strata[s]["edges"] += 1

    rows = []
    for s in _STRATA_ORDER:
        row = strata[s]
        n = row["elements"]
        row["mean_confidence"] = round(row.pop("_conf_sum") / n, 4) if n else 0.0
        if n or row["edges"]:
            rows.append(row)

    total_elems = len(targets)
    total_edges = len(rels)
    abstained = sum(r["abstained"] for r in rows)
    partial = sum(r["partial"] for r in rows)
    mean_conf = round(sum(scores.values()) / total_elems, 4) if total_elems else 0.0
    return {
        "threshold": threshold,
        "elements": total_elems,
        "edges": total_edges,
        "mean_confidence": mean_conf,
        "abstained": abstained,
        "partial": partial,
        "edge_groundedness": round(grounded_edges / total_edges, 4) if total_edges else 1.0,
        "ungrounded_edges": ungrounded_edges,
        "suspicious_edges": suspicious_edges,
        "strata": rows,
    }


# --------------------------------------------------------------------------- render

def render(card: dict[str, Any]) -> str:
    """Render the coverage-honest scorecard as advisory markdown (T2.5)."""
    lines = ["# Coverage-honest fidelity scorecard  (`arch scorecard`)", ""]
    if card["elements"] == 0:
        lines += ["_No curated targets — nothing to score._", ""]
        return "\n".join(lines) + "\n"
    g = card["edge_groundedness"]
    ground = (f"**{g:.0%}** — every edge cites evidence (1.0 by referential-integrity invariant, T2.2)"
              if g >= 1.0 else
              f"**{g:.0%}** — ⚠️ {card['ungrounded_edges']} edge(s) cite NO evidence "
              f"(referential-integrity violation)")
    lines += [
        f"- **{card['elements']} elements**, **{card['edges']} edges** · mean confidence "
        f"**{card['mean_confidence']:.2f}**",
        f"- at τ={card['threshold']:.2f}: **{card['abstained']} abstained**, "
        f"{card['partial']} partial/needs-curation, {card['suspicious_edges']} suspicious-declared edge(s)",
        f"- edge groundedness: {ground}",
        "",
        "## By evidence-coverage stratum (best-grounded first)",
        "",
        "| stratum | elements | edges | mean conf | abstained | partial |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in card["strata"]:
        lines.append(f"| **{row['stratum']}** | {row['elements']} | {row['edges']} | "
                     f"{row['mean_confidence']:.2f} | {row['abstained']} | {row['partial']} |")
    lines += [
        "",
        "> Read the **L0** row first: it is the high-coverage stratum where declared build-graph "
        "evidence makes the placement structural truth. A whole-model average dilutes it with the "
        "low-coverage tail — which this scorecard isolates instead of hiding (T2.5).",
    ]
    return "\n".join(lines) + "\n"


def run(ws: Workspace, threshold: float = confidence.DEFAULT_ABSTAIN_THRESHOLD,
        *, k: int = EVIDENCE_STRENGTH_K) -> dict[str, Any]:
    """Build the scorecard from the curated model, write ``ws.scorecard_md``, return it."""
    facts = load_json(ws.curated_facts) if ws.curated_facts.exists() else \
        (load_json(ws.extracted_facts) if ws.extracted_facts.exists() else
         {"targets": [], "relationships": []})
    card = build(facts, threshold, k=k)
    ws.scorecard_md.parent.mkdir(parents=True, exist_ok=True)
    ws.scorecard_md.write_text(render(card), encoding="utf-8", newline="")
    return card
