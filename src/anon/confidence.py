"""T1.3 — calibrated abstention: a continuous per-element confidence + selective prediction.

SAR clusterers and LLM recovery both emit a **total** decomposition: every element is placed,
coverage is pinned to 1.0, and there is no primitive for *"I don't know."* Anon has an
evidence ladder (L0 build graph → L1 declared refs → L2 symbol/include → L3 fine-grained, plus
``interop`` / ``runtime`` seams), so it can rank elements by how well-grounded their placement is
and **abstain** on the weakly-grounded tail — turning a single (coverage=1.0) point into a
**risk-coverage curve** only an evidence-aware tool can trace (T1.3 / the P§16#10b open question).

This module ships the **deterministic mechanism**; the *calibration measurement* (ECE / AURC
against an independent per-element correctness oracle over the E§ seeded corpus) is out-of-tree
eval-plan work — exactly the T1.5 split (the tool ships the gate, the eval measures the FP rate).
The "risk" axis here is an explicit **model-internal proxy** (mean ``1 − confidence`` over the
retained set), clearly labelled as such; it is NOT a measured error rate.

The continuous score formalises the discrete signals the pipeline already computes, so it never
contradicts them (§8.3 / §11.6):

  * the **evidence ladder** — the strongest layer of evidence incident to the element sets the
    base score (declared build-graph dependency ≫ a lone weak refine-layer include);
  * **corroboration** — incident-edge :func:`model.compute_confidence` and raw evidence
    multiplicity raise it;
  * **documentation** — a doc/README/responsibility seed (:func:`enrich_llm._has_doc_text`)
    raises it slightly (a *naming*-quality bonus, never a structural penalty: a declared build
    edge is structural truth whether or not the element is documented);
  * the **abstention floor** — an element tagged ``needs-curation`` is *capped* below the default
    abstention threshold, so the continuous score and the discrete §7.3 gate always agree about
    the tail. The §8.3 deterministic :func:`enrich_llm.evidence_strength` cross-check is what
    *adds* that tag on a weak element in the LLM modes, so it feeds this floor transitively.

Pure + deterministic: a function of the committed facts only — no model call, no randomness, all
iteration over sorted keys — so the §1.1#2 byte-identity gates are untouched (the score is computed
on demand, never stamped into the determinism-hashed *extracted* facts).
"""
from __future__ import annotations

from typing import Any

from .model import compute_confidence
from .paths import Workspace
from .jsonio import load_json
from .stages.enrich_llm import EVIDENCE_STRENGTH_K, _has_doc_text

# The abstention threshold τ on the continuous confidence: an element scoring below it is routed
# to "needs-curation" (the tool abstains). 0.50 is the midpoint and — by construction of
# :data:`_NEEDS_CURATION_CAP` — strictly above every needs-curation / weak-evidence element, so the
# continuous abstention decision is a superset of the discrete needs-curation gate (never weaker).
DEFAULT_ABSTAIN_THRESHOLD = 0.50

# --- evidence-ladder base scores (§4 / §6.2) -------------------------------------------------
# The strongest evidence layer incident to an element sets its base confidence. Mirrors the
# drift_report._EVIDENCE_LAYER kind→layer mapping (kept local so this module owns its own scoring
# constants and can't be perturbed by a drift-report edit). A declared build-graph dependency is
# structural truth (principle #3) and scores highest; a lone weak refine-layer include scores low.
_LAYER_OF = {
    "link": "L0", "project_ref": "L0", "package_ref": "L1",
    "include": "L2", "symbol_use": "L2",
    "interop": "interop", "runtime": "runtime",
}
_LADDER_BASE = {
    "L0": 0.60,        # declared link / project_ref — the build graph (structural truth)
    "L1": 0.48,        # declared package reference
    "L2": 0.40,        # symbol / include (refine layer — a lone L2-only edge stays below τ)
    "L3": 0.40,        # fine-grained (reserved; same tier as L2 today)
    "interop": 0.50,   # a declared cross-language P/Invoke/COM seam is significant (model.INTEROP_BASE)
    "runtime": 0.38,   # observed runtime topology
}
_NO_EVIDENCE_BASE = 0.10   # an isolated singleton with no incident edges — barely grounded

