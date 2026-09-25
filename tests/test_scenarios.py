"""Tests for stages/scenarios.py — dynamic (behavioral) view generation (plan §9.4).

Covers:
  - Happy-path: valid steps emit ``dynamic system "..." "..."`` blocks with correct
    ident -> ident lines in declaration order.
  - Invalid-edge skipping: a step whose (from, to) container pair does NOT exist in the
    curated graph is skipped; skipped_steps > 0 and the step is NOT emitted.
  - No scenarios.yaml (or empty): banner-only file is written; 0 scenarios returned.
  - Determinism: two runs are byte-identical.
  - Scope=container: dynamic view is scoped to a container identifier.
  - Name / target-id resolution: from/to accepts container_name, container_id, or
    build-target id interchangeably.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from anon.ids import SlugRegistry
from anon.paths import resolve_workspace
from anon.stages import scenarios

# ---------------------------------------------------------------------------
# Shared fixture factory
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"


def _make_ws(tmp_path: Path, sub: str = ""):
    base = tmp_path / sub if sub else tmp_path
    return resolve_workspace(TOY, arch_dir=base / "arch", rules_dir=base / "rules")


def _tiny_facts() -> dict:
    """Three containers: WebFrontend -> OrderingService -> NotificationService.

    Also exercises a build-target id -> container_id resolution (web_target_id).
    """
    return {
        "schema_version": "1.0",
        "targets": [
            {
                "id": "csharp:csproj:src/Web/Web.csproj",
                "name": "Web",
                "container_id": "container:webFrontend",
                "container_name": "Web Frontend",
            },
            {
                "id": "csharp:csproj:src/Ordering/Ordering.csproj",
                "name": "Ordering",
                "container_id": "container:orderingService",
                "container_name": "Ordering Service",
            },
            {
                "id": "csharp:csproj:src/Notification/Notification.csproj",
                "name": "Notification",
                "container_id": "container:notificationService",
                "container_name": "Notification Service",
            },
        ],
        "relationships": [
            {
                "id": "rel:web->ordering",
                "source": "csharp:csproj:src/Web/Web.csproj",
                "target": "csharp:csproj:src/Ordering/Ordering.csproj",
                "kind": "calls",
            },
            {
                "id": "rel:ordering->notification",
                "source": "csharp:csproj:src/Ordering/Ordering.csproj",
                "target": "csharp:csproj:src/Notification/Notification.csproj",
                "kind": "notifies",
            },
        ],
    }


def _make_slugs(facts: dict) -> SlugRegistry:
    """Pre-populate a SlugRegistry with all container ids — mirrors what gen.run does."""
    slugs = SlugRegistry()
    seen: set[str] = set()
    for t in facts.get("targets", []):
        cid = t.get("container_id", t["id"])
        if cid not in seen:
            slugs.assign(cid)
            seen.add(cid)
    return slugs


def _write_scenarios(ws, yaml_text: str) -> None:
    ws.scenarios_yaml.parent.mkdir(parents=True, exist_ok=True)
    ws.scenarios_yaml.write_text(yaml_text, encoding="utf-8", newline="")


# ---------------------------------------------------------------------------
# Happy-path: valid scenario emits dynamic block
# ---------------------------------------------------------------------------

def test_valid_scenario_emits_dynamic_block(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: order-intake
    name: "How an order is ingested"
    scope: system
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "Submits order"
      - from: "Ordering Service"
        to: "Notification Service"
        description: "Notifies customer"
""")

    result = scenarios.run(ws, facts, slugs)

    assert result["scenarios"] == 1
    assert result["views"] == ["order-intake"]
    assert result["skipped_steps"] == 0

    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    assert 'dynamic system "order-intake" "How an order is ingested"' in dsl

    # identifiers must follow slug registry assignment
    web_ident = slugs.assign("container:webFrontend")
    ord_ident = slugs.assign("container:orderingService")
    notif_ident = slugs.assign("container:notificationService")

    assert f'{web_ident} -> {ord_ident} "Submits order"' in dsl
    assert f'{ord_ident} -> {notif_ident} "Notifies customer"' in dsl
    assert "autolayout lr" in dsl


def test_steps_emit_in_declaration_order(tmp_path):
    """Steps must appear in the order the architect declared them (principle #3)."""
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: flow
    name: "Test flow"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "Step 1"
      - from: "Ordering Service"
        to: "Notification Service"
        description: "Step 2"
