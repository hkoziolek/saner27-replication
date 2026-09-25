"""Stage 4 — curation: apply ``mapping-rules.yaml`` -> ``curated-facts.json`` (plan §7).

Deterministic given locked rules (the human gate happens when editing the YAML, §7.1).
What this applies (plan §7.4), in order:

  1. ``id_aliases`` rename map (applied first, §6.3 ID-stability contract).
  2. exclusion (globs + ids): drop test/vendored/generated targets and their edges.
  3. ``drop_external``: NuGet/system boundary targets are not containers by default.
  4. **boundary promotion**: ``boundary:`` rules promote certain deps to external
     systems / persons (kept, not dropped, tagged ``external``/``person``, §7.4).
  5. grouping -> container assignment; ``name_overrides`` (hard pin); ``technology`` tags;
     carry the ``components[]`` tier + the D§5/D§6 ``expand``/``expand_classes`` stamps
     through so §9 can build Component views (the legacy ``component_view``/``code_view``
     flags reinterpret as expand promotions, D§6.2, with a provenance deprecation note).
     The component tier is curated by an optional ``components:`` block —
     ``exclude_namespaces`` globs drop non-architectural namespaces (e.g. ``System*``,
     ``JetBrains*``) and ``aggregate_depth: N`` collapses namespace keys to their first
     N dot-separated segments (merging duplicates), so a real-repo Component view meets
     the §1.1#1 readability budget (resolves part of the open §16#10b). Both default off
     — absent the block the tier passes through unchanged.
  6. the ``needs-curation`` provisional-singleton gate (§7.3, soft by default).
  7. ``relationship_kinds``: rename an edge's ``kind`` by ``when_evidence_contains``
     pattern (§7.4 — e.g. the toy's "Publisher" -> "Publishes and consumes messages").
  8. ``down_weight_private``: de-emphasize PRIVATE-visibility link deps vs PUBLIC
     (§6.2 visibility on evidence). Optionally drop them (``drop_private_links``).
  9. the significance threshold ``min_relationship_weight`` — applied **only to
     non-declared edges**; declared dependencies are NEVER weight-dropped (§7.4). A
     below-threshold declared edge is kept and tagged ``suspicious-declared`` (§5).

Each surviving target gains ``container_id`` / ``container_name`` (the §9 generator
aggregates targets into those containers). The fact-model schema keeps target/relationship
objects OPEN, so these additive fields validate (§6.5a).
"""
from __future__ import annotations

import copy
import fnmatch
from typing import Any

from ..config import MappingRules, load_mapping_rules
from ..ids import dsl_identifier
from ..jsonio import dump_json, load_json
from ..model import (canonicalize, compute_confidence, compute_weight,
                     evidence_strength_str, is_declared)
from ..paths import Workspace

# Weight subtracted from a non-declared edge per PRIVATE-visibility evidence item when
# ``down_weight_private`` is on (§7.4). Tunable; pinned here so it is reproducible.
PRIVATE_DOWN_WEIGHT = 2


def _container_id_for_group(container_name: str) -> str:
    return f"container:{dsl_identifier('x:container:' + container_name)}"


def glob_matches_target(glob: str, target: dict[str, Any]) -> bool:
    """True if *glob* matches a target's id / path / name (§7.4 members_glob semantics).

    fnmatchcase (not fnmatch): ids/paths/names are case-sensitive content, and fnmatch
    applies os.path.normcase — making matching case-insensitive on Windows but case-sensitive
    on POSIX, which would break the §1.1#2 across-OS determinism. Shared by :func:`_group_for`
    and the T2.6 explain/lint replay so the two never disagree about what a glob would claim.
    """
    return (fnmatch.fnmatchcase(target.get("id", ""), glob)
            or fnmatch.fnmatchcase(target.get("path", ""), glob)
            or fnmatch.fnmatchcase(target.get("name", ""), glob))


def tag_matches_target(glob: str, target: dict[str, Any]) -> bool:
    """True if *glob* matches any of a target's ``tags`` (§7.4 ``members_tag`` semantics).

    The tag-keyed sibling of :func:`glob_matches_target`: it lets curation group on a
    *source-declared* signal an extractor surfaced as a tag (e.g. ``module-category:*`` from
    ``extract_orchardcore_manifest``, ``slnfolder:*`` from the build graph) instead of on the
    id/path/name. Deliberately a SEPARATE selector — never folded into ``glob_matches_target`` —
    so a path glob like ``*Content*`` can never accidentally claim a target by its tag text.
    fnmatchcase for the same across-OS determinism reason (§1.1#2). Shared by :func:`_group_for`
    and the T2.6 explain/lint replay so curate and explain agree on what a tag glob claims.
    """
    return any(fnmatch.fnmatchcase(tag, glob) for tag in target.get("tags", []) or [])


