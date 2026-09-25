"""Shared salience scoring (dynamic-view plan §1a#3).

The single home for the salience heuristic, lifted out of ``generate_docs`` so the
guided tour (engine §1.1) and the dynamic-view candidate inbox (dynamic-view plan §1a#3)
agree on what makes an edge / a scenario salient instead of each reinventing a divergent
formula.

Two surfaces:

* :func:`edge_salience` — the per-edge score the guided tour walks by
  (``weight + seam boost + boundary boost``). ``generate_docs._salience`` now delegates
  here so there is one definition.
* :func:`candidate_salience` — the per-candidate score the inbox / picker sorts by
  (``(source, -salience, slug)``): entry-point bonus + sum of per-step weight + distinct
  container count (plan §1a#3). Deterministic and engine-computed — the GUI only renders it.
"""
from __future__ import annotations

from typing import Any

# Salience boosts (engine §1.1; previously private to generate_docs). A pure-weight walk
# would march a reader straight past a low-traffic-but-critical seam.
SEAM_BOOST = 10            # interop/contract evidence
BOUNDARY_BOOST = 5         # a boundary-crossing hop (target external)

# Candidate-level salience constants (plan §1a#3).
ENTRY_POINT_BONUS = 10     # the first step's container is an entry point


def edge_salience(e: dict[str, Any], target_external: bool) -> tuple[int, list[str]]:
    """Per-edge salience = ``weight`` + seam boost (interop/contract) + boundary boost.

    ``e`` is a container-edge dict carrying ``weight`` and optional ``tags`` (a set);
    ``target_external`` marks a boundary-crossing hop. Returns ``(salience, reasons)``."""
    sal = e["weight"]
    reasons: list[str] = []
    if {"interop", "contract"} & e.get("tags", set()):
        sal += SEAM_BOOST
        reasons.append("seam (interop/contract)")
    if target_external:
        sal += BOUNDARY_BOOST
        reasons.append("boundary-crossing")
    return sal, reasons


def _entry_tags(facts: dict[str, Any], container_id: str) -> set[str]:
    """The union of tags across the targets that make up *container_id* (so a service-web
    or worker role on any member surfaces at the container)."""
    tags: set[str] = set()
    for t in facts.get("targets", []):
        cid = t.get("container_id", t.get("id"))
        if cid == container_id:
            tags.update(t.get("tags", []) or [])
    return tags


def _container_indegree(facts: dict[str, Any]) -> dict[str, int]:
    """In-degree over the combined build + runtime container graph (zero in-degree marks an
    entry point). Self-loops excluded."""
    t2c = {t.get("id"): t.get("container_id", t.get("id")) for t in facts.get("targets", [])}
    indeg: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for r in facts.get("relationships", []):
        cs, ct = t2c.get(r.get("source")), t2c.get(r.get("target"))
        if cs and ct and cs != ct and (cs, ct) not in seen:
            seen.add((cs, ct))
            indeg[ct] = indeg.get(ct, 0) + 1
    deployment = facts.get("deployment") or {}
    for e in deployment.get("edges", []):
        if e.get("kind") != "calls":
            continue
        cs, ct = t2c.get(e.get("source")), t2c.get(e.get("target"))
        if cs and ct and cs != ct and (cs, ct) not in seen:
            seen.add((cs, ct))
            indeg[ct] = indeg.get(ct, 0) + 1
    return indeg


# ---------------------------------------------------------------------------
# Shared entry-point detector (dynamic-view plan §1a#3 / §6)
# ---------------------------------------------------------------------------
# The ONE entry-point ranker the guided tour (engine §1.1) and Source C's graph-walk
# (dynamic-view plan §6) agree on, so C never reinvents a weaker, divergent walk. Folds in
# four signals on top of the build/runtime indegree: (1) sdk-role:service-web/worker tags;
# (2) zero combined-graph in-degree; (3) an api-skeleton [ApiController]/route /
# [HttpGet/Post/...] type → owning container is a HIGH-confidence entry root (sharper than
# the heuristic alone, plan §6); (4) for C++, a build-target type=="executable" so
# test/header targets are not mis-rooted (cross-ref anchors.py file-basename entry notion).

# Controller / route attribute substrings (best-effort over the opaque api-skeleton/1
# attribute strings — the Roslyn helper carries them verbatim; honest no-op when absent).
_CONTROLLER_ATTR_MARKERS = ("ApiController", "Route", "Controller")
_ROUTE_METHOD_MARKERS = ("HttpGet", "HttpPost", "HttpPut", "HttpDelete",
                         "HttpPatch", "HttpHead", "HttpOptions")


def _has_controller_signal(types: list[dict[str, Any]]) -> bool:
    """True iff any type-skeleton looks like an ASP.NET controller / endpoint: a type with
    an ``[ApiController]`` / ``Route`` attribute OR a member with an ``[HttpGet/Post/...]``
    attribute (plan §6). Reads the opaque ``attributes`` / ``attrs`` strings best-effort."""
    for ty in types or []:
        if not isinstance(ty, dict):
            continue
        for attr in ty.get("attributes", []) or []:
            if any(m in str(attr) for m in _CONTROLLER_ATTR_MARKERS):
                return True
        for member in ty.get("members", []) or []:
            if not isinstance(member, dict):
                continue
            for attr in member.get("attrs", []) or []:
                if any(m in str(attr) for m in _ROUTE_METHOD_MARKERS):
                    return True
    return False


