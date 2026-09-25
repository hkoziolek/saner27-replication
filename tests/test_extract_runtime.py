"""Tests for the §5 ``runtime``-evidence extractor (extract_runtime).

Covers:
  - Aspire AppHost parsing: AddProject resolution to a real csharp:csproj id, an
    infra:database node from AddPostgres, and a `calls` deployment edge carrying
    `runtime` evidence (the §5 evidence kind this extractor exists to produce).
  - The deployment-facet invariant: runtime facts land under `deployment`, NEVER in
    build `relationships[]` (so they cannot feed the §6.3 build-edge weight).
  - Graceful no-op (returns None, writes nothing) on a build-only / empty repo.
  - mine_test_scenarios: a step naming a non-existent curated edge is dropped
    (principle #3), while a real edge produces a candidate step.
"""
from __future__ import annotations

from pathlib import Path

from anon.paths import resolve_workspace
from anon.jsonio import load_json
from anon.stages import extract_runtime


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _ws(repo: Path):
    return resolve_workspace(repo, arch_dir=repo / "architecture")


def _make_aspire_repo(root: Path) -> Path:
    """A tiny fake repo: an AppHost project + a referenced Api project."""
    apphost = root / "AppHost"
    api = root / "Api"
    apphost.mkdir(parents=True)
    api.mkdir(parents=True)

    # The referenced Api project.
    (api / "Api.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n",
        encoding="utf-8",
    )

    # AppHost.csproj references Aspire.Hosting.AppHost AND ../Api/Api.csproj.
    (apphost / "AppHost.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk\">\n"
        "  <ItemGroup>\n"
        "    <PackageReference Include=\"Aspire.Hosting.AppHost\" Version=\"9.0.0\" />\n"
        "    <ProjectReference Include=\"..\\Api\\Api.csproj\" />\n"
        "  </ItemGroup>\n"
        "</Project>\n",
        encoding="utf-8",
    )

    # AppHost Program.cs wires Postgres + the Api project with a reference + env.
    (apphost / "Program.cs").write_text(
        "var builder = DistributedApplication.CreateBuilder(args);\n"
        "var db = builder.AddPostgres(\"db\");\n"
        "builder.AddProject<Projects.Api>(\"api\")\n"
        "       .WithReference(db)\n"
        "       .WithEnvironment(\"FEATURE_FLAG\", \"on\");\n"
        "builder.Build().Run();\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def test_aspire_apphost_emits_runtime_evidence(tmp_path: Path):
    repo = _make_aspire_repo(tmp_path / "repo")
    ws = _ws(repo)

    out = extract_runtime.run(ws)
    assert out is not None
    assert out == ws.fragments / "runtime.json"

    frag = load_json(out)

    # Invariant: runtime facts go to the deployment facet, never build relationships.
    assert frag["relationships"] == []
    assert frag["targets"] == []
    dep = frag["deployment"]

    node_ids = {n["id"] for n in dep["nodes"]}
    # infra:database:db node from AddPostgres("db")
    assert "infra:database:db" in node_ids
    db_node = next(n for n in dep["nodes"] if n["id"] == "infra:database:db")
    assert db_node["kind"] == "infra"
    assert db_node["subkind"] == "database"

    # The AddProject<Projects.Api> workload resolved to the real csproj id.
    api_id = "csharp:csproj:Api/Api.csproj"

    # A `calls` edge api -> db, carrying `runtime` evidence (the §5 kind).
    calls = [e for e in dep["edges"] if e["kind"] == "calls"]
    assert any(e["source"] == api_id and e["target"] == "infra:database:db"
               for e in calls), dep["edges"]
    call_edge = next(e for e in calls
                     if e["source"] == api_id and e["target"] == "infra:database:db")
    assert call_edge["evidence"][0]["type"] == "runtime"
    assert "WithReference(db)" in call_edge["evidence"][0]["detail"]

    # The WithEnvironment("FEATURE_FLAG") produced a runtime_config edge carrying only
    # the KEY (never the value "on"), also as `runtime` evidence (§8.3).
    cfg = [e for e in dep["edges"] if e["kind"] == "runtime_config"]
    assert cfg, dep["edges"]
    assert cfg[0]["evidence"][0]["type"] == "runtime"
    assert "FEATURE_FLAG" in cfg[0]["evidence"][0]["detail"]
    assert "\"on\"" not in cfg[0]["evidence"][0]["detail"]


def test_project_ref_resolves_to_real_csproj_id(tmp_path: Path):
    repo = _make_aspire_repo(tmp_path / "repo")
    ws = _ws(repo)
    out = extract_runtime.run(ws)
    assert out is not None
    frag = load_json(out)

    api_id = "csharp:csproj:Api/Api.csproj"
    # The csproj id appears as an edge source (the workload), never as a build target.
    sources = {e["source"] for e in frag["deployment"]["edges"]}
    assert api_id in sources


def test_empty_repo_is_noop(tmp_path: Path):
    repo = tmp_path / "empty"
    repo.mkdir()
    ws = _ws(repo)
    assert extract_runtime.run(ws) is None
    assert not (ws.fragments / "runtime.json").exists()


def test_build_only_repo_is_noop(tmp_path: Path):
    """A plain csproj with no Aspire/hosted-service signal -> no fragment."""
    repo = tmp_path / "buildonly"
    (repo / "Lib").mkdir(parents=True)
    (repo / "Lib" / "Lib.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8")
    ws = _ws(repo)
    assert extract_runtime.run(ws) is None


def test_determinism_two_runs_byte_identical(tmp_path: Path):
    repo = _make_aspire_repo(tmp_path / "repo")
    ws = _ws(repo)
    out1 = extract_runtime.run(ws)
    b1 = out1.read_bytes()
    out2 = extract_runtime.run(ws)
    b2 = out2.read_bytes()
    assert b1 == b2


def test_hosted_service_node(tmp_path: Path):
    """AddHostedService<TWorker>() becomes a background runtime node."""
    repo = tmp_path / "repo"
    worker_dir = repo / "Worker"
    worker_dir.mkdir(parents=True)
    (worker_dir / "Worker.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk.Worker\"></Project>\n", encoding="utf-8")
    (worker_dir / "Program.cs").write_text(
        "var b = Host.CreateApplicationBuilder(args);\n"
        "b.Services.AddHostedService<EmailWorker>();\n"
        "b.Build().Run();\n",
        encoding="utf-8",
    )
    ws = _ws(repo)
    out = extract_runtime.run(ws)
    assert out is not None
    frag = load_json(out)
    node_ids = {n["id"] for n in frag["deployment"]["nodes"]}
    assert "deploy:image:EmailWorker" in node_ids
    # and a calls edge from the host csproj to the worker, with runtime evidence
    edge = next((e for e in frag["deployment"]["edges"]
                 if e["target"] == "deploy:image:EmailWorker"), None)
    assert edge is not None
    assert edge["evidence"][0]["type"] == "runtime"


# ---------------------------------------------------------------------------
# mine_test_scenarios()
# ---------------------------------------------------------------------------

def _curated_facts() -> dict:
    """Two containers (orders, payments) with ONE real edge orders -> payments."""
    return {
        "targets": [
            {"id": "csharp:csproj:Orders/Orders.csproj",
             "container_id": "c-orders", "container_name": "orders"},
            {"id": "csharp:csproj:Payments/Payments.csproj",
             "container_id": "c-payments", "container_name": "payments"},
            {"id": "csharp:csproj:Shipping/Shipping.csproj",
             "container_id": "c-shipping", "container_name": "shipping"},
        ],
        "relationships": [
            {"source": "csharp:csproj:Orders/Orders.csproj",
             "target": "csharp:csproj:Payments/Payments.csproj",
             "kind": "project_ref"},
        ],
    }


def test_mine_test_scenarios_keeps_real_edge_drops_phantom(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # A test that calls orders.X() then payments.Y() (a REAL edge orders->payments),
    # then shipping.Z() (orders->payments is real but payments->shipping is NOT).
    (repo / "FlowTests.cs").write_text(
        "public class FlowTests {\n"
        "  public async Task Place() {\n"
        "    await orders.PlaceOrder();\n"
        "    await payments.Charge();\n"
        "    await shipping.Ship();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    ws = _ws(repo)
    candidates = extract_runtime.mine_test_scenarios(ws, _curated_facts())

    assert len(candidates) == 1
    steps = candidates[0]["steps"]
    # orders -> payments survives (real edge); payments -> shipping is dropped (phantom).
    pairs = {(s["from"], s["to"]) for s in steps}
    assert ("orders", "payments") in pairs
    assert ("payments", "shipping") not in pairs
    assert len(steps) == 1


# ---------------------------------------------------------------------------
# _resolve_project_refs / _resolve_proj — separator-normalized alias + ambiguity guard
# ---------------------------------------------------------------------------

def _apphost_csproj(root: Path, ref_includes: list[str]) -> Path:
    """Write an AppHost.csproj under *root* that ProjectReferences each path in
    *ref_includes*, and return its path."""
    apphost = root / "AppHost"
    apphost.mkdir(parents=True, exist_ok=True)
    refs = "".join(
        f"    <ProjectReference Include=\"{inc}\" />\n" for inc in ref_includes)
    (apphost / "AppHost.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk\">\n"
        "  <ItemGroup>\n"
        "    <PackageReference Include=\"Aspire.Hosting.AppHost\" Version=\"9.0.0\" />\n"
        + refs +
        "  </ItemGroup>\n"
        "</Project>\n",
        encoding="utf-8",
    )
    return apphost / "AppHost.csproj"


def test_normalized_alias_resolves_benign_single_stem(tmp_path: Path):
    """The §5/R4 benign case: `Basket.API` ⇄ `Projects.Basket_API` with NO colliding
    sibling still resolves via the separator-normalized alias."""
    repo = tmp_path / "repo"
    (repo / "Basket.API").mkdir(parents=True)
    (repo / "Basket.API" / "Basket.API.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n", encoding="utf-8")
    csproj = _apphost_csproj(repo, ["..\\Basket.API\\Basket.API.csproj"])

    refs = extract_runtime._resolve_project_refs(repo, csproj)
    basket_id = "csharp:csproj:Basket.API/Basket.API.csproj"

    # exact stem resolves
    assert extract_runtime._resolve_proj(refs, "Basket.API") == basket_id
    # and the Projects.Basket_API token (underscores) falls through normalization to it
    assert extract_runtime._resolve_proj(refs, "Basket_API") == basket_id


def test_ambiguous_normalization_collision_declines_both(tmp_path: Path):
    """Two genuinely-DIFFERENT ProjectReferences whose stems normalize to the SAME key
    (`Basket.API` and `BasketApi` -> `basketapi`) must NOT silently bind a fall-through
    token to whichever was processed first: the ambiguous normalized alias is declined
    (returns None), an honest unresolved observation. EXACT-stem resolution stays correct."""
    repo = tmp_path / "repo"
    (repo / "Basket.API").mkdir(parents=True)
    (repo / "Basket.API" / "Basket.API.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n", encoding="utf-8")
    (repo / "BasketApi").mkdir(parents=True)
    (repo / "BasketApi" / "BasketApi.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n", encoding="utf-8")

    csproj = _apphost_csproj(repo, [
        "..\\Basket.API\\Basket.API.csproj",
        "..\\BasketApi\\BasketApi.csproj",
    ])

    refs = extract_runtime._resolve_project_refs(repo, csproj)
    dotted_id = "csharp:csproj:Basket.API/Basket.API.csproj"
    camel_id = "csharp:csproj:BasketApi/BasketApi.csproj"

    # EXACT-stem resolution stays intact and correct for BOTH distinct projects.
    assert extract_runtime._resolve_proj(refs, "Basket.API") == dotted_id
    assert extract_runtime._resolve_proj(refs, "BasketApi") == camel_id

    # The normalized fallback is AMBIGUOUS (both stems -> `basketapi`), so a token that must
    # fall through normalization (`Projects.Basket_API` -> `Basket_API`) resolves to NEITHER
    # — it does NOT bind to the first-processed stem.
    assert extract_runtime._resolve_proj(refs, "Basket_API") is None
    # the normalized key must NOT have silently bound to either real cid
    assert extract_runtime._normalize_proj_token("Basket.API") == "basketapi"
    assert refs.get("basketapi") not in (dotted_id, camel_id)


def test_mine_test_scenarios_no_tests_is_empty(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _ws(repo)
    assert extract_runtime.mine_test_scenarios(ws, _curated_facts()) == []


def test_write_scenario_candidates_writes_yaml(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "FlowTests.cs").write_text(
        "await orders.PlaceOrder();\nawait payments.Charge();\n", encoding="utf-8")
    ws = _ws(repo)
    candidates = extract_runtime.mine_test_scenarios(ws, _curated_facts())
    out = extract_runtime.write_scenario_candidates(ws, candidates)
    assert out is not None
    assert out == ws.generated / "scenario-candidates.yaml"
    text = out.read_text(encoding="utf-8")
    assert "scenarios:" in text
    assert "orders" in text and "payments" in text
    # LF only (byte-stability gate)
    assert "\r\n" not in text


def test_write_scenario_candidates_empty_writes_nothing(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _ws(repo)
    assert extract_runtime.write_scenario_candidates(ws, []) is None
    assert not (ws.generated / "scenario-candidates.yaml").exists()
