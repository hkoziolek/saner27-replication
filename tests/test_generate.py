"""Generator tests (plan §9) — Agent C vertical.

Covers slug stability, intra-container edge dropping + parallel merge, Component-view
emission, element/relationship tagging (§9.2), the deterministic naming reconciler
(§8.3 precedence), and element-budget over-limit detection (§9.1/§1.1#1). All outputs go
to pytest ``tmp_path``.
"""
from __future__ import annotations

from pathlib import Path

from anon.config import MappingRules
from anon.ids import SlugRegistry
from anon.jsonio import dump_json
from anon.model import canonicalize
from anon.paths import resolve_workspace
from anon.stages import apply_mapping_rules, generate_structurizr as gen

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"
TOY_RULES = TOY / "architecture" / "rules"


# --- a tiny curated fact model (post-curation shape) ---------------------------

def _curated() -> dict:
    return canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": "csharp:csproj:src/Web/Web.csproj", "name": "Web", "type": "csproj",
             "language": "csharp", "technology": ".NET assembly",
             "container_id": "container:webFrontend", "container_name": "Web Frontend",
             "component_view": True,
             "components": [{"id": "csharp:csproj:src/Web/Web.csproj/component:Web",
                             "name": "Web", "level": "component", "source": "namespace"}]},
            {"id": "csharp:csproj:src/Core/Core.csproj", "name": "Core", "type": "csproj",
             "language": "csharp", "technology": ".NET assembly",
             "container_id": "container:coreLibrary", "container_name": "Core Library"},
            # two targets in the SAME container -> intra-container edge must drop
            {"id": "csharp:csproj:src/CoreB/CoreB.csproj", "name": "CoreB", "type": "csproj",
             "language": "csharp", "technology": ".NET assembly",
             "container_id": "container:coreLibrary", "container_name": "Core Library"},
        ],
        "relationships": [
            {"id": "rel:csharp:csproj:src/Web/Web.csproj->csharp:csproj:src/Core/Core.csproj",
             "source": "csharp:csproj:src/Web/Web.csproj",
             "target": "csharp:csproj:src/Core/Core.csproj",
             "kind": "uses", "is_declared_dependency": True, "weight": 13, "confidence": "high",
             "evidence": [{"type": "project_ref", "detail": "x"},
                          {"type": "symbol_use", "detail": "y", "count": 3}]},
            # Web->CoreB also lands Web->coreLibrary (parallel edge -> merge)
            {"id": "rel:csharp:csproj:src/Web/Web.csproj->csharp:csproj:src/CoreB/CoreB.csproj",
             "source": "csharp:csproj:src/Web/Web.csproj",
             "target": "csharp:csproj:src/CoreB/CoreB.csproj",
             "kind": "uses", "is_declared_dependency": True, "weight": 5, "confidence": "medium",
             "evidence": [{"type": "project_ref", "detail": "z"}]},
            # intra-container edge Core->CoreB must be dropped
            {"id": "rel:csharp:csproj:src/Core/Core.csproj->csharp:csproj:src/CoreB/CoreB.csproj",
             "source": "csharp:csproj:src/Core/Core.csproj",
             "target": "csharp:csproj:src/CoreB/CoreB.csproj",
             "kind": "uses", "is_declared_dependency": True, "weight": 5, "confidence": "medium",
             "evidence": [{"type": "project_ref", "detail": "intra"}]},
        ],
    })


def _write_curated(tmp_path: Path, curated: dict, sub: str = "") -> "object":
    # IMPORTANT: rules_dir lives under tmp_path (never TOY_RULES) so a test that writes a
    # rules file can never clobber the committed toy fixture.
    base = tmp_path / sub if sub else tmp_path
    ws = resolve_workspace(TOY, arch_dir=base / "arch", rules_dir=base / "rules")
    dump_json(curated, ws.curated_facts)
    dump_json(curated, ws.enriched_facts)   # generator reads enriched if present
    return ws


# --- slug stability (§9) -------------------------------------------------------

def test_slug_stable_across_runs():
    a, b = SlugRegistry(), SlugRegistry()
    cid = "container:webFrontend"
    assert a.assign(cid) == b.assign(cid)


# --- intra-container drop + parallel merge (§9) --------------------------------

