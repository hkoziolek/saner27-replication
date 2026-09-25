"""T2.6 — per-decision explainability (`arch explain`) + shadowed/empty-glob lint.

Two trust instruments over the curated fact model — both pure, deterministic, and a
function of the committed facts + ``mapping-rules.yaml`` only (everything sorted; no
timestamps; no ``set``-iteration order leaking into output):

  * **explain** answers *"why is this element in this container?"* and *"why does this edge
    exist?"*. Curation stamps a ``grouped_by`` provenance object on every target
    (:mod:`anon.stages.apply_mapping_rules`); its ``rule_index`` points back into the
    hand-authored ``mapping-rules.yaml`` ``group:`` list, so a placement is *citable* at
    review time. Edge evidence (the cited ``link``/``include``/``symbol_use``/``interop``
    artifacts behind a relationship) is rendered human-readable. SAR opacity — an opaque
    partition with no rationale — is the thing Anon claims *not* to have; this makes
    the claim concrete exactly where a human inspects the diagram (P§7.1, T2.6).

  * **lint** is the preventive twin. It replays the §7.4 first-match-wins grouping
    resolution to flag the ``members_glob`` footgun: a glob shadowed by an earlier/broader
    glob (or a catch-all ``*``) silently claims nothing and leaves its container empty.
    Findings: ``shadowed-glob`` (every target it matches was already claimed),
    ``dead-glob`` (matches no target at all — a typo'd path), and ``empty-container`` (a
    declared container that ends up with no members).

The ``grouped_by`` field is a curated-only, additive field on the OPEN target object — the
exact precedent of ``container_id`` / ``container_name`` / ``boundary_kind`` (so it needs no
schema change and never reaches the determinism-gated *extracted* facts; the generator
ignores it, so DSL output stays byte-identical, §1.1#2).
"""
from __future__ import annotations

from typing import Any

from . import confidence
from .config import MappingRules, load_mapping_rules
from .jsonio import load_json
from .model import evidence_strength_str
from .paths import Workspace
from .stages.apply_mapping_rules import (_excluded, _group_index,
                                         glob_matches_target, tag_matches_target)

# Human-readable label per grouped_by signal (the placement-decision vocabulary, §7.4).
_SIGNAL_LABEL = {
    "members": "explicit `members:` entry",
    "members_glob": "`members_glob:` pattern match",
    "boundary": "`boundary:` promotion to an external system / person",
    "seam": "kept as a cross-language seam (native / contract) despite drop_external",
    "singleton": "no rule matched — provisional singleton (needs-curation)",
}


# --------------------------------------------------------------------------- explain

def _placement_reason(grouped_by: dict[str, Any]) -> str:
    """One-line, human-readable rationale for a target's container placement (§7.4 / T2.6)."""
    sig = grouped_by.get("signal")
    bits = [_SIGNAL_LABEL.get(sig, sig or "unknown")]
    if grouped_by.get("container"):
        bits.append(f"→ container '{grouped_by['container']}'")
    if grouped_by.get("pattern"):
        bits.append(f"via glob '{grouped_by['pattern']}'")
    if grouped_by.get("rule_index") is not None:
        bits.append(f"(mapping-rules.yaml group #{grouped_by['rule_index']})")
    if grouped_by.get("kind"):
        bits.append(f"[{grouped_by['kind']}]")
    return " ".join(bits)


def _ev(e: dict[str, Any]) -> dict[str, Any]:
    # ``layer`` is the engine's evidence-kind → L0–L3/interop/runtime stratum
    # (``confidence._LAYER_OF``, the same ladder scorecard/confidence use), surfaced here so
    # the GUI renders the stratum badge verbatim rather than re-deriving the mapping (§2.1
    # single-sourcing — a client-side copy would be the bug-prone twin we already burned on).
    return {"type": e.get("type"), "detail": e.get("detail", ""),
            "count": e.get("count"), "visibility": e.get("visibility"),
            "layer": confidence._LAYER_OF.get(e.get("type"))}


def _edge_summary(r: dict[str, Any]) -> dict[str, Any]:
    """Human-readable summary of one relationship + its cited evidence (§5)."""
    return {
        "source": r.get("source"),
        "target": r.get("target"),
        "kind": r.get("kind") or "uses",
        "weight": r.get("weight"),
        "is_declared": bool(r.get("is_declared_dependency")),
        "confidence": r.get("confidence"),
        "evidence_strength": r.get("evidence_strength") or evidence_strength_str(r.get("evidence", []) or []),
        "evidence": [_ev(e) for e in (r.get("evidence", []) or [])],
        # the distinct strata behind this one edge, sorted (the GUI's per-edge layer badges) —
        # engine truth, deduped from the cited evidence's kinds, never recomputed client-side.
        "evidence_layers": sorted({confidence._LAYER_OF[e["type"]]
                                   for e in (r.get("evidence", []) or [])
                                   if e.get("type") in confidence._LAYER_OF}),
        "tags": sorted(r.get("tags", []) or []),
    }


