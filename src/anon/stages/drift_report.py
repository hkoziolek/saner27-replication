"""Stage 8 — drift detection (plan §11, node 8).

Diffs the *current* code facts against the *committed* model baseline and emits
``generated/architecture-drift.md`` (plan §11.1):

  - New code facts not represented in the model (new targets / new edges).
  - Model elements no longer found in code (removed targets / edges).
  - Suspicious dependencies (declared-but-unused; layering violations).

Hardened for v1 (this module):

  * **Coverage-aware scoping (§11.1/§11.6).** A removed element/edge is real *drift* only
    if the evidence layer that produced it was extracted in BOTH the baseline and the
    current run (layer intersection from ``provenance.coverage``); otherwise it is a
    ``coverage-gap``, not drift — a degraded run must not cry wolf.
  * **ADVISORY downgrade (§11.6 stopping rule).** If this run's L0 first-party coverage
    is below the floor (0.80) OR dropped below the baseline, the report header is stamped
    ADVISORY and every finding is non-gating for that run.
  * **Layering / fitness checks (§11.2).** ``rules/layering-rules.yaml`` declares forbidden
    ``from -> to`` container deps; violations are reported, and a check whose endpoints are
    below required coverage reports ``UNVERIFIED (degraded)`` — it never silently passes.
  * **Release-to-release diff (§11.5).** :func:`diff_snapshots` compares two committed fact
    snapshots (added/removed/changed containers + relationships) in the same markdown
    style — the function the orchestrator wires to ``arch diff``.
  * **Suspected-rename detection (§7.3).** When a new id appears AND a prior id disappears
    in the same diff, surface it as a "suspected rename/move".

Advisory, never auto-applied (§11, §16#7).
"""
from __future__ import annotations

from typing import Any

from ..config import load_yaml
from ..jsonio import load_json
from ..model import index_targets
from ..paths import Workspace

COVERAGE_FLOOR = 0.80  # plan §1.1 metric 3 / §11.6

# Which extraction layer each evidence kind comes from (§4 / §6.2). Used to scope a
# removed edge as drift vs coverage-gap: an edge is only "no longer found" if its
# producing layer was extracted in both runs (§11.1).
#
# `interop` and `runtime` map to their OWN pseudo-layer strings (not L0/L1/L2): these
# layers are toolchain-gated (P/Invoke resolution, dynamic/runtime evidence) and are NOT
# recorded in the per-target `provenance.coverage` map. So `_layer_covered`/`_covered_now`
# will always report them as not-covered-now → a disappeared interop/runtime-only edge is
# classified as a `coverage_gap_edge`, NOT build-graph drift. This is the conservative,
# no-cry-wolf behavior the design wants where coverage is most variable (§11.1/§11.6).
_EVIDENCE_LAYER = {
    "link": "L0", "project_ref": "L0", "package_ref": "L1",
    "include": "L2", "symbol_use": "L2",
    "interop": "interop", "runtime": "runtime",
}


def _id_basename(i: str) -> str:
    """Last path-ish segment of a target id, e.g. the filename in
    ``csharp:csproj:src/Web/Toy.Web.csproj`` -> ``Toy.Web.csproj`` (for rename pairing)."""
    return i.replace("\\", "/").rsplit("/", 1)[-1]


def _edges(facts: dict[str, Any]) -> set[tuple[str, str]]:
    return {(r["source"], r["target"]) for r in facts.get("relationships", [])}


def _rel_index(facts: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["source"], r["target"]): r for r in facts.get("relationships", [])}


def _coverage(facts: dict[str, Any]) -> dict[str, Any]:
    return facts.get("provenance", {}).get("coverage", {}) or {}