""")

    scenarios.run(ws, facts, slugs)
    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")

    web_ident = slugs.assign("container:webFrontend")
    ord_ident = slugs.assign("container:orderingService")
    notif_ident = slugs.assign("container:notificationService")

    pos1 = dsl.index(f'{web_ident} -> {ord_ident}')
    pos2 = dsl.index(f'{ord_ident} -> {notif_ident}')
    assert pos1 < pos2


# ---------------------------------------------------------------------------
# Resolution: container_id, container_name, build-target id all accepted
# ---------------------------------------------------------------------------

def test_resolve_via_container_id(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: by-cid
    name: "Resolve by container_id"
    steps:
      - from: "container:webFrontend"
        to: "container:orderingService"
        description: "Direct cid"
""")
    result = scenarios.run(ws, facts, slugs)
    assert result["scenarios"] == 1
    assert result["skipped_steps"] == 0


def test_resolve_via_build_target_id(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: by-target
    name: "Resolve by build-target id"
    steps:
      - from: "csharp:csproj:src/Web/Web.csproj"
        to: "csharp:csproj:src/Ordering/Ordering.csproj"
        description: "Via build-target id"
""")
    result = scenarios.run(ws, facts, slugs)
    assert result["scenarios"] == 1
    assert result["skipped_steps"] == 0


# ---------------------------------------------------------------------------
# Invalid edge → step skipped; scenario with zero valid steps → no block emitted
# ---------------------------------------------------------------------------

def test_nonexistent_edge_step_is_skipped(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: mixed
    name: "Mixed valid and invalid"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "Valid step"
      - from: "Ordering Service"
        to: "Web Frontend"
        description: "Non-existent reverse edge — must be skipped"
""")
    result = scenarios.run(ws, facts, slugs)

    # scenario survives (one valid step)
    assert result["scenarios"] == 1
    assert result["skipped_steps"] == 1

    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    web_ident = slugs.assign("container:webFrontend")
    ord_ident = slugs.assign("container:orderingService")

    # valid step IS emitted
    assert f'{web_ident} -> {ord_ident}' in dsl
    # reverse edge is NOT emitted
    assert f'{ord_ident} -> {web_ident}' not in dsl


def test_unresolvable_element_step_is_skipped(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: bad-ref
    name: "Unresolvable element"
    steps:
      - from: "NonExistentContainer"
        to: "Ordering Service"
        description: "Should be skipped"
""")
    result = scenarios.run(ws, facts, slugs)

    # scenario has zero valid steps -> omitted
    assert result["scenarios"] == 0
    assert result["skipped_steps"] == 1


def test_all_steps_invalid_scenario_omitted(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: all-bad
    name: "All steps invalid"
    steps:
      - from: "Ghost A"
        to: "Ghost B"
""")
    result = scenarios.run(ws, facts, slugs)
    assert result["scenarios"] == 0

    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    assert "dynamic " not in dsl


# ---------------------------------------------------------------------------
# No scenarios.yaml → banner-only file, 0 scenarios
# ---------------------------------------------------------------------------

