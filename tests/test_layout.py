"""Engine §6.9 — the deterministic layout-coordinate export (`anon.layout`).

What is locked down here:

  * **the determinism contract** — same model → byte-identical `layout/<view>.json`
    (the §6.9 CI gate that makes "visual drift" a diffable artifact, GUI §3);
  * **readability by construction** (engine §6.9b / GUI §2.9) — auto-layout emits zero
    node overlaps, and the metrics block (crossings, label fit, aspect) is present and
    gateable;
  * **the §5.10 lock workflow** — pins from the reviewed `layout-overrides.yaml` are
    applied verbatim, unpinned nodes are pushed clear of them, and a stale pin is
    *reported*, never silently applied or dropped;
  * **honest abstention** — a container without L2 facts refuses component drill-down
    (`no_l2`), an unknown view/container is an error, never a guessed picture;
  * **the serve echo** lives in test_serve.py (`GET /model/layout`).

One toy pipeline run is shared module-wide; pin tests write the override file into the
run's *copied* rules dir (the fixture stays pristine, conftest §19.4).
"""
from __future__ import annotations

import pytest
import yaml

from conftest import run_toy_pipeline

from anon import cli, layout
from anon.jsonio import load_json
from anon.model import content_hash


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    tr = run_toy_pipeline(tmp_path_factory.mktemp("layoutrun") / "architecture")
    return tr.ws


def _write_overrides(ws, view_pins: dict) -> None:
    with open(ws.layout_overrides, "w", encoding="utf-8", newline="") as fh:
        yaml.safe_dump({"views": view_pins}, fh)


# --------------------------------------------------------------------------- contract

def test_two_runs_are_byte_identical(toy):
    """The §6.9 determinism contract: the coordinate artifact is a pure function of the
    model (given the pinned engine), tested like every other artifact (moat #3)."""
    _artifact, path = layout.run(toy, view="containers")
    first = path.read_bytes()
    _artifact, path = layout.run(toy, view="containers")
    assert path.read_bytes() == first


def test_artifact_header_and_hash(toy):
    artifact, path = layout.run(toy, view="containers")
    assert artifact["schema"] == "layout/1"
    assert artifact["engine"] == layout.ENGINE
    assert artifact["view"] == "containers"
    assert artifact["derived_from_hash"] == content_hash(layout.load_facts(toy))
    assert artifact["seed"] == artifact["derived_from_hash"]
    assert path == toy.layout_dir / "containers.json"
    assert load_json(path) == artifact


def test_auto_layout_is_overlap_free_and_metrics_present(toy):
    artifact, _path = layout.run(toy, view="containers")
    m = artifact["metrics"]
    assert m["node_overlaps"] == 0, "overlap-free by construction (GUI §2.9 gate)"
    assert m["label_fit_failures"] == 0
    assert m["nodes"] == len(artifact["nodes"]) and m["nodes"] > 1
    assert m["edges"] == len(artifact["edges"])
    assert "edge_crossings" in m and "aspect_ratio" in m
    for n in artifact["nodes"]:
        assert set(n) == {"id", "label", "x", "y", "w", "h", "pinned"}
        assert isinstance(n["x"], int) and isinstance(n["y"], int)
    for e in artifact["edges"]:
        assert e["id"] == f"{e['source']}->{e['target']}"
        assert len(e["points"]) == 2


def test_edges_follow_the_lifted_container_graph(toy):
    """The node/edge set is the engine's two-step lift — never invented (GUI §2.1)."""
    artifact, _path = layout.run(toy, view="containers")
    nodes, edges = layout.container_view_graph(layout.load_facts(toy))
    assert [n["id"] for n in artifact["nodes"]] == sorted(n["id"] for n in nodes)
    assert {(e["source"], e["target"]) for e in artifact["edges"]} == \
        {(e["source"], e["target"]) for e in edges}


# --------------------------------------------------------------------------- pins