def _group_index(rules: MappingRules) -> tuple[
        dict[str, tuple[str, str, int]],
        list[tuple[str, str, list[str], int]],
        dict[str, str]]:
    """Build the §7.4 grouping index from ``rules.groups`` (shared by curate + explain/lint).

    Returns:
      - ``member_to_container``: explicit member id -> ``(container_id, container_name, rule_index)``
      - ``glob_groups``:        ordered ``[(container_id, container_name, [globs], [tags], rule_index)]``
                                (``[tags]`` are ``members_tag`` globs matched against target tags)
      - ``cid_to_cname``:       container_id -> container_name (collision-checked)

    ``rule_index`` is the 0-based index into ``rules.groups`` — a stable, citable handle back
    into the hand-authored ``mapping-rules.yaml`` ``group:`` list (the T2.6 explainability
    anchor: a grouped target records exactly *which* rule placed it). The container-id
    collision check (two distinct names colliding to one lossy ``dsl_identifier`` id) is
    enforced here so curate and explain see the same failure rather than a silent merge.
    """
    member_to_container: dict[str, tuple[str, str, int]] = {}
    glob_groups: list[tuple[str, str, list[str], list[str], int]] = []
    cid_to_cname: dict[str, str] = {}
    for ridx, g in enumerate(rules.groups):
        cname = g.get("container")
        if not cname:
            continue
        cid = _container_id_for_group(cname)
        # dsl_identifier() is lossy (drops separators, camel-cases), so two DISTINCT
        # container names can collapse to the same id and silently merge their members.
        # The element-id path is protected by SlugRegistry; this one is not, so detect the
        # collision and fail loudly rather than dropping a container from the model.
        prior = cid_to_cname.setdefault(cid, cname)
        if prior != cname:
            raise ValueError(
                f"mapping-rules: container names {prior!r} and {cname!r} both map to "
                f"container id {cid!r}; rename one so their members are not silently merged")
        for m in g.get("members", []) or []:
            member_to_container[m] = (cid, cname, ridx)
        globs = [gl for gl in (g.get("members_glob", []) or []) if isinstance(gl, str)]
        tags = [tg for tg in (g.get("members_tag", []) or []) if isinstance(tg, str)]
        if globs or tags:
            glob_groups.append((cid, cname, globs, tags, ridx))
    return member_to_container, glob_groups, cid_to_cname


def _group_for(tid: str, target: dict[str, Any],
               explicit: dict[str, tuple[str, str, int]],
               glob_groups: list[tuple[str, str, list[str], list[str], int]]
               ) -> tuple[str, str, dict[str, Any]] | None:
    """Resolve a target's ``(container_id, container_name, grouped_by)``, or ``None`` (singleton).

    Explicit ``members:`` win over the pattern selectors; among the pattern groups the FIRST (in
    rule order) whose ``members_glob`` (id/path/name) OR ``members_tag`` (a target tag) pattern
    matches wins. ``members_glob`` (§7.4) is what makes a large repo (OrchardCore: 233 projects)
    curatable without hand-listing every ``.csproj``; ``members_tag`` extends that to grouping on
    a source-declared signal an extractor tagged (e.g. ``module-category:*``) — both let the rest
    fall to singletons.

    The third tuple element is the T2.6 ``grouped_by`` provenance: which rule (signal +
    ``rule_index`` into ``mapping-rules.yaml`` ``group:``) made this placement, plus the
    matched glob ``pattern`` for the glob signal. It is stamped verbatim onto the curated
    target so ``arch explain`` can cite the decision at review time.
    """
    if tid in explicit:
        cid, cname, ridx = explicit[tid]
        return cid, cname, {"signal": "members", "container": cname, "rule_index": ridx}
    for cid, cname, globs, tags, ridx in glob_groups:
        for gl in globs:
            if glob_matches_target(gl, target):
                return cid, cname, {"signal": "members_glob", "container": cname,
                                    "rule_index": ridx, "pattern": gl}
        for tg in tags:
            if tag_matches_target(tg, target):
                return cid, cname, {"signal": "members_tag", "container": cname,
                                    "rule_index": ridx, "pattern": tg}
    return None


def _excluded(target: dict[str, Any], rules: MappingRules) -> bool:
    tid = target["id"]
    if tid in rules.exclude_ids:
        return True
    name = target.get("name", "")
    path = target.get("path", "")
    for glob in rules.exclude_globs:
        # fnmatchcase for OS-independent, case-sensitive matching (see _group_for).
        if fnmatch.fnmatchcase(name, glob) or fnmatch.fnmatchcase(path, glob) or fnmatch.fnmatchcase(tid, glob):
            return True
    return False