def test_no_scenarios_yaml_writes_banner_only(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    # do NOT write scenarios.yaml → file does not exist
    ws.dynamic_dsl.parent.mkdir(parents=True, exist_ok=True)

    result = scenarios.run(ws, facts, slugs)

    assert result["scenarios"] == 0
    assert result["views"] == []
    assert result["skipped_steps"] == 0

    assert ws.dynamic_dsl.exists()
    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    # must be valid inside views {} — no dynamic blocks
    assert "dynamic " not in dsl
    # banner must be present
    assert "GENERATED BY ANON" in dsl


def test_empty_scenarios_yaml_writes_banner_only(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, "version: 1\nscenarios: []\n")

    result = scenarios.run(ws, facts, slugs)
    assert result["scenarios"] == 0
    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    assert "dynamic " not in dsl


# ---------------------------------------------------------------------------
# Default label falls back to static edge kind when no description given
# ---------------------------------------------------------------------------

def test_default_label_from_edge_kind(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: default-label
    name: "Uses edge kind as label"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        # no description — should fall back to 'calls' (the edge kind in tiny_facts)
""")
    scenarios.run(ws, facts, slugs)
    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    assert '"calls"' in dsl


def test_default_label_uses_fallback_when_no_kind(tmp_path):
    """When no kind is present on the edge, default label is 'Uses'."""
    ws = _make_ws(tmp_path)
    # Strip the kind from the relationship
    facts = _tiny_facts()
    for r in facts["relationships"]:
        r.pop("kind", None)
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: uses-fallback
    name: "Uses fallback label"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
""")
    scenarios.run(ws, facts, slugs)
    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    assert '"Uses"' in dsl


# ---------------------------------------------------------------------------
# Scope=container: dynamic view scoped to a container ident
# ---------------------------------------------------------------------------

def test_scope_container_emits_container_ident(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: drill-down
    name: "Drill-down inside Ordering Service"
    scope: "Ordering Service"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "Kicks off"
""")
    result = scenarios.run(ws, facts, slugs)
    assert result["scenarios"] == 1

    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    ord_ident = slugs.assign("container:orderingService")
    # scope ident replaces 'system'
    assert f'dynamic {ord_ident} "drill-down"' in dsl


# ---------------------------------------------------------------------------
# Determinism: two runs byte-identical
# ---------------------------------------------------------------------------

def test_deterministic_two_runs_byte_identical(tmp_path):
    facts = _tiny_facts()

    yaml_text = """
version: 1
scenarios:
  - key: order-intake
    name: "How an order is ingested"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "Submits order"
      - from: "Ordering Service"
        to: "Notification Service"
        description: "Notifies customer"
"""

    def _run_once(sub: str) -> bytes:
        ws = _make_ws(tmp_path, sub)
        _write_scenarios(ws, yaml_text)
        slugs = _make_slugs(facts)
        scenarios.run(ws, facts, slugs)
        return ws.dynamic_dsl.read_bytes()

    run_a = _run_once("a")
    run_b = _run_once("b")
    assert run_a == run_b


# ---------------------------------------------------------------------------
# Multiple scenarios in one file
# ---------------------------------------------------------------------------

def test_multiple_scenarios_all_emitted(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)

    _write_scenarios(ws, """
version: 1
scenarios:
  - key: flow-a
    name: "Flow A"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "First call"
  - key: flow-b
    name: "Flow B"
    steps:
      - from: "Ordering Service"
        to: "Notification Service"
        description: "Second call"
""")
    result = scenarios.run(ws, facts, slugs)
    assert result["scenarios"] == 2
    assert set(result["views"]) == {"flow-a", "flow-b"}

    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    assert 'dynamic system "flow-a"' in dsl
    assert 'dynamic system "flow-b"' in dsl


# ---------------------------------------------------------------------------
# Engine §6.9(c) — the evidence-validated step-list JSON for the GUI sequence strip
# ---------------------------------------------------------------------------

def test_dynamic_view_json_emitted_and_cited(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: order-intake
    name: "How an order is ingested"
    scope: system
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "Submits order"
""")
    result = scenarios.run(ws, facts, slugs)
    assert result["dynamic_artifacts"] == 1

    from anon.jsonio import load_json
    from anon.model import content_hash
    view = load_json(ws.dynamic_views_dir / "order-intake.json")
    assert view["schema"] == scenarios.DYNAMIC_VIEW_SCHEMA
    assert view["derived_from_hash"] == content_hash(facts)
    assert view["valid_steps"] == 1 and view["flagged_steps"] == 0
    step = view["steps"][0]
    assert step["status"] == "ok"
    assert step["from"] == "container:webFrontend"
    assert step["to"] == "container:orderingService"
    assert step["description"] == "Submits order"
    assert step["cited_edges"][0]["source"] == "csharp:csproj:src/Web/Web.csproj"


def test_dynamic_view_json_flags_invalid_steps_instead_of_dropping(tmp_path):
    """The DSL skips a bad step; the §6.9c JSON keeps it, FLAGGED — honest abstention."""
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: bad-flow
    name: "Bad flow"
    steps:
      - from: "Notification Service"
        to: "Web Frontend"
        description: "No such edge"
      - from: "Nope"
        to: "Web Frontend"
      - from: "Web Frontend"
        to: "Ordering Service"
""")
    scenarios.run(ws, facts, slugs)
    from anon.jsonio import load_json
    view = load_json(ws.dynamic_views_dir / "bad-flow.json")
    assert [s["status"] for s in view["steps"]] == ["unevidenced", "unresolved", "ok"]
    assert view["valid_steps"] == 1 and view["flagged_steps"] == 2
    assert "never invented" in view["steps"][0]["reason"]
    # authored order is preserved — call order is the architect's, never reordered
    assert [s["index"] for s in view["steps"]] == [1, 2, 3]


def test_dynamic_view_json_deterministic_and_swept(tmp_path):
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: flow-a
    name: "Flow A"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
""")
    scenarios.run(ws, facts, slugs)
    a = (ws.dynamic_views_dir / "flow-a.json").read_bytes()
    scenarios.run(ws, facts, slugs)
    assert (ws.dynamic_views_dir / "flow-a.json").read_bytes() == a

    # renaming the scenario sweeps the stale artifact (machine-owned dir)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: flow-b
    name: "Flow B"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