def test_pin_is_applied_verbatim_and_neighbours_pushed_clear(toy):
    base, _path = layout.run(toy, view="containers")
    a, b = base["nodes"][0], base["nodes"][1]
    try:
        # pin A exactly onto B's auto position — the §5.10 constrained re-layout must
        # honor the pin verbatim and push the unpinned B clear of it
        _write_overrides(toy, {"containers": {a["id"]: {"x": b["x"], "y": b["y"]}}})
        artifact, _path = layout.run(toy, view="containers")
        by_id = {n["id"]: n for n in artifact["nodes"]}
        pa, pb = by_id[a["id"]], by_id[b["id"]]
        assert (pa["x"], pa["y"]) == (b["x"], b["y"])
        assert pa["pinned"] is True and pb["pinned"] is False
        assert artifact["pinned"] == [a["id"]]
        assert not layout._intersects(pa, pb), "unpinned node pushed clear of the pin"
        assert artifact["metrics"]["node_overlaps"] == 0
    finally:
        toy.layout_overrides.unlink()


def test_stale_pin_reported_never_silently_applied_or_dropped(toy):
    try:
        _write_overrides(toy, {"containers": {"container:gone": {"x": 0, "y": 0}}})
        artifact, _path = layout.run(toy, view="containers")
        assert artifact["stale_pins"] == ["container:gone"]
        assert artifact["pinned"] == []
        assert "container:gone" not in {n["id"] for n in artifact["nodes"]}
    finally:
        toy.layout_overrides.unlink()


def test_deleting_overrides_returns_to_pure_auto_layout(toy):
    before, path = layout.run(toy, view="containers")
    auto = path.read_bytes()
    try:
        a = before["nodes"][0]
        _write_overrides(toy, {"containers": {a["id"]: {"x": 9999, "y": 9999}}})
        pinned, path = layout.run(toy, view="containers")
        assert path.read_bytes() != auto
    finally:
        toy.layout_overrides.unlink()
    _after, path = layout.run(toy, view="containers")
    assert path.read_bytes() == auto, "clean return to auto-layout (§5.10)"


# --------------------------------------------------------------------------- honesty

def test_component_view_without_l2_refuses_honestly(toy):
    facts = layout.load_facts(toy)
    # a SINGLETON container with no L2 tier has nothing deeper to show (drill-down
    # plan D§3.1) — webFrontend is Toy.Web alone, L0-only on the toy run
    singleton = next(t for t in facts["targets"]
                     if (t.get("container_id") or t["id"]) == "container:webFrontend")
    cid = singleton.get("container_id") or singleton["id"]
    with pytest.raises(layout.LayoutError) as exc:
        layout.component_view_graph(facts, cid)
    assert exc.value.code == "no_l2"
    with pytest.raises(layout.LayoutError) as exc:
        layout.component_view_graph(facts, "container:gone")
    assert exc.value.code == "unknown_id"
    with pytest.raises(layout.LayoutError) as exc:
        layout.run(toy, view="freeform")
    assert exc.value.code == "bad_view"


def test_multi_target_container_drills_into_member_targets(toy):
    """A curated container with ≥2 members drills into its member build TARGETS, with
    the intra-container target→target edges that the container altitude hides PLUS the
    cross-boundary neighbour containers as external boxes (drill-down plan D§3.1/§4.1).
    Core Library is Toy.Domain + Toy.Common (Domain→Common intra); Web Frontend and
    Messaging reach into it, so both appear as external neighbours with half-edges."""
    domain = "csharp:csproj:src/Domain/Toy.Domain.csproj"
    common = "csharp:csproj:src/Common/Toy.Common.csproj"
    facts = layout.load_facts(toy)
    nodes, edges = layout.component_view_graph(facts, "container:coreLibrary")
    members = {n["id"] for n in nodes if not n.get("external")}
    externals = {n["id"] for n in nodes if n.get("external")}
    assert members == {domain, common}
    assert externals == {"container:webFrontend", "container:messaging"}
    epairs = {(e["source"], e["target"]) for e in edges}
    assert (domain, common) in epairs, "intra-container edge un-hidden"
    assert ("container:webFrontend", domain) in epairs, "cross-boundary half-edge in"
    assert ("container:messaging", common) in epairs


# --------------------------------------------------------------------------- core unit

def test_layout_graph_layers_a_chain_top_down():
    laid = layout.layout_graph(
        [{"id": "a", "label": "A"}, {"id": "b", "label": "B"},
         {"id": "c", "label": "C"}],
        [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}])
    y = {n["id"]: n["y"] for n in laid["nodes"]}
    assert y["a"] < y["b"] < y["c"]
    assert laid["metrics"]["edge_crossings"] == 0