def _apply_aliases(facts: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    if not aliases:
        return facts
    def rename(i: str) -> str:
        return aliases.get(i, i)
    # Build NEW target/relationship dicts rather than mutating the caller's objects in
    # place: curate()'s caller reuses the original facts as the suspected-rename baseline,
    # so the pre-alias ids must survive on the input (§7.3/§6.3).
    out = dict(facts)
    new_targets: list[dict[str, Any]] = []
    for t in facts.get("targets", []):
        t = dict(t)
        t["id"] = rename(t["id"])
        if "depends_on" in t:
            t["depends_on"] = [rename(d) for d in t["depends_on"]]
        new_targets.append(t)
    out["targets"] = new_targets
    # Renaming endpoints can collapse two edges onto one id; merge them by unioning evidence,
    # then RECOMPUTE the derived fields from that union (§6.4b — never max/first-wins, the same
    # rule merge() and interop_resolve.resolve() follow). Taking max(weight) would under-count
    # a collapsed edge whose true weight is the sum of its evidence, so curate()'s threshold
    # could wrongly drop a real dependency; stale confidence/evidence_strength would mislabel it.
    rel_by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in facts.get("relationships", []):
        r = dict(r)
        r["source"] = rename(r["source"])
        r["target"] = rename(r["target"])
        rid = f"rel:{r['source']}->{r['target']}"
        r["id"] = rid
        if rid in rel_by_id:
            prev = rel_by_id[rid]
            prev_ev = prev.get("evidence", []) or []
            prev["evidence"] = prev_ev + [e for e in (r.get("evidence", []) or []) if e not in prev_ev]
        else:
            rel_by_id[rid] = r
            order.append(rid)
    # recompute weight / is_declared / confidence / evidence_strength from the merged evidence
    for r in rel_by_id.values():
        ev = r.get("evidence", []) or []
        r["weight"] = compute_weight(ev)
        r["is_declared_dependency"] = is_declared(ev)
        r["confidence"] = compute_confidence(ev)
        r["evidence_strength"] = evidence_strength_str(ev)
    out["relationships"] = [rel_by_id[i] for i in order]
    return out


# --- §7.4 rule accessors not yet on MappingRules; read from .raw (config is read-only) ---

def _relationship_kind_rules(rules: MappingRules) -> list[dict[str, Any]]:
    raw = rules.raw.get("relationship_kinds", []) or []
    return [r for r in raw if isinstance(r, dict) and r.get("kind")]


def _boundary_rules(rules: MappingRules) -> dict[str, dict[str, Any]]:
    """``boundary:`` block (§7.4 boundary promotion).

    Maps a target id to ``{kind: external|person, name?: <display>}``. Accepts either a
    mapping ``{id: {kind: external}}`` or a list of ``{id:.., kind:.., name:..}`` rows.
    """
    raw = rules.raw.get("boundary", {}) or {}
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for tid, spec in raw.items():
            if isinstance(spec, str):
                out[tid] = {"kind": spec}
            elif isinstance(spec, dict):
                out[tid] = dict(spec)
    elif isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict) and row.get("id"):
                out[row["id"]] = {k: v for k, v in row.items() if k != "id"}
    return out


def _evidence_kind_for(rel: dict[str, Any], kind_rules: list[dict[str, Any]]) -> str | None:
    """First ``relationship_kinds`` rule whose pattern is found in any evidence detail."""
    for rule in kind_rules:
        pat = rule.get("when_evidence_contains")
        if not pat:
            continue
        for ev in rel.get("evidence", []):
            if pat in (ev.get("detail") or ""):
                return rule["kind"]
    return None


def _private_evidence_count(rel: dict[str, Any]) -> int:
    return sum(1 for ev in rel.get("evidence", [])
               if ev.get("visibility") == "private")


def _component_policy(rules: MappingRules) -> tuple[list[str], int | None]:
    """The optional ``components:`` block (§7.4 component-tier curation, §16#10b).

    Returns ``(exclude_namespace_globs, aggregate_depth)``. Both default to a no-op
    (``[]``, ``None``) so a fixture without the block keeps its full namespace tier.
    """
    raw = rules.raw.get("components", {}) or {}
    exclude = [g for g in (raw.get("exclude_namespaces", []) or []) if isinstance(g, str)]
    depth = raw.get("aggregate_depth")
    if not isinstance(depth, int) or depth < 1:
        depth = None
    return exclude, depth


def _transform_components(components: list[dict[str, Any]],
                          exclude_globs: list[str], depth: int | None) -> list[dict[str, Any]]:
    """Curate one target's namespace component tier (§6.3): drop excluded namespaces and
    collapse keys to *depth* segments, merging the ``code[]`` children of merged namespaces.

    Deterministic: aggregated components keep first-seen order (the input is already
    namespace-sorted by the Roslyn helper); §9's ``build_containers`` re-sorts by id.
    A no-op when *exclude_globs* is empty and *depth* is ``None`` — the same list is
    returned untouched so unaffected fixtures stay byte-identical.
    """
    if not components or (not exclude_globs and depth is None):
        return components
    merged: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for comp in components:
        key = comp.get("key", comp.get("name", ""))
        if any(fnmatch.fnmatchcase(key, g) for g in exclude_globs):
            continue
        # aggregate_depth collapses a namespace key to its first N segments. Be separator-
        # aware: C# namespaces are dot-separated (``Toy.Common.Foo``) but C++ are ``::``-
        # separated (``detail::dragonbox::cache_accessor``) — split on whichever this key uses
        # so ``aggregate_depth`` works for both languages, rejoining with the same separator.
        if depth:
            sep = "::" if "::" in key else "."
            agg_key = sep.join(key.split(sep)[:depth])
        else:
            agg_key = key
        if agg_key in merged:
            # collapse: fold this namespace's code-level children into the survivor.
            merged[agg_key].setdefault("code", []).extend(comp.get("code", []) or [])
            continue
        new = dict(comp)
        new["key"] = agg_key
        new["name"] = agg_key
        # id stays parent-qualified (§6.3): keep the "<parent>/component:" prefix, swap the key.
        prefix = comp["id"].rsplit("/component:", 1)[0]
        new["id"] = f"{prefix}/component:{agg_key}"
        new["code"] = list(comp.get("code", []) or [])
        merged[agg_key] = new
        out.append(new)
    return out


