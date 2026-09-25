"""Resolve native interop boundaries to real ``cpp:target:*`` targets (§16#15).

The C#/C++-side interop scan (``extract_interop``) emits an ``interop`` edge from the owning
build target to a *synthetic external boundary*, because the native side that satisfies the
seam was historically unknown. Three seam kinds, three boundary id prefixes:

  * **P/Invoke** → ``native:lib:<name>`` — a ``[DllImport("NativeMath")]`` and a CMake
    ``add_library(NativeMath SHARED …)`` are the same component.
  * **C++/CLI** → ``native:clr:<assembly>`` — a managed↔native ``/clr`` bridge; the native
    side is a ``cpp:target`` whose ``.vcxproj`` declares ``<CLRSupport>`` / ``<CompileAsManaged>``
    (``extract_interop._vcxproj_is_managed`` tags it ``interop:cppcli``).
  * **COM** → ``com:<progid-or-clsid>`` — bound *by identity*, not by name: the native COM
    server target carries a captured ``com_provides`` list (CLSID/ProgID/TypeLib GUID emitted
    by ``extract_msbuild_cpp`` from a sibling ``.idl`` / ``<Midl>`` reference).

Now that mixed-language models carry both sides, this **post-merge** pass (normalize, §6.4)
binds a boundary to its real target when — and only when — the match is **unambiguous**. It is
*fact resolution*, not curation: the seam genuinely reaches that target, so binding it is
structural truth, not a styling choice. On an unambiguous bind it:

  * rewrites every edge ``… -> <boundary>`` to point at ``cpp:target:<…>`` (merging if that
    collapses onto an existing edge), tagging the edge ``interop-resolved``;
  * tags the resolved C++ target by seam kind (``interop:pinvoke`` / ``interop:cppcli`` /
    ``interop:com``) so a view can surface the seam;
  * drops the now-redundant boundary target.

Conservative by design: **no match or an ambiguous (>1) match leaves the boundary intact**
and reports it — a false edge is worse than an honest dangling stub. A boundary whose native
side was never captured (e.g. a COM server with no discoverable identity) is *expected* to
stay dangling: that is correct, not a failure.
"""
from __future__ import annotations

from typing import Any

from .ids import rel_id
from .model import (compute_confidence, compute_weight, evidence_strength_str,
                    is_declared)

# Native-boundary id prefixes the C#/C++-side scan emits (plan §16#15).
_PINVOKE_PREFIX = "native:lib:"
_CLR_PREFIX = "native:clr:"
_COM_PREFIX = "com:"
# Back-compat alias (P/Invoke prefix) — referenced by older imports/tests.
_NATIVE_PREFIX = _PINVOKE_PREFIX
_ALL_PREFIXES = (_PINVOKE_PREFIX, _CLR_PREFIX, _COM_PREFIX)


def _norm_libname(name: str) -> str:
    """Normalize a library name for matching: lowercase, strip one leading ``lib``."""
    n = (name or "").strip().lower()
    if n.startswith("lib") and len(n) > 3:
        n = n[3:]
    return n


def _norm_assembly(name: str) -> str:
    """Normalize a CLR assembly / output name for matching: lowercase, no leading ``lib`` strip.

    A managed assembly name (``Bridge``) is matched verbatim (case-folded) — unlike a Unix
    ``libfoo.so`` P/Invoke name, a .NET assembly never carries a ``lib`` prefix convention.
    """
    return (name or "").strip().lower()


def _norm_progid(value: str) -> str:
    """Normalize a COM ProgID for case-insensitive comparison (GUIDs handled separately)."""
    return (value or "").strip().lower()


def _is_guid(value: str) -> bool:
    """Heuristic: a COM identity that looks like a GUID (matched EXACTLY, not case-folded-name)."""
    s = (value or "").strip().strip("{}")
    parts = s.split("-")
    return len(parts) == 5 and all(c in "0123456789abcdefABCDEF" for c in s if c != "-")


def _norm_guid(value: str) -> str:
    """Normalize a GUID for exact comparison: strip braces, lowercase (GUIDs are case-insensitive)."""
    return (value or "").strip().strip("{}").lower()