def test_layout_graph_counts_the_unavoidable_k22_crossing():
    laid = layout.layout_graph(
        [{"id": f"{s}", "label": s.upper()} for s in ("a1", "a2", "b1", "b2")],
        [{"source": "a1", "target": "b1"}, {"source": "a1", "target": "b2"},
         {"source": "a2", "target": "b1"}, {"source": "a2", "target": "b2"}])
    assert laid["metrics"]["edge_crossings"] == 1, \
        "K2,2 has exactly one unavoidable straight-line crossing"


def test_layout_graph_survives_cycles():
    laid = layout.layout_graph(
        [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}])
    assert laid["metrics"]["nodes"] == 2
    assert laid["metrics"]["edges"] == 2, "back edges still render"
    assert laid["metrics"]["node_overlaps"] == 0


def test_one_weak_edge_cannot_sink_a_net_source_node():
    """Regression: a node that is a net *source* (heavy outbound, one weak inbound edge)
    must lay out at the TOP, not the bottom. The weight-aware feedback-arc breaker makes
    the lone weak edge the back edge; the id-order DFS breaker used to make the two heavy
    edges the back edges (whichever closed a cycle first *alphabetically*), sinking the
    node and rendering its real dependencies backward. `vert` is chosen to sort LAST so
    the old alphabetical fragility is exercised."""
    nodes = [{"id": "infra", "label": "Infra"},
             {"id": "svc", "label": "Service"},
             {"id": "vert", "label": "Vertical"}]
    edges = [{"source": "vert", "target": "infra", "weight": 40},   # heavy real deps
             {"source": "vert", "target": "svc", "weight": 35},
             {"source": "infra", "target": "svc", "weight": 10},
             {"source": "svc", "target": "vert", "weight": 5}]      # the one weak edge
    y = {n["id"]: n["y"] for n in layout.layout_graph(nodes, edges)["nodes"]}
    assert y["vert"] < y["infra"] and y["vert"] < y["svc"], \
        "the net-source node stays on top; the single weak inbound edge is demoted"

    # and it is genuinely the WEIGHT that decides: flip the weights so `vert`'s inbound
    # edge dominates and it should now legitimately sink below its (now-heavy) dependant.
    flipped = [{**e, "weight": (5 if e["weight"] > 30 else 40)} for e in edges]
    y2 = {n["id"]: n["y"] for n in layout.layout_graph(nodes, flipped)["nodes"]}
    assert y2["vert"] > y2["svc"], "reverse the weights and the layer inverts — weight-driven"


def test_view_slug_is_deterministic_and_collision_safe():
    assert layout.view_slug("containers") == "containers"
    s1 = layout.view_slug("component:csharp:csproj:src/Web/Toy.Web.csproj")
    s2 = layout.view_slug("component:csharp:csproj:src/Web,Toy.Web.csproj")
    assert s1 == layout.view_slug("component:csharp:csproj:src/Web/Toy.Web.csproj")
    assert s1 != s2, "distinct view keys never collide on disk"
    assert "/" not in s1 and ":" not in s1


# --------------------------------------------------------------------------- CLI

def test_arch_run_emits_gui_artifacts(tmp_path):
    """`arch run` leaves a workspace the GUI can fully render: the canvas layout
    (engine §6.9) plus the risk + ADR-conformance echoes refresh on every run, so
    none of them can go stale against the new facts (GUI §7.3)."""
    from conftest import pristine_toy
    src = pristine_toy(tmp_path / "toy-src")
    arch = tmp_path / "architecture"
    rc = cli.main(["run", "--repo", str(src), "--arch-dir", str(arch),
                   "--rules-dir", str(src / "architecture" / "rules"), "--no-llm"])
    assert rc == 0
    laid = load_json(arch / "generated" / "layout" / "containers.json")
    from anon.paths import resolve_workspace
    ws = resolve_workspace(src, arch, src / "architecture" / "rules")
    assert laid["derived_from_hash"] == content_hash(layout.load_facts(ws)), \
        "the emitted layout is in lockstep with the facts of THIS run"
    assert load_json(arch / "generated" / "risk-report.json")["schema"] == "risk/1"
    assert (arch / "generated" / "adr-conformance.json").exists()