# Bonuses (bounded so a fully-corroborated, documented, multi-layer L0 element lands ~0.95, never 1.0:
# the tool never claims certainty — even structural truth is "confidence", not proof).
_CORROBORATION_BONUS = 0.25   # scaled by incident evidence multiplicity, ramped to full at 2·k items
_MULTI_LAYER_BONUS = 0.10     # ≥2 distinct evidence layers incident
_HIGH_EDGE_BONUS = 0.10       # ≥1 incident edge that model.compute_confidence rates "high"
_DOC_BONUS = 0.10             # carries a doc/README/responsibility seed (naming-quality bonus only)
# A needs-curation element is capped strictly below DEFAULT_ABSTAIN_THRESHOLD, so the continuous
# score never disagrees with the discrete §7.3 abstention gate (the §11.6 no-cry-wolf join). This
# is a placement-confidence floor: a provisional/uncurated grouping is abstained REGARDLESS of how
# well-grounded its edges are — the edges may be structural truth, but its *placement* is not yet
# trusted, which is exactly what "needs-curation" means.
_NEEDS_CURATION_CAP = 0.49


def _incident(facts: dict[str, Any], tid: str) -> list[dict[str, Any]]:
    """All relationships with *tid* as source or target (the evidence that grounds it)."""
    return [r for r in facts.get("relationships", [])
            if r.get("source") == tid or r.get("target") == tid]


def element_confidence_detail(target: dict[str, Any], facts: dict[str, Any],
                              *, k: int = EVIDENCE_STRENGTH_K) -> dict[str, Any]:
    """The full breakdown behind :func:`element_confidence`: every term that produced the
    score plus a substituted ``calc`` string, so the GUI confidence popover renders the
    arithmetic without recomputing anything (§2.1 iron rule / §2.7 single-sourcing).
    ``confidence`` here IS the :func:`element_confidence` number — one computation path.
    """
    incident = _incident(facts, target.get("id"))
    layers: set[str] = set()
    evidence_count = 0
    any_high = False
    for r in incident:
        ev = r.get("evidence", []) or []
        evidence_count += len(ev)
        for e in ev:
            layer = _LAYER_OF.get(e.get("type"))
            if layer:
                layers.add(layer)
        if (r.get("confidence") or compute_confidence(ev)) == "high":
            any_high = True

    # ties (L2/L3 share a base) resolve over the SORTED layer list — set iteration order
    # varies with hash randomization and would flap the reported base_layer run-to-run.
    base_layer = max(sorted(layers), key=lambda l: _LADDER_BASE[l]) if layers else None
    base = _LADDER_BASE[base_layer] if base_layer else _NO_EVIDENCE_BASE
    bonuses: dict[str, float] = {}
    if evidence_count:
        # ramp to the full corroboration bonus at 2·k incident evidence items.
        bonuses["corroboration"] = _CORROBORATION_BONUS * min(
            1.0, evidence_count / float(2 * max(k, 1)))
    if len(layers) >= 2:
        bonuses["multi_layer"] = _MULTI_LAYER_BONUS
    if any_high:
        bonuses["high_edge"] = _HIGH_EDGE_BONUS
    if _has_doc_text(target):
        bonuses["doc"] = _DOC_BONUS
    score = max(0.0, min(1.0, base + sum(bonuses.values())))

    # Abstention floor: a needs-curation element can never read as confident — cap it strictly
    # below τ so the continuous score is a superset of the discrete §7.3 gate. (In LLM modes the
    # §8.3 evidence_strength cross-check is what adds the tag on a weak element, so that signal
    # feeds this floor transitively without over-penalising documented-but-structural elements.)
    needs_curation = "needs-curation" in (target.get("tags", []) or [])
    capped = needs_curation and score > _NEEDS_CURATION_CAP
    if needs_curation:
        score = min(score, _NEEDS_CURATION_CAP)
    score = round(score, 4)

    parts = [f"base {base:.2f} ({base_layer or 'no incident evidence'})"]
    parts += [f"+ {name.replace('_', '-')} {b:.2f}" for name, b in bonuses.items()]
    calc = " ".join(parts) + f" = {min(1.0, base + sum(bonuses.values())):.2f}"
    if capped:
        calc += f" → capped {_NEEDS_CURATION_CAP:.2f} (needs-curation)"
    return {
        "confidence": score,
        "band": confidence_band(score),
        "base": base,
        "base_layer": base_layer or "none",
        "layers": sorted(layers),
        "evidence_items": evidence_count,
        "bonuses": {name: round(b, 4) for name, b in bonuses.items()},
        "needs_curation_cap": needs_curation,
        "calc": calc,
    }


