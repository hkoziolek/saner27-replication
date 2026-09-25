"""Engine §7.1 — ``arch whatif``: the what-if / refactoring simulator.

Simulates a structural change on the curated graph — **extract** / **merge** / **move**
/ **cut** — and emits before/after metrics + a fact-model diff. Every result is a
**PROPOSAL for human evaluation, never a verdict** (engine §0.5): the op is applied to
an in-memory copy of the curated facts and only the advisory artifacts
``generated/whatif/<scenario>.{md,json}`` are written — never the facts, the DSL
fragments, or ``rules/``.

The load-bearing moat claim (engine §7.1): extract/merge/move only **re-lift existing
target→target evidence into different container boundaries**, and cut only *removes*
relationships hypothetically — no target or edge is ever invented (moat #1). The
mutated copy goes through :func:`model.canonicalize` (mandatory, before diffing), the
``target_to_container`` map is rebuilt from the mutated grouping, the container graph is
re-lifted with the same two-step lift as the generator, the §3 metrics +
:func:`drift_report.check_layering` are recomputed, and the structural diff comes from
:func:`drift_report.diff_snapshots` rendered with the synthetic labels ``current`` /
``whatif:<op>``. The report also carries the GUI-§5.4 **layout twin** (``layout.before``
/ ``layout.after``): both container views laid out by the §6.9 engine so the canvas
renders the hypothetical picture without computing a single coordinate client-side.

This module also hosts the small metric-helper slice §3 wanted sooner
(:func:`structural_metrics`, :func:`attach_scenarios`): ``arch risk --with-scenarios``
attaches a cut/extract proposal to each cycle/hub finding using the same engine.

Determinism: a pure function of the curated facts + the op parameters; everything
sorted; the default scenario slug is a content hash of the op record (no timestamps).
"""
from __future__ import annotations

import argparse
import hashlib
import re
from typing import Any

from ..config import load_yaml
from ..jsonio import dump_json, dumps_json, load_json
from ..model import canonicalize, content_hash
from ..paths import Workspace, resolve_workspace
from .drift_report import check_layering, diff_snapshots, render_snapshots
from .risk import _degree, _internal, _lift, cycles

WHATIF_SCHEMA = "whatif/1"
OPS = ("extract", "merge", "move", "cut")

PROPOSAL = ("This is a PROPOSAL for human evaluation, never a verdict (engine §0.5/"
            "§7.1). Nothing is applied: the op re-lifts EXISTING target->target "
            "evidence into different container boundaries on an in-memory copy (cut "
            "only removes edges hypothetically); no target or edge is invented "
            "(moat #1), and only generated/whatif/ is written.")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class WhatifError(ValueError):
    """A malformed or unresolvable op (unknown container/target, missing parameter)."""


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").lower()).strip("-") or "x"


def _container_lookup(facts: dict[str, Any]) -> dict[str, str]:
    """container id AND container display name -> container id (id wins on clash)."""
    lookup: dict[str, str] = {}
    for t in sorted(facts.get("targets", []), key=lambda t: t.get("id", "")):
        cid = t.get("container_id", t["id"])
        lookup.setdefault(cid, cid)
        cname = t.get("container_name")
        if cname:
            lookup.setdefault(cname, cid)
    return lookup


def _resolve_container(token: str, lookup: dict[str, str]) -> str:
    cid = lookup.get(token)
    if cid is None:
        known = ", ".join(sorted({c for c in lookup.values()}))
        raise WhatifError(f"'{token}' does not name a container (known: {known})")
    return cid


def _fresh_container_id(name: str, facts: dict[str, Any]) -> str:
    """The hypothetical container id for *name* — refusing a silent merge.

    If a container with the derived ``container:whatif:<slug>`` id already exists in
    the model (a re-curated past proposal, or a genuinely matching name), the op must
    not silently fold the extracted/merged members into it — that would corrupt the
    proposal's before/after semantics. Refuse with a rename hint instead."""
    new_cid = f"container:whatif:{_slug(name)}"
    existing = {t.get("container_id", t["id"]) for t in facts.get("targets", [])}
    if new_cid in existing:
        raise WhatifError(
            f"container id '{new_cid}' already exists in the model — pick a "
            "different --name so the proposal does not silently merge into it")
    return new_cid


# ===================================================================== the four ops