def _component_remap(components: list[dict[str, Any]],
                     exclude_globs: list[str], depth: int | None) -> dict[str, str | None]:
    """The id-rewrite :func:`_transform_components` applies, as ``old id -> new id | None``.

    ``None`` marks a component dropped by ``exclude_namespaces``; an aggregated component maps
    to its depth-collapsed survivor id (several originals can map to one). This keeps the
    ``…/component:<ns>`` namespace edges in sync with the curated component tier — an edge to an
    excluded namespace is dropped, an edge into an aggregated namespace is remapped — so curated
    facts never carry a dangling component endpoint (the §6 referential-integrity gate). Mirrors
    ``_transform_components`` exactly so the two can never diverge; a no-op (identity) map when
    ``exclude_globs`` is empty and ``depth`` is ``None``.
    """
    remap: dict[str, str | None] = {}
    for comp in components or []:
        key = comp.get("key", comp.get("name", ""))
        if any(fnmatch.fnmatchcase(key, g) for g in exclude_globs):
            remap[comp["id"]] = None
            continue
        if depth:
            sep = "::" if "::" in key else "."
            agg_key = sep.join(key.split(sep)[:depth])
        else:
            agg_key = key
        prefix = comp["id"].rsplit("/component:", 1)[0]
        remap[comp["id"]] = f"{prefix}/component:{agg_key}"
    return remap


# --- T2.7 seed/adopt: rewrite seeded human-keyed rules to extracted stable ids ---

# Rule blocks whose KEYS (not values) are human element ids and so must be rekeyed when a
# confirmed binding renames them to an extracted stable id. `name_overrides` / `descriptions`
# are dict-keyed; `group[].members` is a per-group id list (handled separately).
_HUMAN_KEYED_BLOCKS = ("name_overrides", "descriptions")


def rekey_rules(
    rules: MappingRules, confirmed: dict[str, str]
) -> tuple[MappingRules, dict[str, Any]]:
    """Rewrite seeded human element ids in ``rules`` to their extracted stable ids (T2.7, §7.4).

    ``arch curate --seed-from`` writes a ``mapping-rules.yaml`` whose ``name_overrides`` keys
    and ``group[].members`` are the model's HUMAN element ids (``orderProcessor``), not the
    pipeline's build-derived stable ids (``csharp:csproj:src/...``). This pure function takes
    the human->extracted map from confirmed ``human_id_bindings`` (``reconcile.confirmed``) and
    returns a NEW :class:`MappingRules` whose ``.raw`` is a deep copy with every confirmed human
    key rewritten to its extracted id, in:

      - ``name_overrides`` / ``descriptions`` (and any other human-dict-keyed block): the KEYS.
      - ``group[].members``: each human member id.

    The HARD RULE / invariant (§7.5): only keys present in ``confirmed`` (i.e. ``status:
    confirmed`` bindings, the §7.5 gate) are rewritten; an unconfirmed/absent key is LEFT AS-IS
    (it may already be a real extracted id, or a genuinely unmatched aspirational element). The
    FACTS are never touched — only the rule keys (humans touch names/grouping, never structure).

    A rewritten member id that no longer exists as an extracted target is *not* an error here:
    :func:`curate` silently drops it via its ``kept_ids`` membership check (the honest "skip" for
    a stale binding — the staleness is resolved downstream against the facts, which we can't see).

    Determinism (a hard CI gate, §1.1#2): all iteration is over sorted keys, so the result never
    depends on dict insertion order. A **collision** — two distinct confirmed human keys mapping
    to the SAME extracted id within ``name_overrides`` — is a real conflict; rather than let dict
    overwrite order decide it non-deterministically, the lexicographically-smallest human id wins
    and the collision is recorded in the returned stats. (We never crash: see §7.4 conservative
    discipline.)

    Returns ``(new_rules, stats)`` where ``stats`` is
    ``{"rekeyed": <int>, "stale": [...], "collisions": [...]}``. ``rekeyed`` counts distinct
    extracted ids written into ``name_overrides``; ``collisions`` lists
    ``{"extracted_id", "winner", "losers": [...]}`` rows; ``stale`` is reserved for staleness we
    could detect here (currently none — staleness is a facts-relative property resolved by curate).
    """
    raw = copy.deepcopy(rules.raw)
    stats: dict[str, Any] = {"rekeyed": 0, "stale": [], "collisions": []}
    if not confirmed:
        return MappingRules(raw=raw), stats

    rekeyed_ids: set[str] = set()

    # --- human-dict-keyed blocks: rewrite KEYS human -> extracted (collision-aware) ---
    for block_name in _HUMAN_KEYED_BLOCKS:
        block = raw.get(block_name)
        if not isinstance(block, dict):
            continue
        # group human keys by the extracted id they map to, so collisions are visible.
        # Iterate sorted so the chosen winner is deterministic, not dict-order dependent.
        by_extracted: dict[str, list[str]] = {}
        passthrough: dict[str, Any] = {}
        for human_key in sorted(block):
            extracted_id = confirmed.get(human_key)
            if extracted_id is None:
                # not a confirmed binding -> leave the key exactly as-is.
                passthrough[human_key] = block[human_key]
            else:
                by_extracted.setdefault(extracted_id, []).append(human_key)
        new_block: dict[str, Any] = dict(passthrough)
        for extracted_id in sorted(by_extracted):
            human_keys = by_extracted[extracted_id]  # already in sorted order
            winner = human_keys[0]
            if len(human_keys) > 1:
                stats["collisions"].append({
                    "block": block_name,
                    "extracted_id": extracted_id,
                    "winner": winner,
                    "losers": human_keys[1:],
                })
            new_block[extracted_id] = block[winner]
            rekeyed_ids.add(extracted_id)
        raw[block_name] = new_block

    # --- group[].members: rewrite each human member id -> extracted id (order-preserving) ---
    groups = raw.get("group")
    if isinstance(groups, list):
        for g in groups:
            if not isinstance(g, dict):
                continue
            members = g.get("members")
            if not isinstance(members, list):
                continue
            new_members: list[Any] = []
            seen: set[Any] = set()
            for m in members:
                new_m = confirmed.get(m, m) if isinstance(m, str) else m
                if isinstance(m, str) and new_m != m:
                    rekeyed_ids.add(new_m)
                # two human members rewriting to the same extracted id collapse to one entry;
                # de-dupe deterministically (keep first occurrence) so curate sees a clean list.
                if new_m in seen:
                    continue
                seen.add(new_m)
                new_members.append(new_m)
            g["members"] = new_members

    stats["rekeyed"] = len(rekeyed_ids)
    return MappingRules(raw=raw), stats