def element_confidence(target: dict[str, Any], facts: dict[str, Any],
                       *, k: int = EVIDENCE_STRENGTH_K) -> float:
    """Continuous confidence in ``[0, 1]`` for one element (T1.3), deterministic & facts-only.

    Composed from the evidence ladder (strongest incident layer → base), corroboration
    (evidence multiplicity + a "high"-confidence incident edge), documentation, and a
    needs-curation/weak-evidence cap. Rounded to 4 dp so the rendered output is byte-stable.
    """
    return element_confidence_detail(target, facts, k=k)["confidence"]


def confidence_band(score: float) -> str:
    """Map a continuous score to a display band consistent with the abstention threshold."""
    if score >= 0.70:
        return "high"
    if score >= DEFAULT_ABSTAIN_THRESHOLD:
        return "medium"
    return "low"


def score_targets(facts: dict[str, Any], *, k: int = EVIDENCE_STRENGTH_K) -> dict[str, float]:
    """``{target_id: confidence}`` for every (first-party + boundary) target, deterministic."""
    return {t["id"]: element_confidence(t, facts, k=k)
            for t in facts.get("targets", []) if t.get("id")}


def container_confidence(facts: dict[str, Any], *, k: int = EVIDENCE_STRENGTH_K) -> dict[str, float]:
    """Mean element confidence per curated container, ``{container_id: score}`` (T1.3
    aggregate; GUI plan §5.1/§5.7).

    This is the engine-side single source for the container-level confidence the GUI
    displays — the ``arch serve`` API must never aggregate scores itself (the GUI §2.1
    iron rule: the engine computes, the UI renders), so the aggregation lives here next
    to :func:`element_confidence`. An ungrouped target falls back to its own id as the
    container, the same rule as the §9 generator's ``build_containers``.
    """
    scores = score_targets(facts, k=k)
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for t in facts.get("targets", []):
        tid = t.get("id")
        if not tid:
            continue
        cid = t.get("container_id") or tid
        sums[cid] = sums.get(cid, 0.0) + scores.get(tid, 0.0)
        counts[cid] = counts.get(cid, 0) + 1
    return {cid: round(sums[cid] / counts[cid], 4) for cid in sorted(sums)}


# One plain-language statement of the whole scoring recipe, shipped with every container
# detail so the GUI popover never paraphrases the engine (§2.7 single-sourcing).
CONTAINER_FORMULA = (
    "container confidence = mean(member element confidences); element confidence = "
    "evidence-ladder base (strongest incident layer) + corroboration + multi-layer + "
    "high-edge + doc bonuses, capped below τ when tagged needs-curation")


