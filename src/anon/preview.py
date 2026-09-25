"""``arch run --diff`` — dry-run preview of model + layout changes (plan §9.1.1 / §18.4).

Distinct from ``arch drift`` (code-vs-model, §11.1) and ``arch diff`` (release-to-release,
§11.5): this previews what *this* regeneration changes versus the previously generated
(committed) curated model, and — crucially — separates **content changes** (added/removed
containers & edges) from **layout-affecting changes** (elements entering / moving-between /
leaving a view), because the latter are what cost a hand-layout cleanup (§9.1.1). The
architect reads this before committing so a regeneration never produces "surprise churn".

It works on the curated fact model: each target carries ``container_id``/``container_name``
(§7.4), so a changed ``container_id`` for the same target is a **regroup** — the #1 layout
event (the element moves between Component views, §9.1.1).
"""
from __future__ import annotations

from typing import Any


def _containers(facts: dict[str, Any]) -> dict[str, str]:
    """container_id -> display name (the Container-view membership)."""
    out: dict[str, str] = {}
    for t in facts.get("targets", []):
        cid = t.get("container_id", t["id"])
        out[cid] = t.get("container_name", cid)
    return out


def _target_container(facts: dict[str, Any]) -> dict[str, str]:
    """target id -> its container_id (to detect regroups)."""
    return {t["id"]: t.get("container_id", t["id"]) for t in facts.get("targets", [])}


def _container_edges(facts: dict[str, Any]) -> set[tuple[str, str]]:
    t2c = _target_container(facts)
    edges: set[tuple[str, str]] = set()
    for r in facts.get("relationships", []):
        cs, ct = t2c.get(r["source"]), t2c.get(r["target"])
        if cs and ct and cs != ct:
            edges.add((cs, ct))
    return edges


def compute(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Diff previous vs new curated facts into content + layout-affecting buckets."""
    new_containers = _containers(new)
    if old is None:
        return {"first_run": True,
                "added_containers": sorted(new_containers),
                "removed_containers": [], "added_edges": [], "removed_edges": [],
                "regrouped": [], "entering_views": sorted(new_containers),
                "leaving_views": []}
    old_containers = _containers(old)
    old_tc, new_tc = _target_container(old), _target_container(new)
    regrouped = sorted(
        f"{tid}: {old_tc[tid]} -> {new_tc[tid]}"
        for tid in (set(old_tc) & set(new_tc)) if old_tc[tid] != new_tc[tid]
    )
    old_edges, new_edges = _container_edges(old), _container_edges(new)
    added_c = sorted(set(new_containers) - set(old_containers))
    removed_c = sorted(set(old_containers) - set(new_containers))
    return {
        "first_run": False,
        # content
        "added_containers": added_c,
        "removed_containers": removed_c,
        "added_edges": sorted(new_edges - old_edges),
        "removed_edges": sorted(old_edges - new_edges),
        # layout-affecting (§9.1.1)
        "regrouped": regrouped,
        "entering_views": added_c,         # a new container enters the Container view
        "leaving_views": removed_c,
    }


def render(diff: dict[str, Any]) -> str:
    lines = ["# Dry-run preview (`arch run --diff`, §9.1.1)", ""]
    if diff.get("first_run"):
        lines += ["_No prior generated model — this is the initial layout; "
                  "every element is new._", ""]
    lines += ["## Content changes (added / removed elements & edges)", ""]
    lines += [f"- + container `{c}`" for c in diff["added_containers"]]
    lines += [f"- − container `{c}`" for c in diff["removed_containers"]]
    lines += [f"- + edge `{s}` → `{t}`" for s, t in diff["added_edges"]]
    lines += [f"- − edge `{s}` → `{t}`" for s, t in diff["removed_edges"]]
    if not any(diff[k] for k in ("added_containers", "removed_containers", "added_edges", "removed_edges")):
        lines += ["- _no content changes_"]
    lines += ["", "## Layout-affecting changes (need a light re-layout, §9.1.1)", ""]
    lines += [f"- entering a view: `{c}` (no saved position yet)" for c in diff["entering_views"]]
    lines += [f"- leaving a view: `{c}`" for c in diff["leaving_views"]]
    lines += [f"- regrouped (moves between Component views): {r}" for r in diff["regrouped"]]
    if not any(diff[k] for k in ("entering_views", "leaving_views", "regrouped")):
        lines += ["- _no layout-affecting changes — unchanged elements keep their position_"]
    return "\n".join(lines) + "\n"