def _container_member_ids(facts: dict[str, Any]) -> dict[str, list[str]]:
    """container_id -> [member build-target id, ...]."""
    out: dict[str, list[str]] = {}
    for t in facts.get("targets", []):
        tid = t.get("id")
        if not tid:
            continue
        cid = t.get("container_id", tid)
        out.setdefault(cid, []).append(tid)
    return out


def _container_has_executable(facts: dict[str, Any], container_id: str) -> bool:
    """True iff any member target of *container_id* is a C++/C# ``executable`` build target
    (plan §6 — root a C++ walk only at executables, not test/header library targets)."""
    for t in facts.get("targets", []):
        cid = t.get("container_id", t.get("id"))
        if cid == container_id and t.get("type") == "executable":
            return True
    return False


def detect_entries(facts: dict[str, Any], *,
                   api_skeletons: dict[str, list[dict[str, Any]]] | None = None,
                   ) -> tuple[list[str], dict[str, list[str]]]:
    """Rank entry-point containers, folding in the four §1a#3/§6 signals.

    Returns ``(entries, reasons)`` where *entries* is the deterministic ordered list of
    candidate entry container ids (high-confidence roots first, then zero-in-degree, each
    tie-broken by container id) and *reasons* maps each entry container id to its
    human-readable signal list. Pure function of *facts* (+ the optional merged
    ``api-skeleton/1`` map): the guided tour and Source C share this so they agree on entries.

    Only INTERNAL containers (no ``external`` / ``person`` tag) are eligible; a container is
    an entry candidate when it has zero combined build+runtime in-degree OR carries a
    high-confidence root signal (sdk-role:service-web/worker, a controller/route api-skeleton
    type, or — for C++ — an ``executable`` member). High-confidence roots sort first."""
    api_skeletons = api_skeletons or {}
    indeg = _container_indegree(facts)
    members = _container_member_ids(facts)

    # All internal containers (exclude external/person-tagged ones — never an entry root).
    containers: set[str] = set()
    for t in facts.get("targets", []):
        cid = t.get("container_id", t.get("id"))
        if not cid:
            continue
        if t.get("external"):
            continue
        tags = _entry_tags(facts, cid)
        if "external" in tags or "person" in tags:
            continue
        containers.add(cid)

    reasons: dict[str, list[str]] = {}
    high_conf: set[str] = set()
    for cid in sorted(containers):
        rs: list[str] = []
        tags = _entry_tags(facts, cid)
        if "sdk-role:service-web" in tags:
            rs.append("entry-point (sdk-role:service-web)")
            high_conf.add(cid)
        elif "sdk-role:worker" in tags:
            rs.append("entry-point (sdk-role:worker)")
            high_conf.add(cid)
        # api-skeleton controller / route signal on any member target (plan §6).
        if any(_has_controller_signal(api_skeletons.get(m, [])) for m in members.get(cid, [])):
            rs.append("entry-point (api controller/route)")
            high_conf.add(cid)
        # C++ executable member (plan §6 — never root at a test/header library target).
        if _container_has_executable(facts, cid):
            rs.append("entry-point (executable target)")
            high_conf.add(cid)
        if not indeg.get(cid):
            rs.append("entry-point (zero in-degree)")
        if rs:
            reasons[cid] = rs

    # Eligible entries: high-confidence roots OR zero-in-degree containers.
    eligible = [cid for cid in sorted(containers)
                if cid in high_conf or not indeg.get(cid)]
    # High-confidence roots first, then by container id (deterministic, slug-stable).
    eligible.sort(key=lambda c: (0 if c in high_conf else 1, c))
    return eligible, reasons


def candidate_salience(view: dict[str, Any], facts: dict[str, Any]) -> tuple[float, list[str]]:
    """Per-candidate salience for the inbox / picker sort (plan §1a#3):

    ``entry-point bonus + sum of per-step weight + distinct-container count``

    * **entry-point bonus** — the first ``ok`` step's ``from`` container is tagged
      ``sdk-role:service-web`` / ``sdk-role:worker`` OR has zero in-degree in the combined
      build+runtime container graph.
    * **sum of per-step weight** — over ``ok`` steps; a runtime-only step's ``weight`` is
      ``null`` and counts as 0 (plan §3.1#1).
    * **distinct-container count** — distinct ``from`` / ``to`` containers across ``ok`` steps.

    Returns ``(salience, salience_reasons)``; deterministic (pure function of view + facts)."""
    ok_steps = [s for s in view.get("steps", []) if s.get("status") == "ok"]
    reasons: list[str] = []
    salience = 0.0

    # entry-point bonus
    if ok_steps:
        first_from = ok_steps[0].get("from")
        if first_from:
            tags = _entry_tags(facts, first_from)
            indeg = _container_indegree(facts)
            if "sdk-role:service-web" in tags:
                salience += ENTRY_POINT_BONUS
                reasons.append("entry-point (sdk-role:service-web)")
            elif "sdk-role:worker" in tags:
                salience += ENTRY_POINT_BONUS
                reasons.append("entry-point (sdk-role:worker)")
            elif not indeg.get(first_from):
                salience += ENTRY_POINT_BONUS
                reasons.append("entry-point (zero in-degree)")

    # sum of per-step weight (runtime-only weight is null -> 0)
    weight_sum = sum((s.get("weight") or 0) for s in ok_steps)
    if weight_sum:
        salience += weight_sum
        reasons.append(f"step weight sum {int(weight_sum)}")

    # distinct-container count
    containers: set[str] = set()
    for s in ok_steps:
        if s.get("from"):
            containers.add(s["from"])
        if s.get("to"):
            containers.add(s["to"])
    if containers:
        salience += len(containers)
        reasons.append(f"crosses {len(containers)} containers")

    return salience, reasons