def _cpp_targets(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """All C++ build targets (cpp:target:* / cpp:vcxproj:*) — both are ``language == cpp``."""
    return [t for t in facts.get("targets", []) if t.get("language") == "cpp"]


def _cpp_lib_index(facts: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Map normalized name -> list of candidate C++ library targets (shared/static/etc.)."""
    index: dict[str, list[dict[str, Any]]] = {}
    for t in _cpp_targets(facts):
        if t.get("type") not in ("shared_lib", "static_lib", "module_lib", "unknown"):
            continue
        index.setdefault(_norm_libname(t.get("name", "")), []).append(t)
    return index


def _pick(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the single best C++ target for a native lib, or None if ambiguous.

    A P/Invoke loads a *shared* library, so a unique shared_lib wins outright. Otherwise the
    match must be unambiguous (exactly one candidate) — we never guess between several."""
    shared = [c for c in candidates if c.get("type") == "shared_lib"]
    if len(shared) == 1:
        return shared[0]
    if not shared and len(candidates) == 1:
        return candidates[0]
    return None


def _pick_unique(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Bind only on an UNAMBIGUOUS single candidate (used by the CLR + COM passes).

    Unlike :func:`_pick` (which prefers a unique *shared* lib for P/Invoke), CLR and COM
    matches have no shared-lib tie-breaker — a managed assembly or a COM coclass is identified
    directly, so >1 candidate is genuinely ambiguous and is left dangling."""
    return candidates[0] if len(candidates) == 1 else None


# --- Per-seam passes: each returns (rewrite, cpp_tags, resolved_pairs, unresolved) ------
# rewrite:   boundary id -> resolved cpp id (unambiguous binds only)
# cpp_tags:  resolved cpp id -> seam tag to add (interop:pinvoke / :cppcli / :com)

def _resolve_pinvoke(facts: dict[str, Any], boundaries: list[dict[str, Any]]
                     ) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    """P/Invoke ``native:lib:<name>`` → C++ lib target by normalized name (unchanged §16#15)."""
    rewrite: dict[str, str] = {}
    cpp_tags: dict[str, str] = {}
    resolved: list[str] = []
    unresolved: list[str] = []
    index = _cpp_lib_index(facts)
    for n in boundaries:
        target = _pick(index.get(_norm_libname(n.get("name", "")), [])) if index else None
        if target is None:
            unresolved.append(n["id"])
            continue
        rewrite[n["id"]] = target["id"]
        cpp_tags[target["id"]] = "interop:pinvoke"
        resolved.append(f"{n['id']} -> {target['id']}")
    return rewrite, cpp_tags, resolved, unresolved


def _resolve_clr(facts: dict[str, Any], boundaries: list[dict[str, Any]]
                 ) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    """C++/CLI ``native:clr:<assembly>`` → the unique ``/clr`` C++ target whose name matches.

    The native side is a C++ target tagged ``interop:cppcli`` (a ``.vcxproj`` with
    ``<CLRSupport>``/``<CompileAsManaged>``). Match by normalized PROJECT name, among *managed*
    targets only, unambiguous-only (no shared-lib tie-breaker).

    Known conservative limitations — a MISS here is an honest dangling boundary, NEVER a wrong
    bind: a target whose OUTPUT assembly name differs from its project name, or whose ``/clr`` is
    declared only in an imported ``.props``/``.targets`` (so it was never tagged
    ``interop:cppcli``), is not matched. Capturing the output-assembly name is a future refine."""
    rewrite: dict[str, str] = {}
    cpp_tags: dict[str, str] = {}
    resolved: list[str] = []
    unresolved: list[str] = []
    # Index only the managed (/clr) C++ targets by normalized assembly name. A managed
    # boundary should never bind to a plain native lib that merely shares its name.
    index: dict[str, list[dict[str, Any]]] = {}
    for t in _cpp_targets(facts):
        if "interop:cppcli" not in (t.get("tags") or []):
            continue
        index.setdefault(_norm_assembly(t.get("name", "")), []).append(t)
    for n in boundaries:
        target = _pick_unique(index.get(_norm_assembly(n.get("name", "")), [])) if index else None
        if target is None:
            unresolved.append(n["id"])
            continue
        rewrite[n["id"]] = target["id"]
        cpp_tags[target["id"]] = "interop:cppcli"
        resolved.append(f"{n['id']} -> {target['id']}")
    return rewrite, cpp_tags, resolved, unresolved


def _com_identity_index(facts: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]],
                                                        dict[str, list[dict[str, Any]]]]:
    """Index C++ targets by their captured ``com_provides`` identities.

    Returns ``(by_guid, by_progid)``: GUIDs keyed by normalized (braces-stripped, lowercased)
    form for EXACT match; ProgIDs keyed case-folded. A target may provide several identities."""
    by_guid: dict[str, list[dict[str, Any]]] = {}
    by_progid: dict[str, list[dict[str, Any]]] = {}
    for t in _cpp_targets(facts):
        for ident in t.get("com_provides") or []:
            if _is_guid(ident):
                by_guid.setdefault(_norm_guid(ident), []).append(t)
            else:
                by_progid.setdefault(_norm_progid(ident), []).append(t)
    return by_guid, by_progid


def _resolve_com(facts: dict[str, Any], boundaries: list[dict[str, Any]]
                 ) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    """COM ``com:<progid-or-clsid>`` → the unique native target whose ``com_provides`` lists it.

    Identity is by GUID (exact) or ProgID (case-insensitive), NOT by name. A boundary whose
    identity nobody captured stays dangling — that is the honest, correct outcome, not a bug."""
    rewrite: dict[str, str] = {}
    cpp_tags: dict[str, str] = {}
    resolved: list[str] = []
    unresolved: list[str] = []
    by_guid, by_progid = _com_identity_index(facts)
    for n in boundaries:
        ident = n.get("name", "")
        if _is_guid(ident):
            cand = by_guid.get(_norm_guid(ident), [])
        else:
            cand = by_progid.get(_norm_progid(ident), [])
        target = _pick_unique(cand)
        if target is None:
            unresolved.append(n["id"])
            continue
        rewrite[n["id"]] = target["id"]
        cpp_tags[target["id"]] = "interop:com"
        resolved.append(f"{n['id']} -> {target['id']}")
    return rewrite, cpp_tags, resolved, unresolved


_PASSES = {
    _PINVOKE_PREFIX: _resolve_pinvoke,
    _CLR_PREFIX: _resolve_clr,
    _COM_PREFIX: _resolve_com,
}


def _apply_rewrites(facts: dict[str, Any], rewrite: dict[str, str],
                    cpp_tags: dict[str, set[str]]) -> None:
    """Apply a boundary→cpp ``rewrite`` to targets, relationships and deployment edges.

    Shared machinery for all three seam passes (§16#15): (1) drop the resolved boundary
    targets and tag the C++ targets they bound to; (2) rewrite edge endpoints, merging +
    recomputing derived fields when a rewrite collapses two edges onto one id; (3) rewrite
    any deployment-edge endpoints so dropping a boundary can never dangle (§4.4)."""
    if not rewrite:
        return

    # 1) drop the resolved boundary targets; tag the C++ targets they bound to (by seam kind).
    new_targets: list[dict[str, Any]] = []
    for t in facts.get("targets", []):
        tid = t.get("id")
        if tid in rewrite:  # a resolved native boundary — remove it
            continue
        if tid in cpp_tags:
            tags = set(t.get("tags", [])) | cpp_tags[tid]
            t = {**t, "tags": sorted(tags)}
        new_targets.append(t)
    facts["targets"] = new_targets

    # 2) rewrite edge endpoints; merge if a rewrite collapses two edges onto one id.
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in facts.get("relationships", []):
        src = rewrite.get(r.get("source"), r.get("source"))
        tgt = rewrite.get(r.get("target"), r.get("target"))
        r = dict(r)
        if r.get("target") != tgt or r.get("source") != src:
            tags = set(r.get("tags", []))
            tags.add("interop-resolved")
            r["tags"] = sorted(tags)
        r["source"], r["target"] = src, tgt
        rid = rel_id(src, tgt)
        r["id"] = rid
        if rid in merged:
            prev = merged[rid]
            prev_ev = prev.get("evidence", []) or []
            prev["evidence"] = prev_ev + [e for e in (r.get("evidence", []) or []) if e not in prev_ev]
            prev["tags"] = sorted({*prev.get("tags", []), *r.get("tags", [])})
        else:
            merged[rid] = r
            order.append(rid)
    # recompute derived fields from possibly-merged evidence (§6.4b).
    for r in merged.values():
        ev = r.get("evidence", [])
        r["weight"] = compute_weight(ev)
        r["is_declared_dependency"] = is_declared(ev)
        r["confidence"] = compute_confidence(ev)
        r["evidence_strength"] = evidence_strength_str(ev)
    facts["relationships"] = [merged[i] for i in order]

    # 3) rewrite any deployment-edge endpoints that referenced a resolved boundary, so dropping
    # the boundary target (step 1) can never leave a dangling deployment edge (§4.4 referential
    # integrity). No producer emits a deployment edge to a native boundary today, so this is
    # normally a no-op — but it keeps the invariant enforced rather than implicit.
    deployment = facts.get("deployment")
    if deployment:
        for e in deployment.get("edges", []) or []:
            if e.get("source") in rewrite:
                e["source"] = rewrite[e["source"]]
            if e.get("target") in rewrite:
                e["target"] = rewrite[e["target"]]


def resolve(facts: dict[str, Any]) -> dict[str, Any]:
    """Resolve native interop boundaries against C++ targets in place; return stats.

    Binds the three §16#15 seam kinds — P/Invoke (``native:lib:``), C++/CLI (``native:clr:``)
    and COM (``com:``) — to real ``cpp:target:*`` targets when the match is unambiguous, sharing
    one rewrite/merge pass. Mutates *facts* (targets/relationships/deployment) and returns
    ``{"resolved": [...], "unresolved": [...]}`` for the run report. A no-op (and no model
    change) when there are no native boundaries or no C++ targets to bind to — so a pure-C# or
    pure-C++ model is byte-identical to one that never called this.
    """
    # Bucket the boundary targets by id prefix (seam kind). Sorted so the resolved/unresolved
    # report ordering is deterministic regardless of target iteration order.
    buckets: dict[str, list[dict[str, Any]]] = {p: [] for p in _ALL_PREFIXES}
    for t in facts.get("targets", []):
        tid = str(t.get("id", ""))
        for prefix in _ALL_PREFIXES:
            if tid.startswith(prefix):
                buckets[prefix].append(t)
                break
    if not any(buckets.values()):
        return {"resolved": [], "unresolved": []}
    if not _cpp_targets(facts):
        # No native side to bind to: every boundary is honestly unresolved (no model change).
        unresolved = sorted(t["id"] for b in buckets.values() for t in b)
        return {"resolved": [], "unresolved": unresolved}

    rewrite: dict[str, str] = {}
    cpp_tags: dict[str, set[str]] = {}
    resolved_pairs: list[str] = []
    unresolved: list[str] = []
    for prefix in _ALL_PREFIXES:
        bucket = sorted(buckets[prefix], key=lambda t: t.get("id", ""))
        if not bucket:
            continue
        rw, tags, res, unres = _PASSES[prefix](facts, bucket)
        rewrite.update(rw)
        # A cpp target reached by SEVERAL seams (e.g. a /clr bridge that is also a COM server)
        # must keep ALL its seam tags — accumulate into a set, not last-writer-wins.
        for cid, tag in tags.items():
            cpp_tags.setdefault(cid, set()).add(tag)
        resolved_pairs.extend(res)
        unresolved.extend(unres)

    _apply_rewrites(facts, rewrite, cpp_tags)
    return {"resolved": sorted(resolved_pairs), "unresolved": sorted(unresolved)}
