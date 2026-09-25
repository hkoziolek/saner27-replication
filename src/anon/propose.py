"""T1.4 — auto-grouping cold-start / P§7.1 Auto-propose (pure, deterministic).

The cold-start failure this fixes: the very first run of a fresh repo, with no
hand-authored ``rules/mapping-rules.yaml``, curates every target into its own singleton
container and dumps a flat "assign or exclude" list on the architect (§7.3). That
violates principle #9 ("a good first draft → human curation"): there *is* enough signal
in the fact model to propose a real decomposition; the build graph just stays silent on
*grouping* by default.

This module turns that latent signal into a **reviewable DRAFT** ``mapping-rules.yaml``
written to ``generated/proposed-mapping-rules.yaml`` (machine-owned, advisory). It is
**never auto-applied**: the architect reviews it, copies the blocks they want into the
hand-owned ``rules/mapping-rules.yaml``, and locks them. Five signals feed the draft,
each grouping candidate stamped with ``confidence`` (high/medium/low) and ``provenance``
(which signal proposed it):

  0. **grouping-tag** (high, STRONGEST) — a fine-grained source-declared family an extractor
     read straight from source and stamped as a tag: ``module-category:<C>`` (OrchardCore module
     ``Manifest.cs`` Category) or ``plugin-type:<T>`` (a ``<Vendor>.Plugin.<Type>`` namespace).
     One container per tag value, emitted as a ``members_tag:`` rule (the curation tag selector).
     Deliberately EXCLUDES ``sdk-role:*`` — that role (``library`` lumps every shared lib into one
     box) is a good residual catch-all in a hand overlay but a bad AUTO-grouping key, the same
     over-merge failure that makes solution-folder unreliable on flat repos.
  1. **solution-folder** (high) — VS Solution Folders (``slnfolder:<name>`` hint tags,
     logical, ≠ disk). Tags are resolved to **explicit member ids HERE**, because the
     curation matcher's ``members_glob`` matches id/path/name but **not** tags — so a tag-keyed
     group needs the resolution step (``members_tag`` now exists, but solution-folder predates it
     and stays id-resolved for back-compat).
  2. **directory** (medium) — the top 1-2 repo-relative path segments → ``members_glob:``
     patterns (which the matcher *does* support on path).
  3. **namespace** (medium) — the namespace root (first segment of an L2 ``namespaces``
     list), when symbol-level facts are present → ``members_glob:`` on the project path of
     the bucket members.
  4. **dependency-cluster** (low) — pure-Python connected components over the build
     ``relationships`` (treated UNDIRECTED; sorted-neighbour BFS, **no networkx**) → one
     group per non-trivial weakly-connected component.

:func:`build_proposal` reconciles the signals so each target is proposed into **at
most one** container, preferring the highest-confidence signal that claims it
(grouping-tag > solution-folder > directory ≈ namespace > cluster). A target no signal claims is left
out of the draft (it stays a singleton — an honest "we have no grouping evidence" rather
than a confident guess; "wrong is worse than absent").

Determinism is a hard CI gate (§1.1 metric 2): every collection produced here is sorted,
there is no ``set``-iteration order leaking into output, no timestamps, no randomness, no
networkx. The draft is an **additive** artifact, so the existing facts/DSL goldens are
unaffected. :func:`to_proposal_yaml` reuses :func:`seed.to_yaml` (sorted-key block YAML,
LF) so the byte-stability machinery is shared.
"""
from __future__ import annotations

from collections import deque
from typing import Any

import yaml

from . import seed
from .jsonio import load_json
from .paths import Workspace

# Confidence bands per signal (§7.1). Ordered worst→best so build_proposal can rank.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# The hint-tag prefix the build-graph extractor stamps for each VS Solution Folder a
# project sits in (extract_build_graph: `slnfolder:<name>`). Logical, not on-disk.
_SLNFOLDER_PREFIX = "slnfolder:"

