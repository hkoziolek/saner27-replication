"""Engine §3 — ``arch risk``: the structural MAINTAINABILITY-RISK report.

A single ranked, cited report of the **structural** maintainability risks in the system —
the mechanical risk inventory an architect would otherwise assemble by hand (engine plan
``plans/2026-06-07-anon-architecture-intelligence-features.md`` §3). Detectors:

  * **cycles** — directed SCCs (Tarjan, below) over the lifted container graph. L0.
  * **hubs / god-modules** — extreme fan-in/out (degree over the lifted edges). L0.
  * **layering violations** — reuses :func:`drift_report.check_layering`, never a private
    re-check. Available when ``layering-rules.yaml`` exists.
  * **Martin instability/abstractness** ``I = Ce/(Ca+Ce)``, ``A`` *approximated* from the
    share of members exposing public L2 surface, ``D = |A + I − 1|`` — honestly degraded
    to I-only when no visibility-bearing L2 evidence exists.
  * **churn × coupling hotspots** — CONDITIONAL on git history (§0.6). Directory-granular
    (`git log --name-only` against member target dirs), windowed
    :data:`CHURN_WINDOW_DAYS` days **before the HEAD commit's date** (never wall-clock,
    so the report is a pure function of the repo state).
  * **interop seams** — ``interop`` evidence (P/Invoke / COM / C++-CLI) as fragile
    cross-language coupling.
  * **deployment SPOFs** — CONDITIONAL on the deployment facet: one ``infra:*`` node many
    containers route through.
  * **coverage gaps** — containers below :data:`drift_report.COVERAGE_FLOOR`, where the
    *absence* of findings is itself the caveat (honest abstention, moat #5).

**Severity is a transparent, documented heuristic — recomputable from the cited inputs**
(engine §3.3): every finding carries ``severity_inputs`` (the Ca/Ce counts, weights,
churn commit count, …) and the formulas live in one place, :func:`_severity_formulas`,
which the report header echoes. Detectors whose optional input is absent emit an explicit
``not_evaluated`` entry — "couldn't check" is never rendered as "none found" (§3.1).

**Explicitly NOT ATAM** (§0.5): the header states this provides *cited structural inputs
an architecture risk review may consult*, not a tradeoff analysis.

Determinism: pure function of the curated facts (+ the repo's committed git state for
churn); everything sorted; advisory artifacts only (`generated/risk-report.{md,json}`),
never the hashed extracted core (§9). The Tarjan SCC pass below is also the engine the
§6 ``cycles`` query primitive uses (:func:`anon.query.cycles_of`). Pure Python in
the no-networkx style of ``propose.connected_components``, sharing no code with it.
"""
from __future__ import annotations

import argparse
import datetime
import subprocess
from typing import Any, Iterable

from .. import confidence, scorecard
from ..config import load_yaml
from ..jsonio import dump_json, load_json
from ..model import content_hash
from ..paths import Workspace, resolve_workspace
from .drift_report import (COVERAGE_FLOOR, _coverage, _coverage_l0,
                           _layer_covered, check_layering)
from .generate_structurizr import build_container_edges, build_containers

RISK_SCHEMA = "risk/1"

# --- detector thresholds (documented knobs, pinned here for reproducibility) ---------
HUB_MIN_NEIGHBORS = 4        # a hub must touch at least this many distinct containers
HUB_MIN_SEVERITY = 0.33      # …and at least a third of the graph
D_FLAG = 0.6                 # |A + I − 1| at/above this is a main-sequence finding
CHURN_WINDOW_DAYS = 90       # window BEFORE the HEAD commit date (never wall-clock)
CHURN_MIN_COMMITS = 2
CHURN_MIN_SEVERITY = 0.3
SPOF_MIN_CONTAINERS = 3      # an infra node this many containers route through

NOT_ATAM = ("This report provides CITED STRUCTURAL INPUTS an architecture risk review "
            "may consult. It is NOT an ATAM or tradeoff analysis: it cannot see business "
            "criticality, runtime hotness, or quality-attribute scenarios (engine §0.5).")
