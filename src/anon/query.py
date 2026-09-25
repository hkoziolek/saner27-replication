"""Engine §6 — the ORACLE: deterministic, cited query primitives over the curated model.

A queryable grounded graph is the highest-utility front-end for the no-head-model
audience and the bridge to the developer (engine plan
``plans/2026-06-07-anon-architecture-intelligence-features.md`` §6). Three
consumers, one engine: the human/CI ``arch query`` verb, the GUI's ``POST /ask`` /
``GET /query/*`` endpoints (GUI plan §7.6), and — later — the read-only MCP server.

The primitives (each a pure, deterministic, cited function of the curated facts):

  * :func:`dependents` / :func:`dependencies` — direct + transitive, every hop cited;
  * :func:`blast_radius` — "if I change X, what is impacted and what is reachable?";
  * :func:`paths_between` — why does A reach B? (the cited shortest edge chains);
  * :func:`cycles_of` — the directed cycles an element participates in (the §3 Tarjan
    SCC pass in :mod:`anon.stages.risk`), at target AND lifted-container level;
  * :func:`conformance_of` — is the edge A→B allowed? (reuses the layering machinery of
    :func:`anon.stages.drift_report.check_layering`, never a private re-check);
  * :func:`explain_of` — why is X here / why does this edge exist? (delegates to the
    shipped :mod:`anon.explain`).

Moat-safety (engine §6.3): the oracle only reads and traverses the grounded model — it
never invents an edge or answers beyond the evidence. "No path found", "unknown id (did
you mean …?)" and "below coverage floor — advisory" are first-class answers. Every
result carries ``derived_from_hash`` (the :func:`anon.model.content_hash` of the
facts it was computed against) plus the coverage caveat, so a caller that cached an
answer across a ``git pull`` can detect staleness and re-query.

Determinism: no networkx (the ``propose.connected_components`` style precedent), all
iteration over sorted keys, results ranked by weight then id, all I/O through
:mod:`anon.jsonio`. Same model → byte-identical answer.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .config import MappingRules, load_mapping_rules, load_yaml
from .confidence import _LAYER_OF
from .explain import _edge_summary, explain_edge, explain_target
from .jsonio import dump_json, dumps_json, load_json
from .model import content_hash, evidence_strength_str
from .paths import Workspace
from .stages.drift_report import (COVERAGE_FLOOR, _coverage_l0,
                                  _matches_container, check_layering)
from .stages.generate_structurizr import build_container_edges, build_containers
from .stages.risk import cycles as _directed_cycles

QUERY_SCHEMA = "query/1"
PRIMITIVES = ("dependents", "dependencies", "blast-radius", "path", "cycles",
              "conformance", "explain")
DEFAULT_LIMIT = 100      # transitive answers can be large (engine §6.7) — ranked + capped
MAX_PATHS = 10


# --------------------------------------------------------------------------- substrate

def load_facts(ws: Workspace) -> dict[str, Any] | None:
    """The model a query runs against: curated, falling back to extracted (pre-curation)."""
    for path in (ws.curated_facts, ws.extracted_facts):
        if path.exists():
            return load_json(path)
    return None


def coverage_caveat(facts: dict[str, Any]) -> dict[str, Any]:
    """The honesty caveat every answer carries (engine §6.1): completeness is bounded by
    coverage, and an answer over a below-floor model is advisory, never authoritative."""
    cov = _coverage_l0(facts)
    caveat: dict[str, Any] = {
        "l0_coverage": round(cov, 4),
        "floor": COVERAGE_FLOOR,
        "advisory": cov < COVERAGE_FLOOR,
    }
    if caveat["advisory"]:
        caveat["note"] = ("below coverage floor — the answer is advisory; absence of an "
                          "edge/path is not evidence of absence (§11.6)")
    return caveat


def _envelope(facts: dict[str, Any], primitive: str, params: dict[str, Any],
              payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": QUERY_SCHEMA,
        "primitive": primitive,
        "params": {k: v for k, v in sorted(params.items()) if v is not None},
        "derived_from_hash": content_hash(facts),
        "coverage": coverage_caveat(facts),
        **payload,
    }


def cite_edge(r: dict[str, Any]) -> dict[str, Any]:
    """Compact cited summary of one relationship — every hop in an answer cites its
    evidence basis (engine §6.3: no fact without evidence)."""
    ev = r.get("evidence", []) or []
    return {
        "source": r.get("source"),
        "target": r.get("target"),
        "kind": r.get("kind") or "uses",
        "weight": r.get("weight"),
        "is_declared": bool(r.get("is_declared_dependency")),
        "confidence": r.get("confidence"),
        "evidence_strength": r.get("evidence_strength") or evidence_strength_str(ev),
        "evidence_layers": sorted({layer for layer in (_LAYER_OF.get(e.get("type"))
                                                       for e in ev) if layer}),
        "evidence_count": len(ev),
    }


def _graph(facts: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]],
                                           dict[tuple[str, str], dict[str, Any]]]:
    """Sorted forward/reverse adjacency + the (source, target) → relationship index."""
    fwd: dict[str, list[str]] = {}
    rev: dict[str, list[str]] = {}
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for r in facts.get("relationships", []) or []:
        s, t = r.get("source"), r.get("target")
        if not s or not t:
            continue
        # the deterministic oracle graphs the BUILD-TARGET altitude; component-altitude
        # edges (`…/component:` endpoints) are a below-grid drill-down concern and would
        # otherwise inject synthetic component nodes into neighbours/path/cycles answers.
        if "/component:" in s or "/component:" in t:
            continue
        index[(s, t)] = r
        fwd.setdefault(s, []).append(t)
        rev.setdefault(t, []).append(s)
    return ({k: sorted(set(v)) for k, v in fwd.items()},
            {k: sorted(set(v)) for k, v in rev.items()},
            index)


def _meta(by_id: dict[str, dict[str, Any]], tid: str) -> dict[str, Any]:
    t = by_id.get(tid, {})
    return {
        "id": tid,
        "name": t.get("name", tid),
        "container_id": t.get("container_id") or tid,
        "container_name": t.get("container_name") or t.get("name", tid),
        "external": bool(t.get("external")),
    }


# --------------------------------------------------------------------------- resolve

def resolve(facts: dict[str, Any], ident: str | None) -> dict[str, Any]:
    """Resolve a user-supplied identifier to its graph seeds (target ids).

    Resolution order (first hit wins, deterministic): exact target id → exact container
    id → unique exact container name → unique exact target name → component/code child
    id (the owning target) → unique case-insensitive match. When nothing matches the
    result is ``{"kind": "unknown", "targets": [], "candidates": [...]}`` — an unknown
    id is a first-class answer with suggestions, never a guess (engine §6.3).
    """
    ident = (ident or "").strip()
    targets = [t for t in facts.get("targets", []) or [] if t.get("id")]
    by_id = {t["id"]: t for t in targets}
    if ident in by_id:
        return {"kind": "target", "id": ident,
                "name": by_id[ident].get("name", ident), "targets": [ident]}

    members_by_cid: dict[str, list[str]] = {}
    cname_by_cid: dict[str, str] = {}
    for t in targets:
        cid = t.get("container_id") or t["id"]
        members_by_cid.setdefault(cid, []).append(t["id"])
        cname_by_cid.setdefault(cid, t.get("container_name") or t.get("name") or cid)
    if ident in members_by_cid:
        return {"kind": "container", "id": ident, "name": cname_by_cid[ident],
                "targets": sorted(members_by_cid[ident])}
    cids_by_name: dict[str, list[str]] = {}
    for cid, cname in cname_by_cid.items():
        cids_by_name.setdefault(cname, []).append(cid)
    if ident in cids_by_name and len(cids_by_name[ident]) == 1:
        cid = cids_by_name[ident][0]
        return {"kind": "container", "id": cid, "name": ident,
                "targets": sorted(members_by_cid[cid])}

    named = [t for t in targets if t.get("name") == ident]
    if len(named) == 1:
        return {"kind": "target", "id": named[0]["id"], "name": ident,
                "targets": [named[0]["id"]]}

    for tid in sorted(by_id):
        for comp in by_id[tid].get("components", []) or []:
            children = [comp] + list(comp.get("code", []) or [])
            for child in children:
                if child.get("id") == ident:
                    return {"kind": "component", "id": ident,
                            "name": child.get("name", ident), "targets": [tid],
                            "note": f"component of `{tid}` — answered at the owning "
                                    "target's granularity (engine §6.7)"}

    low = ident.lower()
    ci: list[tuple[str, str, str]] = []
    for tid in sorted(by_id):
        t = by_id[tid]
        if tid.lower() == low or (t.get("name") or "").lower() == low:
            ci.append(("target", tid, t.get("name", tid)))
    for cid in sorted(members_by_cid):
        if cid in by_id:
            continue
        if cid.lower() == low or cname_by_cid[cid].lower() == low:
            ci.append(("container", cid, cname_by_cid[cid]))
    if len(ci) == 1:
        kind, rid, name = ci[0]
        seeds = [rid] if kind == "target" else sorted(members_by_cid[rid])
        return {"kind": kind, "id": rid, "name": name, "targets": seeds}

    candidates: list[str] = []
    for tid in sorted(by_id):
        t = by_id[tid]
        if low and (low in tid.lower() or low in (t.get("name") or "").lower()):
            candidates.append(tid)
    for cid in sorted(members_by_cid):
        if cid in by_id:
            continue
        if low and (low in cid.lower() or low in cname_by_cid[cid].lower()):
            candidates.append(cid)
    return {"kind": "unknown", "id": ident, "name": None, "targets": [],
            "candidates": candidates[:10]}


def _unknown(facts: dict[str, Any], primitive: str, params: dict[str, Any],
             res: dict[str, Any], role: str = "id") -> dict[str, Any]:
    return _envelope(facts, primitive, params, {
        "resolved": None,
        "error": "unknown_element",
        "message": f"no element matches {role}='{res['id']}'",
        "candidates": res.get("candidates", []),
    })


# --------------------------------------------------------------------------- traversal

def _related(facts: dict[str, Any], res: dict[str, Any], direction: str,
             transitive: bool, limit: int) -> dict[str, Any]:
    """Shared engine for dependents (``direction="in"``) / dependencies (``"out"``).

    Direct answers cite every contributing edge in full; transitive answers carry depth
    + the discovery path (each hop still resolvable via :func:`explain_edge`). Results
    are weight-ranked then id-sorted (engine §6.2) and capped at *limit* (engine §6.7).
    """
    fwd, rev, index = _graph(facts)
    adj = rev if direction == "in" else fwd
    by_id = {t["id"]: t for t in facts.get("targets", []) or [] if t.get("id")}
    seed_set = set(res["targets"])

    if not transitive:
        hits: dict[str, list[dict[str, Any]]] = {}
        for s in sorted(seed_set):
            for n in adj.get(s, []):
                if n in seed_set:
                    continue  # intra-container edges are not "dependents of the container"
                pair = (n, s) if direction == "in" else (s, n)
                hits.setdefault(n, []).append(cite_edge(index[pair]))
        results = []
        for n in sorted(hits):
            edges = hits[n]
            results.append({**_meta(by_id, n),
                            "weight": sum(e.get("weight") or 0 for e in edges),
                            "edges": edges})
        results.sort(key=lambda x: (-(x["weight"] or 0), x["id"]))
        containers = sorted({r["container_id"] for r in results})
        return {"transitive": False, "total": len(results), "containers": containers,
                "results": results[:limit], "truncated": len(results) > limit}

    depth = {s: 0 for s in seed_set}
    parent: dict[str, str] = {}
    frontier = sorted(seed_set)
    d = 0
    while frontier:
        d += 1
        nxt: list[str] = []
        for u in frontier:
            for v in adj.get(u, []):
                if v in depth:
                    continue
                depth[v] = d
                parent[v] = u
                nxt.append(v)
        frontier = sorted(set(nxt))
    results = []
    for v in sorted(depth, key=lambda x: (depth[x], x)):
        if v in seed_set:
            continue
        chain = [v]
        while chain[-1] not in seed_set:
            chain.append(parent[chain[-1]])
        # the chain follows forward edges v→…→seed for dependents; reverse it for
        # dependencies so a path always reads source → … → target.
        path = chain if direction == "in" else list(reversed(chain))
        results.append({**_meta(by_id, v), "depth": depth[v], "path": path})
    containers = sorted({r["container_id"] for r in results})
    return {"transitive": True, "total": len(results), "containers": containers,
            "results": results[:limit], "truncated": len(results) > limit}


# --------------------------------------------------------------------------- primitives

def dependents(facts: dict[str, Any], ident: str, *, transitive: bool = False,
               limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Who depends on *ident* (direct, or transitive with depth + path)?"""
    params = {"id": ident, "transitive": transitive}
    res = resolve(facts, ident)
    if res["kind"] == "unknown":
        return _unknown(facts, "dependents", params, res)
    payload = {"resolved": res, **_related(facts, res, "in", transitive, limit)}
    return _envelope(facts, "dependents", params, payload)


