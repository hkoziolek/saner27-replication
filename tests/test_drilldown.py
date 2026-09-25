"""Container drill-down behaviour tests (plan ``2026-06-10-container-component-drilldown.md`` §11).

Covers the D-plan's test matrix: model-shape auto-select + pinning (D§2), Model X
target-components (D§3.1), component-altitude edges incl. the formerly-discarded
intra-container edges and the cross-boundary half-edges (D§4 / the D§12.1 spike),
disconnected-exemption behaviour (D§4.3), the expand/expand_classes deep path (D§5),
slug stability across element roles (D§9), and advisory budgets (D§7.2). All outputs go
to pytest ``tmp_path``; nothing here touches committed fixtures or goldens.
"""
from __future__ import annotations

from pathlib import Path

from anon.config import MappingRules
from anon.ids import SlugRegistry
from anon.jsonio import dump_json
from anon.model import canonicalize
from anon.paths import resolve_workspace
from anon.stages import generate_structurizr as gen

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"


def _target(i: int, container: str | None = None, **extra) -> dict:
    t = {"id": f"csharp:csproj:src/T{i}/T{i}.csproj", "name": f"T{i}", "type": "csproj",
         "language": "csharp", "technology": ".NET assembly"}
    if container:
        t["container_id"] = f"container:{container}"
        t["container_name"] = container.capitalize()
    else:
        t["container_id"] = t["id"]
        t["container_name"] = t["name"]
    t.update(extra)
    return t


def _rel(src: int, dst: int, weight: int = 5) -> dict:
    s = f"csharp:csproj:src/T{src}/T{src}.csproj"
    d = f"csharp:csproj:src/T{dst}/T{dst}.csproj"
    return {"id": f"rel:{s}->{d}", "source": s, "target": d, "kind": None,
            "is_declared_dependency": True, "weight": weight, "confidence": "medium",
            "evidence": [{"type": "project_ref", "detail": f"T{src}->T{dst}"}]}


def _facts(targets: list[dict], rels: list[dict]) -> dict:
    return canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": targets,
        "relationships": rels,
    })


def _write(tmp_path: Path, facts: dict, rules_yaml: str = "") -> object:
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "arch", rules_dir=tmp_path / "rules")
    dump_json(facts, ws.curated_facts)
    dump_json(facts, ws.enriched_facts)
    if rules_yaml:
        ws.mapping_rules.parent.mkdir(parents=True, exist_ok=True)
        ws.mapping_rules.write_text(rules_yaml, encoding="utf-8", newline="")
    return ws


# ---- D§2.3: auto-select + pinning ------------------------------------------------

def test_auto_select_y_at_or_below_threshold_x_above():
    small = _facts([_target(i, "core") for i in range(gen.MODEL_Y_MAX)], [])
    shape, n = gen.decide_model_shape(small, MappingRules(raw={}))
    assert (shape, n) == ("y", gen.MODEL_Y_MAX)
    big = _facts([_target(i, "core") for i in range(gen.MODEL_Y_MAX + 1)], [])
    shape, n = gen.decide_model_shape(big, MappingRules(raw={}))
    assert (shape, n) == ("x", gen.MODEL_Y_MAX + 1)


def test_model_shape_pin_overrides_auto():
    small = _facts([_target(1, "core"), _target(2, "core")], [])
    assert gen.decide_model_shape(small, MappingRules(raw={"model_shape": "x"}))[0] == "x"
    big = _facts([_target(i, "core") for i in range(40)], [])
    assert gen.decide_model_shape(big, MappingRules(raw={"model_shape": "y"}))[0] == "y"
    # an unrecognized value degrades to auto, never fails (D§6.1)
    assert gen.decide_model_shape(small, MappingRules(raw={"model_shape": "zz"}))[0] == "y"


def test_boundary_promotions_do_not_count_toward_n():
    targets = [_target(1, "core"), _target(2, "core"),
               _target(3, external=True, boundary_kind="external")]
    assert gen.decide_model_shape(_facts(targets, []), MappingRules(raw={}))[1] == 2


# ---- D§3.1: Model X target-components ---------------------------------------------

def test_model_x_multi_target_container_materializes_member_components():
    facts = _facts([_target(1, "core"), _target(2, "core"), _target(3, "solo")], [])
    containers = gen.build_containers(facts, shape="x")
    core = containers["container:core"]
    assert [c.cid for c in core.components] == [
        "csharp:csproj:src/T1/T1.csproj", "csharp:csproj:src/T2/T2.csproj"]
    assert all("target" in c.tags for c in core.components)
    assert all(c.description.startswith("Build target T") for c in core.components)
    # singleton resolution (D§3.1): a single-target container has nothing to drill
    assert containers["container:solo"].components == []


