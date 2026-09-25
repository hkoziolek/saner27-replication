"""The Architecture Fact Model — in-memory helpers and the authoritative weight/
confidence functions.

The fact model is plain JSON (the contract is ``schema/fact-model.schema.json``); we
deliberately do not wrap it in heavy classes. This module centralizes the *derived*
quantities the schema cannot express — the §6.5 weight function, the §6.3 confidence
rule, the §6.4(d) canonical ordering, and provenance-stripping for the §18.4
idempotency / §11 drift comparison — so every stage computes them identically.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .ids import rel_id
from .jsonio import dumps_json

# Evidence kinds (schema enum, plan §5). interop/runtime are defined-but-deferred.
DECLARED_EVIDENCE = {"link", "project_ref"}

# --- Weight function constants (plan §6.5 "Weight (authoritative function)") ---
# weight is edge INTENSITY, not identity and not trust. Tunable; pinned here so the
# value is reproducible and reviewable rather than scattered across stages.
DECLARED_BASE = 5      # link / project_ref: fixed declared-dependency base
PACKAGE_BASE = 1       # package_ref
INCLUDE_CAP = 10       # include term contributes min(count, cap)
INTEROP_BASE = 5       # interop (P/Invoke): a declared cross-language seam — significant (§16#15)


def is_declared(evidence: list[dict[str, Any]]) -> bool:
    """True iff any evidence is a declared dependency (link/project_ref) — §6.3.

    Declared dependencies are never weight-dropped (§7.4)."""
    return any(e.get("type") in DECLARED_EVIDENCE for e in evidence)


def compute_weight(evidence: list[dict[str, Any]]) -> int:
    """Authoritative §6.5 weight function: a deterministic sum over merged evidence."""
    w = 0
    for e in evidence:
        etype = e.get("type")
        count = e.get("count")
        if etype in DECLARED_EVIDENCE:
            w += DECLARED_BASE
        elif etype == "package_ref":
            w += PACKAGE_BASE
        elif etype == "include":
            w += min(int(count) if count is not None else 1, INCLUDE_CAP)
        elif etype == "symbol_use":
            w += int(count) if count is not None else 1
        elif etype == "interop":
            # A P/Invoke is a declared cross-language dependency (§16#15) — significant, so
            # it clears the §7.4 threshold rather than being weight-dropped like noise.
            w += INTEROP_BASE
        # runtime contributes 0 to build-edge weight in v1 (§5).
    return w


def compute_confidence(evidence: list[dict[str, Any]]) -> str:
    """Extraction confidence from §5 evidence multiplicity (plan §6.5 "Weight vs confidence").

    - multiple corroborating kinds (e.g. link + include + symbol_use) -> high
    - a single declared link with zero include/symbol usage           -> low
      (the §5 "declared but never used = suspicious" case)
    - single include-only / reflection-only                            -> low
    - otherwise                                                        -> medium
    """
    kinds = {e.get("type") for e in evidence}
    corroborating = kinds & {"link", "project_ref", "include", "symbol_use"}
    if len(corroborating) >= 3:
        return "high"
    if is_declared(evidence) and not (kinds & {"include", "symbol_use"}):
        return "low"
    if len(corroborating) <= 1:
        return "low"
    return "medium"


def evidence_strength_str(evidence: list[dict[str, Any]]) -> str:
    """Human-readable rationale, e.g. 'link + include corroborate'."""
    order = ["link", "project_ref", "include", "symbol_use", "package_ref", "interop", "runtime"]
    present = [k for k in order if any(e.get("type") == k for e in evidence)]
    if not present:
        return "no evidence"
    if len(present) == 1:
        return f"{present[0]} only"
    return " + ".join(present) + " corroborate"


# --- Canonical ordering (plan §6.4d): any fragment order -> byte-identical output ---

def _sort_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(evidence, key=lambda e: (e.get("type", ""), e.get("detail", "")))


def canonicalize(facts: dict[str, Any]) -> dict[str, Any]:
    """Return *facts* with deterministic ordering of targets/relationships/evidence.

    Sorting is by id / (source,target) / (type,detail). Re-running over the same data
    in any order yields byte-identical JSON once written via :func:`jsonio.dump_json`.
    Does not mutate the input: each target/relationship is shallow-copied before its
    namespaces/depends_on/evidence lists are re-sorted.
    """
    out = dict(facts)
    targets = []
    for t in facts.get("targets", []):
        t = dict(t)
        if "namespaces" in t and isinstance(t["namespaces"], list):
            t["namespaces"] = sorted(t["namespaces"])
        if "depends_on" in t and isinstance(t["depends_on"], list):
            t["depends_on"] = sorted(t["depends_on"])
        # responsibilities is the 4th set-like union field (normalize._UNION_LIST_FIELDS) — sort
        # it too, or a single-fragment target leaks its emit order into output (the union-merge
        # only neutralizes order for multi-fragment targets). §6.4d order-independence.
        if "responsibilities" in t and isinstance(t["responsibilities"], list):
            t["responsibilities"] = sorted(t["responsibilities"])
        # com_provides (§16#15 / 5b) is a set-like list of captured COM identities — sort it so a
        # lone fragment's emit order can't leak into output (same rationale as tags below).
        if "com_provides" in t and isinstance(t["com_provides"], list):
            t["com_provides"] = sorted(t["com_provides"])
        # tags are set-like (built via sorted()/union everywhere) — sort here too so a target
        # seen by a SINGLE fragment can't leak its extractor's tag order into output. Without
        # this, the §6.4d order-independence guarantee held only for multi-fragment targets
        # (the merge unions+sorts those); a lone fragment's tag order would survive verbatim.
        if "tags" in t and isinstance(t["tags"], list):
            t["tags"] = sorted(t["tags"])
        targets.append(t)
    out["targets"] = sorted(targets, key=lambda t: t.get("id", ""))

    rels = []
    for r in facts.get("relationships", []):
        r = dict(r)
        if "evidence" in r and isinstance(r["evidence"], list):
            r["evidence"] = _sort_evidence(r["evidence"])
        rels.append(r)
    out["relationships"] = sorted(rels, key=lambda r: (r.get("source", ""), r.get("target", "")))
    return out


# --- Idempotency / drift hashing (plan §11.3 content-based gate) ---

# Provenance fields excluded from the idempotency & drift comparison (§18.4). `generated_at`/
# `commit` change every run; `interop_resolved` is a derived human-readable record of the
# §16#15 resolution whose *structural* effect already shows in targets/relationships, so
# keeping it in the hash would only add non-structural churn to the §11.3 drift gate.
_VOLATILE_PROVENANCE = {"generated_at", "commit", "interop_resolved"}


def provenance_stripped(facts: dict[str, Any]) -> dict[str, Any]:
    """Canonicalized copy with volatile provenance removed — the thing we hash/compare."""
    canon = canonicalize(facts)
    prov = dict(canon.get("provenance", {}))
    for k in _VOLATILE_PROVENANCE:
        prov.pop(k, None)
    canon["provenance"] = prov
    return canon


def content_hash(facts: dict[str, Any]) -> str:
    """Stable content hash of the fact model, ignoring volatile provenance (§11.3)."""
    return hashlib.sha256(dumps_json(provenance_stripped(facts)).encode("utf-8")).hexdigest()


# --- Convenience accessors -------------------------------------------------------

def index_targets(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in facts.get("targets", [])}


def all_element_ids(facts: dict[str, Any]) -> set[str]:
    """Every addressable element id: targets and their component/code children (§6.3)."""
    ids: set[str] = set()
    for t in facts.get("targets", []):
        ids.add(t["id"])
        for c in t.get("components", []) or []:
            ids.add(c["id"])
            for code in c.get("code", []) or []:
                ids.add(code["id"])
    return ids


def make_relationship(source: str, target: str, evidence: list[dict[str, Any]],
                      kind: str | None = None) -> dict[str, Any]:
    """Build a relationship dict with weight/is_declared/confidence derived from evidence."""
    rel: dict[str, Any] = {
        "id": rel_id(source, target),
        "source": source,
        "target": target,
        "weight": compute_weight(evidence),
        "is_declared_dependency": is_declared(evidence),
        "confidence": compute_confidence(evidence),
        "evidence_strength": evidence_strength_str(evidence),
        "evidence": _sort_evidence(evidence),
    }
    if kind:
        rel["kind"] = kind
    return rel