def test_intra_container_edges_dropped_and_merged():
    curated = _curated()
    containers = gen.build_containers(curated)
    t2c = {t["id"]: t["container_id"] for t in curated["targets"]}
    edges = gen.build_container_edges(curated, t2c)
    # Core->CoreB is intra-container -> not present
    assert ("container:coreLibrary", "container:coreLibrary") not in edges
    # Web->Core and Web->CoreB merge into ONE web->coreLibrary edge
    assert ("container:webFrontend", "container:coreLibrary") in edges
    merged = edges[("container:webFrontend", "container:coreLibrary")]
    assert merged["weight"] == 18   # 13 + 5


# --- Component view emission (auto-drawn, D§7.1) ---------------------------------

def test_auto_model_y_targets_become_grouped_containers(tmp_path):
    """The tiny fixture auto-selects Model Y (D§2.3: N=3 ≤ 25): every target is a
    container, the curated themes render as cosmetic group{} labels, the namespace tier
    nests natively, and its Component view is auto-drawn (no opt-in flag)."""
    ws = _write_curated(tmp_path, _curated())
    res = gen.run(ws)
    assert res["model_shape"] == "y" and res["targets_n"] == 3
    assert res["containers"] == 3                 # Web / Core / CoreB, one per target
    assert res["component_views"] == 1            # only Web carries an L2 tier
    comp_dsl = ws.components_dsl.read_text(encoding="utf-8")
    assert 'group "Core Library" {' in comp_dsl   # theme -> group, not a container
    assert 'group "Web Frontend" {' in comp_dsl
    assert '= container "Core Library"' not in comp_dsl
    assert 'component "Web"' in comp_dsl          # Web's namespace tier nests natively
    views = ws.views_dsl.read_text(encoding="utf-8")
    assert "component web " in views and "Component-web" in views


def test_code_view_renders_classes_not_namespaces(tmp_path):
    """A code_view container renders the CLASSES of the matching namespace as components
    (the C4 code altitude, §9.1), and excludes both the namespace tier and non-matching
    namespaces' classes."""
    base = "csharp:csproj:src/Lib/Lib.csproj"
    curated = canonicalize({
        "schema_version": "1.1",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": base, "name": "Lib", "type": "csproj", "language": "csharp",
             "container_id": "container:codeProbe", "container_name": "Code Probe",
             "component_view": True, "code_view": "Foo.Bar",
             "components": [
                 {"id": f"{base}/component:Foo.Bar", "name": "Foo.Bar", "level": "component",
                  "source": "namespace", "key": "Foo.Bar",
                  "code": [{"id": f"{base}/component:Foo.Bar/code:Alpha", "name": "Alpha", "level": "code"},
                           {"id": f"{base}/component:Foo.Bar/code:Beta", "name": "Beta", "level": "code"}]},
                 {"id": f"{base}/component:Foo.Other", "name": "Foo.Other", "level": "component",
                  "source": "namespace", "key": "Foo.Other",
                  "code": [{"id": f"{base}/component:Foo.Other/code:Gamma", "name": "Gamma", "level": "code"}]},
             ]},
        ],
        "relationships": [],
    })
    ws = _write_curated(tmp_path, curated)
    gen.run(ws)
    comp_dsl = ws.components_dsl.read_text(encoding="utf-8")
    assert 'component "Alpha"' in comp_dsl and 'component "Beta"' in comp_dsl   # matching-ns classes
    assert 'component "Gamma"' not in comp_dsl    # non-matching namespace's classes excluded
    assert 'component "Foo.Bar"' not in comp_dsl  # the namespace itself is NOT shown in a code view
    assert "code" in comp_dsl                     # classes carry the `code` tag


def test_component_view_only_where_components_materialize(tmp_path):
    """No L2 tier and no multi-target drill -> no Component view (an empty view would trip
    `views.view.empty`, D§7.1). Here every target loses its components[]; under auto-Y
    nothing materializes, so zero views and zero nested components are emitted."""
    curated = _curated()
    for t in curated["targets"]:
        t.pop("component_view", None)
        t.pop("components", None)
    ws = _write_curated(tmp_path, curated)
    res = gen.run(ws)
    assert res["component_views"] == 0
    assert "= component" not in ws.components_dsl.read_text(encoding="utf-8")


# --- element + relationship tags (§9.2) ----------------------------------------

def test_element_and_relationship_tags(tmp_path):
    ws = _write_curated(tmp_path, _curated())
    gen.run(ws)
    comp = ws.components_dsl.read_text(encoding="utf-8")
    assert "csharp" in comp and ".NET assembly" in comp and "Container" in comp
    rels = ws.relationships_dsl.read_text(encoding="utf-8")
    assert "Relationship" in rels
    assert "declared" in rels                 # is_declared_dependency
    assert "project_ref" in rels              # evidence kind
    assert "confidence:" in rels              # confidence tag