def curate(facts: dict[str, Any], rules: MappingRules) -> dict[str, Any]:
    facts = _apply_aliases(dict(facts), rules.id_aliases)
    defaults = rules.defaults
    # A user may override min_relationship_weight to null / a non-number in the YAML;
    # coerce defensively so the `weight < min_weight` comparisons can't raise TypeError.
    raw_min = defaults.get("min_relationship_weight", 3)
    try:
        min_weight = int(raw_min) if raw_min is not None else 0
    except (TypeError, ValueError):
        min_weight = 0
    drop_external = defaults["drop_external"]
    down_weight_private = defaults.get("down_weight_private", True)
    drop_private_links = defaults.get("drop_private_links", False)

    component_view_containers = rules.component_view_containers()
    code_view_containers = rules.code_view_containers()
    expand_rules = rules.expand_rules()
    comp_exclude, comp_depth = _component_policy(rules)
    kind_rules = _relationship_kind_rules(rules)
    boundary = _boundary_rules(rules)
    deprecations: list[str] = []
    if component_view_containers:
        deprecations.append(
            "component_view: true is deprecated (drill-down D§6.2) — every multi-target "
            "container drills into its build targets by default; reinterpreted as "
            "expand: [<all members>] for: " + ", ".join(sorted(component_view_containers)))
    if code_view_containers:
        deprecations.append(
            "code_view: \"<ns>\" is deprecated (drill-down D§6.2) — reinterpreted as an "
            "expand promotion with the namespace filter for: "
            + ", ".join(sorted(code_view_containers)))

    # membership map: target id -> (container_id, container_name, rule_index) from groups.
    # Explicit `members:` are exact ids; `members_glob:` patterns (rule order) are the
    # fallback for bulk-curating a large repo (§7.4). The rule_index threads through so each
    # placement can be cited back to its mapping-rules.yaml `group:` entry (T2.6 explain).
    member_to_container, glob_groups, _cid_to_cname = _group_index(rules)

    # optional cosmetic parent tier (container-groups plan §3): container name -> parent
    # label, stamped per-member below like container_name. Advisory warnings only — a
    # parent can never make curation fail.
    parents = rules.parents
    parent_warnings: list[str] = []
    if parents:
        group_cnames = {g.get("container") for g in rules.groups if g.get("container")}
        for label in sorted(set(parents.values())):
            if label in group_cnames:
                parent_warnings.append(
                    f"parent {label!r} is also a container name — legal, but the "
                    f"Structurizr 'Group:{label}' boundary and the container will read "
                    "as the same thing on a diagram")
        by_fold: dict[str, set[str]] = {}
        for label in parents.values():
            by_fold.setdefault(label.casefold(), set()).add(label)
        for fold in sorted(by_fold):
            variants = sorted(by_fold[fold])
            if len(variants) > 1:
                parent_warnings.append(
                    "parent labels " + " / ".join(repr(v) for v in variants)
                    + " differ only by case and will render as SEPARATE boundaries "
                    "(labels are compared exactly — same boundary intended?)")

    kept_targets: list[dict[str, Any]] = []
    kept_ids: set[str] = set()
    # old component id -> surviving id | None, accumulated as each target's namespace tier is
    # curated (exclude/aggregate), so the namespace edges stay in sync (no dangling endpoints).
    component_remap: dict[str, str | None] = {}
    promoted = 0
    for t in facts.get("targets", []):
        if _excluded(t, rules):
            continue
        tid = t["id"]
        # boundary promotion: keep the dep but mark it as an external system / person (§7.4).
        if tid in boundary:
            t = dict(t)
            spec = boundary[tid]
            kind = spec.get("kind", "external")
            t["external"] = True
            tags = set(t.get("tags", []))
            tags.add("external" if kind == "external" else "person")
            t["tags"] = sorted(tags)
            if spec.get("name"):
                t["name"] = spec["name"]
            cname = spec.get("name") or rules.name_overrides.get(tid, t.get("name", tid))
            t["container_id"] = tid
            t["container_name"] = cname
            bdesc = rules.descriptions.get(cname)
            if bdesc:
                t["container_description"] = bdesc
            t["boundary_kind"] = kind
            # T2.6 placement provenance: a `boundary:` rule promoted this dep to a boundary.
            t["grouped_by"] = {"signal": "boundary", "kind": kind}
            kept_targets.append(t)
            kept_ids.add(tid)
            promoted += 1
            continue
        grp = _group_for(tid, t, member_to_container, glob_groups)
        if drop_external and t.get("external") and grp is None:
            # A native interop library (P/Invoke/COM/C++-CLI, §16#15) or a shared contract
            # definition (.proto/IDL, §16#14) is an architecturally significant cross-component
            # SEAM, not droppable third-party noise like a NuGet package — keep it as an
            # external-system boundary so the interop / contract edge renders.
            if (t.get("language") in ("native", "idl") or "native" in t.get("tags", [])
                    or t.get("type") == "contract" or "contract" in t.get("tags", [])):
                t = dict(t)
                t["container_id"] = tid
                t["container_name"] = rules.name_overrides.get(tid, t.get("name", tid))
                t["boundary_kind"] = "external"
                # T2.6: kept as a cross-language seam despite drop_external (§16#14/§16#15).
                t["grouped_by"] = {"signal": "seam", "kind": "external"}
                kept_targets.append(t)
                kept_ids.add(tid)
            continue
        t = dict(t)
        if grp is not None:
            cid, cname, grouped_by = grp
            t["grouped_by"] = grouped_by
            # cosmetic parent boundary, keyed by the RULE's container name (a member-level
            # name_overrides pin below may re-label the container display name, but the
            # parent belongs to the group rule, not the pin — container-groups plan §3.2).
            parent = parents.get(cname)
            if parent:
                t["container_parent"] = parent
        else:
            # singleton container; provisional + needs-curation (the §7.3 gate)
            cid = tid
            cname = rules.name_overrides.get(tid, t.get("name", tid))
            tags = set(t.get("tags", []))
            tags.add("needs-curation")
            t["tags"] = sorted(tags)
            # T2.6: no rule matched — this is itself the explanation for a needs-curation singleton.
            t["grouped_by"] = {"signal": "singleton"}
        # name pin wins as the container display name
        if tid in rules.name_overrides:
            cname = rules.name_overrides[tid]
        t["container_id"] = cid
        t["container_name"] = cname
        # hand-authored container description (§7.4), keyed by display name. Carried onto
        # every member so the §9 generator can render it; a deterministic fallback fills the
        # gap when unset, so a container is never description-less (Structurizr inspection).
        desc = rules.descriptions.get(cname)
        if desc:
            t["container_description"] = desc
        # D§5/D§6 deep-path stamps, carried per-member for the §9 generator:
        # the legacy flags reinterpret (D§6.2) — `component_view: true` meant "the
        # architect wanted depth here", whose closest equivalent is expanding every
        # member; `code_view: "<ns>"` becomes an expand promotion + the class filter.
        if cname in component_view_containers:
            t["expand"] = True
        if cname in code_view_containers:
            t["expand"] = True
            t["code_view"] = code_view_containers[cname]
        # the new explicit `expand:` rules (D§6.1): per-group rows scope to that group's
        # members; top-level rows are global. First matching row wins; its `namespace:`
        # filter scopes the class tier to the matching namespace(s) (code_view's successor).
        for er in expand_rules:
            if er["container"] not in (None, cname):
                continue
            if glob_matches_target(er["pattern"], t):
                t["expand"] = True
                if er["classes"]:
                    t["expand_classes"] = True
                if er.get("namespace"):
                    t["code_view"] = er["namespace"]
                break
        # curate the namespace component tier (§7.4 components: block) — exclude noise
        # namespaces + aggregate to depth so the Component view is readable (§16#10b).
        # Record the id-rewrite BEFORE transforming so the namespace edges below follow it.
        if t.get("components"):
            component_remap.update(_component_remap(t["components"], comp_exclude, comp_depth))
            t["components"] = _transform_components(t["components"], comp_exclude, comp_depth)
        # technology tag by target type
        tech = rules.technology.get(t.get("type", ""))
        if tech:
            t["technology"] = tech
        kept_targets.append(t)
        kept_ids.add(tid)

    # §16#14: rebind a contract extractor's ``dir:<path>`` placeholder endpoint to the real
    # build target that owns that directory (the extractor couldn't resolve the stable id
    # without the build graph; curation has it). Most specific (longest) project path wins.
    path_index = sorted(((t["path"], t["id"]) for t in kept_targets if t.get("path")),
                        key=lambda pi: len(pi[0]), reverse=True)

    def _rebind(endpoint: str) -> str:
        if not endpoint.startswith("dir:"):
            return endpoint
        d = endpoint[4:]
        for path, tid in path_index:
            if d == path or d.startswith(path + "/"):
                return tid
        return endpoint  # unresolved -> dropped by the kept_ids check below

    # the namespace components that survived the §7.4 component-tier curation (post exclude/
    # aggregate) — the authority for keeping a namespace edge's endpoints non-dangling.
    surviving_components = {c["id"] for t in kept_targets for c in (t.get("components") or [])}

    kept_rels: list[dict[str, Any]] = []
    dropped = 0
    renamed_kinds = 0
    for r in facts.get("relationships", []):
        r = dict(r)
        # Component-altitude edges (`…/component:<ns>` endpoints, e.g. the Roslyn
        # namespace→namespace symbol_use edges) live one zoom BELOW the curation grid:
        # they are neither grouped into containers nor rebound/thresholded here. Keep them
        # iff their owning targets survive AND both endpoints survive the component-tier
        # curation: an edge to an EXCLUDED namespace is dropped, an edge into an AGGREGATED
        # namespace is remapped to the survivor (and a now-self edge dropped) — so curated
        # facts never carry a dangling component endpoint (§6 referential-integrity gate).
        # The remap is identity when no `components:` policy is set, so this is a no-op then.
        # The guard requires BOTH endpoints to be component ids: every extractor that emits
        # component edges emits them symmetric (ns→ns), so a half-component edge (one target,
        # one component endpoint) is not expected and falls through to the normal path (and
        # drops, since a component id is never a kept target id).
        if "/component:" in r["source"] and "/component:" in r["target"]:
            owners = (r["source"].split("/component:", 1)[0],
                      r["target"].split("/component:", 1)[0])
            if owners[0] not in kept_ids or owners[1] not in kept_ids:
                dropped += 1
                continue
            src = component_remap.get(r["source"], r["source"])
            tgt = component_remap.get(r["target"], r["target"])
            if (src is None or tgt is None or src == tgt
                    or src not in surviving_components or tgt not in surviving_components):
                dropped += 1
                continue
            r["source"], r["target"] = src, tgt
            kept_rels.append(r)
            continue
        r["source"] = _rebind(r["source"])
        r["target"] = _rebind(r["target"])
        if r["source"] not in kept_ids or r["target"] not in kept_ids:
            dropped += 1
            continue
        declared = r.get("is_declared_dependency", False)
        is_contract = "contract" in r.get("tags", [])
        weight = r.get("weight", 0)

        # relationship_kinds: rename the curated kind by evidence pattern (§7.4).
        new_kind = _evidence_kind_for(r, kind_rules)
        if new_kind and r.get("kind") != new_kind:
            r["kind"] = new_kind
            renamed_kinds += 1

        # down_weight_private: PRIVATE link deps are implementation detail (§6.2/§7.4).
        priv = _private_evidence_count(r) if down_weight_private else 0
        if priv:
            tags = set(r.get("tags", []))
            tags.add("private-link")
            r["tags"] = sorted(tags)
            if not declared:
                if drop_private_links:
                    dropped += 1
                    continue
                # de-emphasize: lower the effective weight used for thresholding.
                weight = max(0, weight - PRIVATE_DOWN_WEIGHT * priv)
            elif drop_private_links:
                # declared deps are never dropped; flag instead (§7.4).
                tags = set(r.get("tags", []))
                tags.add("suspicious-declared")
                r["tags"] = sorted(tags)

        if not declared and not is_contract and weight < min_weight:
            dropped += 1  # weak refine-layer edge (§7.4)
            continue
        # a shared-contract edge (§16#14) is a real cross-component dependency — kept
        # regardless of weight, like a declared dep; it already carries its `contract` tag.
        if declared and weight < min_weight:
            # declared but below threshold: keep + flag (§5 suspicious link)
            tags = set(r.get("tags", []))
            tags.add("suspicious-declared")
            r["tags"] = sorted(tags)
        kept_rels.append(r)

    curated = dict(facts)
    curated["targets"] = kept_targets
    curated["relationships"] = kept_rels
    prov = curated.setdefault("provenance", {})
    prov["edges_dropped"] = dropped
    prov["boundary_promoted"] = promoted
    prov["kinds_renamed"] = renamed_kinds
    if deprecations:
        # D§6.2: legacy-flag reinterpretation is legible, never silent — the CLI surfaces
        # these one-liners so the ~4 overlays get hand-migrated rather than rot.
        prov["deprecations"] = deprecations
    if parent_warnings:
        # container-groups plan §3.3: advisory parent-label hygiene (never a refusal);
        # surfaced by the CLI like deprecations and by the GUI curate-edit preview.
        prov["parent_warnings"] = parent_warnings
    return canonicalize(curated)