def explain_target(facts: dict[str, Any], tid: str,
                   rules: MappingRules | None = None) -> dict[str, Any] | None:
    """Explain a target's placement + the edges that touch it, or ``None`` if not found."""
    by_id = {t["id"]: t for t in facts.get("targets", []) if t.get("id")}
    t = by_id.get(tid)
    if t is None:
        return None
    grouped_by = t.get("grouped_by") or {}
    out_edges, in_edges = [], []
    for r in facts.get("relationships", []):
        if r.get("source") == tid:
            out_edges.append(_edge_summary(r))
        elif r.get("target") == tid:
            in_edges.append(_edge_summary(r))
    name_pin = rules.name_overrides.get(tid) if rules is not None else None
    return {
        "id": tid,
        "name": t.get("name", tid),
        "container_id": t.get("container_id"),
        "container_name": t.get("container_name"),
        "grouped_by": grouped_by,
        "placement_reason": _placement_reason(grouped_by),
        "name_pin": name_pin,
        "external": bool(t.get("external")),
        "tags": sorted(t.get("tags", []) or []),
        "out_edges": sorted(out_edges, key=lambda e: (e["target"] or "")),
        "in_edges": sorted(in_edges, key=lambda e: (e["source"] or "")),
    }


def explain_edge(facts: dict[str, Any], source: str, target: str) -> dict[str, Any] | None:
    """Explain why an edge exists: its cited evidence rendered human-readable (§5)."""
    for r in facts.get("relationships", []):
        if r.get("source") == source and r.get("target") == target:
            return _edge_summary(r)
    return None


# --------------------------------------------------------------------------- lint

def lint_rules(facts: dict[str, Any], rules: MappingRules) -> list[dict[str, Any]]:
    """Replay §7.4 first-match-wins grouping over *facts* to flag glob/container footguns.

    Mirrors :func:`apply_mapping_rules.curate`'s resolution exactly: excluded targets drop
    out first; explicit ``members:`` win; then ``members_glob:`` patterns are tried in rule
    order, each claiming only targets not already claimed (first-match-wins). A glob that
    claims nothing — because an earlier/broader glob or ``*`` already swept its targets — is
    a ``shadowed-glob``; a glob matching no target at all is a ``dead-glob``; a declared
    container left with no members is an ``empty-container``. Returns findings sorted
    deterministically.
    """
    candidates = [t for t in facts.get("targets", []) if t.get("id") and not _excluded(t, rules)]
    member_to_container, glob_groups, _cid_to_cname = _group_index(rules)

    claimed: dict[str, str] = {}   # target id -> the container name that claimed it
    for t in candidates:
        tid = t["id"]
        if tid in member_to_container:
            claimed[tid] = member_to_container[tid][1]

    findings: list[dict[str, Any]] = []
    for _cid, cname, globs, tags, ridx in glob_groups:
        # members_glob (id/path/name) and members_tag (target tags) replay identically for the
        # dead/shadowed lint — only the matcher and the "matches nothing" reason differ.
        for pattern, matcher, what in (
                *((gl, glob_matches_target, "target id/path/name") for gl in globs),
                *((tg, tag_matches_target, "any target tag") for tg in tags)):
            matched = [t["id"] for t in candidates if matcher(pattern, t)]
            fresh = [tid for tid in matched if tid not in claimed]
            for tid in fresh:
                claimed[tid] = cname
            if not matched:
                findings.append({
                    "kind": "dead-glob", "container": cname, "rule_index": ridx,
                    "pattern": pattern,
                    "reason": f"matches no {what} (typo'd pattern?)",
                })
            elif not fresh:
                shadowers = sorted({claimed[tid] for tid in matched})
                findings.append({
                    "kind": "shadowed-glob", "container": cname, "rule_index": ridx,
                    "pattern": pattern, "shadowed_by": shadowers,
                    "reason": "every target it matches was already claimed by: "
                              + ", ".join(shadowers),
                })

    claimed_containers = set(claimed.values())
    for ridx, g in enumerate(rules.groups):
        cname = g.get("container")
        if not cname or cname in claimed_containers:
            continue
        findings.append({
            "kind": "empty-container", "container": cname, "rule_index": ridx,
            "reason": "no target is assigned to this container "
                      "(its members/globs match nothing, or were shadowed)",
        })

    findings.sort(key=lambda f: (f["kind"], f.get("rule_index", -1),
                                 f.get("pattern", ""), f["container"]))
    return findings


# --------------------------------------------------------------------------- render