# --- naming reconciler (§8.3) --------------------------------------------------

def test_reconciler_name_override_hard_wins():
    sibs = [("csharp:csproj:src/Web/Web.csproj", "Toy.Web")]
    overrides = {"csharp:csproj:src/Web/Web.csproj": "Web Frontend"}
    resolved, mixed = gen.reconcile_names(sibs, overrides)
    assert resolved["csharp:csproj:src/Web/Web.csproj"] == "Web Frontend"


def test_reconciler_disambiguates_duplicates_deterministically():
    sibs = [("csharp:csproj:src/A/Foo.csproj", "Foo"),
            ("csharp:csproj:src/B/Foo.csproj", "Foo")]
    resolved, _ = gen.reconcile_names(sibs, {})
    names = set(resolved.values())
    assert len(names) == 2                      # disambiguated
    assert all(n.startswith("Foo (") for n in names)
    # deterministic: qualifier from stable id (parent folder)
    assert resolved["csharp:csproj:src/A/Foo.csproj"] == "Foo (A)"
    assert resolved["csharp:csproj:src/B/Foo.csproj"] == "Foo (B)"


def test_reconciler_flags_mixed_conventions():
    sibs = [("x:t:1", "Order Processor"), ("x:t:2", "pricing_lib_wrapper")]
    _, mixed = gen.reconcile_names(sibs, {})
    assert mixed is True
    sibs2 = [("x:t:1", "Order Processor"), ("x:t:2", "Pricing Library")]
    _, mixed2 = gen.reconcile_names(sibs2, {})
    assert mixed2 is False


def test_reconciler_flags_only_minority_outlier():
    # two Title-Case containers (the majority) + one snake_case outlier: only the outlier is
    # flagged. One odd sibling must not paint the well-named majority needs-curation (§8.3).
    cs = {
        "a": gen.Container(cid="a", name="Order Processor"),
        "b": gen.Container(cid="b", name="Pricing Library"),
        "c": gen.Container(cid="c", name="event_bus"),
    }
    gen._apply_reconciler(cs, {})
    assert "needs-curation" not in cs["a"].tags
    assert "needs-curation" not in cs["b"].tags
    assert "needs-curation" in cs["c"].tags


def test_reconciler_ignores_external_seam_in_convention_check():
    # a lowercase external/contract seam (auto-named from a .proto) neither trips the
    # mixed-convention flag for the curated containers nor receives it itself (§8.3).
    cs = {
        "a": gen.Container(cid="a", name="Order Processor"),
        "b": gen.Container(cid="b", name="Event Bus"),
        "s": gen.Container(cid="s", name="basket", tags={"external", "contract"}),
    }
    gen._apply_reconciler(cs, {})
    assert all("needs-curation" not in c.tags for c in cs.values())


def test_reconciler_applied_in_generate(tmp_path):
    curated = _curated()
    # rename both Core Library members' containers to collide as display names? Instead,
    # pin a name_override and confirm it reaches the DSL.
    ws = _write_curated(tmp_path, curated)
    # write a rules file with a name override for the container's member
    rules_yaml = (
        "version: 1\n"
        "model_shape: x\n"     # pin X so the theme container (the override's key) is emitted
        "group:\n"
        "  - container: \"Web Frontend\"\n"
        "    members: [ \"csharp:csproj:src/Web/Web.csproj\" ]\n"
        "name_overrides:\n"
        "  \"container:webFrontend\": \"Public Web\"\n"
    )
    ws.mapping_rules.parent.mkdir(parents=True, exist_ok=True)
    ws.mapping_rules.write_text(rules_yaml, encoding="utf-8", newline="")
    gen.run(ws)
    assert 'container "Public Web"' in ws.components_dsl.read_text(encoding="utf-8")


# --- element budget over-limit detection (§9.1/§1.1#1) -------------------------

def test_container_budget_over_limit():
    curated = canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": f"csharp:csproj:c{i}/c{i}.csproj", "name": f"c{i}", "type": "csproj",
             "language": "csharp", "container_id": f"container:c{i}", "container_name": f"C{i}"}
            for i in range(gen.CONTAINER_BUDGET + 2)
        ],
        "relationships": [],
    })
    containers = gen.build_containers(curated)
    over = gen.check_budgets(containers)
    assert any("Containers" in o for o in over)