""")
    scenarios.run(ws, facts, slugs)
    assert not (ws.dynamic_views_dir / "flow-a.json").exists()
    assert (ws.dynamic_views_dir / "flow-b.json").exists()

    # deleting scenarios.yaml clears the dir
    ws.scenarios_yaml.unlink()
    scenarios.run(ws, facts, slugs)
    assert list(ws.dynamic_views_dir.glob("*.json")) == []


def test_colliding_scenario_slugs_disambiguate_not_overwrite(tmp_path):
    """'Order Intake' and 'order intake' both sanitize to order-intake — the second
    artifact must not silently overwrite the first (honest engine, never silent)."""
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: "Order Intake"
    name: "First"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
  - key: "order intake"
    name: "Second"
    steps:
      - from: "Ordering Service"
        to: "Notification Service"
""")
    result = scenarios.run(ws, facts, slugs)
    assert result["dynamic_artifacts"] == 2
    files = sorted(p.name for p in ws.dynamic_views_dir.glob("*.json"))
    assert len(files) == 2 and "order-intake.json" in files
    from anon.jsonio import load_json
    names = {load_json(ws.dynamic_views_dir / f)["name"] for f in files}
    assert names == {"First", "Second"}
    # deterministic across reruns
    blobs = [(ws.dynamic_views_dir / f).read_bytes() for f in files]
    scenarios.run(ws, facts, slugs)
    assert [(ws.dynamic_views_dir / f).read_bytes() for f in files] == blobs


# ---------------------------------------------------------------------------
# dynamic-view/2 — runtime-layer validation extension (dynamic-view plan §3.1)
# ---------------------------------------------------------------------------

def _facts_with_runtime() -> dict:
    """tiny_facts + a runtime `calls` edge Web->Catalog that has NO build relationship,
    plus a Catalog->infra runtime hop that must NOT resolve to a container step."""
    facts = _tiny_facts()
    facts["targets"].append({
        "id": "csharp:csproj:src/Catalog/Catalog.csproj",
        "name": "Catalog",
        "container_id": "container:catalogService",
        "container_name": "Catalog Service",
    })
    facts["deployment"] = {
        "edges": [
            {"source": "csharp:csproj:src/Web/Web.csproj",
             "target": "csharp:csproj:src/Catalog/Catalog.csproj",
             "kind": "calls",
             "evidence": [{"type": "runtime",
                           "detail": "AddProject<Projects.WebApp>().WithReference(catalogApi)"}]},
            # workload -> infra: must NOT become a container step (plan §0.3)
            {"source": "csharp:csproj:src/Catalog/Catalog.csproj",
             "target": "infra:queue:eventbus", "kind": "calls",
             "evidence": [{"type": "runtime", "detail": "WithReference(rabbitMq)"}]},
            # runtime_config is not a `calls` edge — must be ignored
            {"source": "csharp:csproj:src/Web/Web.csproj",
             "target": "csharp:csproj:src/Web/Web.csproj", "kind": "runtime_config",
             "evidence": [{"type": "runtime", "detail": "WithEnvironment(\"X\")"}]},
        ],
    }
    return facts


def test_runtime_only_step_is_ok_runtime_dsl_false():
    """A step evidenced ONLY by a runtime `calls` edge is ok / evidence_layer runtime /
    dsl:false, with a real runtime citation and null weight (plan §3.1#1/#2)."""
    facts = _facts_with_runtime()
    view = scenarios.build_dynamic_view(
        {"key": "rt", "name": "Runtime", "steps": [
            {"from": "Web Frontend", "to": "Catalog Service"}]}, facts)
    step = view["steps"][0]
    assert step["status"] == "ok"
    assert step["evidence_layer"] == "runtime"
    assert step["dsl"] is False
    assert "dsl_reason" in step
    assert step["weight"] is None          # runtime edges carry no relationships[] weight
    assert step["cited_edges"]             # real citation, not [] (the §10.1 trust tooltip)
    assert step["cited_edges"][0]["evidence_layer"] == "runtime"
    # description comes from the .WithReference detail, never a bare "Uses" (§1a#2)
    assert "WithReference" in step["description"]
    assert view["valid_steps"] == 1 and view["dsl_steps"] == 0
    scenarios.validate_dynamic_view(view)