def apply_op(facts: dict[str, Any], op: str, *, members: list[str] | None = None,
             ids: list[str] | None = None, name: str | None = None,
             frm: str | None = None, to: str | None = None,
             ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply *op* to a copy of *facts*; return ``(canonicalized_mutation, op_record)``.

    Only container boundaries change (extract/merge/move) or relationships are
    hypothetically removed (cut) — targets and evidence are never invented or edited
    beyond their grouping fields. Raises :class:`WhatifError` on bad parameters.
    """
    if op not in OPS:
        raise WhatifError(f"unknown op '{op}' (one of: {', '.join(OPS)})")
    out = dict(facts)
    out["targets"] = [dict(t) for t in facts.get("targets", [])]
    out["relationships"] = [dict(r) for r in facts.get("relationships", [])]
    by_id = {t["id"]: t for t in out["targets"]}
    lookup = _container_lookup(facts)
    record: dict[str, Any]

    if op == "extract":
        if not members or not name:
            raise WhatifError("extract needs --members (repeatable) and --name")
        unknown = sorted(m for m in members if m not in by_id)
        if unknown:
            raise WhatifError(f"unknown member target(s): {', '.join(unknown)}")
        new_cid = _fresh_container_id(name, facts)
        for m in sorted(set(members)):
            by_id[m]["container_id"] = new_cid
            by_id[m]["container_name"] = name
            by_id[m]["grouped_by"] = "whatif"
        record = {"op": op, "members": sorted(set(members)), "new_container": new_cid,
                  "name": name}

    elif op == "merge":
        if not ids or len(set(ids)) < 2 or not name:
            raise WhatifError("merge needs --ids (two or more containers) and --name")
        cids = sorted({_resolve_container(i, lookup) for i in ids})
        new_cid = _fresh_container_id(name, facts)
        for t in out["targets"]:
            if t.get("container_id", t["id"]) in cids:
                t["container_id"] = new_cid
                t["container_name"] = name
                t["grouped_by"] = "whatif"
        record = {"op": op, "merged": cids, "new_container": new_cid, "name": name}

    elif op == "move":
        if not members or not to:
            raise WhatifError("move needs --members (repeatable) and --to <container>")
        unknown = sorted(m for m in members if m not in by_id)
        if unknown:
            raise WhatifError(f"unknown member target(s): {', '.join(unknown)}")
        dest = _resolve_container(to, lookup)
        dest_name = next((t.get("container_name") for t in facts.get("targets", [])
                          if t.get("container_id", t["id"]) == dest
                          and t.get("container_name")), dest)
        for m in sorted(set(members)):
            by_id[m]["container_id"] = dest
            by_id[m]["container_name"] = dest_name
            by_id[m]["grouped_by"] = "whatif"
        record = {"op": op, "members": sorted(set(members)), "to": dest}

    else:  # cut
        if not frm or not to:
            raise WhatifError("cut needs --from <container> and --to <container>")
        cf = _resolve_container(frm, lookup)
        ct = _resolve_container(to, lookup)
        t2c = {t["id"]: t.get("container_id", t["id"])
               for t in facts.get("targets", [])}
        keep, removed = [], []
        for r in out["relationships"]:
            if t2c.get(r.get("source")) == cf and t2c.get(r.get("target")) == ct:
                removed.append({"source": r["source"], "target": r["target"]})
            else:
                keep.append(r)
        if not removed:
            raise WhatifError(f"no container edge {cf} -> {ct} exists to cut")
        out["relationships"] = keep
        record = {"op": op, "from": cf, "to": ct,
                  "removed_relationships": sorted(removed,
                                                  key=lambda r: (r["source"],
                                                                 r["target"]))}

    # mandatory before diffing (engine §7.1) — the diff and the metrics must see the
    # same deterministic ordering an on-disk artifact would have.
    return canonicalize(out), record


# ===================================================================== metrics slice

def structural_metrics(facts: dict[str, Any],
                       layering_rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """The §3 metric slice the simulator (and `arch risk --with-scenarios`) recomputes
    on both sides of an op: container/edge counts, cycles, per-container fan-in/out,
    and layering violations (``None`` when there are no rules to check)."""
    containers, _t2c, cedges = _lift(facts)
    internal = _internal(containers)
    cadj: dict[str, list[str]] = {}
    for (s, t) in cedges:
        cadj.setdefault(s, []).append(t)
    cyc = cycles(containers.keys(), cadj)
    in_n, out_n, _w_in, _w_out = _degree(cedges, set(internal))
    metrics: dict[str, Any] = {
        "containers": len(containers),
        "container_edges": len(cedges),
        "total_weight": sum(e["weight"] for e in cedges.values()),
        "cycles": len(cyc),
        "cycle_groups": cyc,
        "fan_in": {cid: len(in_n.get(cid, set())) for cid in sorted(containers)},
        "fan_out": {cid: len(out_n.get(cid, set())) for cid in sorted(containers)},
    }
    if layering_rules and (layering_rules.get("forbidden") or []):
        metrics["layering_violations"] = sum(
            1 for f in check_layering(facts, layering_rules)
            if f["status"] == "VIOLATION")
    else:
        metrics["layering_violations"] = None
    return metrics


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k in ("containers", "container_edges", "total_weight", "cycles"):
        d[k] = after[k] - before[k]
    if before["layering_violations"] is None:
        d["layering_violations"] = None
    else:
        d["layering_violations"] = (after["layering_violations"]
                                    - before["layering_violations"])
    broken = [c for c in before["cycle_groups"] if c not in after["cycle_groups"]]
    introduced = [c for c in after["cycle_groups"] if c not in before["cycle_groups"]]
    d["cycles_broken"] = broken
    d["cycles_introduced"] = introduced
    return d


# ===================================================================== simulation

def _layout_twin(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Engine-emitted layout twins for the GUI §5.4 canvas form: the before and the
    hypothetical after container view, each laid out by the same §6.9 layered engine
    the curated views use — coordinates are computed HERE, never in the client (the
    iron rule, GUI §2.1/§8.1). Both sides are pure auto-layout (no §5.10 pins — a
    hypothetical view is ephemeral, like a §5.6 derived view, and pinning only one
    side would make the pair incomparable)."""
    from ..layout import ENGINE, container_view_graph, layout_graph

    def _side(facts: dict[str, Any]) -> dict[str, Any]:
        nodes, edges = container_view_graph(facts)
        laid = layout_graph(nodes, edges, seed=content_hash(facts))
        return {"nodes": laid["nodes"], "edges": laid["edges"],
                "metrics": laid["metrics"]}

    return {"engine": ENGINE, "before": _side(before), "after": _side(after)}


def simulate(ws: Workspace, facts: dict[str, Any], op: str, *,
             scenario: str | None = None, with_layout: bool = True,
             **params: Any) -> dict[str, Any]:
    """Run one what-if simulation — pure (no writes). Raises :class:`WhatifError`.

    *with_layout* additionally emits the before/after layout twin (additive
    ``layout`` field, still ``whatif/1`` per the additive-fields rule, GUI §7.8);
    :func:`attach_scenarios` passes ``False`` — a risk finding carries only the
    op + delta, never coordinates."""
    before = canonicalize(facts)
    after, record = apply_op(before, op, **params)
    layering_rules = load_yaml(ws.layering_rules)
    m_before = structural_metrics(before, layering_rules)
    m_after = structural_metrics(after, layering_rules)
    slug = _slug(scenario) if scenario else \
        f"{op}-{hashlib.sha256(dumps_json(record).encode('utf-8')).hexdigest()[:12]}"
    report = {
        "schema": WHATIF_SCHEMA,
        "scenario": slug,
        "label": f"whatif:{op}",
        "proposal": PROPOSAL,
        "derived_from_hash": content_hash(before),
        "op": record,
        "before": m_before,
        "after": m_after,
        "delta": _delta(m_before, m_after),
        "diff": diff_snapshots(before, after),
    }
    if with_layout:
        report["layout"] = _layout_twin(before, after)
    return report


def render(report: dict[str, Any]) -> str:
    """Markdown render: the proposal header, the metric movement, the §11.5 diff."""
    b, a, d = report["before"], report["after"], report["delta"]
    op = report["op"]
    import json
    lines = [f"# What-if: `{report['scenario']}`  ({report['label']})", "",
             f"> **PROPOSAL.** {report['proposal']}", "",
             f"- op: `{json.dumps(op, sort_keys=True)}`", "",
             "## Metrics (current → whatif)", ""]

    def _row(label: str, key: str) -> str:
        sign = f"{d[key]:+d}" if d[key] else "±0"
        return f"- {label}: {b[key]} → {a[key]} ({sign})"

    lines += [_row("containers", "containers"),
              _row("container edges", "container_edges"),
              _row("total edge weight", "total_weight"),
              _row("cycles", "cycles")]
    if d["cycles_broken"]:
        lines.append("  - broken: "
                     + "; ".join(" ⇄ ".join(c) for c in d["cycles_broken"]))
    if d["cycles_introduced"]:
        lines.append("  - **introduced**: "
                     + "; ".join(" ⇄ ".join(c) for c in d["cycles_introduced"]))
    if b["layering_violations"] is None:
        lines.append("- layering violations: not evaluated (no layering-rules.yaml)")
    else:
        sign = f"{d['layering_violations']:+d}" if d["layering_violations"] else "±0"
        lines.append(f"- layering violations: {b['layering_violations']} → "
                     f"{a['layering_violations']} ({sign})")
    lines += ["", render_snapshots(report["diff"], "current", report["label"])]
    return "\n".join(lines).rstrip() + "\n"


def run(ws: Workspace, op: str, *, scenario: str | None = None, write: bool = True,
        **params: Any) -> dict[str, Any] | None:
    """The ``arch whatif`` entry point: simulate over the curated facts and write the
    advisory ``generated/whatif/<scenario>.{md,json}`` pair. ``None`` = no fact model."""
    path = ws.curated_facts if ws.curated_facts.exists() else ws.extracted_facts
    if not path.exists():
        return None
    facts = load_json(path)
    report = simulate(ws, facts, op, scenario=scenario, **params)
    if write:
        ws.whatif_dir.mkdir(parents=True, exist_ok=True)
        dump_json(report, ws.whatif_dir / f"{report['scenario']}.json")
        (ws.whatif_dir / f"{report['scenario']}.md").write_text(
            render(report), encoding="utf-8", newline="")
    return report


# ============================================== §3 `--with-scenarios` (risk fusion)

def attach_scenarios(ws: Workspace, facts: dict[str, Any],
                     findings: list[dict[str, Any]]) -> int:
    """Attach a §7.1 improvement-scenario PROPOSAL to each cycle/hub finding, in place.

    - **cycle** → cut the lowest-weight intra-cycle container edge (deterministic
      tie-break by edge id) and report the metric movement.
    - **hub** (≥ 2 members) → extract the member with the highest external fan-in into
      its own container and report the hub's fan-in movement.

    Every attached scenario is labelled a proposal; a finding whose op cannot be built
    (single-member hub, no intra-cycle edge) is left untouched. Returns the number of
    scenarios attached.
    """
    containers, _t2c, cedges = _lift(facts)
    n = 0
    for f in findings:
        try:
            if f["kind"] == "cycle":
                members = set(f["severity_inputs"]["members"])
                intra = sorted(((e["weight"], s, t) for (s, t), e in cedges.items()
                                if s in members and t in members and s != t))
                if not intra:
                    continue
                _w, s, t = intra[0]
                report = simulate(ws, facts, "cut", frm=s, to=t,
                                  with_layout=False)
            elif f["kind"] == "hub":
                cid = f["elements"][0]
                hub_members = sorted(containers[cid].members) \
                    if cid in containers else []
                if len(hub_members) < 2:
                    continue
                member_set = set(hub_members)
                ext_in = {m: 0 for m in hub_members}
                for r in facts.get("relationships", []) or []:
                    if r.get("target") in member_set \
                            and r.get("source") not in member_set:
                        ext_in[r["target"]] += 1
                # highest external fan-in wins; ties break on id (sorted order)
                pick = sorted(hub_members, key=lambda m: (-ext_in[m], m))[0]
                report = simulate(ws, facts, "extract", members=[pick],
                                  name=f"whatif extract {pick.rsplit('/', 1)[-1]}",
                                  with_layout=False)
                report["hub_fan_in"] = {
                    "before": report["before"]["fan_in"].get(cid, 0),
                    "after": report["after"]["fan_in"].get(cid, 0)}
            else:
                continue
        except WhatifError:
            continue
        f["scenario"] = {"op": report["op"], "delta": report["delta"],
                         **({"hub_fan_in": report["hub_fan_in"]}
                            if "hub_fan_in" in report else {}),
                         "note": "PROPOSAL for human evaluation (engine §7.1), "
                                 "never a verdict; nothing is applied"}
        n += 1
    return n


# ===================================================================== CLI

def _main(argv: list[str] | None = None) -> int:  # pragma: no cover
    from .. import force_utf8_stdio
    force_utf8_stdio()   # the §22.3 reconfig the arch CLI applies — needed here too
    ap = argparse.ArgumentParser(prog="anon.stages.whatif", description=__doc__)
    ap.add_argument("op", choices=list(OPS))
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    ap.add_argument("--rules-dir")
    ap.add_argument("--members", action="append")
    ap.add_argument("--ids", action="append")
    ap.add_argument("--name")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--scenario")
    args = ap.parse_args(argv)
    ws = resolve_workspace(args.repo, args.arch_dir, args.rules_dir)
    try:
        report = run(ws, args.op, scenario=args.scenario, members=args.members,
                     ids=args.ids, name=args.name, frm=args.frm, to=args.to)
    except WhatifError as exc:
        print(f"[whatif] {exc}")
        return 2
    if report is None:
        print("[whatif] no fact model — run `arch run` first.")
        return 2
    print(render(report), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