def _rationale_block(rules: MappingRules) -> dict[str, Any]:
    raw = rules.raw.get("rationale", {}) or {}
    return raw if isinstance(raw, dict) else {}


def write_proposal(ws: Workspace, curated: dict[str, Any], rules: MappingRules,
                   baseline: dict[str, Any] | None = None) -> None:
    """Rich §7.1/§7.2B curation proposal (readable .md + machine-readable .yaml/.json).

    Renders the headline counts, the container -> members mapping, low-confidence
    groupings, needs-curation targets, suspected renames (if a baseline is supplied,
    §7.3), boundary promotions, dropped-edge accounting, and the ``rationale:`` block.
    """
    targets = curated.get("targets", [])
    rels = curated.get("relationships", [])
    prov = curated.get("provenance", {})

    containers: dict[str, list[str]] = {}
    needs_curation: list[str] = []
    low_confidence_groups: set[str] = set()
    boundary_promoted: list[str] = []
    for t in targets:
        cname = t.get("container_name", t["id"])
        containers.setdefault(cname, []).append(t.get("name", t["id"]))
        tags = t.get("tags", [])
        if "needs-curation" in tags:
            needs_curation.append(t["id"])
            low_confidence_groups.add(cname)
        if t.get("boundary_kind"):
            boundary_promoted.append(f"{t['id']} -> {t['boundary_kind']}")

    suspicious = sorted(r["id"] for r in rels if "suspicious-declared" in r.get("tags", []))
    private_links = sorted(r["id"] for r in rels if "private-link" in r.get("tags", []))
    edges_dropped = prov.get("edges_dropped", 0)
    rationale = _rationale_block(rules)

    # suspected renames: a new id appears AND a prior id disappears (§7.3) — only if a
    # prior extracted baseline is available.
    suspected_renames: list[str] = []
    if baseline:
        old = {t["id"] for t in baseline.get("targets", [])}
        new = {t["id"] for t in targets}
        gone = sorted(old - new)
        added = sorted(new - old)
        if gone and added:
            for a in added:
                suspected_renames.append(f"`{a}` may be a rename of one of: "
                                         + ", ".join(f"`{g}`" for g in gone))

    lines = [
        "# Curation proposal",
        "",
        "## Summary",
        "",
        f"- **{len(targets)} targets -> {len(containers)} containers**",
        f"- **{edges_dropped} edges below threshold dropped** "
        f"(non-declared `weight < {rules.defaults['min_relationship_weight']}`; declared deps never dropped, §7.4)",
        f"- **{len(low_confidence_groups)} low-confidence groupings** flagged",
        f"- **{len(needs_curation)} targets need curation** (provisional singleton containers, §7.3)",
        f"- **{len(boundary_promoted)} targets promoted to boundary** (external system / person, §7.4)",
        f"- **{len(suspicious)} suspicious declared edges** (declared but below threshold / private — §5)",
        "",
        "## Proposed containers (container -> members)",
        "",
    ]
    for cname in sorted(containers):
        flag = " _(needs curation)_" if cname in low_confidence_groups else ""
        lines.append(f"- **{cname}**{flag}: {', '.join(sorted(containers[cname]))}")

    if suspected_renames:
        lines += ["", "## Suspected renames / moves (pin via `id_aliases`, §7.3/§6.3)", ""]
        lines += [f"- {s}" for s in suspected_renames]

    if needs_curation:
        lines += ["", "## Needs curation (assign a group or exclude)", ""]
        lines += [f"- `{i}`" for i in sorted(needs_curation)]

    if boundary_promoted:
        lines += ["", "## Boundary promotions", ""]
        lines += [f"- {b}" for b in sorted(boundary_promoted)]

    if suspicious:
        lines += ["", "## Suspicious declared edges (§5 — kept + tagged, never dropped)", ""]
        lines += [f"- `{i}`" for i in suspicious]

    if private_links:
        lines += ["", "## Private link deps (down-weighted, §6.2/§7.4)", ""]
        lines += [f"- `{i}`" for i in private_links]

    if rationale:
        lines += ["", "## Rationale (§7.4 — explainable curation knowledge)", ""]
        for key in sorted(rationale):
            r = rationale[key]
            if isinstance(r, dict):
                reason = r.get("reason", "")
                by = r.get("decided_by", "")
                date = r.get("date", "")
                conf = r.get("confidence", "")
                meta = ", ".join(p for p in (f"by {by}" if by else "", date, conf) if p)
                lines.append(f"- **{key}**: {reason}" + (f" _({meta})_" if meta else ""))
            else:
                lines.append(f"- **{key}**: {r}")

    ws.curation_proposal_md.parent.mkdir(parents=True, exist_ok=True)
    ws.curation_proposal_md.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")

    machine = {
        "summary": {
            "targets": len(targets),
            "containers": len(containers),
            "edges_dropped": edges_dropped,
            "low_confidence_groupings": len(low_confidence_groups),
            "needs_curation": len(needs_curation),
            "boundary_promoted": len(boundary_promoted),
            "suspicious_declared": len(suspicious),
        },
        "containers": {k: sorted(v) for k, v in containers.items()},
        "needs_curation": sorted(needs_curation),
        "boundary_promoted": sorted(boundary_promoted),
        "suspicious_declared": suspicious,
        "private_links": private_links,
        "suspected_renames": suspected_renames,
        "rationale": rationale,
    }
    # curation-proposal.yaml holds the machine-readable proposal (§12 filename). JSON is a
    # valid YAML subset and goes through jsonio for the LF/canonical determinism guarantee
    # (§7.1) — so the .yaml file parses with any YAML loader and is byte-stable.
    dump_json(machine, ws.curation_proposal_yaml)