def test_build_backed_step_is_dsl_true():
    """A build-graph-backed step is ok / evidence_layer build / dsl:true (plan §3.1#2)."""
    facts = _facts_with_runtime()
    view = scenarios.build_dynamic_view(
        {"key": "bd", "name": "Build", "steps": [
            {"from": "Web Frontend", "to": "Ordering Service"}]}, facts)
    step = view["steps"][0]
    assert step["status"] == "ok"
    assert step["evidence_layer"] == "build"
    assert step["dsl"] is True
    assert step["weight"] == 0 or isinstance(step["weight"], int)
    assert view["valid_steps"] == 1 and view["dsl_steps"] == 1
    scenarios.validate_dynamic_view(view)


def test_build_wins_evidence_layer_tie():
    """When (from,to) is BOTH a build and a runtime edge, build wins the tie — it is the
    stronger structural layer and the step is DSL-emittable (plan §3.1#2)."""
    facts = _tiny_facts()
    # add a runtime calls edge over the SAME pair the build relationship already covers
    facts["deployment"] = {"edges": [
        {"source": "csharp:csproj:src/Web/Web.csproj",
         "target": "csharp:csproj:src/Ordering/Ordering.csproj", "kind": "calls",
         "evidence": [{"type": "runtime", "detail": "WithReference(orderingApi)"}]},
    ]}
    view = scenarios.build_dynamic_view(
        {"key": "tie", "name": "Tie", "steps": [
            {"from": "Web Frontend", "to": "Ordering Service"}]}, facts)
    step = view["steps"][0]
    assert step["evidence_layer"] == "build"
    assert step["dsl"] is True


def test_runtime_infra_hop_is_unevidenced_not_a_container_step():
    """A workload->infra runtime hop cannot name a C4 container, so a step over it is
    unevidenced (the infra endpoint does not resolve to a container, plan §0.3)."""
    facts = _facts_with_runtime()
    view = scenarios.build_dynamic_view(
        {"key": "infra", "name": "Infra", "steps": [
            {"from": "Catalog Service", "to": "infra:queue:eventbus"}]}, facts)
    step = view["steps"][0]
    # the infra id does not resolve to a container -> unresolved
    assert step["status"] == "unresolved"
    scenarios.validate_dynamic_view(view)


def test_emit_dynamic_views_emits_only_dsl_true_with_numbered_placeholders(tmp_path):
    """The DSL emits ONLY ok/dsl:true steps as arrows; every skipped (unevidenced) or
    runtime-only step is a NUMBERED COMMENT PLACEHOLDER so GUI step N == DSL step N
    (the step-number correspondence invariant, plan §11/§3.1#3)."""
    ws = _make_ws(tmp_path)
    facts = _facts_with_runtime()
    slugs = _make_slugs(facts)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: mixed-layers
    name: "Mixed layers"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "build step"
      - from: "Web Frontend"
        to: "Catalog Service"
        description: "runtime step"
      - from: "Ordering Service"
        to: "Web Frontend"
        description: "no such edge"
""")
    result = scenarios.run(ws, facts, slugs)
    assert result["runtime_only_steps"] == 1
    assert result["skipped_steps"] == 1
    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    web = slugs.assign("container:webFrontend")
    ordr = slugs.assign("container:orderingService")
    # build-backed step is a real numbered arrow
    assert f'1: {web} -> {ordr} "build step"' in dsl
    # runtime-only step is a numbered comment placeholder, never a real arrow
    assert "// 2:" in dsl and "runtime-only" in dsl
    # unevidenced step is a numbered comment placeholder
    assert "// 3:" in dsl and "skipped" in dsl
    # the JSON keeps all three with the right status/dsl
    from anon.jsonio import load_json
    view = load_json(ws.dynamic_views_dir / "mixed-layers.json")
    statuses = [(s["status"], s.get("dsl")) for s in view["steps"]]
    assert statuses == [("ok", True), ("ok", False), ("unevidenced", None)]
    assert view["valid_steps"] == 2 and view["dsl_steps"] == 1 and view["flagged_steps"] == 1


def test_parallel_steps_share_order_number(tmp_path):
    """Two build-backed steps from one source declaring the same `order` render with the
    SAME shared step number in DSL (the parallel-band primary form, plan §3.1#5)."""
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    # add a second outbound build edge from Web so the fan-out has two real steps
    facts["relationships"].append({
        "id": "rel:web->notification",
        "source": "csharp:csproj:src/Web/Web.csproj",
        "target": "csharp:csproj:src/Notification/Notification.csproj",
        "kind": "calls",
    })
    slugs = _make_slugs(facts)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: fan-out
    name: "Fan out"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        order: 1
        description: "to ordering"
      - from: "Web Frontend"
        to: "Notification Service"
        order: 1
        description: "to notification"
""")
    scenarios.run(ws, facts, slugs)
    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    web = slugs.assign("container:webFrontend")
    ordr = slugs.assign("container:orderingService")
    notif = slugs.assign("container:notificationService")
    assert f'1: {web} -> {ordr} "to ordering"' in dsl
    assert f'1: {web} -> {notif} "to notification"' in dsl
    from anon.jsonio import load_json
    view = load_json(ws.dynamic_views_dir / "fan-out.json")
    assert [s["order"] for s in view["steps"]] == [1, 1]
    assert [s["index"] for s in view["steps"]] == [1, 2]


def test_existing_build_only_view_gains_additive_fields_only(tmp_path):
    """An existing build-only scenario is unchanged EXCEPT the additive /2 fields
    (evidence_layer/dsl/order/ordering_provenance/dsl_steps) — no /1 field is dropped."""
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: order-intake
    name: "How an order is ingested"
    scope: system
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "Submits order"
""")
    scenarios.run(ws, facts, slugs)
    from anon.jsonio import load_json
    view = load_json(ws.dynamic_views_dir / "order-intake.json")
    # every dynamic-view/1 field preserved
    for f in ("schema", "scenario", "slug", "name", "scope",
              "derived_from_hash", "steps", "valid_steps", "flagged_steps", "note"):
        assert f in view, f
    # /2 view-level additions
    assert view["schema"] == "dynamic-view/2"
    assert view["ordering_provenance"] == "hand-authored"
    assert view["dsl_steps"] == 1
    step = view["steps"][0]
    # /1 step fields preserved
    assert step["status"] == "ok"
    assert step["from"] == "container:webFrontend"
    assert step["description"] == "Submits order"
    assert step["cited_edges"][0]["source"] == "csharp:csproj:src/Web/Web.csproj"
    # /2 step additions
    assert step["evidence_layer"] == "build" and step["dsl"] is True
    assert step["order"] == 1
    scenarios.validate_dynamic_view(view)