# Fine-grained source-declared grouping tags an extractor surfaces (one container per value) — the
# STRONGEST auto-grouping signal. `sdk-role:` is deliberately NOT here: `library` over-merges.
_GROUPING_TAG_PREFIXES = ("module-category:", "plugin-type:")

# Minimum members for a proposed group to be worth emitting — a one-member "group" is just
# a singleton with extra ceremony, so every signal requires ≥2 (§7.1 non-trivial only).
_MIN_GROUP = 2

# Leading banner stamped on every emitted draft so a reader never mistakes the advisory
# draft for the hand-owned, locked rules. LF-terminated lines (the determinism gate).
_BANNER = (
    "# DRAFT auto-proposed grouping (T1.4) — advisory, human-gated.\n"
    "# Review, copy the blocks you want into rules/mapping-rules.yaml, and lock.\n"
    "# NEVER auto-applied.\n"
)


def _first_party_ids(facts: dict[str, Any]) -> list[str]:
    """Sorted ids of non-external targets — the only candidates for grouping (§7.4).

    External boundary targets (NuGet packages, system libs) are never *containers*; they
    are dropped or boundary-promoted by curation, so a proposed group never lists them.
    """
    return sorted(
        t["id"]
        for t in facts.get("targets", [])
        if isinstance(t, dict) and t.get("id") and not t.get("external")
    )


# --------------------------------------------------------------------------- clustering

def connected_components(facts: dict[str, Any]) -> list[list[str]]:
    """Weakly-connected components of the build graph (pure-Python BFS, no networkx).

    Edges from ``relationships`` are treated as **UNDIRECTED** (an A→B link clusters A and
    B regardless of direction). Only first-party (non-external) targets participate — an
    edge to an external package is ignored, so a hub package can't fuse the whole repo into
    one giant component. Returns components as **sorted lists of ids**, the list of
    components sorted by ``(size desc, first id asc)`` so the ordering is fully
    deterministic and independent of relationship/target input order.

    The traversal seeds and visits neighbours in sorted order, so for a fixed edge set the
    component contents are order-independent (the membership of a component never depends on
    traversal order anyway — only the cosmetic seed/visit order, which we pin for clarity).
    """
    first_party = set(_first_party_ids(facts))
    # Build a sorted undirected adjacency map over first-party ids only.
    adj: dict[str, set[str]] = {tid: set() for tid in first_party}
    for r in facts.get("relationships", []):
        if not isinstance(r, dict):
            continue
        s = r.get("source")
        t = r.get("target")
        if s in first_party and t in first_party and s != t:
            adj[s].add(t)
            adj[t].add(s)

    seen: set[str] = set()
    components: list[list[str]] = []
    # Seed in sorted id order for deterministic component discovery order (before re-sort).
    for start in sorted(first_party):
        if start in seen:
            continue
        comp: list[str] = []
        queue: deque[str] = deque([start])
        seen.add(start)
        while queue:
            node = queue.popleft()
            comp.append(node)
            # Visit neighbours in sorted order — pins the BFS frontier order.
            for nb in sorted(adj[node]):
                if nb not in seen:
                    seen.add(nb)
                    queue.append(nb)
        components.append(sorted(comp))

    # Deterministic component order: largest first, ties broken by the (sorted) first id.
    components.sort(key=lambda c: (-len(c), c[0] if c else ""))
    return components


# --------------------------------------------------------------------------- signals

def _index(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in facts.get("targets", [])
            if isinstance(t, dict) and t.get("id")}