def test_cli_export_layout_smoke(toy):
    rc = cli.main(["export", "--repo", str(toy.repo), "--arch-dir", str(toy.arch_dir),
                   "--rules-dir", str(toy.rules_dir), "--layout"])
    assert rc == 0
    assert (toy.layout_dir / "containers.json").exists()
    rc = cli.main(["export", "--repo", str(toy.repo), "--arch-dir", str(toy.arch_dir),
                   "--rules-dir", str(toy.rules_dir), "--layout", "--view", "bogus"])
    assert rc == 2


def test_run_all_views_emits_component_layouts_for_l2_containers(toy):
    """`arch run`'s advisory pass: the container view plus a component drill-down for
    every container that has something deeper to show — a build-TARGET drill for any
    multi-member container (even L0-only, drill-down plan D§3.1), and a NAMESPACE drill
    where the L2 tier exists. The scaffolded workspace.dsl always carries the Operator
    person, so the system-context view is emitted too; no deployment facts -> none."""
    from anon.jsonio import dump_json
    # base toy (L0): Core Library has 2 members -> a build-target drill-down; the
    # singleton containers (webFrontend, messaging) have nothing deeper yet.
    assert layout.run_all_views(toy) == [
        "containers", "system-context", "component:container:coreLibrary"]
    core = load_json(toy.layout_dir
                     / f"{layout.view_slug('component:container:coreLibrary')}.json")
    core_members = {n["id"] for n in core["nodes"] if not n.get("external")}
    assert core_members == {"csharp:csproj:src/Domain/Toy.Domain.csproj",
                            "csharp:csproj:src/Common/Toy.Common.csproj"}
    # inject an L2 namespace tier into the singleton Web target -> its container now
    # drills into namespaces (a singleton's namespaces ARE its container view, §3.1)
    facts = layout.load_facts(toy)
    web = next(t for t in facts["targets"]
               if (t.get("container_id") or t["id"]) == "container:webFrontend")
    web["components"] = [
        {"id": "comp:a", "name": "A", "source": "roslyn"},
        {"id": "comp:b", "name": "B", "source": "roslyn"},
    ]
    src = toy.enriched_facts if toy.enriched_facts.exists() else toy.curated_facts
    dump_json(facts, src)
    emitted = layout.run_all_views(toy)
    assert "component:container:webFrontend" in emitted
    slug = layout.view_slug("component:container:webFrontend")
    artifact = load_json(toy.layout_dir / f"{slug}.json")
    assert {n["id"] for n in artifact["nodes"]} == {"comp:a", "comp:b"}
    assert artifact["metrics"]["node_overlaps"] == 0


def test_wide_edgeless_layer_wraps_into_a_grid():
    """GUI §2.9 readability: N isolated nodes (no edges -> all in layer 0) lay out as
    a centered grid, not one endless horizontal strip - engine-side, deterministic."""
    nodes = [{"id": f"n{i:02d}", "label": f"Component {i:02d}"} for i in range(18)]
    laid = layout.layout_graph(nodes, [])
    ys = sorted({n["y"] for n in laid["nodes"]})
    assert len(ys) >= 3, "18 isolated nodes wrap into multiple rows"
    xs = [n["x"] + n["w"] for n in laid["nodes"]]
    assert max(xs) <= layout.ROW_TARGET + layout.MIN_W, "rows respect the width target"
    assert laid["metrics"]["node_overlaps"] == 0
    # wrapped rows of the SAME layer sit closer than distinct layers would
    assert ys[1] - ys[0] == layout.NODE_H + layout.ROW_GAP
    # determinism: same input -> identical placement
    again = layout.layout_graph(nodes, [])
    assert again["nodes"] == laid["nodes"]


# --------------------------------------------------------------------- new views


def _bare_ws(tmp_path):
    """A minimal workspace for synthetic-facts view tests (no pipeline run)."""
    from anon.paths import resolve_workspace
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return resolve_workspace(repo, tmp_path / "architecture", tmp_path / "rules")