def test_ordering_provenance_flows_into_view_and_note():
    """A scenario-level ordering_provenance is carried into the view AND the note, so a
    promoted non-hand-authored ordering never renders as observed (plan §0.4#5)."""
    facts = _tiny_facts()
    view = scenarios.build_dynamic_view(
        {"key": "syn", "name": "Synth", "ordering_provenance": "synthesized",
         "steps": [{"from": "Web Frontend", "to": "Ordering Service"}]}, facts)
    assert view["ordering_provenance"] == "synthesized"
    assert "synthesized" in view["note"]
    scenarios.validate_dynamic_view(view)


def test_runtime_only_view_validates_against_schema():
    """The /2 artifact (with runtime-only + flagged steps) is schema-valid inline."""
    facts = _facts_with_runtime()
    view = scenarios.build_dynamic_view(
        {"key": "all", "name": "All", "steps": [
            {"from": "Web Frontend", "to": "Ordering Service"},   # build
            {"from": "Web Frontend", "to": "Catalog Service"},    # runtime-only
            {"from": "Ghost", "to": "Web Frontend"},              # unresolved
        ]}, facts)
    scenarios.validate_dynamic_view(view)


def test_derived_from_hash_helper_uses_emitted_facts():
    """The derived_from_hash helper stamps content_hash(facts_emitted), not the curated
    hash — the artifact carries the same value (plan §3.2/§0.4#4)."""
    from anon.model import content_hash
    facts = _tiny_facts()
    assert scenarios.derived_from_hash(facts) == content_hash(facts)
    view = scenarios.build_dynamic_view(
        {"key": "h", "name": "H", "steps": [
            {"from": "Web Frontend", "to": "Ordering Service"}]}, facts)
    assert view["derived_from_hash"] == scenarios.derived_from_hash(facts)


def test_runtime_view_byte_identical_across_runs(tmp_path):
    """A runtime-bearing dynamic view JSON is byte-identical across two independent runs."""
    facts = _facts_with_runtime()
    yaml_text = """
version: 1
scenarios:
  - key: rt-flow
    name: "Runtime flow"
    steps:
      - from: "Web Frontend"
        to: "Catalog Service"
      - from: "Web Frontend"
        to: "Ordering Service"
"""

    def _run_once(sub: str) -> bytes:
        ws = _make_ws(tmp_path, sub)
        _write_scenarios(ws, yaml_text)
        slugs = _make_slugs(facts)
        scenarios.run(ws, facts, slugs)
        return (ws.dynamic_views_dir / "rt-flow.json").read_bytes()

    assert _run_once("ra") == _run_once("rb")


# ---------------------------------------------------------------------------
# §0.4#1 — the single shared emission-normalized facts helper
# ---------------------------------------------------------------------------