OVERFIXATION = ("This is a risk INVENTORY, not a verdict list — a high-severity-but-"
                "intentional hub is a known failure mode of over-fixating on it (§3.7).")


# ===================================================================== graph algorithms
# The §3 metrics core. Also consumed by the §6 `cycles` query primitive.

def strongly_connected_components(nodes: Iterable[str],
                                  adjacency: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's SCC over a directed graph — iterative, so deep graphs can't blow the
    recursion limit.

    *adjacency* maps node -> successor list; successors outside *nodes* are ignored.
    Returns each SCC as a sorted member list, the list sorted by first member.
    """
    order = sorted(set(nodes))
    node_set = set(order)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0

    for root in order:
        if root in index:
            continue
        # explicit work stack of (node, position into its sorted successor list)
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, i = work[-1]
            if i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            succs = [s for s in sorted(set(adjacency.get(node, []) or [])) if s in node_set]
            descended = False
            while i < len(succs):
                s = succs[i]
                i += 1
                if s not in index:
                    work[-1] = (node, i)
                    work.append((s, 0))
                    descended = True
                    break
                if s in on_stack:
                    low[node] = min(low[node], index[s])
            if descended:
                continue
            work.pop()
            if low[node] == index[node]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                sccs.append(sorted(comp))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return sorted(sccs, key=lambda c: c[0])


def cycles(nodes: Iterable[str], adjacency: dict[str, list[str]]) -> list[list[str]]:
    """Directed cycles: SCCs of size ≥ 2, plus single nodes with a self-edge.

    Sorted and deterministic like :func:`strongly_connected_components`.
    """
    out: list[list[str]] = []
    for comp in strongly_connected_components(nodes, adjacency):
        if len(comp) > 1 or comp[0] in (adjacency.get(comp[0], []) or []):
            out.append(comp)
    return out


# ===================================================================== shared lift bits

def _lift(facts: dict[str, Any]):
    """The two-step container lift every detector shares (one lift, one truth)."""
    containers = build_containers(facts)
    t2c = {m: c.cid for c in containers.values() for m in c.members}
    cedges = build_container_edges(facts, t2c)
    return containers, t2c, cedges


def _internal(containers) -> list[str]:
    """First-party container ids — external/person boundaries are not refactoring
    targets, so detectors flag findings on internal containers only."""
    return sorted(cid for cid, c in containers.items()
                  if not ({"external", "person"} & c.tags))


def _degree(cedges, internal: set[str]):
    """Per-container neighbor sets + weighted degrees over the lifted edges."""
    in_n: dict[str, set[str]] = {}
    out_n: dict[str, set[str]] = {}
    w_in: dict[str, int] = {}
    w_out: dict[str, int] = {}
    for (s, t), e in cedges.items():
        out_n.setdefault(s, set()).add(t)
        in_n.setdefault(t, set()).add(s)
        w_out[s] = w_out.get(s, 0) + e["weight"]
        w_in[t] = w_in.get(t, 0) + e["weight"]
    return in_n, out_n, w_in, w_out


def _cedge_evidence(cedges, members: set[str]) -> list[dict[str, Any]]:
    """Cited evidence rows for the lifted edges inside *members* (sorted)."""
    return [{"type": "container_edge",
             "detail": f"{s} -> {t} (weight {e['weight']}, "
                       f"{e.get('kind') or 'uses'}{', declared' if e['declared'] else ''})"}
            for (s, t), e in sorted(cedges.items())
            if s in members and t in members and s != t]


def _finding(kind: str, severity: float, message: str, elements: list[str],
             evidence: list[dict[str, Any]], severity_inputs: dict[str, Any],
             conf: float | None, **extra: Any) -> dict[str, Any]:
    sev = round(severity, 2)
    f = {"kind": kind, "severity": sev, "message": message,
         "elements": sorted(elements), "evidence": evidence,
         "severity_inputs": severity_inputs,
         "severity_calc": _severity_calc(kind, sev, severity_inputs),
         "confidence": conf}
    f.update(extra)
    return f


def _severity_calc(kind: str, severity: float, si: dict[str, Any]) -> str | None:
    """The §3.3 formula with this finding's concrete inputs substituted, ending in the
    emitted score — the GUI popover and the report show the actual arithmetic, not just
    the recipe. Purely presentational: *severity* is the already-computed score, so even
    if this registry ever drifted from a detector, the trailing value stays true."""
    try:
        if kind == "cycle":
            return (f"min(1, 0.6 + 0.05·({len(si['members'])}−2) + "
                    f"0.005·{si['total_weight']}) = {severity:.2f}")
        if kind == "hub":
            return f"min(1, {si['neighbors']}/({si['n_containers']}−1)) = {severity:.2f}"
        if kind == "layering-violation":
            return f"0.9 fixed (an explicitly forbidden edge exists) = {severity:.2f}"
        if kind == "main-sequence":
            return (f"|{si['abstractness_A']} + {si['instability_I']} − 1| "
                    f"= {severity:.2f}")
        if kind == "churn-coupling":
            return (f"min(1, ({si['churn_commits']}/{si['max_churn']}) · "
                    f"({si['coupling_weight']}/{si['max_coupling_weight']})) "
                    f"= {severity:.2f}")
        if kind == "interop-seam":
            return f"min(0.7, 0.5 + 0.05·{si['edges']}) = {severity:.2f}"
        if kind == "deployment-spof":
            return (f"min(1, {si['containers_routed']}/{si['n_internal_containers']}) "
                    f"= {severity:.2f}")
        if kind == "coverage-gap":
            return f"1 − {si['covered_share']} = {severity:.2f}"
    except KeyError:
        pass
    return None


def _severity_formulas() -> dict[str, str]:
    """The documented severity heuristics, echoed into the report header so every score
    is recomputable from its ``severity_inputs`` by hand (engine §3.3)."""
    return {
        "cycle": "min(1, 0.6 + 0.05·(members−2) + 0.005·total_weight)",
        "hub": "distinct_neighbors / (n_containers − 1)",
        "layering-violation": "0.9 (an explicitly forbidden edge exists)",
        "main-sequence": "D = |A + I − 1| (A approximated from public L2 surface)",
        "churn-coupling": "(churn/max_churn) · (coupling_weight/max_coupling_weight)",
        "interop-seam": "min(0.7, 0.5 + 0.05·edges)",
        "deployment-spof": "containers_routed / n_internal_containers",
        "coverage-gap": "1 − covered_share (always advisory)",
    }


# ===================================================================== detectors

def _detect_cycles(containers, cedges, conf_c) -> list[dict[str, Any]]:
    cadj: dict[str, list[str]] = {}
    for (s, t) in cedges:
        cadj.setdefault(s, []).append(t)
    out = []
    for comp in cycles(containers.keys(), cadj):
        members = set(comp)
        total_w = sum(e["weight"] for (s, t), e in cedges.items()
                      if s in members and t in members)
        sev = min(1.0, 0.6 + 0.05 * (len(comp) - 2) + 0.005 * total_w)
        names = " ⇄ ".join(containers[c].name for c in comp)
        out.append(_finding(
            "cycle", sev,
            f"Containers {names} form a dependency cycle — changes propagate around "
            "the loop and the members cannot be built, tested, or reasoned about in "
            "isolation.",
            comp, _cedge_evidence(cedges, members),
            {"members": comp, "edges": sum(1 for (s, t) in cedges
                                           if s in members and t in members and s != t),
             "total_weight": total_w},
            min((conf_c.get(c) for c in comp if conf_c.get(c) is not None),
                default=None)))
    return out


def _detect_hubs(containers, cedges, internal: list[str], conf_c) -> list[dict[str, Any]]:
    in_n, out_n, w_in, w_out = _degree(cedges, set(internal))
    n = len(containers)
    out = []
    for cid in internal:
        ca = len(in_n.get(cid, set()))
        ce = len(out_n.get(cid, set()))
        neighbors = len(in_n.get(cid, set()) | out_n.get(cid, set()))
        if neighbors < HUB_MIN_NEIGHBORS or n < 3:
            continue
        sev = min(1.0, neighbors / max(1, n - 1))
        if sev < HUB_MIN_SEVERITY:
            continue
        instability = round(ce / (ca + ce), 2) if (ca + ce) else None
        name = containers[cid].name
        ev = [{"type": "container_edge",
               "detail": f"{s} -> {cid} (weight {cedges[(s, cid)]['weight']})"}
              for s in sorted(in_n.get(cid, set()))]
        ev += [{"type": "container_edge",
                "detail": f"{cid} -> {t} (weight {cedges[(cid, t)]['weight']})"}
               for t in sorted(out_n.get(cid, set()))]
        out.append(_finding(
            "hub", sev,
            f"`{name}` is depended on by {ca} container(s) and depends on {ce} — "
            "changes here ripple widely and it is hard to isolate.",
            [cid], ev,
            {"Ca": ca, "Ce": ce, "neighbors": neighbors, "n_containers": n,
             "weighted_in": w_in.get(cid, 0), "weighted_out": w_out.get(cid, 0),
             "instability_I": instability},
            conf_c.get(cid)))
    return out


def _detect_layering(facts, layering_rules, conf_c, t2c) -> list[dict[str, Any]] | None:
    """Returns findings, or None when there are no rules to check (not_evaluated)."""
    forbidden = (layering_rules or {}).get("forbidden") or []
    if not forbidden:
        return None
    out = []
    for f in check_layering(facts, layering_rules):
        if f["status"] != "VIOLATION":
            continue
        ev = [{"type": "relationship", "detail": f"{s} -> {t}"} for s, t in f["edges"]]
        cids = sorted({t2c.get(s) for s, _ in f["edges"] if t2c.get(s)}
                      | {t2c.get(t) for _, t in f["edges"] if t2c.get(t)})
        out.append(_finding(
            "layering-violation", 0.9,
            f"Forbidden dependency `{f['from']}` -> `{f['to']}` exists in the model — "
            "an explicitly disallowed edge (layering-rules.yaml).",
            cids or [f["from"], f["to"]], ev,
            {"rule_from": f["from"], "rule_to": f["to"], "edges": len(f["edges"])},
            min((conf_c.get(c) for c in cids if conf_c.get(c) is not None),
                default=None)))
    return out


def _detect_main_sequence(facts, containers, cedges, internal: list[str],
                          conf_c) -> tuple[list[dict[str, Any]], str | None]:
    """Martin I/A/D. Returns (findings, degraded_reason). A is approximated from the
    share of members exposing public L2 surface; absent visibility-bearing L2 evidence
    the detector degrades to I-only and emits no D findings (engine §3.1)."""
    has_visibility_l2 = any(
        e.get("type") in ("symbol_use", "include") and e.get("visibility") is not None
        for r in facts.get("relationships", []) or []
        for e in r.get("evidence", []) or [])
    in_n, out_n, _w_in, _w_out = _degree(cedges, set(internal))
    if not has_visibility_l2:
        return [], "L2 visibility evidence absent — A unavailable, I reported only"
    # members exposing public surface: the TARGET of an edge owns the symbol being used.
    public_members: set[str] = set()
    for r in facts.get("relationships", []) or []:
        for e in r.get("evidence", []) or []:
            if e.get("type") in ("symbol_use", "include") and e.get("visibility") == "public":
                if r.get("target"):
                    public_members.add(r["target"])
    out = []
    for cid in internal:
        ca = len(in_n.get(cid, set()))
        ce = len(out_n.get(cid, set()))
        if ca + ce < 2:
            continue
        instability = ce / (ca + ce)
        members = containers[cid].members
        abstractness = (sum(1 for m in members if m in public_members) / len(members)) \
            if members else 0.0
        d = abs(abstractness + instability - 1.0)
        if d < D_FLAG:
            continue
        zone = ("concrete and heavily depended on (zone of pain) — change is painful"
                if instability < 0.5 else
                "abstract/exposed but little depended on (zone of uselessness)")
        name = containers[cid].name
        out.append(_finding(
            "main-sequence", d,
            f"`{name}` is far from the main sequence (D={d:.2f}): {zone}. "
            "A is an APPROXIMATION from public L2 surface share, not parsed "
            "abstractness.",
            [cid], [],
            {"Ca": ca, "Ce": ce, "instability_I": round(instability, 2),
             "abstractness_A": round(abstractness, 2),
             "members_public": sum(1 for m in members if m in public_members),
             "members": len(members), "approximated_A": True},
            conf_c.get(cid)))
    return out, None


def _git(repo, *args: str, timeout: int = 60) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                             text=True, timeout=timeout, encoding="utf-8",
                             errors="replace")
        if out.returncode == 0:
            return out.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _detect_churn(ws: Workspace, facts, containers, cedges,
                  internal: list[str], conf_c) -> tuple[list[dict[str, Any]], str | None]:
    """Churn × coupling, CONDITIONAL on committed git history (engine §0.6). The window
    is anchored to the HEAD commit's date — never wall-clock — so the same repo state
    always yields the same report."""
    inside = _git(ws.repo, "rev-parse", "--is-inside-work-tree")
    if not inside or inside.strip() != "true":
        return [], "not a git repository"
    shallow = _git(ws.repo, "rev-parse", "--is-shallow-repository")
    if shallow and shallow.strip() == "true":
        return [], "shallow clone — history truncated, churn would under-count"
    head_date = _git(ws.repo, "log", "-1", "--format=%cI")
    if not head_date or not head_date.strip():
        return [], "no commits"
    head = datetime.datetime.fromisoformat(head_date.strip())
    since = (head - datetime.timedelta(days=CHURN_WINDOW_DAYS)).isoformat()
    log = _git(ws.repo, "log", f"--since={since}", "--name-only", "--format=%H",
               timeout=300)
    if log is None:
        return [], "git log failed"

    # git log paths are GIT-ROOT-relative; member paths are repo-relative — prepend the
    # prefix when the analyzed repo is a subdirectory of a larger git work tree.
    prefix = (_git(ws.repo, "rev-parse", "--show-prefix") or "").strip()
    by_id = {t["id"]: t for t in facts.get("targets", []) or [] if t.get("id")}
    dir_to_cid: list[tuple[str, str]] = []
    for cid in internal:
        for tid in containers[cid].members:
            path = (by_id.get(tid, {}).get("path") or "").replace("\\", "/")
            d = path.rsplit("/", 1)[0] if "/" in path else ""
            if d:
                dir_to_cid.append((prefix + d.rstrip("/") + "/", cid))
    dir_to_cid.sort(key=lambda x: (-len(x[0]), x[0]))  # longest prefix wins

    churn: dict[str, int] = {}
    touched: set[str] = set()
    for line in log.splitlines() + [""]:
        line = line.strip()
        if not line:  # commit boundary
            for cid in touched:
                churn[cid] = churn.get(cid, 0) + 1
            touched = set()
            continue
        if len(line) == 40 and all(ch in "0123456789abcdef" for ch in line):
            continue  # the %H hash line
        f = line.replace("\\", "/")
        for prefix, cid in dir_to_cid:
            if f.startswith(prefix):
                touched.add(cid)
                break

    _in_n, _out_n, w_in, w_out = _degree(cedges, set(internal))
    coupling = {cid: w_in.get(cid, 0) + w_out.get(cid, 0) for cid in internal}
    max_churn = max(churn.values(), default=0)
    max_coupling = max((coupling[c] for c in internal), default=0)
    out = []
    if max_churn and max_coupling:
        for cid in internal:
            ch = churn.get(cid, 0)
            if ch < CHURN_MIN_COMMITS:
                continue
            sev = min(1.0, (ch / max_churn) * (coupling[cid] / max_coupling))
            if sev < CHURN_MIN_SEVERITY:
                continue
            name = containers[cid].name
            out.append(_finding(
                "churn-coupling", sev,
                f"`{name}` changed in {ch} commit(s) in the {CHURN_WINDOW_DAYS} days "
                f"before HEAD AND carries coupling weight {coupling[cid]} — it changes "
                "often and everyone depends on it. Churn is DIRECTORY-granular and can "
                "over-count shared folders (§3.7).",
                [cid], [],
                {"churn_commits": ch, "window_days": CHURN_WINDOW_DAYS,
                 "window_anchor": head_date.strip(),
                 "coupling_weight": coupling[cid], "max_churn": max_churn,
                 "max_coupling_weight": max_coupling, "granularity": "directory"},
                conf_c.get(cid)))
    return out, None


def _detect_interop_seams(facts, t2c, containers, conf_c) -> list[dict[str, Any]]:
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in facts.get("relationships", []) or []:
        if not any(e.get("type") == "interop" for e in r.get("evidence", []) or []):
            continue
        cs = t2c.get(r.get("source"))
        ct = t2c.get(r.get("target"))
        if not cs or not ct or cs == ct:
            continue
        pairs.setdefault((cs, ct), []).append(r)
    out = []
    for (cs, ct), rels in sorted(pairs.items()):
        sev = min(0.7, 0.5 + 0.05 * len(rels))
        ev = [{"type": "interop",
               "detail": f"{r['source']} -> {r['target']} ("
                         + (r.get("evidence_strength") or "interop") + ")"}
              for r in sorted(rels, key=lambda r: (r["source"], r["target"]))]
        sname = containers[cs].name if cs in containers else cs
        tname = containers[ct].name if ct in containers else ct
        out.append(_finding(
            "interop-seam", sev,
            f"Cross-language seam between `{sname}` and `{tname}` (P/Invoke / COM / "
            "C++-CLI) — ABI and marshalling changes here break at runtime, not at "
            "compile time.",
            [cs, ct], ev,
            {"edges": len(rels),
             "total_weight": sum(r.get("weight") or 0 for r in rels)},
            min((conf_c.get(c) for c in (cs, ct) if conf_c.get(c) is not None),
                default=None)))
    return out


def _detect_spofs(facts, t2c, n_internal: int) -> tuple[list[dict[str, Any]], str | None]:
    dep = facts.get("deployment") or {}
    if not dep.get("nodes"):
        return [], "no deployment facet (no manifests found / extractor skipped)"
    edges = dep.get("edges") or []
    image_to_cids: dict[str, set[str]] = {}
    for e in edges:
        if e.get("kind") == "packages" and e.get("target") in t2c:
            image_to_cids.setdefault(e["source"], set()).add(t2c[e["target"]])
    users: dict[str, set[str]] = {}
    cited: dict[str, list[str]] = {}
    for e in edges:
        tgt = e.get("target") or ""
        if not tgt.startswith("infra:"):
            continue
        src = e.get("source") or ""
        cids = ({t2c[src]} if src in t2c else image_to_cids.get(src, set()))
        if cids:
            users.setdefault(tgt, set()).update(cids)
            cited.setdefault(tgt, []).append(f"{src} -[{e.get('kind')}]-> {tgt}")
    out = []
    for node, cids in sorted(users.items()):
        if len(cids) < SPOF_MIN_CONTAINERS:
            continue
        sev = min(1.0, len(cids) / max(1, n_internal))
        out.append(_finding(
            "deployment-spof", sev,
            f"Infrastructure node `{node}` is routed through by {len(cids)} "
            "container(s) — a single point of failure / shared-fate coupling.",
            sorted(cids) + [node],
            [{"type": "deployment_edge", "detail": d} for d in sorted(cited[node])],
            {"infra_node": node, "containers_routed": len(cids),
             "n_internal_containers": n_internal},
            None))
    return out, None


def _detect_coverage_gaps(facts, containers, internal: list[str]) -> list[dict[str, Any]]:
    coverage = _coverage(facts)
    by_id = {t["id"]: t for t in facts.get("targets", []) or [] if t.get("id")}
    out = []
    for cid in internal:
        members = [m for m in containers[cid].members
                   if not by_id.get(m, {}).get("external")]
        if not members:
            continue
        covered = sum(1 for m in members if _layer_covered(coverage, m, "L0"))
        share = covered / len(members)
        if share >= COVERAGE_FLOOR:
            continue
        name = containers[cid].name
        out.append(_finding(
            "coverage-gap", 1.0 - share,
            f"`{name}` has L0 coverage {share:.0%} (floor {COVERAGE_FLOOR:.0%}) — the "
            "ABSENCE of risk findings in this region is itself a caveat, not a clean "
            "bill of health (honest abstention, moat #5).",
            [cid], [],
            {"covered_members": covered, "members": len(members),
             "covered_share": round(share, 2), "floor": COVERAGE_FLOOR},
            None, advisory=True))
    return out


# ===================================================================== report assembly

def build_report(ws: Workspace, facts: dict[str, Any], *,
                 top: int | None = None, with_scenarios: bool = False) -> dict[str, Any]:
    """Compute the full §3 risk report over *facts* — pure (no writes).

    *with_scenarios* attaches a §7.1 what-if PROPOSAL (cut the weakest intra-cycle
    edge / extract the hub's most-depended-on member) to each cycle/hub finding —
    computed by :mod:`anon.stages.whatif`, never applied (engine §3.4)."""
    containers, t2c, cedges = _lift(facts)
    internal = _internal(containers)
    conf_c = confidence.container_confidence(facts)
    layering_rules = load_yaml(ws.layering_rules)

    findings: list[dict[str, Any]] = []
    ran: list[str] = []
    degraded: list[dict[str, str]] = []
    not_evaluated: list[dict[str, str]] = []

    findings += _detect_cycles(containers, cedges, conf_c)
    ran.append("cycles")
    findings += _detect_hubs(containers, cedges, internal, conf_c)
    ran.append("hubs")

    layering_findings = _detect_layering(facts, layering_rules, conf_c, t2c)
    if layering_findings is None:
        not_evaluated.append({"detector": "layering",
                              "reason": "no layering-rules.yaml — nothing to check"})
    else:
        findings += layering_findings
        ran.append("layering")

    ms_findings, ms_degraded = _detect_main_sequence(facts, containers, cedges,
                                                     internal, conf_c)
    findings += ms_findings
    if ms_degraded:
        degraded.append({"detector": "main-sequence", "reason": ms_degraded})
    else:
        ran.append("main-sequence")

    churn_findings, churn_reason = _detect_churn(ws, facts, containers, cedges,
                                                 internal, conf_c)
    if churn_reason:
        not_evaluated.append({"detector": "churn-coupling", "reason": churn_reason})
    else:
        findings += churn_findings
        ran.append("churn-coupling")

    findings += _detect_interop_seams(facts, t2c, containers, conf_c)
    ran.append("interop-seams")

    spof_findings, spof_reason = _detect_spofs(facts, t2c, len(internal))
    if spof_reason:
        not_evaluated.append({"detector": "deployment-spof", "reason": spof_reason})
    else:
        findings += spof_findings
        ran.append("deployment-spof")

    findings += _detect_coverage_gaps(facts, containers, internal)
    ran.append("coverage-gaps")

    coverage_l0 = _coverage_l0(facts)
    global_advisory = coverage_l0 < COVERAGE_FLOOR
    if global_advisory:
        # below the floor the WHOLE report is advisory — never confidently red (§2.5).
        for f in findings:
            f["advisory"] = True

    if with_scenarios:
        # late import — whatif reuses this module's lift/cycle helpers (engine §7.1).
        from .whatif import attach_scenarios
        attach_scenarios(ws, facts, findings)

    findings.sort(key=lambda f: (-f["severity"], f["kind"], f["elements"]))
    total = len(findings)
    if top is not None:
        findings = findings[:top]

    card = scorecard.build(facts)
    return {
        "schema": RISK_SCHEMA,
        "derived_from_hash": content_hash(facts),
        "not_atam": NOT_ATAM,
        "warning": OVERFIXATION,
        "severity_formulas": _severity_formulas(),
        "detectors": {"ran": sorted(ran), "degraded": degraded,
                      "not_evaluated": not_evaluated},
        "coverage": {"l0_coverage": round(coverage_l0, 4), "floor": COVERAGE_FLOOR,
                     "advisory": global_advisory,
                     "mean_confidence": card["mean_confidence"],
                     "abstained": card["abstained"],
                     "strata": card["strata"]},
        "containers": len(containers),
        "internal_containers": len(internal),
        "total_findings": total,
        "findings": findings,
    }


def render(report: dict[str, Any]) -> str:
    """Markdown render — the §3.6 specimen shape with the honest header."""
    det = report["detectors"]
    lines = ["# Structural maintainability-risk report  (`arch risk`)", "",
             f"> **Not ATAM.** {report['not_atam']}", ">",
             f"> {report['warning']}", ""]
    ran = ", ".join(det["ran"]) or "—"
    lines.append(f"- detectors ran: {ran}")
    for d in det["degraded"]:
        lines.append(f"- ⚠ degraded: **{d['detector']}** — {d['reason']}")
    for d in det["not_evaluated"]:
        lines.append(f"- ✗ couldn't check: **{d['detector']}** — {d['reason']} "
                     "(NOT \"none found\")")
    cov = report["coverage"]
    cov_note = " — **ADVISORY: below floor, findings are not gating**" \
        if cov["advisory"] else ""
    lines += [f"- L0 coverage {cov['l0_coverage']:.0%} (floor {cov['floor']:.0%})"
              f"{cov_note} · mean confidence {cov['mean_confidence']:.2f} · "
              f"{report['total_findings']} finding(s) over "
              f"{report['internal_containers']} internal container(s)", ""]
    if not report["findings"]:
        lines += ["_No findings above thresholds. Check the detector list above — a "
                  "detector that couldn't run found nothing because it didn't look._", ""]
    for f in report["findings"]:
        adv = " · ADVISORY" if f.get("advisory") else ""
        lines += [f"### [severity {f['severity']:.2f}{adv}] "
                  f"{f['kind']}: {', '.join(f['elements'])}", "",
                  f["message"], ""]
        for ev in f["evidence"][:10]:
            lines.append(f"- {ev['type']}: `{ev['detail']}`")
        if len(f["evidence"]) > 10:
            lines.append(f"- … {len(f['evidence']) - 10} more (see JSON)")
        si = ", ".join(f"{k}={v}" for k, v in sorted(f["severity_inputs"].items()))
        conf = f"{f['confidence']:.2f}" if f.get("confidence") is not None else "—"
        calc = f.get("severity_calc") or "—"
        lines += ["", f"_Severity inputs: {si}. Calc: {calc}. Confidence: {conf}._", ""]
        if f.get("scenario"):
            sc = f["scenario"]
            d = sc["delta"]
            bits = [f"cycles {d['cycles']:+d}", f"edges {d['container_edges']:+d}"]
            if sc.get("hub_fan_in"):
                bits.append(f"hub fan-in {sc['hub_fan_in']['before']} → "
                            f"{sc['hub_fan_in']['after']}")
            lines += [f"_What-if proposal (`{sc['op']['op']}`): {', '.join(bits)} — "
                      "for human evaluation, never applied (engine §7.1)._", ""]
    return "\n".join(lines).rstrip() + "\n"


def run(ws: Workspace, *, top: int | None = None, with_scenarios: bool = False,
        write: bool = True) -> dict[str, Any] | None:
    """The ``arch risk`` entry point: build the report over the curated facts and write
    ``generated/risk-report.{md,json}`` (advisory artifacts, never the hashed core)."""
    path = ws.curated_facts if ws.curated_facts.exists() else ws.extracted_facts
    if not path.exists():
        return None
    facts = load_json(path)
    report = build_report(ws, facts, top=top, with_scenarios=with_scenarios)
    if write:
        dump_json(report, ws.risk_report_json)
        md = ws.generated / "risk-report.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render(report), encoding="utf-8", newline="")
    return report


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover
    from .. import force_utf8_stdio
    force_utf8_stdio()   # the §22.3 reconfig the arch CLI applies — needed here too
    ap = argparse.ArgumentParser(prog="anon.stages.risk", description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    ap.add_argument("--rules-dir")
    ap.add_argument("--top", type=int)
    args = ap.parse_args(argv)
    ws = resolve_workspace(args.repo, args.arch_dir, args.rules_dir)
    report = run(ws, top=args.top)
    if report is None:
        print("[risk] no fact model — run `arch run` first.")
        return 2
    print(render(report), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