def _layer_covered(coverage: dict[str, Any], tid: str, layer: str) -> bool:
    """True if *layer* was extracted (truthy, incl. a partial 'n/m' string) for *tid*."""
    val = (coverage.get(tid, {}) or {}).get(layer)
    if isinstance(val, str):
        if val.strip().lower() == "partial":
            # §4.1/§16#14: a `partial:codegen` target's generated source content was absent
            # during a configure-only run, so this fine-grained layer is NOT reliably
            # extracted. Treat it as not-covered so a removed include/symbol edge on it is a
            # coverage-gap, not phantom drift (the extractor stamps L2/L3 = "partial").
            return False
        head = val.split("/", 1)[0].strip()
        try:
            return int(head) > 0
        except ValueError:
            return bool(val)
    return bool(val)


def _run_covers_layer(facts: dict[str, Any], layer: str) -> bool:
    """True if *any* first-party target in this run has *layer* extracted.

    Used when a target/edge has vanished from the current run (so it has no coverage
    entry of its own): we judge by whether the run as a whole still extracts the layer.
    """
    coverage = _coverage(facts)
    for t in facts.get("targets", []):
        if t.get("external"):
            continue
        if _layer_covered(coverage, t["id"], layer):
            return True
    return False


def _edge_layers(rel: dict[str, Any]) -> set[str]:
    """The extraction layers that could have produced this edge, from its evidence."""
    return {_EVIDENCE_LAYER.get(e.get("type")) for e in rel.get("evidence", [])
            if e.get("type") in _EVIDENCE_LAYER}