def _selfcontainer_facts() -> dict:
    """A fact set where every target's container_id == its own id, so NO container lift
    occurs under either model shape (the no-copy identity branch of §0.4#1)."""
    return {
        "targets": [
            {"id": "container:web", "name": "Web",
             "container_id": "container:web", "container_name": "Web"},
            {"id": "container:ord", "name": "Ord",
             "container_id": "container:ord", "container_name": "Ord"},
        ],
        "relationships": [],
    }


def test_emission_normalized_facts_no_lift_returns_same_object():
    """On a fact set with no container lift, the helper returns the SAME object (no
    spurious copy — preserves byte-identity, §0.4#1)."""
    from anon.config import load_mapping_rules
    from anon.stages.generate_structurizr import emission_normalized_facts
    facts = _selfcontainer_facts()
    rules = load_mapping_rules(TOY / "architecture" / "rules" / "mapping-rules.yaml")
    emitted = emission_normalized_facts(facts, rules)
    assert emitted is facts


def test_emission_normalized_facts_lifts_and_is_deterministic():
    """When a Model-Y lift occurs, the helper re-points each target's container_id at the
    EMITTED parent in-memory only (curated facts untouched) and is deterministic (§0.4#1)."""
    from anon.config import load_mapping_rules
    from anon.stages.generate_structurizr import emission_normalized_facts
    facts = _tiny_facts()
    before_ids = [t["container_id"] for t in facts["targets"]]
    rules = load_mapping_rules(TOY / "architecture" / "rules" / "mapping-rules.yaml")
    emitted = emission_normalized_facts(facts, rules)
    # Model Y: each target becomes its own container -> emitted container_id == target id
    assert all(t["container_id"] == t["id"] for t in emitted["targets"])
    # the curated facts on disk/in-memory are untouched (no side effects)
    assert [t["container_id"] for t in facts["targets"]] == before_ids
    # deterministic: a second call yields the same emitted container_ids
    emitted2 = emission_normalized_facts(facts, rules)
    assert ([t["container_id"] for t in emitted["targets"]]
            == [t["container_id"] for t in emitted2["targets"]])


# ---------------------------------------------------------------------------
# Bug fixes: unresolvable-scope skipped count, float-order fallback, fail-soft
# hand-authored validation.
# ---------------------------------------------------------------------------

def test_unresolvable_scope_counts_build_backed_steps_as_skipped(tmp_path):
    """An unresolvable `scope` omits the whole block, so even build-backed ok/dsl:true
    steps that WOULD have rendered are dropped — they must be counted as skipped, not
    silently lost ('flagged, never silently dropped')."""
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    slugs = _make_slugs(facts)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: bad-scope
    name: "Scope does not resolve"
    scope: "No Such Container"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "build-backed — would have emitted"
      - from: "Ordering Service"
        to: "Notification Service"
        description: "build-backed — would have emitted"
      - from: "Ordering Service"
        to: "Web Frontend"
        description: "unevidenced reverse edge"
""")
    result = scenarios.run(ws, facts, slugs)
    # no block emitted (scope unresolvable)
    assert result["scenarios"] == 0
    # ALL three would-be-emitted steps counted as skipped (2 build-backed + 1 unevidenced),
    # not just the unevidenced one — the OLD contract under-counted at 1.
    assert result["skipped_steps"] == 3
    assert result["runtime_only_steps"] == 0
    dsl = ws.dynamic_dsl.read_text(encoding="utf-8")
    assert "dynamic " not in dsl


def test_unresolvable_scope_runtime_only_step_tallied_separately(tmp_path):
    """Under an unresolvable scope a runtime-only ok/dsl:false step is counted in
    runtime_only (as everywhere else), NOT folded into skipped."""
    ws = _make_ws(tmp_path)
    facts = _facts_with_runtime()
    slugs = _make_slugs(facts)
    _write_scenarios(ws, """
version: 1
scenarios:
  - key: bad-scope-rt
    name: "Bad scope with runtime + build + bad steps"
    scope: "No Such Container"
    steps:
      - from: "Web Frontend"
        to: "Ordering Service"
        description: "build-backed"
      - from: "Web Frontend"
        to: "Catalog Service"
        description: "runtime-only"
      - from: "Ordering Service"
        to: "Web Frontend"
        description: "unevidenced"