def test_system_context_view_reads_the_scaffolded_workspace_dsl(toy):
    """The C4 context altitude comes from the HAND-OWNED workspace.dsl (persons /
    external systems are human truth, the same tier as rules/*.yaml) — the scaffold
    ships the Operator person + `user -> system`, so the view exists after any run."""
    artifact, path = layout.run(toy, view="system-context")
    by_id = {n["id"]: n for n in artifact["nodes"]}
    assert "system" in by_id and by_id["system"]["kind"] == "system"
    assert by_id["dsl:person:user"]["kind"] == "person"
    assert by_id["dsl:person:user"]["label"] == "Operator"
    edges = {(e["source"], e["target"]): e for e in artifact["edges"]}
    assert ("dsl:person:user", "system") in edges
    assert edges[("dsl:person:user", "system")]["label"] == "Uses"
    assert artifact["metrics"]["node_overlaps"] == 0
    # determinism: same model + same DSL -> byte-identical artifact
    first = path.read_bytes()
    _artifact, path = layout.run(toy, view="system-context")
    assert path.read_bytes() == first


def test_system_context_lifts_external_deployment_infra(tmp_path):
    """External infra observed by the deployment facet renders as an external system,
    and the system->infra edge exists only when a deploy:image actually calls it."""
    from anon.jsonio import dump_json
    ws = _bare_ws(tmp_path)
    dump_json({
        "schema_version": "1.1", "targets": [{"id": "csharp:csproj:src/A/A.csproj"}],
        "relationships": [],
        "deployment": {
            "nodes": [
                {"id": "deploy:image:api", "name": "api", "kind": "image"},
                {"id": "infra:database:orders-db", "name": "orders-db",
                 "kind": "infra", "technology": "PostgreSQL", "external": True},
                {"id": "infra:queue:bus", "name": "bus", "kind": "infra",
                 "subkind": "queue", "external": True},
            ],
            "edges": [
                {"source": "deploy:image:api", "target": "infra:database:orders-db",
                 "kind": "calls", "evidence": [{"type": "runtime"}]},
            ],
        },
    }, ws.curated_facts)
    nodes, edges = layout.system_context_view_graph(ws, layout.load_facts(ws))
    by_id = {n["id"]: n for n in nodes}
    assert by_id["infra:database:orders-db"]["external"] is True
    assert by_id["infra:database:orders-db"]["tech"] == "PostgreSQL"
    assert by_id["infra:queue:bus"]["kind"] == "infra"
    pairs = {(e["source"], e["target"]) for e in edges}
    assert ("system", "infra:database:orders-db") in pairs
    assert ("system", "infra:queue:bus") not in pairs, \
        "no observed runtime call -> no edge (no edge without evidence)"


def test_system_context_refuses_a_one_box_view(tmp_path):
    """No workspace.dsl + no external deployment infra -> honest no_context refusal;
    a one-box context diagram is never invented (§5.1)."""
    from anon.jsonio import dump_json
    ws = _bare_ws(tmp_path)
    dump_json({"schema_version": "1.1",
               "targets": [{"id": "csharp:csproj:src/A/A.csproj"}],
               "relationships": []}, ws.curated_facts)
    with pytest.raises(layout.LayoutError) as exc:
        layout.compute(ws, view="system-context")
    assert exc.value.code == "no_context"


def test_code_view_renders_the_code_tier_and_refuses_without_it():
    facts = {"targets": [
        {"id": "csharp:csproj:src/A/A.csproj", "components": [
            {"id": "comp:ns", "name": "App.Ns", "code": [
                {"id": "comp:ns/code:Foo", "name": "Foo", "level": "code"},
                {"id": "comp:ns/code:Bar", "name": "Bar", "level": "code"},
            ]},
            {"id": "comp:empty", "name": "App.Empty"},
        ]},
    ], "relationships": [
        {"source": "comp:ns/code:Foo", "target": "comp:ns/code:Bar", "kind": "uses"},
    ]}
    nodes, edges = layout.code_view_graph(facts, "comp:ns")
    assert {n["id"] for n in nodes} == {"comp:ns/code:Foo", "comp:ns/code:Bar"}
    assert all(n["kind"] == "code" for n in nodes)
    assert edges == [{"source": "comp:ns/code:Foo", "target": "comp:ns/code:Bar",
                      "kind": "uses"}]
    with pytest.raises(layout.LayoutError) as exc:
        layout.code_view_graph(facts, "comp:empty")
    assert exc.value.code == "no_code"
    with pytest.raises(layout.LayoutError) as exc:
        layout.code_view_graph(facts, "comp:gone")
    assert exc.value.code == "unknown_id"