def propose_by_solution_folder(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Bucket first-party targets by their ``slnfolder:<name>`` hint tags (confidence high).

    A VS Solution Folder is an explicit, human-authored logical grouping (§4.2) — the
    strongest signal available, hence ``high``. Each tag is resolved to the **explicit
    member ids** of the targets carrying it (NOT a glob), sidestepping that ``members_glob``
    cannot match tags. A project may sit in more than one folder level, so it can appear in
    more than one folder's candidate; build_proposal later assigns it to exactly one.

    Returns one ``{container, members, confidence, provenance}`` row per folder with ≥2
    members, sorted by container name; members are sorted ids.
    """
    index = _index(facts)
    first_party = set(_first_party_ids(facts))
    buckets: dict[str, set[str]] = {}
    for tid in first_party:
        for tag in index[tid].get("tags", []) or []:
            if isinstance(tag, str) and tag.startswith(_SLNFOLDER_PREFIX):
                name = tag[len(_SLNFOLDER_PREFIX):].strip("/").strip()
                if name:
                    buckets.setdefault(name, set()).add(tid)
    out: list[dict[str, Any]] = []
    for name in sorted(buckets):
        members = sorted(buckets[name])
        if len(members) < _MIN_GROUP:
            continue
        out.append({
            "container": name,
            "members": members,
            "confidence": "high",
            "provenance": f"solution-folder:{name}",
        })
    return out


def propose_by_grouping_tag(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Bucket first-party targets by a fine-grained source-declared grouping tag (confidence high).

    The strongest signal: an extractor read a FUNCTIONAL/TYPE family straight from source and
    stamped it as a tag (``module-category:*`` from OrchardCore ``Manifest.cs`` Category,
    ``plugin-type:*`` from a ``<Vendor>.Plugin.<Type>`` namespace). One container per tag value,
    emitted as a ``members_tag:`` rule so the draft binds directly via the curation tag selector
    (no id-resolution dance — unlike solution-folder, which predates ``members_tag``). ``sdk-role:*``
    is excluded by :data:`_GROUPING_TAG_PREFIXES` because ``library`` lumps every shared lib into one
    box — a bad auto-grouping key (good only as a hand-authored residual catch-all).

    Returns one ``{container, members, members_tag, confidence, provenance}`` row per tag value with
    ≥2 members, sorted by container name; members are sorted ids.
    """
    index = _index(facts)
    first_party = set(_first_party_ids(facts))
    buckets: dict[str, set[str]] = {}            # full tag string -> member ids
    for tid in first_party:
        for tag in index[tid].get("tags", []) or []:
            if isinstance(tag, str) and tag.startswith(_GROUPING_TAG_PREFIXES):
                value = tag.split(":", 1)[1].strip()
                if value:
                    buckets.setdefault(tag, set()).add(tid)
    out: list[dict[str, Any]] = []
    for tag in sorted(buckets):
        members = sorted(buckets[tag])
        if len(members) < _MIN_GROUP:
            continue
        out.append({
            "container": tag.split(":", 1)[1].strip(),
            "members": members,
            "members_tag": [tag],
            "confidence": "high",
            "provenance": f"grouping-tag:{tag}",
        })
    return out


def _path_segments(path: str) -> list[str]:
    return [seg for seg in (path or "").replace("\\", "/").split("/") if seg]


def propose_by_directory(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Bucket first-party targets by their top 1-2 repo-relative path segments (medium).

    Directory layout is a decent-but-not-authoritative grouping proxy: most repos co-locate
    a component's projects under a shared folder. We bucket by the deepest of the first TWO
    path segments that still yields a ≥2-member group, preferring the more specific 2-segment
    prefix (``src/Modules``) when it groups enough, else falling back to the 1-segment root
    (``src``). Emits ``members_glob:`` patterns — which the curation matcher DOES support on
    path — so the bucket re-derives the same members at curation time.

    Returns one ``{container, members_glob, confidence, provenance}`` row per chosen prefix,
    sorted by container name. ``members`` (resolved ids) is included for build_proposal's
    one-target-one-container reconciliation but is NOT emitted into the glob-based rule.
    """
    index = _index(facts)
    first_party = sorted(_first_party_ids(facts))

    # bucket id -> sorted member ids, for both 1-seg and 2-seg prefixes.
    one_seg: dict[str, list[str]] = {}
    two_seg: dict[str, list[str]] = {}
    for tid in first_party:
        segs = _path_segments(index[tid].get("path", ""))
        if not segs:
            continue
        one_seg.setdefault(segs[0], []).append(tid)
        if len(segs) >= 2:
            two_seg.setdefault("/".join(segs[:2]), []).append(tid)

    out: list[dict[str, Any]] = []
    claimed: set[str] = set()
    # Prefer the more specific 2-segment prefix first (so members it claims aren't
    # double-counted by a 1-segment bucket), then 1-segment roots for the remainder.
    for prefix in sorted(two_seg):
        members = sorted(m for m in two_seg[prefix] if m not in claimed)
        if len(members) < _MIN_GROUP:
            continue
        claimed.update(members)
        out.append(_dir_group(prefix, members))
    for prefix in sorted(one_seg):
        members = sorted(m for m in one_seg[prefix] if m not in claimed)
        if len(members) < _MIN_GROUP:
            continue
        claimed.update(members)
        out.append(_dir_group(prefix, members))
    out.sort(key=lambda g: g["container"])
    return out


def _dir_group(prefix: str, members: list[str]) -> dict[str, Any]:
    # Container name is the last segment of the prefix (the leaf dir), keeping names short.
    leaf = prefix.split("/")[-1]
    return {
        "container": leaf,
        # match the project PATH (members_glob supports path) — `<prefix>/*` plus the bare
        # prefix itself in case a project sits directly at the prefix dir.
        "members_glob": [f"{prefix}/*", prefix],
        "members": members,
        "confidence": "medium",
        "provenance": f"directory:{prefix}",
    }


def _namespace_root(target: dict[str, Any]) -> str | None:
    """First dot/'::'-separated segment of a target's first namespace (L2 only), or None."""
    nss = target.get("namespaces")
    if not isinstance(nss, list) or not nss:
        return None
    first = sorted(str(n) for n in nss if n)
    if not first:
        return None
    ns = first[0]
    sep = "::" if "::" in ns else "."
    root = ns.split(sep)[0].strip()
    return root or None


def propose_by_namespace(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Bucket first-party targets by their namespace root, when L2 namespaces exist (medium).

    A shared namespace root (``Co.Orders`` and ``Co.Orders.Api`` both root at ``Co``) is a
    symbol-level grouping signal that only appears once the L2 Roslyn/clang facts have run;
    absent ``namespaces`` this proposes nothing (graceful). Emits ``members_glob:`` on the
    member project paths (the matcher supports path globs); ``members`` carries the resolved
    ids for reconciliation.

    Returns one row per namespace root with ≥2 members, sorted by container name.
    """
    index = _index(facts)
    first_party = sorted(_first_party_ids(facts))
    buckets: dict[str, list[str]] = {}
    for tid in first_party:
        root = _namespace_root(index[tid])
        if root:
            buckets.setdefault(root, []).append(tid)

    out: list[dict[str, Any]] = []
    for root in sorted(buckets):
        members = sorted(buckets[root])
        if len(members) < _MIN_GROUP:
            continue
        # Globs over each member's exact project path — deterministic, sorted.
        globs = sorted({index[m].get("path", "") for m in members} - {""})
        out.append({
            "container": root,
            "members_glob": globs,
            "members": members,
            "confidence": "medium",
            "provenance": f"namespace:{root}",
        })
    return out


def _cluster_container_name(members: list[str], index: dict[str, dict[str, Any]]) -> str:
    """A stable display name for a dependency cluster.

    Prefer a common path prefix (the shared leaf dir) or namespace root across the cluster's
    members; failing a shared one, fall back to the first member's short name + "-cluster".
    All inputs are sorted so the chosen name is order-independent.
    """
    # 1) common path-segment prefix across members (deepest shared dir leaf).
    seg_lists = [_path_segments(index[m].get("path", "")) for m in members]
    seg_lists = [s for s in seg_lists if s]
    if seg_lists:
        common: list[str] = []
        for col in zip(*seg_lists):
            if len(set(col)) == 1:
                common.append(col[0])
            else:
                break
        if common:
            return common[-1]
    # 2) shared namespace root.
    roots = {_namespace_root(index[m]) for m in members}
    roots.discard(None)
    if len(roots) == 1:
        only = next(iter(roots))
        if only:
            return only
    # 3) fallback: first member's short name + "-cluster".
    first = index[members[0]]
    short = first.get("name") or members[0].rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    return f"{short}-cluster"


def propose_by_cluster(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """One group per non-trivial connected component of the build graph (confidence low).

    The weakest signal (a dependency cluster is a *consequence* of grouping, not a
    declaration of it), hence ``low`` — but it is the only signal that works on a repo with
    flat directories, no solution folders, and no L2 namespaces. Emits **explicit member
    ids** (the cluster is a closed set of real targets). One row per component with ≥2
    members, sorted by container name.
    """
    index = _index(facts)
    out: list[dict[str, Any]] = []
    for comp in connected_components(facts):
        if len(comp) < _MIN_GROUP:
            continue
        name = _cluster_container_name(comp, index)
        out.append({
            "container": name,
            "members": sorted(comp),
            "confidence": "low",
            "provenance": "dependency-cluster",
        })
    out.sort(key=lambda g: g["container"])
    return out


# --------------------------------------------------------------------------- combine

# Signal precedence (best→worst). build_proposal walks these in order so a target is
# claimed by the strongest signal that mentions it, and once claimed is not re-grouped.
_SIGNAL_ORDER = (
    ("grouping-tag", propose_by_grouping_tag),
    ("solution-folder", propose_by_solution_folder),
    ("directory", propose_by_directory),
    ("namespace", propose_by_namespace),
    ("dependency-cluster", propose_by_cluster),
)


def build_proposal(facts: dict[str, Any]) -> dict[str, Any]:
    """Combine the four signals into ONE best-effort draft (each target in ≤1 container).

    Walks the signals in precedence order (solution-folder > directory > namespace >
    dependency-cluster). For each candidate group, only members NOT already claimed by a
    higher-precedence group are taken; a group that drops below ``_MIN_GROUP`` after that
    pruning is discarded. Two distinct groups that would collapse onto the same container
    name (e.g. a directory leaf equal to a namespace root) are merged deterministically:
    the FIRST (higher-precedence, then sorted-provenance) group keeps the name and absorbs
    the later group's unclaimed members — so a container name is never duplicated.

    Returns a ``mapping-rules.yaml``-shaped dict::

        {"group": [ {container, members?|members_glob?, confidence, provenance}, ... ],
         "rationale": { <container>: {reason, provenance, confidence}, ... }}

    ``group`` rows are sorted by ``(confidence-rank desc, container)`` so the strongest
    proposals read first. Targets claimed by no signal are simply absent (honest
    singletons). Deterministic throughout — sorted everything, no set-order leakage.
    """
    claimed: set[str] = set()
    # container name -> assembled group row (so same-name groups merge instead of clashing).
    groups: dict[str, dict[str, Any]] = {}
    group_order: list[str] = []

    for _signal_name, fn in _SIGNAL_ORDER:
        candidates = fn(facts)
        for cand in candidates:
            orig = set(cand.get("members", []))
            members = sorted(orig - claimed)
            if len(members) < _MIN_GROUP:
                continue
            claimed.update(members)
            cname = cand["container"]
            if cname in groups:
                # merge into the existing (higher-precedence) group: extend members. The
                # existing members_glob no longer EXCLUSIVELY describes the membership, so drop
                # it — emit the explicit (accurate) member list instead.
                existing = groups[cname]
                existing["members"] = sorted(set(existing.get("members", [])) | set(members))
                existing.pop("members_glob", None)
                existing.pop("members_tag", None)
                continue
            row: dict[str, Any] = {
                "container": cname,
                "members": members,
                "confidence": cand["confidence"],
                "provenance": cand["provenance"],
            }
            # Carry the compact members_glob ONLY when it is SAFE — i.e. NO member was pruned to a
            # higher-precedence group (members == the glob's full bucket). Otherwise the glob would
            # re-select a claimed target at curate time (curate matches path/name globs), so fall
            # back to the explicit, accurate member list.
            if cand.get("members_glob") and len(members) == len(orig):
                row["members_glob"] = sorted(set(cand["members_glob"]))
            # Same safety for members_tag: carry it only when no member was pruned to a
            # higher-precedence group (grouping-tag is precedence-0, so this always holds for it).
            if cand.get("members_tag") and len(members) == len(orig):
                row["members_tag"] = sorted(set(cand["members_tag"]))
            groups[cname] = row
            group_order.append(cname)

    group_rows = [groups[c] for c in group_order]
    # strongest proposals first: confidence desc, then container name asc.
    group_rows.sort(key=lambda g: (-_CONFIDENCE_RANK.get(g["confidence"], 0), g["container"]))

    rationale = {
        g["container"]: {
            "reason": f"auto-proposed by {g['provenance']} ({len(g['members'])} targets)",
            "provenance": g["provenance"],
            "confidence": g["confidence"],
        }
        for g in group_rows
    }
    return {"group": group_rows, "rationale": rationale}


# --------------------------------------------------------------------------- render / run

def to_proposal_yaml(proposal: dict[str, Any]) -> str:
    """Render a proposal dict as a banner-prefixed, sorted-key block YAML draft (LF).

    Reuses :func:`seed.to_yaml` (the shared sorted-key/LF determinism machinery) for the
    body so the draft is byte-stable, then prepends the advisory banner. An empty proposal
    still yields a valid (banner-only-plus-empty-blocks) document — never a crash.
    """
    body = seed.to_yaml(proposal)
    text = _BANNER + body
    # Defensive LF normalization (seed.to_yaml already does this; keep the invariant local).
    return text.replace("\r\n", "\n").replace("\r", "\n")


def run(ws: Workspace) -> dict[str, Any]:
    """Build the draft proposal and write it to ``ws.proposed_mapping_rules`` (advisory).

    Loads curated facts when present (they carry curation's refinements) else the raw
    extracted facts; both are valid inputs. ALWAYS writes a draft — even on empty/absent
    facts it emits a valid banner-only document (graceful degradation, never a crash) and
    never touches the curated/extracted fact files (the draft is additive). Returns a
    summary ``{"groups": N, "targets_grouped": M, "signals": {provenance: count, ...}}``.
    """
    facts: dict[str, Any] = {}
    for path in (ws.curated_facts, ws.extracted_facts):
        try:
            if path.exists():
                loaded = load_json(path)
                if isinstance(loaded, dict):
                    facts = loaded
                    break
        except (OSError, ValueError):
            # a malformed/half-written facts file degrades to an empty draft, not a crash.
            continue

    proposal = build_proposal(facts)
    yaml_text = ws.proposed_mapping_rules
    yaml_text.parent.mkdir(parents=True, exist_ok=True)
    yaml_text.write_text(to_proposal_yaml(proposal), encoding="utf-8", newline="")

    groups = proposal.get("group", [])
    signals: dict[str, int] = {}
    grouped: set[str] = set()
    for g in groups:
        signals[g["provenance"]] = signals.get(g["provenance"], 0) + 1
        grouped.update(g.get("members", []))
    return {
        "groups": len(groups),
        "targets_grouped": len(grouped),
        "signals": dict(sorted(signals.items())),
    }


# --------------------------------------------------------------------------- accept (T1.4)

def _group_block_end(lines: list[str], start: int) -> int:
    """Index one past the last line of the top-level ``group:`` block starting at
    *start* — the next zero-indent, non-comment, non-blank line ends it. Trailing
    blank/comment runs before that boundary belong to the NEXT section, so back up
    over them (keeps inserted items adjacent to the last existing item)."""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.startswith((" ", "\t", "#", "-")):
            end = i
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def accept(ws: Workspace, group_names: list[str]) -> dict[str, Any]:
    """The T1.4 one-click acceptance: copy the selected DRAFT groups from
    ``generated/proposed-mapping-rules.yaml`` into the hand-owned
    ``rules/mapping-rules.yaml`` (GUI §7.4 `POST /rules/accept-proposal`).

    The hand file is reviewed and commented, so it is NEVER round-tripped through a
    YAML re-dump: the accepted rows are rendered as YAML and **textually spliced**
    into the existing top-level ``group:`` block (or a new block is appended),
    preserving every existing byte. The spliced result is re-parsed before being
    returned — an invalid splice raises rather than corrupting the reviewed file.

    Raises ``KeyError`` for an unknown draft name, ``ValueError`` when the container
    already exists in the rules (acceptance is additive, never an overwrite) or when
    no proposal file exists. Returns ``{old_text, new_text, accepted}`` — the caller
    writes the file and renders the diff (the §2.3 one-reviewed-file-plus-diff shape).
    """
    if not ws.proposed_mapping_rules.exists():
        raise ValueError("no proposal draft — run `arch propose` first (T1.4)")
    proposal = yaml.safe_load(ws.proposed_mapping_rules.read_text(encoding="utf-8")) or {}
    drafts = {g.get("container"): g for g in proposal.get("group", []) or []}
    missing = sorted(set(group_names) - set(drafts))
    if missing:
        raise KeyError(f"no draft group(s) named: {', '.join(missing)}")

    old_text = ws.mapping_rules.read_text(encoding="utf-8") \
        if ws.mapping_rules.exists() else ""
    current = yaml.safe_load(old_text) or {}
    existing = {g.get("container") for g in current.get("group", []) or []}
    clashes = sorted(set(group_names) & existing)
    if clashes:
        raise ValueError(f"container(s) already in mapping-rules.yaml: "
                         f"{', '.join(clashes)} — acceptance is additive (T1.4)")

    # Render ONLY the accepted rows (curation keys only — confidence/provenance stay in
    # the draft; `rationale` is the human's to write, never auto-copied).
    rows = []
    for name in sorted(set(group_names)):
        draft = drafts[name]
        row = {"container": draft["container"]}
        if draft.get("members_glob"):
            row["members_glob"] = draft["members_glob"]
        else:
            row["members"] = draft.get("members", [])
        rows.append(row)
    rendered = yaml.safe_dump(rows, sort_keys=True, default_flow_style=False,
                              allow_unicode=True)
    item_lines = ["  " + ln if ln.strip() else ln
                  for ln in rendered.rstrip("\n").split("\n")]

    lines = old_text.split("\n") if old_text else []
    group_at = next((i for i, ln in enumerate(lines) if ln.rstrip() == "group:"), None)
    if group_at is None:
        body = (old_text.rstrip("\n") + "\n\n" if old_text.strip() else "")
        new_text = body + "group:\n" + "\n".join(item_lines) + "\n"
    else:
        end = _group_block_end(lines, group_at)
        new_lines = lines[:end] + item_lines + lines[end:]
        new_text = "\n".join(new_lines)
        if not new_text.endswith("\n"):
            new_text += "\n"

    merged = yaml.safe_load(new_text)  # fail-closed: never write an unparsable splice
    got = {g.get("container") for g in (merged or {}).get("group", []) or []}
    if not set(group_names) <= got:
        raise ValueError("splice failed to land the accepted groups — refusing to write")
    return {"old_text": old_text, "new_text": new_text,
            "accepted": sorted(set(group_names))}