def run(ws: Workspace) -> dict[str, Any]:
    facts = load_json(ws.extracted_facts)
    rules = load_mapping_rules(ws.mapping_rules)
    # T2.7: a seeded mapping-rules.yaml is keyed by HUMAN element ids; rewrite those keys to
    # extracted stable ids using ONLY confirmed human_id_bindings (§7.5 gate) BEFORE curating.
    # Guard on `if confirmed:` so a repo with no bindings (e.g. the toy fixture) is a
    # byte-identical no-op — the goldens must stay identical.
    from .. import reconcile
    confirmed = reconcile.confirmed(reconcile.load_bindings(ws.human_id_bindings))
    if confirmed:
        rules, _rekey_stats = rekey_rules(rules, confirmed)
    curated = curate(facts, rules)
    dump_json(curated, ws.curated_facts)
    # the extracted facts double as a same-run baseline for suspected-rename detection
    # only when a *prior* committed baseline differs; here we pass the raw extracted facts
    # so a rename that curation's id_aliases introduced is surfaced (optional, §7.3).
    write_proposal(ws, curated, rules, baseline=facts)
    return curated


if __name__ == "__main__":  # pragma: no cover
    import argparse

    from ..paths import resolve_workspace

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    ap.add_argument("--rules-dir")
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir, args.rules_dir)
    c = run(w)
    print(f"{len(c['targets'])} curated targets")