def dependencies(facts: dict[str, Any], ident: str, *, transitive: bool = False,
                 limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """What does *ident* depend on (direct, or transitive with depth + path)?"""
    params = {"id": ident, "transitive": transitive}
    res = resolve(facts, ident)
    if res["kind"] == "unknown":
        return _unknown(facts, "dependencies", params, res)
    payload = {"resolved": res, **_related(facts, res, "out", transitive, limit)}
    return _envelope(facts, "dependencies", params, payload)


def blast_radius(facts: dict[str, Any], ident: str, *,
                 limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Change-impact: who is (transitively) impacted if *ident* changes, and what does
    it (transitively) reach — the developer's single most-asked architecture question
    (engine §6.1)."""
    params = {"id": ident}
    res = resolve(facts, ident)
    if res["kind"] == "unknown":
        return _unknown(facts, "blast-radius", params, res)
    up = _related(facts, res, "in", True, limit)
    down = _related(facts, res, "out", True, limit)
    payload = {
        "resolved": res,
        "impacted": {"description": "transitively depends on it — affected if it changes",
                     "total": up["total"], "containers": up["containers"],
                     "elements": up["results"], "truncated": up["truncated"]},
        "reachable": {"description": "it transitively depends on — what a change here can touch",
                      "total": down["total"], "containers": down["containers"],
                      "elements": down["results"], "truncated": down["truncated"]},
    }
    return _envelope(facts, "blast-radius", params, payload)


def paths_between(facts: dict[str, Any], ident: str, to: str, *,
                  max_paths: int = MAX_PATHS) -> dict[str, Any]:
    """Why does A reach B? All *shortest* directed paths (cited edge chains), capped at
    *max_paths*. "No path found" is a first-class answer (engine §6.3)."""
    params = {"id": ident, "to": to}
    res_s = resolve(facts, ident)
    if res_s["kind"] == "unknown":
        return _unknown(facts, "path", params, res_s, role="id")
    res_t = resolve(facts, to)
    if res_t["kind"] == "unknown":
        return _unknown(facts, "path", params, res_t, role="to")

    fwd, _rev, index = _graph(facts)
    sources = set(res_s["targets"])
    sinks = set(res_t["targets"])

    overlap = sorted(sources & sinks)
    if overlap:
        payload = {"resolved_source": res_s, "resolved_target": res_t, "found": True,
                   "shortest_length": 0, "paths": [{"elements": [overlap[0]], "edges": []}],
                   "truncated": False,
                   "note": "source and target resolve to the same element(s)"}
        return _envelope(facts, "path", params, payload)

    depth = {s: 0 for s in sources}
    parents: dict[str, list[str]] = {}
    frontier = sorted(sources)
    hits: list[str] = []
    d = 0
    while frontier and not hits:
        d += 1
        nxt: list[str] = []
        for u in frontier:
            for v in fwd.get(u, []):
                if v in depth and depth[v] < d:
                    continue
                if v not in depth:
                    depth[v] = d
                    nxt.append(v)
                if u not in parents.setdefault(v, []):
                    parents[v].append(u)
        frontier = sorted(set(nxt))
        hits = sorted(v for v in sinks if depth.get(v) == d)

    if not hits:
        payload = {"resolved_source": res_s, "resolved_target": res_t, "found": False,
                   "shortest_length": None, "paths": [], "truncated": False,
                   "note": "no path found — over an incomplete model this is advisory, "
                           "not proof of isolation (engine §6.3)"}
        return _envelope(facts, "path", params, payload)

    chains: list[list[str]] = []

    def _walk(node: str, acc: list[str]) -> None:
        if len(chains) > max_paths:  # collect one extra so `truncated` is honest
            return
        if depth[node] == 0:
            chains.append(list(reversed(acc + [node])))
            return
        for p in sorted(parents.get(node, [])):
            _walk(p, acc + [node])

    for h in hits:
        _walk(h, [])
    chains.sort()
    truncated = len(chains) > max_paths
    paths = []
    for chain in chains[:max_paths]:
        edges = [_edge_summary(index[(a, b)]) for a, b in zip(chain, chain[1:])]
        paths.append({"elements": chain, "edges": edges})
    payload = {"resolved_source": res_s, "resolved_target": res_t, "found": True,
               "shortest_length": d, "paths": paths, "truncated": truncated}
    return _envelope(facts, "path", params, payload)


def cycles_of(facts: dict[str, Any], ident: str) -> dict[str, Any]:
    """The directed cycles *ident* participates in — at target level and at the lifted
    container level (the §3 Tarjan SCC pass over both graphs)."""
    params = {"id": ident}
    res = resolve(facts, ident)
    if res["kind"] == "unknown":
        return _unknown(facts, "cycles", params, res)

    fwd, _rev, index = _graph(facts)
    by_id = {t["id"]: t for t in facts.get("targets", []) or [] if t.get("id")}
    seed_set = set(res["targets"])

    target_cycles = []
    for comp in _directed_cycles(by_id.keys(), fwd):
        if not seed_set & set(comp):
            continue
        members = set(comp)
        edges = [cite_edge(index[(a, b)]) for (a, b) in sorted(index)
                 if a in members and b in members and a != b]
        target_cycles.append({"members": comp, "edges": edges})

    containers = build_containers(facts)
    t2c = {m: c.cid for c in containers.values() for m in c.members}
    cedges = build_container_edges(facts, t2c)
    cadj: dict[str, list[str]] = {}
    for (s, t) in cedges:
        cadj.setdefault(s, []).append(t)
    seed_containers = {t2c[s] for s in seed_set if s in t2c}
    container_cycles = []
    for comp in _directed_cycles(containers.keys(), cadj):
        if not seed_containers & set(comp):
            continue
        members = set(comp)
        edges = [{"source": s, "target": t, "weight": e["weight"],
                  "kind": e.get("kind") or "uses", "is_declared": bool(e["declared"])}
                 for (s, t), e in sorted(cedges.items())
                 if s in members and t in members]
        container_cycles.append({"members": comp, "edges": edges})

    payload = {"resolved": res, "found": bool(target_cycles or container_cycles),
               "target_cycles": target_cycles, "container_cycles": container_cycles}
    return _envelope(facts, "cycles", params, payload)


def conformance_of(facts: dict[str, Any], ident: str, to: str,
                   layering_rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """Is the (proposed or actual) edge *ident* → *to* allowed by the layering rules?

    Reuses :func:`drift_report.check_layering`'s rule matching + coverage-conditioned
    status, so an answer here can never disagree with ``arch verify`` (engine §2a/§6.1).
    ALLOWED with no rules configured is honestly flagged as unverified, never presented
    as a guarantee (§2.5 honest abstention).
    """
    params = {"id": ident, "to": to}
    res_s = resolve(facts, ident)
    if res_s["kind"] == "unknown":
        return _unknown(facts, "conformance", params, res_s, role="id")
    res_t = resolve(facts, to)
    if res_t["kind"] == "unknown":
        return _unknown(facts, "conformance", params, res_t, role="to")

    rules = layering_rules or {}
    forbidden = rules.get("forbidden", []) or []
    by_id = {t["id"]: t for t in facts.get("targets", []) or [] if t.get("id")}
    src_targets = [by_id[s] for s in res_s["targets"] if s in by_id]
    dst_targets = [by_id[t] for t in res_t["targets"] if t in by_id]

    findings = {(f["from"], f["to"]): f for f in check_layering(facts, rules)}
    matched = []
    for rule in forbidden:
        frm, to_tok = rule.get("from"), rule.get("to")
        if not frm or not to_tok:
            continue
        if (any(_matches_container(frm, t) for t in src_targets)
                and any(_matches_container(to_tok, t) for t in dst_targets)):
            finding = findings.get((frm, to_tok), {})
            matched.append({"from": frm, "to": to_tok,
                            "status": finding.get("status"),
                            "edges": finding.get("edges", []),
                            "require_layers": rule.get("require_layers", []) or []})

    _fwd, _rev, index = _graph(facts)
    existing = [cite_edge(index[(s, t)])
                for (s, t) in sorted(index)
                if s in set(res_s["targets"]) and t in set(res_t["targets"])]

    payload = {
        "resolved_source": res_s,
        "resolved_target": res_t,
        "verdict": "FORBIDDEN" if matched else "ALLOWED",
        "allowed": not matched,
        "matched_rules": matched,
        "existing_edges": existing,
        "rules_present": bool(forbidden),
    }
    if not forbidden:
        payload["note"] = ("no layering rules configured — ALLOWED is the absence of a "
                           "rule, not a verified guarantee (§2.5 honest abstention)")
    return _envelope(facts, "conformance", params, payload)


def explain_of(facts: dict[str, Any], ident: str, *, to: str | None = None,
               mapping_rules: MappingRules | None = None) -> dict[str, Any]:
    """Why is X here / why does this edge exist? Thin delegation to the shipped
    :mod:`anon.explain` (T2.6), wrapped in the query envelope."""
    if to is None and "->" in (ident or ""):
        ident, to = (s.strip() for s in ident.split("->", 1))
    params = {"id": ident, "to": to}
    res = resolve(facts, ident)
    if res["kind"] == "unknown":
        return _unknown(facts, "explain", params, res)
    if to is not None:
        res_t = resolve(facts, to)
        if res_t["kind"] == "unknown":
            return _unknown(facts, "explain", params, res_t, role="to")
        src_set, dst_set = set(res["targets"]), set(res_t["targets"])
        edges = [_edge_summary(r) for r in facts.get("relationships", []) or []
                 if r.get("source") in src_set and r.get("target") in dst_set]
        edges.sort(key=lambda e: (e["source"] or "", e["target"] or ""))
        payload = {"resolved_source": res, "resolved_target": res_t,
                   "found": bool(edges), "edges": edges}
        return _envelope(facts, "explain", params, payload)
    if res["kind"] == "container":
        members = []
        for tid in res["targets"]:
            expl = explain_target(facts, tid, mapping_rules)
            members.append({"id": tid, "placement_reason": expl["placement_reason"]
                            if expl else None})
        payload = {"resolved": res,
                   "container": {"id": res["id"], "name": res["name"], "members": members}}
        return _envelope(facts, "explain", params, payload)
    payload = {"resolved": res,
               "target": explain_target(facts, res["targets"][0], mapping_rules)}
    return _envelope(facts, "explain", params, payload)


# --------------------------------------------------------------------------- dispatch

def dispatch(facts: dict[str, Any], primitive: str, *, ident: str | None = None,
             to: str | None = None, transitive: bool = False,
             limit: int = DEFAULT_LIMIT,
             layering_rules: dict[str, Any] | None = None,
             mapping_rules: MappingRules | None = None) -> dict[str, Any]:
    """Route one primitive invocation; the single entry point `arch query`, `POST /ask`
    and the MCP tools all share, so the three surfaces can never disagree."""
    def _bad(message: str) -> dict[str, Any]:
        return {"schema": QUERY_SCHEMA, "primitive": primitive, "error": "bad_query",
                "message": message, "primitives": list(PRIMITIVES)}

    if primitive not in PRIMITIVES:
        return _bad(f"unknown primitive '{primitive}'")
    if not ident:
        return _bad(f"'{primitive}' needs an element id (--id)")
    if primitive in ("path", "conformance") and not to:
        return _bad(f"'{primitive}' needs a destination (--to)")

    if primitive == "dependents":
        return dependents(facts, ident, transitive=transitive, limit=limit)
    if primitive == "dependencies":
        return dependencies(facts, ident, transitive=transitive, limit=limit)
    if primitive == "blast-radius":
        return blast_radius(facts, ident, limit=limit)
    if primitive == "path":
        return paths_between(facts, ident, to)
    if primitive == "cycles":
        return cycles_of(facts, ident)
    if primitive == "conformance":
        return conformance_of(facts, ident, to, layering_rules)
    return explain_of(facts, ident, to=to, mapping_rules=mapping_rules)


# --------------------------------------------------------------------------- /ask router

def _route(q: str) -> str | None:
    """Deterministic keyword routing for a natural-language question. Documented, tested,
    no LLM: narration is a client concern and governed egress (GUI §5.6); routing here is
    plain string matching so the same question always reaches the same primitive."""
    s = q.lower()
    if "blast" in s or "impact" in s or ("if" in s and "change" in s):
        return "blast-radius"
    if "cycle" in s:
        return "cycles"
    if "allowed" in s or "forbidden" in s or "violat" in s or "conform" in s:
        return "conformance"
    if "path" in s or "reach" in s:
        return "path"
    if "why" in s or "explain" in s:
        return "explain"
    if "depend" in s:
        import re
        if re.search(r"(what|who|which\s+\w*)\s+depends|dependents|depended", s):
            return "dependents"
        return "dependencies"
    return None


def _extract_ids(facts: dict[str, Any], q: str) -> list[str]:
    """Deterministic mention-spotting: known ids/names found verbatim in *q*, returned in
    question order. Longest-first matching so `Catalog.Db` wins over `Catalog`; word
    boundaries so `Web` never matches inside `Webhook`."""
    mentions: set[str] = set()
    for t in facts.get("targets", []) or []:
        for key in ("id", "name", "container_id", "container_name"):
            v = t.get(key)
            if v:
                mentions.add(v)
    sl = q.lower()
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, str]] = []
    for mention in sorted(mentions, key=lambda m: (-len(m), m)):
        ml = mention.lower()
        idx = sl.find(ml)
        while idx != -1:
            end = idx + len(ml)
            boundary = ((idx == 0 or not sl[idx - 1].isalnum())
                        and (end == len(sl) or not sl[end].isalnum()))
            overlaps = any(a < end and idx < b for a, b in taken)
            if boundary and not overlaps:
                taken.append((idx, end))
                found.append((idx, mention))
            idx = sl.find(ml, idx + 1)
    out: list[str] = []
    for _pos, mention in sorted(found):
        if mention not in out:
            out.append(mention)
    return out


def ask(facts: dict[str, Any], *, q: str | None = None, primitive: str | None = None,
        ident: str | None = None, to: str | None = None, transitive: bool = False,
        limit: int = DEFAULT_LIMIT, layering_rules: dict[str, Any] | None = None,
        mapping_rules: MappingRules | None = None) -> dict[str, Any]:
    """The `POST /ask` entry point (GUI §7.6): explicit ``primitive``/``ident`` win; a
    natural-language *q* is routed by :func:`_route` and its element mentions extracted
    by :func:`_extract_ids`. Every fact in the answer comes from a primitive — the
    router only chooses which one (the iron rule, GUI §2.1)."""
    prim = primitive or (_route(q) if q else None)
    if prim is None:
        return {"schema": QUERY_SCHEMA, "error": "bad_query",
                "message": "could not route the question to a query primitive",
                "primitives": list(PRIMITIVES),
                "hint": "pass `primitive` explicitly, or phrase the question with "
                        "depends/path/cycle/allowed/blast/why"}
    ids = _extract_ids(facts, q) if q else []
    if ident is None and ids:
        ident = ids[0]
    if to is None and len(ids) > 1:
        to = ids[1]
    if ident is None:
        return {"schema": QUERY_SCHEMA, "primitive": prim, "error": "bad_query",
                "message": "no known element mentioned in the question",
                "hint": "name a target/container exactly as the model knows it, or "
                        "pass `id` explicitly"}
    result = dispatch(facts, prim, ident=ident, to=to, transitive=transitive,
                      limit=limit, layering_rules=layering_rules,
                      mapping_rules=mapping_rules)
    if q is not None and "error" not in result:
        result["routed"] = {"q": q, "primitive": prim, "mentions": ids}
    return result


# --------------------------------------------------------------------------- derived views

VIEW_SCHEMA = "derived-view/1"


def _collect_target_ids(obj: Any, known: set[str]) -> set[str]:
    """Every known target id mentioned anywhere in a query result — the deterministic,
    primitive-agnostic scoping rule for derived views (§5.6): the view shows exactly the
    elements the cited answer talks about, nothing invented."""
    found: set[str] = set()
    if isinstance(obj, str):
        if obj in known:
            found.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= _collect_target_ids(v, known)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= _collect_target_ids(v, known)
    return found


def _slugify(text: str) -> str:
    import re
    return re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]+", "-", text)).strip("-").lower()


def materialize_view(ws: Workspace, facts: dict[str, Any], result: dict[str, Any],
                     slug: str | None = None) -> dict[str, Any]:
    """Materialize a query answer as a **derived view** (engine §6.4/§6.6, GUI §5.6):
    the scoped sub-graph → ``generated/views/derived/<slug>.json`` (producing query
    recorded as provenance + ``derived_from_hash``) plus its layout twin from the §6.9
    layout stage (auto-layout only — ``layout-overrides.yaml`` never applies to
    ephemeral derived views).

    This is the **single view-derivation path** shared by the GUI (``POST
    /views/derive``), external agents (``arch query --emit-view`` / the MCP tool), and
    CI — the artifact is the contract, so it does not matter who asked. Derived views
    are advisory and machine-owned: regenerate-on-demand (same query → same slug →
    overwritten), promoted to a curated view only via a hand-written
    ``view-intents.yaml`` entry.
    """
    if result.get("error"):
        raise ValueError("cannot materialize an error answer as a view "
                         f"({result['error']})")
    from .layout import layout_graph, view_slug
    key = dumps_json({"primitive": result.get("primitive"),
                      "params": result.get("params")})
    default = f"{result.get('primitive', 'query')}-" \
              f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"
    slug = _slugify(slug) if slug else default
    if not slug:
        slug = default

    by_id = {t["id"]: t for t in facts.get("targets", []) or [] if t.get("id")}
    ids = sorted(_collect_target_ids(result, set(by_id)))
    nodes = [{"id": tid, "label": by_id[tid].get("name", tid),
              "container_id": by_id[tid].get("container_id") or tid}
             for tid in ids]
    id_set = set(ids)
    edges = [cite_edge(r) for r in facts.get("relationships", []) or []
             if r.get("source") in id_set and r.get("target") in id_set]
    edges.sort(key=lambda e: (e["source"], e["target"]))

    h = result.get("derived_from_hash") or content_hash(facts)
    view_key = f"derived:{slug}"
    artifact = {
        "schema": VIEW_SCHEMA,
        "slug": slug,
        "view": view_key,
        "provenance": {k: result[k] for k in ("primitive", "params", "routed")
                       if k in result},
        "derived_from_hash": h,
        "coverage": result.get("coverage") or coverage_caveat(facts),
        "nodes": nodes,
        "edges": edges,
    }
    view_path = ws.derived_views_dir / f"{slug}.json"
    dump_json(artifact, view_path)

    laid = layout_graph([{"id": n["id"], "label": n["label"]} for n in nodes],
                        [{"source": e["source"], "target": e["target"]} for e in edges],
                        seed=h, pins={})
    layout_artifact = {
        "schema": "layout/1", "view": view_key, "engine": "python-layered/1",
        "derived_from_hash": h, "seed": h, "pinned": [], "stale_pins": [],
        "metrics": laid["metrics"], "nodes": laid["nodes"], "edges": laid["edges"],
    }
    layout_path = ws.layout_dir / f"{view_slug(view_key)}.json"
    dump_json(layout_artifact, layout_path)
    return {"slug": slug, "view_path": view_path, "layout_path": layout_path,
            "artifact": artifact, "layout": layout_artifact}


# --------------------------------------------------------------------------- CLI surface

def write_result(ws: Workspace, result: dict[str, Any]) -> Any:
    """Persist a query answer under ``generated/queries/<hash>.json`` (engine §6.6) so an
    answer is reproducible and reviewable. The name is a pure function of the query +
    the model hash, so the same question over the same model lands in the same file."""
    key = dumps_json({"primitive": result.get("primitive"),
                      "params": result.get("params"),
                      "derived_from_hash": result.get("derived_from_hash")})
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    path = ws.queries_dir / f"{name}.json"
    dump_json(result, path)
    return path


def run(ws: Workspace, primitive: str, *, ident: str | None = None,
        to: str | None = None, transitive: bool = False, limit: int = DEFAULT_LIMIT,
        out: bool = False, emit_view: bool = False,
        slug: str | None = None) -> dict[str, Any] | None:
    """The ``arch query`` entry point: load facts + rules from the workspace, dispatch,
    optionally persist the cited answer (``--out``) and/or materialize it as a derived
    view + layout twin (``--emit-view``, §5.6). Returns ``None`` when no fact model
    exists."""
    facts = load_facts(ws)
    if facts is None:
        return None
    result = dispatch(facts, primitive, ident=ident, to=to, transitive=transitive,
                      limit=limit,
                      layering_rules=load_yaml(ws.layering_rules),
                      mapping_rules=load_mapping_rules(ws.mapping_rules))
    if out and "error" not in result:
        result["written_to"] = str(write_result(ws, result))
    if emit_view and "error" not in result:
        info = materialize_view(ws, facts, result, slug=slug)
        result["view"] = {"slug": info["slug"],
                          "view_path": str(info["view_path"]),
                          "layout_path": str(info["layout_path"])}
    return result


# --------------------------------------------------------------------------- render

def _render_header(result: dict[str, Any]) -> list[str]:
    params = " ".join(f"{k}={v}" for k, v in (result.get("params") or {}).items())
    lines = [f"# arch query {result.get('primitive')}  ({params})", ""]
    h = result.get("derived_from_hash", "")
    cov = result.get("coverage", {})
    lines.append(f"derived_from {h[:12]}…  ·  L0 coverage {cov.get('l0_coverage', 0):.0%}")
    if cov.get("advisory"):
        lines.append(f"> ⚠ {cov.get('note')}")
    lines.append("")
    return lines


def render(result: dict[str, Any]) -> str:
    """Human-readable rendering of a query answer (the `--format text` CLI surface)."""
    if result.get("error"):
        lines = [f"✗ {result['message']}"]
        if result.get("candidates"):
            lines.append("did you mean:")
            lines += [f"  - {c}" for c in result["candidates"]]
        if result.get("hint"):
            lines.append(f"hint: {result['hint']}")
        return "\n".join(lines) + "\n"

    lines = _render_header(result)
    prim = result.get("primitive")
    if prim in ("dependents", "dependencies"):
        rs = result.get("results", [])
        word = "depend on it" if prim == "dependents" else "it depends on"
        lines.append(f"{result.get('total', 0)} element(s) {word} "
                     f"(containers: {', '.join(result.get('containers', [])) or '—'})")
        for r in rs:
            if result.get("transitive"):
                lines.append(f"- `{r['id']}` ({r['container_name']}) — depth {r['depth']}: "
                             f"{' → '.join(r['path'])}")
            else:
                strengths = "; ".join(e["evidence_strength"] for e in r.get("edges", []))
                lines.append(f"- `{r['id']}` ({r['container_name']}) — weight {r['weight']}: "
                             f"{strengths}")
        if result.get("truncated"):
            lines.append(f"… truncated at {len(rs)} (total {result['total']})")
    elif prim == "blast-radius":
        for key, label in (("impacted", "Impacted (depend on it)"),
                           ("reachable", "Reachable (it depends on)")):
            blk = result.get(key, {})
            lines.append(f"## {label}: {blk.get('total', 0)} element(s) in "
                         f"{len(blk.get('containers', []))} container(s)")
            for r in blk.get("elements", []):
                lines.append(f"- `{r['id']}` ({r['container_name']}) — depth {r['depth']}")
            lines.append("")
    elif prim == "path":
        if not result.get("found"):
            lines.append("no path found" + (" — " + result.get("note", "") if result.get("note") else ""))
        for p in result.get("paths", []):
            lines.append(f"- {' → '.join(p['elements'])}")
            for e in p.get("edges", []):
                lines.append(f"    · {e['source']} → {e['target']}: {e['evidence_strength']} "
                             f"(weight {e['weight']}, {e['confidence']})")
    elif prim == "cycles":
        if not result.get("found"):
            lines.append("no cycles involve this element ✓")
        for label, key in (("target-level", "target_cycles"), ("container-level", "container_cycles")):
            for c in result.get(key, []):
                lines.append(f"- {label}: {' ⇄ '.join(c['members'])}")
    elif prim == "conformance":
        lines.append(f"verdict: **{result.get('verdict')}**")
        for m in result.get("matched_rules", []):
            lines.append(f"- forbidden rule `{m['from']}` -/-> `{m['to']}` "
                         f"(current status: {m.get('status')})")
        if result.get("existing_edges"):
            lines.append("existing edges:")
            for e in result["existing_edges"]:
                lines.append(f"  - {e['source']} → {e['target']}: {e['evidence_strength']}")
        if result.get("note"):
            lines.append(f"> {result['note']}")
    else:  # explain — structured payload; keep it JSON-faithful
        body = {k: v for k, v in result.items()
                if k not in ("schema", "primitive", "params", "derived_from_hash", "coverage")}
        lines.append(dumps_json(body).rstrip())
    return "\n".join(lines).rstrip() + "\n"