def test_model_x_namespace_tier_not_rendered_without_expand():
    t = _target(1, "core", components=[
        {"id": "csharp:csproj:src/T1/T1.csproj/component:Ns", "name": "Ns",
         "level": "component", "source": "namespace"}])
    facts = _facts([t, _target(2, "core")], [])
    containers = gen.build_containers(facts, shape="x")
    cids = [c.cid for c in containers["container:core"].components]
    assert "csharp:csproj:src/T1/T1.csproj/component:Ns" not in cids   # moved to deep path


# ---- D§4: component-altitude edges -------------------------------------------------

def _xfixture():
    """Two-member container `core` (T1,T2), single-target `solo` (T3): T1->T2 intra,
    T1->T3 cross (component side), T3->T2 cross (container side)."""
    targets = [_target(1, "core"), _target(2, "core"), _target(3, "solo")]
    rels = [_rel(1, 2), _rel(1, 3), _rel(3, 2)]
    return _facts(targets, rels)


def _xedges(facts):
    containers = gen.build_containers(facts, shape="x")
    t2c = {t["id"]: (t["id"] if t["id"] in containers else t["container_id"])
           for t in facts["targets"]}
    materialized = {m for c in containers.values() if len(c.members) >= 2 for m in c.members}
    return (containers,
            gen.build_container_edges(facts, t2c),
            gen.build_component_edges(facts, t2c, materialized))


def test_intra_container_edge_unhidden_at_component_altitude():
    _, cedges, kedges = _xedges(_xfixture())
    t1 = "csharp:csproj:src/T1/T1.csproj"
    t2 = "csharp:csproj:src/T2/T2.csproj"
    assert (t1, t2) in kedges                                  # un-hidden (D§4.1)
    assert ("container:core", "container:core") not in cedges  # still no self edge


def test_cross_boundary_edges_emit_half_edges_not_foreign_components():
    """The D§12.1 spike: `include *` never pulls a foreign component into a Component
    view, so cross-container edges materialize as the two component->container halves."""
    _, cedges, kedges = _xedges(_xfixture())
    t1 = "csharp:csproj:src/T1/T1.csproj"
    t2 = "csharp:csproj:src/T2/T2.csproj"
    assert (t1, "container:solo") in kedges          # materialized source half
    assert ("container:solo", t2) in kedges          # materialized destination half
    # container altitude is carried by the explicit container edges, unchanged
    assert ("container:core", "container:solo") in cedges
    assert ("container:solo", "container:core") in cedges


def test_connected_components_lose_disconnected_ignore_leaves_keep_it(tmp_path):
    facts = _xfixture()
    ws = _write(tmp_path, facts, "version: 1\nmodel_shape: x\n")
    gen.run(ws)
    dsl = ws.components_dsl.read_text(encoding="utf-8")
    # T1 and T2 carry component edges -> subject to the inspection (no ignore in their block)
    t1_block = dsl.split('component "T1"')[1].split("}")[0]
    t2_block = dsl.split('component "T2"')[1].split("}")[0]
    assert "disconnected" not in t1_block
    assert "disconnected" not in t2_block


def test_edgeless_component_keeps_disconnected_exemption(tmp_path):
    # T2 has no edges at all -> an evidence-based leaf keeps the exemption (D§4.3)
    facts = _facts([_target(1, "core"), _target(2, "core")], [])
    ws = _write(tmp_path, facts, "version: 1\nmodel_shape: x\n")
    gen.run(ws)
    dsl = ws.components_dsl.read_text(encoding="utf-8")
    t2_block = dsl.split('component "T2"')[1].split("    }")[0]
    assert "disconnected" in t2_block


def test_component_edges_empty_under_model_y():
    facts = _xfixture()
    containers = gen.build_containers(facts, shape="y")
    t2c = {t["id"]: t["id"] for t in facts["targets"]}
    kedges = gen.build_component_edges(facts, t2c, set())
    assert kedges == {}
    # ...because every target edge IS a container edge under Y
    cedges = gen.build_container_edges(facts, t2c)
    assert len(cedges) == 3


# ---- D§4: namespace-tier interior edges (Model Y / expand) -------------------------

def _ns_target(i: int, container: str | None, namespaces: list[str], **extra) -> dict:
    """A C# target carrying a namespace component tier (the Roslyn §6.3 child tier)."""
    base = f"csharp:csproj:src/T{i}/T{i}.csproj"
    comps = [{"id": f"{base}/component:{ns}", "name": ns, "level": "component",
              "source": "namespace", "key": ns} for ns in namespaces]
    return _target(i, container, components=comps, **extra)