""")
    result = scenarios.run(ws, facts, slugs)
    assert result["scenarios"] == 0
    # build-backed + unevidenced are skipped (2); runtime-only is tallied separately (1)
    assert result["skipped_steps"] == 2
    assert result["runtime_only_steps"] == 1


def test_float_order_with_fraction_falls_back_to_index():
    """A fractional float `order` (2.5) must NOT silently truncate to 2 and collapse two
    authored bands — it falls back to the sequential index instead."""
    facts = _tiny_facts()
    facts["relationships"].append({
        "id": "rel:web->notification",
        "source": "csharp:csproj:src/Web/Web.csproj",
        "target": "csharp:csproj:src/Notification/Notification.csproj",
        "kind": "calls",
    })
    view = scenarios.build_dynamic_view(
        {"key": "frac", "name": "Frac", "steps": [
            {"from": "Web Frontend", "to": "Ordering Service", "order": 2.5},
            {"from": "Web Frontend", "to": "Notification Service", "order": 3.5},
        ]}, facts)
    # fractional orders fall back to the 1-based index, NOT int(2.5)==int(3.5)==2/3 truncation
    assert [s["order"] for s in view["steps"]] == [1, 2]
    scenarios.validate_dynamic_view(view)


def test_clean_integer_valued_float_order_is_honored():
    """A float that is a whole number (2.0) is a clean integer band and is honored as 2."""
    facts = _tiny_facts()
    view = scenarios.build_dynamic_view(
        {"key": "whole", "name": "Whole", "steps": [
            {"from": "Web Frontend", "to": "Ordering Service", "order": 2.0}]}, facts)
    assert view["steps"][0]["order"] == 2
    scenarios.validate_dynamic_view(view)


def test_explicit_integer_order_is_honored():
    """A clean positive integer `order` is taken verbatim (the common authored case)."""
    facts = _tiny_facts()
    view = scenarios.build_dynamic_view(
        {"key": "int", "name": "Int", "steps": [
            {"from": "Web Frontend", "to": "Ordering Service", "order": 5}]}, facts)
    assert view["steps"][0]["order"] == 5
    scenarios.validate_dynamic_view(view)


@pytest.mark.parametrize("bad_order", ["x", 0, -1, True])
def test_invalid_order_values_fall_back_to_index(bad_order):
    """A non-numeric string, a sub-1 integer, or a bool all fall back to the 1-based index
    so the artifact stays schema-valid (order ≥ 1 integer)."""
    facts = _tiny_facts()
    view = scenarios.build_dynamic_view(
        {"key": "bad", "name": "Bad", "steps": [
            {"from": "Web Frontend", "to": "Ordering Service", "order": bad_order}]}, facts)
    assert view["steps"][0]["order"] == 1
    scenarios.validate_dynamic_view(view)


def test_hand_authored_invalid_view_is_written_fail_soft(tmp_path, caplog):
    """A hand-authored scenario whose view would be schema-invalid (here forced via a
    monkeypatched order: 0) must NOT crash arch run: the artifact is still written and a
    warning is logged (advisory, fail-soft)."""
    import logging as _logging
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()

    # Force a schema-invalid view (order: 0 < minimum 1) by bypassing the coercion: patch
    # build_dynamic_view to stamp an out-of-range order on the otherwise-valid view.
    real_build = scenarios.build_dynamic_view

    def _bad_build(scenario, f):
        view = real_build(scenario, f)
        for s in view["steps"]:
            s["order"] = 0  # schema violation: order minimum is 1
        return view

    import pytest as _pytest
    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(scenarios, "build_dynamic_view", _bad_build)
    try:
        with caplog.at_level(_logging.WARNING, logger="anon.scenarios"):
            n = scenarios._write_dynamic_views(ws, [{
                "key": "bad-view", "name": "Bad view", "steps": [
                    {"from": "Web Frontend", "to": "Ordering Service"}]}], facts)
    finally:
        monkeypatch.undo()

    # still written (fail-soft) ...
    assert n == 1
    assert (ws.dynamic_views_dir / "bad-view.json").exists()
    # ... and a warning naming the scenario was logged
    assert any("bad-view" in r.message for r in caplog.records)


def test_hand_authored_valid_view_logs_no_warning(tmp_path, caplog):
    """A well-formed hand-authored scenario passes inline validation with no warning."""
    import logging as _logging
    ws = _make_ws(tmp_path)
    facts = _tiny_facts()
    with caplog.at_level(_logging.WARNING, logger="anon.scenarios"):
        n = scenarios._write_dynamic_views(ws, [{
            "key": "good-view", "name": "Good view", "steps": [
                {"from": "Web Frontend", "to": "Ordering Service"}]}], facts)
    assert n == 1
    assert (ws.dynamic_views_dir / "good-view.json").exists()
    assert not any(r.levelno >= _logging.WARNING for r in caplog.records)