def container_confidence_detail(facts: dict[str, Any],
                                *, k: int = EVIDENCE_STRENGTH_K) -> dict[str, dict[str, Any]]:
    """Per-container explanation behind :func:`container_confidence` (GUI §5.2 hover
    popover): member count, the substituted mean calc, abstained members, and the
    weakest/strongest members as concrete worked examples (their own ``calc`` from
    :func:`element_confidence_detail`). Lives engine-side next to the score so the serve
    layer and the UI render it verbatim and never aggregate themselves (§2.1/§2.7);
    ``confidence``/``band`` here are byte-equal to :func:`container_confidence`'s.
    """
    by_container: dict[str, list[dict[str, Any]]] = {}
    for t in facts.get("targets", []):
        tid = t.get("id")
        if not tid:
            continue
        by_container.setdefault(t.get("container_id") or tid, []).append(t)
    out: dict[str, dict[str, Any]] = {}
    for cid in sorted(by_container):
        details = {t["id"]: element_confidence_detail(t, facts, k=k)
                   for t in by_container[cid]}
        scores = [d["confidence"] for d in details.values()]
        mean = round(sum(scores) / len(scores), 4)
        # worked examples: the two weakest members + the strongest (deterministic order)
        ordered = sorted(details.items(), key=lambda kv: (kv[1]["confidence"], kv[0]))
        picks = ordered[:2] + ([ordered[-1]] if len(ordered) > 2 else [])
        out[cid] = {
            "confidence": mean,
            "band": confidence_band(mean),
            "members": len(scores),
            "abstained_members": sum(1 for s in scores
                                     if s < DEFAULT_ABSTAIN_THRESHOLD),
            "threshold": DEFAULT_ABSTAIN_THRESHOLD,
            "calc": (f"mean of {len(scores)} member confidence(s) "
                     f"(min {min(scores):.2f}, max {max(scores):.2f}) = {mean:.2f}"),
            "formula": CONTAINER_FORMULA,
            "examples": [{"id": tid, **d} for tid, d in picks],
        }
    return out


def selective_partition(facts: dict[str, Any], threshold: float = DEFAULT_ABSTAIN_THRESHOLD,
                        *, k: int = EVIDENCE_STRENGTH_K) -> dict[str, list[str]]:
    """Abstain on the weak tail: ``{retained, abstained}`` id lists at *threshold* (T1.3).

    Retained = score ≥ threshold (the tool stands behind the placement); abstained = the rest
    (routed to "needs-curation" honesty). Both lists are sorted for determinism.
    """
    scores = score_targets(facts, k=k)
    retained = sorted(tid for tid, s in scores.items() if s >= threshold)
    abstained = sorted(tid for tid, s in scores.items() if s < threshold)
    return {"retained": retained, "abstained": abstained}


def risk_coverage_curve(facts: dict[str, Any], *,
                        k: int = EVIDENCE_STRENGTH_K) -> list[dict[str, Any]]:
    """The selective-prediction risk-coverage curve over the internal confidence ordering (T1.3).

    Sweeps the threshold τ over the sorted distinct confidence scores; at each τ the model
    *retains* elements scoring ≥ τ. Each point is ``{threshold, coverage, retained,
    selective_risk}`` where:

      * ``coverage`` = fraction of elements retained (1.0 at τ=0; → 0 as τ→1);
      * ``selective_risk`` = mean ``1 − confidence`` over the retained set — a **model-internal
        risk proxy**, NOT a measured error rate. The independent-oracle ECE/AURC is out-of-tree
        eval work; here the curve shape (risk falls monotonically as we abstain on the weak tail)
        is the structural property only an evidence-aware tool can exhibit.

    Deterministic: thresholds are the sorted distinct scores plus 0.0, evaluated low→high.
    """
    scores = sorted(score_targets(facts, k=k).values())
    n = len(scores)
    if n == 0:
        return []
    thresholds = sorted({0.0, *scores})
    curve: list[dict[str, Any]] = []
    for tau in thresholds:
        retained = [s for s in scores if s >= tau]
        coverage = len(retained) / n
        risk = (sum(1.0 - s for s in retained) / len(retained)) if retained else 0.0
        curve.append({
            "threshold": round(tau, 4),
            "coverage": round(coverage, 4),
            "retained": len(retained),
            "selective_risk": round(risk, 4),
        })
    return curve


def aurc(curve: list[dict[str, Any]]) -> float:
    """Area under the risk-coverage curve (trapezoid over coverage); lower = better-calibrated.

    A single deterministic summary of the curve (the selective-prediction scalar). With the
    model-internal risk proxy it is a *self-consistency* number, not the out-of-tree calibrated
    AURC — reported so the curve has a headline figure, clearly proxy-based.
    """
    if len(curve) < 2:
        return 0.0
    # order by increasing coverage so the trapezoid integrates left→right.
    pts = sorted(((p["coverage"], p["selective_risk"]) for p in curve))
    area = 0.0
    for (c0, r0), (c1, r1) in zip(pts, pts[1:]):
        area += (c1 - c0) * (r0 + r1) / 2.0
    return round(area, 4)