def _ns_rel(i: int, src_ns: str, dst_ns: str, weight: int = 4) -> dict:
    """A `…/component:<ns>` namespace->namespace symbol_use edge (RoslynWorker emits these)."""
    base = f"csharp:csproj:src/T{i}/T{i}.csproj"
    s, d = f"{base}/component:{src_ns}", f"{base}/component:{dst_ns}"
    return {"id": f"rel:{s}->{d}", "source": s, "target": d, "kind": None,
            "is_declared_dependency": False, "weight": weight, "confidence": "medium",
            "evidence": [{"type": "symbol_use", "detail": f"{src_ns}->{dst_ns}"}]}


def test_namespace_edges_projected_between_emitted_components():
    facts = _facts([_ns_target(1, None, ["Api", "Core"])], [_ns_rel(1, "Api", "Core")])
    containers = gen.build_containers(facts, shape="y")
    emitted = {comp.cid for c in containers.values() for comp in c.components}
    nedges = gen.build_namespace_edges(facts, emitted)
    base = "csharp:csproj:src/T1/T1.csproj"
    assert list(nedges) == [(f"{base}/component:Api", f"{base}/component:Core")]


def test_namespace_edge_to_unmaterialized_component_is_dropped():
    # an endpoint that is not an emitted component (excluded/aggregated namespace) has no
    # element to attach to -> dropped, never a dangling reference.
    facts = _facts([_ns_target(1, None, ["Api"])], [_ns_rel(1, "Api", "Excluded")])
    containers = gen.build_containers(facts, shape="y")
    emitted = {comp.cid for c in containers.values() for comp in c.components}
    assert gen.build_namespace_edges(facts, emitted) == {}


def test_namespace_projection_ignores_target_altitude_edges():
    # target->target edges (no `/component:`) are never claimed by the namespace projection.
    assert gen.build_namespace_edges(_xfixture(), set()) == {}


def test_namespace_edges_render_in_model_y_and_clear_disconnected(tmp_path):
    facts = _facts([_ns_target(1, None, ["Api", "Core"])], [_ns_rel(1, "Api", "Core")])
    ws = _write(tmp_path, facts, "version: 1\nmodel_shape: y\n")
    res = gen.run(ws)
    assert res["namespace_edges"] == 1
    # the only `->` in the relationships fragment is the namespace edge (no inter-target
    # edges exist), so it renders rather than being discarded as before.
    rels = ws.relationships_dsl.read_text(encoding="utf-8")
    assert rels.count(" -> ") == 1
    # both connected namespace components lose the disconnected-ignore (D§4.3).
    comps = ws.components_dsl.read_text(encoding="utf-8")
    api_block = comps.split('component "Api"')[1].split("    }")[0]
    core_block = comps.split('component "Core"')[1].split("    }")[0]
    assert "disconnected" not in api_block
    assert "disconnected" not in core_block


def test_namespace_edges_byte_identical_across_runs(tmp_path):
    facts = _facts([_ns_target(1, None, ["Api", "Core", "Data"])],
                   [_ns_rel(1, "Api", "Core"), _ns_rel(1, "Core", "Data")])
    out = []
    for sub in ("a", "b"):
        ws = _write(tmp_path / sub, facts, "version: 1\nmodel_shape: y\n")
        gen.run(ws)
        out.append(ws.relationships_dsl.read_text(encoding="utf-8"))
    assert out[0] == out[1]


# ---- D§5: the expand deep path -----------------------------------------------------

def _deep_target() -> dict:
    base = "csharp:csproj:src/T1/T1.csproj"
    return _target(1, "core", expand=True, components=[
        {"id": f"{base}/component:Ns.A", "name": "Ns.A", "level": "component",
         "source": "namespace", "key": "Ns.A",
         "code": [{"id": f"{base}/component:Ns.A/code:Klass", "name": "Klass", "level": "code"}]}])


def test_expand_promotes_target_to_grouped_container(tmp_path):
    facts = _facts([_deep_target(), _target(2, "core"), _target(3, "core")], [_rel(1, 2)])
    ws = _write(tmp_path, facts, "version: 1\nmodel_shape: x\n")
    res = gen.run(ws)
    dsl = ws.components_dsl.read_text(encoding="utf-8")
    # T1 stands alone, nesting its namespace tier; the theme wraps both in a group (D§5)
    assert 'container "T1"' in dsl
    assert 'component "Ns.A"' in dsl
    assert 'group "Core" {' in dsl
    assert 'container "Core"' in dsl              # the remaining members keep the theme
    # the promoted container is drillable -> auto Component view (D§7.1)
    assert res["component_views"] >= 1


def test_expand_classes_surfaces_code_tier():
    facts = _facts([dict(_deep_target(), expand_classes=True), _target(2, "core")], [])
    containers = gen.build_containers(facts, shape="x", promote_expanded=True)
    t1 = containers["csharp:csproj:src/T1/T1.csproj"]
    assert [c.name for c in t1.components] == ["Klass"]        # classes, not namespaces
    assert all("code" in c.tags for c in t1.components)