def _deployment_facts() -> dict:
    return {
        "schema_version": "1.1",
        "targets": [
            {"id": "csharp:csproj:src/Web/Web.csproj", "name": "Web",
             "container_id": "container:web", "container_name": "webFrontend"},
            {"id": "csharp:csproj:src/Core/Core.csproj", "name": "Core",
             "container_id": "container:core", "container_name": "coreLibrary"},
        ],
        "relationships": [],
        "deployment": {
            "nodes": [
                {"id": "env:prod", "name": "prod", "kind": "env"},
                {"id": "deploy:image:web", "name": "web", "kind": "image",
                 "technology": "mcr.microsoft.com/dotnet/aspnet:9.0",
                 "env": "prod", "replicas": 3},
                {"id": "deploy:image:worker", "name": "worker", "kind": "image",
                 "env": "prod"},
                {"id": "infra:database:orders-db", "name": "orders-db",
                 "kind": "infra", "technology": "PostgreSQL", "env": "prod",
                 "external": True},
            ],
            "edges": [
                {"source": "deploy:image:web",
                 "target": "csharp:csproj:src/Web/Web.csproj", "kind": "packages",
                 "evidence": [{"type": "deploy"}]},
                {"source": "deploy:image:web",
                 "target": "csharp:csproj:src/Core/Core.csproj", "kind": "packages",
                 "evidence": [{"type": "deploy"}]},
                {"source": "deploy:image:web", "target": "deploy:image:worker",
                 "kind": "calls", "evidence": [{"type": "runtime"}]},
                {"source": "deploy:image:worker",
                 "target": "infra:database:orders-db", "kind": "reads",
                 "evidence": [{"type": "runtime_config"}]},
            ],
        },
    }


def test_deployment_view_boundaries_instances_and_runtime_edges(tmp_path):
    """The C4 deployment view: the env boundary encloses its members, an image
    carries the container instances it packages (lifted through the curated
    mapping), runtime edges render with their kind, packaging edges do not."""
    from anon.jsonio import dump_json
    ws = _bare_ws(tmp_path)
    dump_json(_deployment_facts(), ws.curated_facts)
    artifact, path = layout.run(ws, view="deployment")
    by_id = {n["id"]: n for n in artifact["nodes"]}
    env = by_id["env:prod"]
    assert env["kind"] == "env"
    members = [n for n in artifact["nodes"] if n.get("kind") in ("image", "infra")]
    for m in members:
        assert m["parent"] == "env:prod"
        assert env["x"] <= m["x"] and env["y"] <= m["y"]
        assert m["x"] + m["w"] <= env["x"] + env["w"]
        assert m["y"] + m["h"] <= env["y"] + env["h"]
    web = by_id["deploy:image:web"]
    assert web["instances"] == ["coreLibrary", "webFrontend"], \
        "packages edges lift to curated container names"
    assert web["replicas"] == 3
    assert web["h"] > layout.NODE_H, "instance chips get vertical room"
    assert by_id["infra:database:orders-db"]["external"] is True
    edges = {(e["source"], e["target"]): e for e in artifact["edges"]}
    assert edges[("deploy:image:web", "deploy:image:worker")]["kind"] == "calls"
    assert edges[("deploy:image:worker", "infra:database:orders-db")]["kind"] == "reads"
    assert not any(t.endswith(".csproj") for _s, t in edges), \
        "packaging edges render as contained instances, never as arrows"
    assert artifact["metrics"]["node_overlaps"] == 0, \
        "members overlap-free (boundaries excluded by design)"
    # determinism: byte-identical on re-run
    first = path.read_bytes()
    _artifact, path = layout.run(ws, view="deployment")
    assert path.read_bytes() == first


def test_deployment_view_refuses_without_the_facet(tmp_path):
    from anon.jsonio import dump_json
    ws = _bare_ws(tmp_path)
    dump_json({"schema_version": "1.1", "targets": [], "relationships": []},
              ws.curated_facts)
    with pytest.raises(layout.LayoutError) as exc:
        layout.compute(ws, view="deployment")
    assert exc.value.code == "no_deployment_facet"