def test_component_budget_over_limit():
    """Budget detection is advisory and counts MATERIALIZED components (D§7.2) — here a
    Model-Y module-container whose namespace tier exceeds COMPONENT_BUDGET."""
    comps = [{"id": f"csharp:csproj:src/Web/Web.csproj/component:n{i}", "name": f"n{i}",
              "level": "component", "source": "namespace"}
             for i in range(gen.COMPONENT_BUDGET + 3)]
    curated = canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": "csharp:csproj:src/Web/Web.csproj", "name": "Web", "type": "csproj",
             "language": "csharp", "container_id": "container:webFrontend",
             "container_name": "Web Frontend", "components": comps}],
        "relationships": [],
    })
    containers = gen.build_containers(curated, shape="y")
    over = gen.check_budgets(containers)
    assert any("Component-Web" in o for o in over)


# --- end-to-end determinism of the generator over the real toy (§1.1 #2) -------

def test_toy_generate_byte_identical(tmp_path):
    from anon.stages import enrich_llm

    def run_once(sub: str) -> bytes:
        ws = resolve_workspace(TOY, arch_dir=tmp_path / sub / "arch", rules_dir=TOY_RULES)
        apply_mapping_rules.run(_real_ws_with_extract(ws))
        enrich_llm.run(ws, "no-llm")
        gen.run(ws)
        return (ws.components_dsl.read_bytes(), ws.relationships_dsl.read_bytes())

    # use the foundation extractor to produce extracted-facts.json for the toy.
    def _real_ws_with_extract(ws):
        from anon.stages import extract_build_graph, extract_csharp_facts, normalize_facts
        extract_build_graph.run(ws)
        extract_csharp_facts.run(ws)
        normalize_facts.run(ws, repo="toy", commit="c", generated_at="2026-01-01T00:00:00Z")
        return ws

    a = run_once("a")
    b = run_once("b")
    assert a == b
    assert b"Web Frontend" in a[0]
    # The component tier needs the Roslyn L2 helper (csharp-helper/, `dotnet build` once). In a
    # fresh clone before that build, or in the anonymized review artifact (which does not ship
    # the helper), the toy degrades to L0 and emits no components -- byte identity above is the
    # gate; the component assertion only applies when the helper can run (found 2026-08-31 by
    # the review-artifact self-test, plan 2026-08-30 item 7.1).
    from anon.stages import extract_csharp_facts
    if extract_csharp_facts.helper_available():
        assert b"= component" in a[0]
    else:
        assert b"= component" not in a[0]   # honest L0 degradation, not a silent partial run


# --- cosmetic parent tier (container-groups plan §4) -----------------------------

def _with_parent(curated: dict, container_name: str, parent: str) -> dict:
    for t in curated["targets"]:
        if t.get("container_name") == container_name:
            t["container_parent"] = parent
    return canonicalize(curated)


def test_parent_wraps_model_x_container_in_group(tmp_path):
    """Model X (plan §4.1): a curated container whose members carry `container_parent`
    renders inside a `group{}` boundary; unparented siblings stay outside it."""
    curated = _with_parent(_curated(), "Core Library", "Platform")
    ws = _write_curated(tmp_path, curated)
    ws.mapping_rules.parent.mkdir(parents=True, exist_ok=True)
    ws.mapping_rules.write_text("model_shape: x\n", encoding="utf-8", newline="")
    res = gen.run(ws)
    assert res["model_shape"] == "x"
    comp_dsl = ws.components_dsl.read_text(encoding="utf-8")
    gidx = comp_dsl.index('group "Platform" {')
    gend = comp_dsl.index("\n}", gidx)
    assert 'container "Core Library"' in comp_dsl[gidx:gend]
    assert 'container "Web Frontend"' not in comp_dsl[gidx:gend]
    assert 'container "Web Frontend"' in comp_dsl        # still emitted, ungrouped


def test_parent_flattens_with_theme_label_in_model_y(tmp_path):
    """Model Y (plan §4.2): the theme label already occupies the group slot, so a parent
    flattens to one "Parent / Theme" label — never a nested group{} in v1."""
    curated = _with_parent(_curated(), "Core Library", "Platform")
    ws = _write_curated(tmp_path, curated)
    res = gen.run(ws)
    assert res["model_shape"] == "y"
    comp_dsl = ws.components_dsl.read_text(encoding="utf-8")
    assert 'group "Platform / Core Library" {' in comp_dsl
    assert 'group "Web Frontend" {' in comp_dsl          # unparented theme unchanged
    assert 'group "Core Library" {' not in comp_dsl