def summarize(facts: dict[str, Any], threshold: float = DEFAULT_ABSTAIN_THRESHOLD,
              *, k: int = EVIDENCE_STRENGTH_K) -> dict[str, Any]:
    """Full structured abstention summary for one model (the ``arch abstain`` payload, T1.3)."""
    scores = score_targets(facts, k=k)
    part = selective_partition(facts, threshold, k=k)
    curve = risk_coverage_curve(facts, k=k)
    n = len(scores)
    bands = {"high": 0, "medium": 0, "low": 0}
    for s in scores.values():
        bands[confidence_band(s)] += 1
    mean_conf = round(sum(scores.values()) / n, 4) if n else 0.0
    return {
        "threshold": threshold,
        "elements": n,
        "mean_confidence": mean_conf,
        "bands": bands,
        "retained": len(part["retained"]),
        "abstained": len(part["abstained"]),
        "coverage": round(len(part["retained"]) / n, 4) if n else 0.0,
        "aurc": aurc(curve),
        "abstained_ids": part["abstained"],
        "scores": dict(sorted(scores.items())),
        "curve": curve,
    }


# --------------------------------------------------------------------------- render

def render(summary: dict[str, Any]) -> str:
    """Render the abstention summary as advisory markdown (T1.3)."""
    lines = ["# Calibrated abstention  (`arch abstain`)", ""]
    if summary["elements"] == 0:
        lines += ["_No curated targets — nothing to score._", ""]
        return "\n".join(lines) + "\n"
    b = summary["bands"]
    lines += [
        f"- **{summary['elements']} elements** · mean confidence **{summary['mean_confidence']:.2f}**",
        f"- bands: **{b['high']} high** · {b['medium']} medium · {b['low']} low",
        f"- at τ={summary['threshold']:.2f}: **retain {summary['retained']} "
        f"({summary['coverage']:.0%})**, abstain {summary['abstained']} → needs-curation",
        f"- AURC (model-internal risk proxy, lower better): **{summary['aurc']:.3f}**",
        "",
        "> The risk axis is `mean(1 − confidence)` over the retained set — a model-internal "
        "proxy, not a measured error rate. The independent-oracle ECE/AURC is out-of-tree "
        "eval work (T1.3).",
        "",
        "## Risk–coverage curve (abstain on the weak tail → risk falls)",
        "",
        "| τ | coverage | retained | selective risk (proxy) |",
        "|---:|---:|---:|---:|",
    ]
    for p in summary["curve"]:
        lines.append(f"| {p['threshold']:.2f} | {p['coverage']:.0%} | "
                     f"{p['retained']} | {p['selective_risk']:.3f} |")
    if summary["abstained_ids"]:
        lines += ["", "## Abstained (below τ — the tool says \"needs curation\")", ""]
        lines += [f"- `{tid}` (confidence {summary['scores'][tid]:.2f})"
                  for tid in summary["abstained_ids"]]
    else:
        lines += ["", "_No element falls below τ — the model stands behind every placement._"]
    return "\n".join(lines) + "\n"


def run(ws: Workspace, threshold: float = DEFAULT_ABSTAIN_THRESHOLD,
        *, k: int = EVIDENCE_STRENGTH_K) -> dict[str, Any]:
    """Score the curated model, write ``ws.abstention_report``, return the structured summary."""
    facts = load_json(ws.curated_facts) if ws.curated_facts.exists() else \
        (load_json(ws.extracted_facts) if ws.extracted_facts.exists() else
         {"targets": [], "relationships": []})
    summary = summarize(facts, threshold, k=k)
    ws.abstention_report.parent.mkdir(parents=True, exist_ok=True)
    ws.abstention_report.write_text(render(summary), encoding="utf-8", newline="")
    return summary