def test_run_all_views_emits_context_deployment_and_code_views(tmp_path):
    """The advisory pass enumerates every renderable altitude: containers, context
    (deployment externals here — no workspace.dsl), deployment, and the code view
    for the one component carrying the optional code tier."""
    from anon.jsonio import dump_json
    ws = _bare_ws(tmp_path)
    facts = _deployment_facts()
    facts["targets"][0]["components"] = [
        {"id": "comp:ns", "name": "Web.Ns", "code": [
            {"id": "comp:ns/code:Foo", "name": "Foo", "level": "code"}]},
    ]
    dump_json(facts, ws.curated_facts)
    emitted = layout.run_all_views(ws)
    assert emitted == ["containers", "system-context", "deployment",
                       "component:container:web", "code:comp:ns"]
    assert (ws.layout_dir / f"{layout.view_slug('deployment')}.json").exists()
    assert (ws.layout_dir / f"{layout.view_slug('code:comp:ns')}.json").exists()


def test_drilldown_dispatches_target_then_namespace_altitude():
    """The two altitudes (drill-down plan D§2–4): a curated container with ≥2 members
    drills into its member build TARGETS (clean target names, no cross-project
    namespace flattening); each target then drills one hop further into its own
    NAMESPACE tier — unique within a target, so no disambiguation is needed."""
    facts = {"targets": [
        {"id": "csharp:csproj:src/A/Plugin.A.csproj", "name": "Plugin.A",
         "container_id": "container:p",
         "components": [{"id": "comp:a1", "name": "Plugin.A.Core"},
                        {"id": "comp:a2", "name": "Plugin.A.Web"}]},
        {"id": "csharp:csproj:src/B/Plugin.B.csproj", "name": "Plugin.B",
         "container_id": "container:p",
         "components": [{"id": "comp:b", "name": "Plugin.B"}]},
    ], "relationships": [
        {"source": "comp:a2", "target": "comp:a1"},          # intra-target ns edge
    ]}
    # container altitude → member targets
    nodes, _edges = layout.component_view_graph(facts, "container:p")
    assert {n["id"]: n["label"] for n in nodes} == {
        "csharp:csproj:src/A/Plugin.A.csproj": "Plugin.A",
        "csharp:csproj:src/B/Plugin.B.csproj": "Plugin.B"}
    # namespace altitude → one target's namespaces, with their internal edge
    ns_nodes, ns_edges = layout.component_view_graph(
        facts, "csharp:csproj:src/A/Plugin.A.csproj")
    assert {n["id"] for n in ns_nodes} == {"comp:a1", "comp:a2"}
    assert {(e["source"], e["target"]) for e in ns_edges} == {("comp:a2", "comp:a1")}


# --------------------------------------------------------------------------- layering

def test_declared_bands_matches_by_id_and_label():
    """`layers:` maps a container to a band by its id OR its label (the same identity
    the forbidden rules match on); first listed layer is band 0 (top)."""
    nodes = [{"id": "container:web", "label": "Web Frontend"},
             {"id": "container:svc", "label": "Services"},
             {"id": "container:db", "label": "Data"},
             {"id": "container:loose", "label": "Misc"}]
    rules = {"layers": ["Web Frontend", "container:svc", "Data"]}
    bands = layout.declared_bands(nodes, rules)
    assert bands == {"container:web": 0, "container:svc": 1, "container:db": 2}
    assert "container:loose" not in bands, "unmatched nodes get no declared band"
    assert layout.declared_bands(nodes, {}) == {}, "no `layers:` → structural fallback"


def test_layout_honours_declared_bands_top_to_bottom():
    """A declared layer stack lays out as horizontal bands top→bottom, OVERRIDING the
    structural longest-path order — here the edge points 'up' the declared stack, but
    the declaration still wins the vertical order (band 0 at the smallest y)."""
    nodes = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"},
             {"id": "c", "label": "C"}]
    edges = [{"source": "c", "target": "a"}]   # structural layering would lift c above a
    bands = {"a": 0, "b": 1, "c": 2}
    laid = layout.layout_graph(nodes, edges, bands=bands)
    y = {n["id"]: n["y"] for n in laid["nodes"]}
    assert y["a"] < y["b"] < y["c"], "declared bands drive the vertical order"
    # the residual band catches an unnamed node below the declared stack
    laid2 = layout.layout_graph(
        nodes + [{"id": "z", "label": "Z"}], edges, bands=bands)
    y2 = {n["id"]: n["y"] for n in laid2["nodes"]}
    assert y2["z"] > y2["c"], "an undeclared node falls to the residual bottom band"