def diff_facts(baseline: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """Return the structural diff sections (plan §11.1), coverage-scoped (§11.1/§11.6).

    Removed targets/edges are split into genuine ``removed_*`` (drift) and ``coverage_gap_*``
    when the producing layer was not extracted in both runs.
    """
    cur_targets = index_targets(current)
    cur_edges = _edges(current)
    suspicious = [r for r in current.get("relationships", [])
                  if "suspicious-declared" in r.get("tags", [])]
    # First-party target id sets for the target diff: external (NuGet/system) targets are
    # dependency listings, so a transitive-package set change on a version bump is
    # dependency churn, not architectural code drift (§11.1 excludes externals elsewhere).
    cur_fp = {tid for tid, t in cur_targets.items() if not t.get("external")}
    if baseline is None:
        return {"first_run": True,
                "new_targets": sorted(cur_fp), "removed_targets": [],
                "coverage_gap_targets": [], "new_edges": [], "removed_edges": [],
                "coverage_gap_edges": [], "suspected_renames": [], "suspicious": suspicious}

    base_targets = index_targets(baseline)
    base_edges = _edges(baseline)
    base_rels = _rel_index(baseline)
    base_fp = {tid for tid, t in base_targets.items() if not t.get("external")}

    new_targets = sorted(cur_fp - base_fp)
    gone_targets = sorted(base_fp - cur_fp)
    new_edges = sorted(cur_edges - base_edges)
    gone_edges = sorted(base_edges - cur_edges)

    # --- coverage-aware scoping of disappearances (§11.1) ---
    # A removed *target* was produced by L0 (the build graph). It is real drift only if
    # the current run still extracts L0 (else it's a coverage-gap from a degraded run).
    cur_has_l0 = _run_covers_layer(current, "L0")
    base_cov = _coverage(baseline)
    removed_targets, coverage_gap_targets = [], []
    for tid in gone_targets:
        base_had_l0 = _layer_covered(base_cov, tid, "L0") or not base_cov
        if base_had_l0 and cur_has_l0:
            removed_targets.append(tid)
        else:
            coverage_gap_targets.append(tid)

    # Hoisted out of the edge loop (was redefined per edge and re-scanned all targets per
    # call): cache the current coverage map and per-layer run-level coverage once.
    cur_cov = _coverage(current)
    _run_layer_cache: dict[str, bool] = {}

    def _run_has(layer: str) -> bool:
        if layer not in _run_layer_cache:
            _run_layer_cache[layer] = _run_covers_layer(current, layer)
        return _run_layer_cache[layer]

    def _covered_now(s: str, t: str, layer: str) -> bool:
        # the edge is real drift only if every producing layer is still extracted now for
        # the (still-present) endpoint targets; if an endpoint vanished, fall back to the
        # run-level layer check.
        ok = True
        for endpoint in (s, t):
            if endpoint in cur_targets:
                if not _layer_covered(cur_cov, endpoint, layer):
                    ok = False
            elif not _run_has(layer):
                ok = False
        return ok

    removed_edges, coverage_gap_edges = [], []
    for s, t in gone_edges:
        layers = _edge_layers(base_rels.get((s, t), {})) or {"L0"}
        if all(_covered_now(s, t, layer) for layer in layers):
            removed_edges.append((s, t))
        else:
            coverage_gap_edges.append((s, t))

    # --- suspected rename/move (§7.3): a new id appears AND a prior id disappears ---
    # Pair conservatively to avoid an N×M cartesian explosion of meaningless suggestions:
    # the unambiguous 1-add/1-remove case pairs directly; otherwise pair only ids that
    # share a basename (a move keeps the filename but changes the path).
    suspected_renames = []
    if new_targets and removed_targets:
        if len(new_targets) == 1 and len(removed_targets) == 1:
            suspected_renames = [{"old": removed_targets[0], "new": new_targets[0]}]
        else:
            by_base: dict[str, list[str]] = {}
            for n in new_targets:
                by_base.setdefault(_id_basename(n), []).append(n)
            for old in removed_targets:
                for n in by_base.get(_id_basename(old), []):
                    suspected_renames.append({"old": old, "new": n})

    return {
        "first_run": False,
        "new_targets": new_targets,
        "removed_targets": sorted(removed_targets),
        "coverage_gap_targets": sorted(coverage_gap_targets),
        "new_edges": new_edges,
        "removed_edges": sorted(removed_edges),
        "coverage_gap_edges": sorted(coverage_gap_edges),
        "suspected_renames": suspected_renames,
        "suspicious": suspicious,
    }


# --- §11.2 layering / fitness rules ------------------------------------------------

def _container_of(target: dict[str, Any]) -> str:
    """The curated container a target belongs to (id), falling back to the target id."""
    return target.get("container_id") or target["id"]


def _container_name_of(target: dict[str, Any]) -> str:
    return target.get("container_name") or target.get("name") or target["id"]


def _matches_container(token: str, target: dict[str, Any]) -> bool:
    """A layering-rule token matches a target by container id, container name, or raw id."""
    return token in (_container_of(target), _container_name_of(target), target["id"])


def check_layering(facts: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Evaluate forbidden ``from -> to`` container deps (§11.2).

    ``layering-rules.yaml`` shape (forbidden edges)::

        forbidden:
          - from: "UI"            # matches a container id, container name, or target id
            to:   "Database"
            require_layers: ["L2"]   # optional: coverage required to *verify* this rule

    Returns one finding per forbidden rule: status ``VIOLATION`` (a matching edge exists),
    ``OK`` (no matching edge and coverage adequate), or ``UNVERIFIED (degraded)`` (the
    endpoints' required coverage is missing — absence of evidence is never proof, §11.2).
    """
    targets = index_targets(facts)
    coverage = _coverage(facts)
    forbidden = rules.get("forbidden", []) or []
    findings: list[dict[str, Any]] = []
    for rule in forbidden:
        frm, to = rule.get("from"), rule.get("to")
        if not frm or not to:
            continue
        require_layers = rule.get("require_layers", []) or []
        # find edges whose source matches `from` and target matches `to`
        offending: list[tuple[str, str]] = []
        endpoints_seen: set[str] = set()
        for r in facts.get("relationships", []):
            s_t = targets.get(r["source"])
            t_t = targets.get(r["target"])
            if s_t and t_t and _matches_container(frm, s_t) and _matches_container(to, t_t):
                offending.append((r["source"], r["target"]))
                endpoints_seen.update((r["source"], r["target"]))
        # coverage gate: a check whose endpoints lack required coverage is UNVERIFIED
        relevant = [tid for tid, t in targets.items()
                    if _matches_container(frm, t) or _matches_container(to, t)]
        degraded = bool(require_layers) and any(
            not _layer_covered(coverage, tid, layer)
            for tid in relevant for layer in require_layers)
        if offending:
            status = "VIOLATION"
        elif degraded:
            status = "UNVERIFIED (degraded)"
        else:
            status = "OK"
        findings.append({"from": frm, "to": to, "status": status, "edges": sorted(offending)})
    return findings


# --- §11.6 stopping rules / coverage ----------------------------------------------

def _coverage_l0(facts: dict[str, Any]) -> float:
    """First-party L0 coverage. An EMPTY first-party set is 0.0, not 1.0: when the build-graph
    extractor produced no target at all, nothing is covered and the run must be advisory. The
    former vacuous-truth convention (1.0) let a target added while the extractor was lost go
    unreported and unflagged -- found by the RQ6 combined change-plus-degradation experiment
    (mutate/combined.py, 2026-08-30), the only silent cell in that study."""
    cov = _coverage(facts)
    first_party = [t["id"] for t in facts.get("targets", []) if not t.get("external")]
    if not first_party:
        return 0.0
    have = sum(1 for tid in first_party if (cov.get(tid, {}) or {}).get("L0"))
    return have / len(first_party)


def _dropped_below_baseline(baseline: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    """True if this run's L0 first-party coverage fell below the baseline's (§11.6)."""
    if not baseline:
        return False
    return _coverage_l0(current) < _coverage_l0(baseline)


# --- rendering ---------------------------------------------------------------------

def render(diff: dict[str, Any], commit: str, advisory: bool, coverage: float,
           layering: list[dict[str, Any]] | None = None,
           advisory_reason: str = "") -> str:
    layering = layering or []
    lines = [f"# Architecture drift report  (commit {commit})", ""]
    if advisory:
        lines += [f"> **ADVISORY — {advisory_reason}** "
                  f"(L0 first-party coverage {coverage:.0%}; floor {COVERAGE_FLOOR:.0%}, plan §11.6). "
                  f"Findings are advisory-only and non-gating for this run.", ""]
    if diff.get("first_run"):
        lines += ["_First run — no committed baseline to diff against. "
                  "Commit this model to establish the baseline._", ""]

    lines += ["## New code facts not represented in model"]
    lines += [f"- new target `{t}`" for t in diff["new_targets"]]
    lines += [f"- new edge `{s}` → `{t}`" for s, t in diff["new_edges"]]
    if not diff["new_targets"] and not diff["new_edges"]:
        lines += ["- _none_"]

    lines += ["", "## Model elements no longer found in code"]
    lines += [f"- `{t}`" for t in diff["removed_targets"]]
    lines += [f"- removed edge `{s}` → `{t}`" for s, t in diff["removed_edges"]]
    if not diff["removed_targets"] and not diff["removed_edges"]:
        lines += ["- _none_"]

    if diff.get("coverage_gap_targets") or diff.get("coverage_gap_edges"):
        lines += ["", "## Coverage gaps (NOT drift — producing layer not extracted in both runs, §11.1)"]
        lines += [f"- `{t}` (coverage-gap)" for t in diff.get("coverage_gap_targets", [])]
        lines += [f"- edge `{s}` → `{t}` (coverage-gap)"
                  for s, t in diff.get("coverage_gap_edges", [])]

    if diff.get("suspected_renames"):
        lines += ["", "## Suspected renames / moves (§7.3 — pin via id_aliases)"]
        lines += [f"- `{r['old']}` → `{r['new']}` (suspected rename/move)"
                  for r in diff["suspected_renames"]]

    lines += ["", "## Suspicious dependencies (declared but unused / layering violations)"]
    susp = [f"- `{r['source']}` → `{r['target']}` ({r.get('evidence_strength', '')})"
            for r in diff["suspicious"]]
    lines += susp or ["- _none_"]

    if layering:
        lines += ["", "## Layering / fitness checks (§11.2)"]
        for f in layering:
            mark = {"VIOLATION": "❌", "OK": "✅"}.get(f["status"], "⚠️")
            detail = ""
            if f["status"] == "VIOLATION" and f["edges"]:
                detail = " — " + ", ".join(f"`{s}`→`{t}`" for s, t in f["edges"])
            lines += [f"- {mark} `{f['from']}` -/-> `{f['to']}`: **{f['status']}**{detail}"]

    return "\n".join(lines) + "\n"


# --- §11.5 release-to-release snapshot diff (`arch diff`) ---------------------------

def _containers(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Curated containers keyed by container id (falls back to per-target singletons)."""
    out: dict[str, dict[str, Any]] = {}
    for t in facts.get("targets", []):
        cid = _container_of(t)
        out.setdefault(cid, {"id": cid, "name": _container_name_of(t), "members": []})
        out[cid]["members"].append(t["id"])
    for c in out.values():
        c["members"] = sorted(c["members"])
    return out


def diff_snapshots(facts_a: dict[str, Any], facts_b: dict[str, Any]) -> dict[str, Any]:
    """Release-to-release diff (§11.5): added/removed/changed containers + relationships.

    Matches by stable id (principle #5). A container is *changed* if its display name or
    membership set differs; a relationship is *changed* if its ``kind`` or
    ``is_declared_dependency`` differs. Returns a structured diff; render with
    :func:`render_snapshots`.
    """
    ca, cb = _containers(facts_a), _containers(facts_b)
    added_c = sorted(set(cb) - set(ca))
    removed_c = sorted(set(ca) - set(cb))
    changed_c = []
    for cid in sorted(set(ca) & set(cb)):
        a, b = ca[cid], cb[cid]
        if a["name"] != b["name"] or a["members"] != b["members"]:
            changed_c.append({"id": cid, "from_name": a["name"], "to_name": b["name"],
                              "added_members": sorted(set(b["members"]) - set(a["members"])),
                              "removed_members": sorted(set(a["members"]) - set(b["members"]))})

    ra, rb = _rel_index(facts_a), _rel_index(facts_b)
    added_r = sorted(set(rb) - set(ra))
    removed_r = sorted(set(ra) - set(rb))
    changed_r = []
    for key in sorted(set(ra) & set(rb)):
        a, b = ra[key], rb[key]
        if a.get("kind") != b.get("kind") or \
                a.get("is_declared_dependency") != b.get("is_declared_dependency"):
            changed_r.append({"source": key[0], "target": key[1],
                              "from_kind": a.get("kind"), "to_kind": b.get("kind"),
                              "from_declared": a.get("is_declared_dependency"),
                              "to_declared": b.get("is_declared_dependency")})

    return {"added_containers": added_c, "removed_containers": removed_c,
            "changed_containers": changed_c, "added_relationships": added_r,
            "removed_relationships": removed_r, "changed_relationships": changed_r}


def render_snapshots(snap: dict[str, Any], ref_a: str, ref_b: str) -> str:
    """Markdown for :func:`diff_snapshots`, in the same style as the drift report (§11.5)."""
    lines = [f"# Architecture diff  ({ref_a} → {ref_b})", ""]

    lines += ["## Added containers"]
    lines += [f"- `{c}`" for c in snap["added_containers"]] or ["- _none_"]
    lines += ["", "## Removed containers"]
    lines += [f"- `{c}`" for c in snap["removed_containers"]] or ["- _none_"]
    lines += ["", "## Changed containers"]
    if snap["changed_containers"]:
        for c in snap["changed_containers"]:
            bits = []
            if c["from_name"] != c["to_name"]:
                bits.append(f"name `{c['from_name']}` → `{c['to_name']}`")
            if c["added_members"]:
                bits.append("+members " + ", ".join(f"`{m}`" for m in c["added_members"]))
            if c["removed_members"]:
                bits.append("-members " + ", ".join(f"`{m}`" for m in c["removed_members"]))
            lines += [f"- `{c['id']}`: " + "; ".join(bits)]
    else:
        lines += ["- _none_"]

    lines += ["", "## Added relationships"]
    lines += [f"- `{s}` → `{t}`" for s, t in snap["added_relationships"]] or ["- _none_"]
    lines += ["", "## Removed relationships"]
    lines += [f"- `{s}` → `{t}`" for s, t in snap["removed_relationships"]] or ["- _none_"]
    lines += ["", "## Changed relationships"]
    if snap["changed_relationships"]:
        for r in snap["changed_relationships"]:
            bits = []
            if r["from_kind"] != r["to_kind"]:
                bits.append(f"kind `{r['from_kind']}` → `{r['to_kind']}`")
            if r["from_declared"] != r["to_declared"]:
                bits.append(f"declared {r['from_declared']} → {r['to_declared']}")
            lines += [f"- `{r['source']}` → `{r['target']}`: " + "; ".join(bits)]
    else:
        lines += ["- _none_"]
    return "\n".join(lines) + "\n"


# --- §11.5 baseline reconciliation (adopted hand-crafted model vs code) ------------

def reconcile_baseline(current: dict[str, Any], baseline_elements: list[dict[str, Any]],
                       confirmed_bindings: dict[str, str]) -> dict[str, Any]:
    """Reconcile EXTRACTED code facts against an adopted hand-crafted baseline (§11.5).

    *baseline_elements* are the hand-crafted model's elements (the shape produced by
    ``seed.parse_model(...).elements`` — ``{key, name, ...}``); *confirmed_bindings* is
    ``reconcile.confirmed(...)`` → ``{human_id: extracted_id}``.

    The HARD RULE (§7.5 / §11.6): **no ``confirmed`` binding, no finding.** A hand-crafted
    element with no confirmed binding is classified ``unreconciled`` (never drift), so an
    unconfirmed match can never masquerade as erosion. Findings (all advisory, principle #7):
      - ``model_not_in_code``: a confirmed element whose bound target is absent from code
        (aspirational or stale, §4.5.3);
      - ``code_not_in_model``: a first-party code target no confirmed element covers
        (genuinely new, or simply never drawn);
      - ``unreconciled``: a hand-crafted element with no confirmed binding.
    """
    targets = index_targets(current)
    code_ids = {tid for tid, t in targets.items() if not t.get("external")}
    model_not_in_code: list[dict[str, str]] = []
    unreconciled: list[str] = []
    bound_code: set[str] = set()
    for el in baseline_elements:
        hid = el.get("key") or el.get("id") or el.get("name") or ""
        ext = confirmed_bindings.get(hid)
        if not ext:
            unreconciled.append(hid)
            continue
        if ext in targets:
            bound_code.add(ext)
        else:
            model_not_in_code.append({"human_id": hid, "extracted_id": ext})
    code_not_in_model = sorted(tid for tid in code_ids if tid not in bound_code)
    return {
        "model_not_in_code": sorted(model_not_in_code, key=lambda d: d["human_id"]),
        "code_not_in_model": code_not_in_model,
        "unreconciled": sorted(unreconciled),
    }


def render_reconciliation(recon: dict[str, Any], model_path: str) -> str:
    """Markdown for :func:`reconcile_baseline`, in the drift-report style (§11.5)."""
    lines = [f"# Baseline reconciliation report  (model {model_path})", "",
             "_Advisory, never auto-applied (§11.5 / principle #7). Only `confirmed` "
             "`human_id_bindings` entries produce findings; an unbound/unconfirmed element "
             "is `unreconciled`, never drift (§7.5 / §11.6)._", ""]
    lines += ["## Model says it, code doesn't have it (aspirational / stale, §4.5.3)"]
    lines += [f"- `{d['human_id']}` → bound `{d['extracted_id']}` not found in code"
              for d in recon["model_not_in_code"]] or ["- _none_"]
    lines += ["", "## Code has it, the model never drew it"]
    lines += [f"- `{t}`" for t in recon["code_not_in_model"]] or ["- _none_"]
    lines += ["", "## Unreconciled (no confirmed binding — NOT drift, §7.5/§11.6)"]
    lines += [f"- `{h}`" for h in recon["unreconciled"]] or ["- _none_"]
    return "\n".join(lines) + "\n"


def evaluate(ws: Workspace, baseline_path=None, commit: str = "working",
             *, write: bool = True) -> dict[str, Any]:
    """Run the full drift evaluation and return a structured, gate-able result (§11.2/§11.6).

    Does everything :func:`run` does — loads current/baseline facts, computes the
    coverage-scoped diff, the §11.6 advisory stopping rule, the §11.2 layering findings,
    renders the markdown and writes ``ws.drift_md`` — but returns a dict a caller (e.g. a
    CLI ``--fail-on`` flag) can gate on rather than just the markdown string.

    ``write=False`` skips writing ``ws.drift_md``: `arch serve`'s ``GET /drift`` is a
    read endpoint and must never mutate ``generated/`` (GUI plan §2.2/§7.8).

    Returned keys:
      - ``md``        — the rendered markdown (identical to what :func:`run` returns);
      - ``advisory``  — bool: the §11.6 stopping rule fired (coverage below floor or dropped);
      - ``coverage``  — float: L0 first-party coverage of the current run;
      - ``diff``      — the :func:`diff_facts` dict;
      - ``layering``  — the :func:`check_layering` list;
      - ``violations``— the subset of ``layering`` whose ``status == "VIOLATION"``;
      - ``gating``    — bool: True iff there is ≥1 VIOLATION AND the run is NOT advisory.

    ``gating`` semantics (§11.6): a layering violation is a hard failure only on a
    non-degraded run; when the run is advisory (coverage below floor or dropped), findings
    are non-gating — we never gate on a degraded run.
    """
    current = load_json(ws.curated_facts if ws.curated_facts.exists() else ws.extracted_facts)
    baseline = load_json(baseline_path) if baseline_path else None
    diff = diff_facts(baseline, current)
    coverage = _coverage_l0(current)

    # §11.6 stopping rule: below floor OR dropped below baseline -> ADVISORY, non-gating.
    below_floor = coverage < COVERAGE_FLOOR
    dropped = _dropped_below_baseline(baseline, current)
    advisory = below_floor or dropped
    advisory_reason = ("coverage below floor" if below_floor
                       else "coverage dropped below baseline" if dropped else "")

    layering = check_layering(current, load_yaml(ws.layering_rules))
    violations = [f for f in layering if f["status"] == "VIOLATION"]
    md = render(diff, commit, advisory, coverage, layering, advisory_reason)
    if write:
        ws.drift_md.parent.mkdir(parents=True, exist_ok=True)
        ws.drift_md.write_text(md, encoding="utf-8", newline="")
    return {
        "md": md,
        "advisory": advisory,
        "coverage": coverage,
        "diff": diff,
        "layering": layering,
        "violations": violations,
        "gating": bool(violations) and not advisory,
    }


def run(ws: Workspace, baseline_path=None, commit: str = "working") -> str:
    return evaluate(ws, baseline_path=baseline_path, commit=commit)["md"]


if __name__ == "__main__":  # pragma: no cover
    import argparse

    from ..paths import resolve_workspace

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    ap.add_argument("--baseline")
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir)
    run(w, args.baseline)
    print(f"wrote {w.drift_md}")