def test_promotion_only_for_generator_not_facts_consumers():
    """serve/query/confidence call build_containers(facts) without promote_expanded and
    must keep the pure curated container_id grouping (D§0.1#7 / the §2.1 iron rule)."""
    facts = _facts([_deep_target(), _target(2, "core")], [])
    assert sorted(gen.build_containers(facts)) == ["container:core"]
    promoted = gen.build_containers(facts, shape="x", promote_expanded=True)
    assert sorted(promoted) == ["container:core", "csharp:csproj:src/T1/T1.csproj"]


# ---- D§9: slug stability across roles ----------------------------------------------

def test_same_target_id_resolves_to_same_ident_in_any_role():
    tid = "csharp:csproj:src/T1/T1.csproj"
    a = SlugRegistry().assign(tid)      # as a Model-X component / edge endpoint
    b = SlugRegistry().assign(tid)      # as a promoted / Model-Y container
    assert a == b


def test_two_runs_byte_identical_model_x_with_expand(tmp_path):
    facts = _facts([_deep_target(), _target(2, "core"), _target(3, "solo")],
                   [_rel(1, 2), _rel(1, 3), _rel(3, 2)])
    outs = []
    for sub in ("a", "b"):
        ws = _write(tmp_path / sub, facts, "version: 1\nmodel_shape: x\n")
        gen.run(ws)
        outs.append((ws.components_dsl.read_bytes(), ws.relationships_dsl.read_bytes()))
    assert outs[0] == outs[1]


# ---- D§7.2: budgets stay advisory ---------------------------------------------------

def test_over_budget_component_view_is_reported_not_fatal(tmp_path):
    n = gen.COMPONENT_BUDGET + 5
    targets = [_target(i, "big") for i in range(n)]
    facts = _facts(targets, [])
    ws = _write(tmp_path, facts, "version: 1\nmodel_shape: x\n")
    res = gen.run(ws)          # generation SUCCEEDS (no trim, no refusal, D§0.1#3)
    assert any("Component-Big" in o for o in res["over_budget_views"])
    dsl = ws.components_dsl.read_text(encoding="utf-8")
    assert dsl.count("= component") == n           # the full truth is rendered


# ---- scenarios.yaml authoring contract survives the shape projection ----------------

def test_scenario_steps_resolve_curated_names_under_model_y(tmp_path):
    """Hand-authored scenarios.yaml steps address CURATED container names. Under Model Y
    the generator hands the view stages the emitted per-target projection — a
    single-member theme keeps its curated name there (unambiguous alias), so the step
    resolves; a multi-member theme name is genuinely ambiguous at the emitted altitude
    and its step is skipped (honest unresolved — use the build-target id instead)."""
    facts = _facts([_target(1, "front"), _target(2, "core"), _target(3, "core")],
                   [_rel(1, 2), _rel(2, 3)])
    ws = _write(tmp_path, facts)
    ws.scenarios_yaml.parent.mkdir(parents=True, exist_ok=True)
    ws.scenarios_yaml.write_text(
        "scenarios:\n"
        "  - key: order\n"
        "    name: Order flow\n"
        "    scope: system\n"
        "    steps:\n"
        "      - from: \"Front\"\n"          # curated single-member theme name -> resolves
        "        to: \"T2\"\n"
        "        description: \"submit\"\n"
        "  - key: ambiguous\n"
        "    name: Ambiguous theme\n"
        "    scope: system\n"
        "    steps:\n"
        "      - from: \"Core\"\n"           # multi-member theme name -> unresolved, skipped
        "        to: \"T2\"\n"
        "        description: \"never emitted\"\n",
        encoding="utf-8", newline="")
    res = gen.run(ws)
    assert res["model_shape"] == "y"
    assert res["dynamic_views"] == 1
    dyn = ws.dynamic_dsl.read_text(encoding="utf-8")
    assert "Order flow" in dyn and "submit" in dyn
    assert "Ambiguous theme" not in dyn


# ---- D§4.2 spike conclusion frozen as a regression ----------------------------------

def test_relationships_fragment_pins_implied_relationships_off(tmp_path):
    facts = _xfixture()
    ws = _write(tmp_path, facts, "version: 1\nmodel_shape: x\n")
    gen.run(ws)
    rels = ws.relationships_dsl.read_text(encoding="utf-8")
    assert "!impliedRelationships false" in rels
    # the pin precedes every edge, and container edges precede component edges, in the
    # one machine-owned fragment (D§4.2 spike conclusion)
    assert rels.index("!impliedRelationships false") < rels.index(" -> ")
    assert rels.index("core -> solo") < rels.index("t1 -> t2")