def render_target(expl: dict[str, Any]) -> str:
    lines = [f"# Explain target `{expl['id']}`", "",
             f"- **name**: {expl['name']}",
             f"- **container**: {expl.get('container_name')} (`{expl.get('container_id')}`)",
             f"- **placement**: {expl['placement_reason']}"]
    if expl.get("name_pin"):
        lines.append(f"- **name pinned** (name_overrides): {expl['name_pin']}")
    if expl.get("tags"):
        lines.append(f"- **tags**: {', '.join(expl['tags'])}")
    for title, key, peer in (("Outgoing dependencies", "out_edges", "target"),
                             ("Incoming dependencies", "in_edges", "source")):
        lines += ["", f"## {title}"]
        edges = expl.get(key, [])
        if not edges:
            lines.append("- _none_")
            continue
        for e in edges:
            lines.append(f"- `{e[peer]}` — **{e['kind']}** "
                         f"(weight {e['weight']}, {e['confidence']}, "
                         f"{'declared' if e['is_declared'] else 'inferred'}): "
                         f"{e['evidence_strength']}")
    return "\n".join(lines) + "\n"


def render_edge(edge: dict[str, Any]) -> str:
    lines = [f"# Explain edge `{edge['source']}` → `{edge['target']}`", "",
             f"- **kind**: {edge['kind']}",
             f"- **weight**: {edge['weight']}  ·  **confidence**: {edge['confidence']}  ·  "
             f"**{'declared' if edge['is_declared'] else 'inferred'}**",
             f"- **rationale**: {edge['evidence_strength']}"]
    if edge.get("tags"):
        lines.append(f"- **tags**: {', '.join(edge['tags'])}")
    lines += ["", "## Cited evidence (§5 — no edge without evidence)"]
    for e in edge.get("evidence", []):
        extra = []
        if e.get("count") is not None:
            extra.append(f"×{e['count']}")
        if e.get("visibility"):
            extra.append(str(e["visibility"]))
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"- **{e['type']}**{suffix}: `{e.get('detail', '')}`")
    return "\n".join(lines) + "\n"


def render_lint(findings: list[dict[str, Any]]) -> str:
    lines = ["# Mapping-rules lint (T2.6 — shadowed / empty globs)", ""]
    if not findings:
        lines += ["✅ No shadowed/dead globs or empty containers — every "
                  "`group:` rule binds at least one target."]
        return "\n".join(lines) + "\n"
    icon = {"shadowed-glob": "⚠️", "dead-glob": "⚠️", "empty-container": "❌"}
    for f in findings:
        where = f"group #{f['rule_index']} '{f['container']}'"
        pat = f" pattern `{f['pattern']}`" if f.get("pattern") else ""
        lines.append(f"- {icon.get(f['kind'], '⚠️')} **{f['kind']}** ({where}){pat}: {f['reason']}")
    return "\n".join(lines) + "\n"


def render_decision_log(decisions: list[dict[str, Any]]) -> str:
    """A compact placement decision log over all curated targets (the no-arg `explain`)."""
    lines = ["# Curation decision log (T2.6 — every placement, citable)", ""]
    if not decisions:
        lines.append("- _no targets_")
        return "\n".join(lines) + "\n"
    for d in sorted(decisions, key=lambda d: d["id"]):
        lines.append(f"- `{d['id']}` → **{d.get('container_name')}**: {d['placement_reason']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- run

def _load(path) -> dict[str, Any] | None:
    try:
        if path.exists():
            data = load_json(path)
            return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    return None


def run(ws: Workspace, element: str | None = None, lint: bool = False) -> dict[str, Any]:
    """Render an explanation/lint to text (the ``arch explain`` entry point).

    ``element`` selects what to explain: a target id, or an edge as ``source->target``.
    ``lint`` runs the shadowed/empty-glob lint. With neither, prints the full decision log
    plus an advisory lint. Returns ``{"text": <rendered>, ...structured...}`` for callers/tests.
    """
    rules = load_mapping_rules(ws.mapping_rules)
    curated = _load(ws.curated_facts) or _load(ws.extracted_facts) or {}
    out: dict[str, Any] = {}
    parts: list[str] = []

    if element:
        if "->" in element:
            src, tgt = (s.strip() for s in element.split("->", 1))
            edge = explain_edge(curated, src, tgt)
            out["edge"] = edge
            parts.append(render_edge(edge) if edge
                         else f"No edge `{src}` → `{tgt}` in the curated model.\n")
        else:
            tgt_expl = explain_target(curated, element.strip(), rules)
            out["target"] = tgt_expl
            parts.append(render_target(tgt_expl) if tgt_expl
                         else f"No target `{element.strip()}` in the curated model.\n")

    if lint or not element:
        # lint replays grouping over the FULL extracted universe (curate has already dropped
        # excluded/external singletons, which would hide a glob that matches them), §7.4.
        extracted = _load(ws.extracted_facts) or curated
        findings = lint_rules(extracted, rules)
        out["lint"] = findings
        parts.append(render_lint(findings))

    if not element and not lint:
        decisions = [explain_target(curated, t["id"], rules)
                     for t in curated.get("targets", []) if t.get("id")]
        decisions = [d for d in decisions if d]
        out["decisions"] = decisions
        parts.insert(0, render_decision_log(decisions))

    out["text"] = "\n".join(parts)
    return out
