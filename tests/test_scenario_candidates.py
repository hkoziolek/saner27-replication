"""Tests for the scenario-candidate pipeline (dynamic-view plan §2/§5/§8/§12).

Phase-2 coverage — Source B (Aspire `.WithReference` runtime topology) + the common
candidate envelope/index writer + the §5/R4 name-normalization fix:

  - The name-normalization fix (R4): `Projects.Basket_API` (underscore) resolves to the
    `Basket.API.csproj` stem (dot), so the genuine service-to-service `.WithReference`
    edges become real `csproj -> csproj` runtime edges instead of `deploy:image:*`
    fallbacks (the eShop yield prerequisite, §5).
  - Source B fan-out: an entry workload's N outgoing edges share ONE `order` (a parallel
    group, not a linearized DFS chain, §5/§2).
  - The common envelope: `proposal` (stable ids) + `validated_view` (build_dynamic_view
    output) present; runtime-only steps carry `dsl:false`; the quality-bar drop is logged.
  - Determinism (§11): byte-identical on a second run AND identical with vs without
    `obj/` / `bin/` present (the cross-environment hazard, §0.2).

The fixture is a minimal Aspire AppHost (`tests/fixtures/aspire-apphost/`) — NOT injected
into toy-repo, because `extract_runtime` rglobs `*.cs` repo-wide and would break the
byte-identical core golden (§12).
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from anon.jsonio import load_json
from anon.paths import resolve_workspace
from anon.stages import extract_runtime, scenario_candidates

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "aspire-apphost"

# The three workload containers (each csproj is its own container in this minimal fixture).
_WEBAPP = "csharp:csproj:WebApp/WebApp.csproj"
_BASKET = "csharp:csproj:Basket.API/Basket.API.csproj"
_CATALOG = "csharp:csproj:Catalog.API/Catalog.API.csproj"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ws(repo: Path):
    # Redirect artifacts out of tree (the fixture stays pristine).
    return resolve_workspace(repo, arch_dir=repo / "_arch")


def _emission_facts(deployment: dict) -> dict:
    """Minimal emission-normalized facts: each referenced csproj is its own container.
    WebApp is tagged sdk-role:service-web so it ranks as the salience entry point."""
    targets = []
    for cid, name, tag in (
        (_WEBAPP, "WebApp", "sdk-role:service-web"),
        (_BASKET, "Basket.API", None),
        (_CATALOG, "Catalog.API", None),
    ):
        targets.append({
            "id": cid, "container_id": cid, "container_name": name,
            "tags": [tag] if tag else [],
        })
    return {"targets": targets, "relationships": [], "deployment": deployment}


def _run_extract_and_candidates(repo: Path):
    """Extract runtime facts from *repo* and write candidate sidecars; return (ws, facts)."""
    ws = _ws(repo)
    out = extract_runtime.run(ws)
    assert out is not None, "the Aspire AppHost fixture must yield a runtime fragment"
    frag = load_json(out)
    facts = _emission_facts(frag["deployment"])
    scenario_candidates.run(ws, facts, sources=["runtime"])
    return ws, facts


def _copy_fixture(dst: Path) -> Path:
    shutil.copytree(FIXTURE, dst)
    return dst


# ---------------------------------------------------------------------------
# §5/R4 — name-normalization fix unblocks the workload->workload edges
# ---------------------------------------------------------------------------

def test_name_normalization_resolves_underscore_tokens(tmp_path: Path):
    """`Projects.Basket_API` resolves to the `Basket.API.csproj` stem, so the WebApp
    `.WithReference` edges are real csproj->csproj runtime calls (not deploy:image:*)."""
    repo = _copy_fixture(tmp_path / "repo")
    ws = _ws(repo)
    frag = load_json(extract_runtime.run(ws))
    calls = {(e["source"], e["target"]) for e in frag["deployment"]["edges"]
             if e["kind"] == "calls"}
    # WebApp -> Basket.API and WebApp -> Catalog.API are real workload->workload edges.
    assert (_WEBAPP, _BASKET) in calls, calls
    assert (_WEBAPP, _CATALOG) in calls, calls
    # No deploy:image:* fallback leaked in for these resolvable projects.
    assert not any(t.startswith("deploy:image:") for (s, t) in calls
                   if s == _WEBAPP), calls


def test_derive_runtime_scenarios_fanout_shares_order(tmp_path: Path):
    """The entry workload's two outgoing edges form ONE parallel group (shared `order`),
    not a linearized chain (§5/§2 fan-out)."""
    repo = _copy_fixture(tmp_path / "repo")
    frag = load_json(extract_runtime.run(_ws(repo)))
    facts = _emission_facts(frag["deployment"])
    raws = extract_runtime.derive_runtime_scenarios(facts)
    assert len(raws) == 1, [r["key"] for r in raws]
    raw = raws[0]
    assert raw["key"] == "runtime-webapp"
    assert raw["source"] == "runtime"
    assert raw["confidence"] == "high"
    assert raw["ordering_provenance"] == "declared-in-aspire"
    # both steps from the WebApp entry share order==1 (parallel fan-out).
    orders = [s["order"] for s in raw["steps"]]
    assert orders == [1, 1], orders
    froms = {s["from"] for s in raw["steps"]}
    assert froms == {_WEBAPP}
    tos = {s["to"] for s in raw["steps"]}
    assert tos == {_BASKET, _CATALOG}
    # from/to are STABLE ids; display names live only in from_name/to_name (§0.4#6).
    assert all(s["from"].startswith("csharp:csproj:") for s in raw["steps"])
    assert raw["steps"][0]["from_name"] == "WebApp"
    # the workload->infra hop (Basket.API -> redis) is recorded out-of-model, not a step.
    oom = raw["notes"]["out_of_model"]
    assert any(h["target"] == "infra:cache:redis" for h in oom), oom


# ---------------------------------------------------------------------------
# §8 — the common envelope + index
# ---------------------------------------------------------------------------

def test_candidate_envelope_and_index(tmp_path: Path):
    repo = _copy_fixture(tmp_path / "repo")
    ws, _facts = _run_extract_and_candidates(repo)

    cand = load_json(ws.scenario_candidates_dir / "runtime" / "runtime-webapp.json")
    assert cand["schema"] == "scenario-candidate/1"
    assert cand["source"] == "runtime"
    # proposal (the promotable material) + validated_view (the rendered view) BOTH present.
    assert "proposal" in cand and "validated_view" in cand
    assert cand["proposal"]["steps"][0]["from"].startswith("csharp:csproj:")
    view = cand["validated_view"]
    assert view["schema"] == "dynamic-view/2"
    # runtime-only steps carry dsl:false (no static container relationship to reference).
    ok_steps = [s for s in view["steps"] if s["status"] == "ok"]
    assert len(ok_steps) == 2
    assert all(s["evidence_layer"] == "runtime" and s["dsl"] is False for s in ok_steps)
    # salience computed deterministically (entry-point bonus + distinct-container count).
    assert cand["salience"] > 0
    assert any("entry-point" in r for r in cand["salience_reasons"])

    index = load_json(ws.scenario_candidates_dir / "index.json")
    rows = index["candidates"]
    assert len(rows) == 1
    row = rows[0]
    assert row["key"] == "runtime-webapp"
    assert row["source"] == "runtime"
    assert row["has_runtime_only"] is True
    assert row["dsl_steps"] == 0
    assert row["valid_steps"] == 2


def test_quality_bar_drop_is_logged(tmp_path: Path, caplog):
    """A candidate with <2 ok steps is dropped with a logged count, never silently (§1a#1)."""
    # A single-edge runtime graph -> one ok step -> below the 2-step floor.
    deployment = {
        "nodes": [],
        "edges": [{
            "source": _WEBAPP, "target": _BASKET, "kind": "calls",
            "evidence": [{"type": "runtime", "detail": "AddProject<Projects.WebApp>().WithReference(basketApi)"}],
        }],
    }
    facts = _emission_facts(deployment)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _ws(repo)
    with caplog.at_level(logging.INFO, logger="anon.scenario_candidates"):
        summary = scenario_candidates.run(ws, facts, sources=["runtime"])
    assert summary["dropped_quality_bar"] == 1
    assert summary["candidates"] == 0
    assert any("quality bar" in rec.getMessage() for rec in caplog.records), caplog.records
    # an absent candidate still writes a valid (empty) index — honest absence (§1a#5).
    index = load_json(ws.scenario_candidates_dir / "index.json")
    assert index["candidates"] == []


# ---------------------------------------------------------------------------
# §11 — determinism
# ---------------------------------------------------------------------------

def test_candidates_byte_identical_on_second_run(tmp_path: Path):
    repo = _copy_fixture(tmp_path / "repo")
    ws, facts = _run_extract_and_candidates(repo)
    cand_path = ws.scenario_candidates_dir / "runtime" / "runtime-webapp.json"
    idx_path = ws.scenario_candidates_dir / "index.json"
    b1_cand, b1_idx = cand_path.read_bytes(), idx_path.read_bytes()

    scenario_candidates.run(ws, facts, sources=["runtime"])
    assert cand_path.read_bytes() == b1_cand
    assert idx_path.read_bytes() == b1_idx
    # LF-only (byte-stability gate).
    assert b"\r\n" not in b1_cand
    assert b"\r\n" not in b1_idx


def test_candidates_identical_with_and_without_obj_bin(tmp_path: Path):
    """Cross-environment determinism (§0.2/§11): a gitignored locally-built obj/ + bin/
    must NOT change the candidate bytes (a fresh CI clone has none; a dev box does)."""
    clean = _copy_fixture(tmp_path / "clean")
    ws_clean, _ = _run_extract_and_candidates(clean)
    clean_cand = (ws_clean.scenario_candidates_dir / "runtime" / "runtime-webapp.json").read_bytes()
    clean_idx = (ws_clean.scenario_candidates_dir / "index.json").read_bytes()

    dirty = _copy_fixture(tmp_path / "dirty")
    # Simulate a local build leaving obj/ + bin/ with stray *.cs the rglob could pick up.
    for proj in ("AppHost", "WebApp", "Basket.API"):
        for art in ("obj", "bin"):
            d = dirty / proj / art
            d.mkdir(parents=True)
            (d / "Generated.AssemblyInfo.cs").write_text(
                "// auto-generated\nbuilder.AddProject<Projects.Bogus>(\"bogus\");\n",
                encoding="utf-8", newline="")
    ws_dirty, _ = _run_extract_and_candidates(dirty)
    dirty_cand = (ws_dirty.scenario_candidates_dir / "runtime" / "runtime-webapp.json").read_bytes()
    dirty_idx = (ws_dirty.scenario_candidates_dir / "index.json").read_bytes()

    assert clean_cand == dirty_cand
    assert clean_idx == dirty_idx


# ---------------------------------------------------------------------------
# §5/§14 phase 3 — Source B yield gate (>=1 workload->workload candidate)
# ---------------------------------------------------------------------------

# The environment scout: a REAL eShop AppHost lives at fixtures/eshop (gitignored, needs a
# full extract). Per §14-R4 the yield gate runs on the committed Phase-2 minimal Aspire
# fixture (deterministic, no clone) — the same workload->workload `.WithReference` shape.
ESHOP = Path(__file__).resolve().parents[1] / "fixtures" / "eshop"


def test_source_b_yields_at_least_one_workload_to_workload_candidate(tmp_path: Path):
    """The "request flow for free" claim, MEASURED not asserted (plan §5/§14 phase 3): Source
    B yields >=1 workload->workload candidate on the Aspire AppHost fixture (post the §5/R4
    name-normalization fix, which is already landed)."""
    repo = _copy_fixture(tmp_path / "repo")
    frag = load_json(extract_runtime.run(_ws(repo)))
    facts = _emission_facts(frag["deployment"])
    raws = extract_runtime.derive_runtime_scenarios(facts)
    # at least one candidate, with at least one workload->workload (csproj->csproj) step.
    assert raws, "Source B must yield >=1 candidate on the Aspire fixture"
    ww_steps = [s for r in raws for s in r["steps"]
                if s["from"].startswith("csharp:csproj:") and s["to"].startswith("csharp:csproj:")]
    assert len(ww_steps) >= 1, raws


# ---------------------------------------------------------------------------
# §6 / §14 phase 3 — Source C (graph-walk) entry->sink walk on a toy-like graph
# ---------------------------------------------------------------------------

# A toy-like container graph (mirrors tests/fixtures/toy-repo): Web -> {Domain, Messaging};
# Domain -> Common; Messaging -> Common. Entry = Web (zero in-degree); sink = Common.
_C_WEB = "container:webFrontend"
_C_DOMAIN = "container:domain"
_C_MESSAGING = "container:messaging"
_C_COMMON = "container:common"


def _walk_facts() -> dict:
    def tgt(cid, name):
        return {"id": cid, "container_id": cid, "container_name": name, "tags": []}

    def rel(s, t):
        return {"source": s, "target": t, "weight": 5,
                "evidence": [{"type": "project_ref", "detail": s}]}

    return {
        "targets": [tgt(_C_WEB, "Web"), tgt(_C_DOMAIN, "Domain"),
                    tgt(_C_MESSAGING, "Messaging"), tgt(_C_COMMON, "Common")],
        "relationships": [rel(_C_WEB, _C_DOMAIN), rel(_C_WEB, _C_MESSAGING),
                          rel(_C_DOMAIN, _C_COMMON), rel(_C_MESSAGING, _C_COMMON)],
    }


def test_graph_walk_entry_to_sink(tmp_path: Path):
    """Source C: single-source BFS from the zero-in-degree entry, fan-out shares an `order`,
    reaches the zero-out-degree sink; every step carries the synthesized marker (§6)."""
    facts = _walk_facts()
    raws = scenario_candidates.synthesize_graph_walk_scenarios(facts)
    assert len(raws) == 1, [r["key"] for r in raws]
    raw = raws[0]
    assert raw["key"] == "walk-web"
    assert raw["source"] == "graph-walk"
    assert raw["confidence"] == "low"
    assert raw["ordering_provenance"] == "synthesized"
    # the Web fan-out (Domain + Messaging) shares order 1; Domain/Messaging->Common is later.
    by_order = {}
    for s in raw["steps"]:
        by_order.setdefault(s["order"], set()).add((s["from"], s["to"]))
    assert by_order[1] == {(_C_WEB, _C_DOMAIN), (_C_WEB, _C_MESSAGING)}, by_order
    # Common is discovered once (parent-on-first-discovery pins a single path to it).
    common_in = [s for s in raw["steps"] if s["to"] == _C_COMMON]
    assert len(common_in) == 1, common_in
    assert common_in[0]["order"] == 2
    # the sink is reached and recorded.
    assert raw["notes"]["reached_sinks"] == [_C_COMMON]
    # every step is dependency-framed and carries the inline "synthesized" marker (not a
    # bare "Uses", never reading as observed behavior).
    assert all("synthesized" in s["description"] for s in raw["steps"])
    assert all("depends on" in s["description"] for s in raw["steps"])


def test_graph_walk_candidate_validates_and_is_byte_identical(tmp_path: Path):
    """The walk candidate flows through the ONE validator (build_dynamic_view) and is
    byte-identical on a second run (§11)."""
    facts = _walk_facts()
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _ws(repo)
    scenario_candidates.run(ws, facts, sources=["graph-walk"])
    cand_path = ws.scenario_candidates_dir / "graph-walk" / "walk-web.json"
    cand = load_json(cand_path)
    assert cand["schema"] == "scenario-candidate/1"
    view = cand["validated_view"]
    # every walk step is a real BUILD edge -> ok / dsl:true.
    ok = [s for s in view["steps"] if s["status"] == "ok"]
    assert len(ok) == 3
    assert all(s["evidence_layer"] == "build" and s["dsl"] is True for s in ok)
    b1 = cand_path.read_bytes()
    idx1 = (ws.scenario_candidates_dir / "index.json").read_bytes()
    scenario_candidates.run(ws, facts, sources=["graph-walk"])
    assert cand_path.read_bytes() == b1
    assert (ws.scenario_candidates_dir / "index.json").read_bytes() == idx1
    assert b"\r\n" not in b1


def test_graph_walk_off_by_default(tmp_path: Path):
    """Source C is OFF in the default sweep (plan §6): the default `run` (sources=["runtime"])
    over a graph with build edges but no Aspire wiring yields no graph-walk candidates."""
    facts = _walk_facts()
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _ws(repo)
    summary = scenario_candidates.run(ws, facts)   # default sources = runtime only
    assert summary["per_source"].get("graph-walk", 0) == 0
    assert not (ws.scenario_candidates_dir / "graph-walk").exists()


def test_graph_walk_candidate_cap_records_truncation():
    """The candidate cap is applied AFTER the key sort, recording notes.truncated:{cap,
    dropped} on every retained candidate — never a silent truncation (§6). `cap`/`dropped`
    describe the cap's own truncation only and do NOT claim a post-quality-bar emitted count
    (the quality bar runs downstream in `run()` — honest, never overstated)."""
    # Build N+5 independent entry->leaf chains so each entry yields exactly one candidate.
    n = scenario_candidates.MAX_WALK_CANDIDATES + 5
    targets, rels = [], []
    for i in range(n):
        entry, leaf = f"container:e{i:03d}", f"container:l{i:03d}"
        targets.append({"id": entry, "container_id": entry, "container_name": f"E{i}", "tags": []})
        targets.append({"id": leaf, "container_id": leaf, "container_name": f"L{i}", "tags": []})
        rels.append({"source": entry, "target": leaf, "weight": 5,
                     "evidence": [{"type": "project_ref", "detail": entry}]})
    facts = {"targets": targets, "relationships": rels}
    raws = scenario_candidates.synthesize_graph_walk_scenarios(facts)
    assert len(raws) == scenario_candidates.MAX_WALK_CANDIDATES
    trunc = raws[0]["notes"]["truncated"]
    assert trunc == {"cap": scenario_candidates.MAX_WALK_CANDIDATES, "dropped": 5}


# ---------------------------------------------------------------------------
# §8 — write robustness: slug-collision dedup + per-candidate fail-soft
# ---------------------------------------------------------------------------

def _dup_name_walk_facts() -> dict:
    """Two INDEPENDENT 3-node chains whose entries share the display name "Web" — so both
    yield a graph-walk candidate with the SAME key/slug (`walk-web`). Distinct container ids
    (entryA / entryB) are the only thing that differs, forcing the write to disambiguate."""
    def tgt(cid, name):
        return {"id": cid, "container_id": cid, "container_name": name, "tags": []}

    def rel(s, t):
        return {"source": s, "target": t, "weight": 5,
                "evidence": [{"type": "project_ref", "detail": s}]}

    return {
        "targets": [
            tgt("container:a-entry", "Web"), tgt("container:a-mid", "MidA"),
            tgt("container:a-leaf", "LeafA"),
            tgt("container:b-entry", "Web"), tgt("container:b-mid", "MidB"),
            tgt("container:b-leaf", "LeafB"),
        ],
        "relationships": [
            rel("container:a-entry", "container:a-mid"),
            rel("container:a-mid", "container:a-leaf"),
            rel("container:b-entry", "container:b-mid"),
            rel("container:b-mid", "container:b-leaf"),
        ],
    }


def test_write_disambiguates_colliding_slugs(tmp_path: Path):
    """Two distinct entry containers sharing a display name collide on the bare slug; the
    writer must give each a UNIQUE deterministic slug and keep the on-disk filename and the
    index row in agreement — no second body silently overwriting the first (plan §8)."""
    facts = _dup_name_walk_facts()
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _ws(repo)
    # Both raw candidates share key "walk-web" (same display name) -> same bare slug.
    raws = scenario_candidates.synthesize_graph_walk_scenarios(facts)
    assert [r["key"] for r in raws] == ["walk-web", "walk-web"], [r["key"] for r in raws]

    summary = scenario_candidates.run(ws, facts, sources=["graph-walk"])
    assert summary["candidates"] == 2, summary

    # Two DISTINCT files on disk (the second was NOT overwritten by sharing a filename).
    bodies = sorted((ws.scenario_candidates_dir / "graph-walk").glob("*.json"))
    assert len(bodies) == 2, bodies
    file_slugs = {p.stem for p in bodies}
    assert len(file_slugs) == 2, file_slugs
    assert "walk-web" in file_slugs   # the first-by-(source,key) keeps the bare slug

    # Index lists both with slugs that EXACTLY match the on-disk filenames (no mismatch).
    rows = load_json(ws.scenario_candidates_dir / "index.json")["candidates"]
    assert len(rows) == 2, rows
    index_slugs = {r["slug"] for r in rows}
    assert index_slugs == file_slugs, (index_slugs, file_slugs)
    # Every index row points at a body that actually exists (the inbox-row integrity claim).
    for r in rows:
        assert (ws.scenario_candidates_dir / "graph-walk" / f"{r['slug']}.json").exists(), r
        body = load_json(ws.scenario_candidates_dir / "graph-walk" / f"{r['slug']}.json")
        assert body["validated_view"]["slug"] == r["slug"]   # body's own slug == filename

    # Deterministic: the disambiguated slug is byte-identical on a second sweep.
    b_idx = (ws.scenario_candidates_dir / "index.json").read_bytes()
    scenario_candidates.run(ws, facts, sources=["graph-walk"])
    assert (ws.scenario_candidates_dir / "index.json").read_bytes() == b_idx
    assert sorted(p.stem for p in (ws.scenario_candidates_dir / "graph-walk").glob("*.json")) \
        == sorted(file_slugs)


def test_write_is_fail_soft_per_candidate(tmp_path: Path, caplog):
    """One envelope that fails inline validation is logged + skipped (no index row), the
    OTHER candidates are still written, and index.json is ALWAYS written from the survivors
    only — so files and index stay consistent (never an orphaned index-less tree, §8)."""
    facts = _walk_facts()
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _ws(repo)

    # Build two real envelopes via the normal path, then corrupt one so validation raises.
    from anon.stages.extract_runtime import derive_runtime_scenarios  # noqa: F401
    raws = scenario_candidates.synthesize_graph_walk_scenarios(facts)
    envs = [scenario_candidates.build_candidate(r, facts) for r in raws]
    envs = [e for e in envs if e is not None]
    assert len(envs) == 1, envs   # the toy graph yields one walk candidate

    # Make a SECOND, deliberately-invalid envelope (drops the required `schema` key) that
    # shares a slug with the good one so we also prove a failed write frees nothing wrong.
    bad = {k: v for k, v in envs[0].items() if k != "schema"}
    bad = {**bad}
    bad["validated_view"] = {**bad["validated_view"], "slug": "walk-bad"}
    bad["proposal"] = {**bad["proposal"], "key": "walk-bad"}

    with caplog.at_level(logging.WARNING, logger="anon.scenario_candidates"):
        summary = scenario_candidates.write_candidates(ws, [envs[0], bad])

    # The good candidate was written; the bad one was skipped (counted), not fatal.
    assert summary["candidates"] == 1, summary
    assert summary.get("write_failures") == 1, summary
    assert any("SKIPPING candidate" in rec.getMessage() for rec in caplog.records), caplog.records

    # index.json exists and lists ONLY the survivor; every row has a real body file.
    index = load_json(ws.scenario_candidates_dir / "index.json")
    assert index["schema"] == "scenario-candidate-index/1"
    assert len(index["candidates"]) == 1, index
    survivor_slug = index["candidates"][0]["slug"]
    assert (ws.scenario_candidates_dir / "graph-walk" / f"{survivor_slug}.json").exists()
    # the bad candidate left no orphan body behind.
    assert not (ws.scenario_candidates_dir / "graph-walk" / "walk-bad.json").exists()


# ---------------------------------------------------------------------------
# §1a#3 — salience sort determinism (source, -salience, slug)
# ---------------------------------------------------------------------------

def test_index_salience_sort_is_deterministic(tmp_path: Path):
    """Index rows sort by (source, -salience, slug); the order is stable + byte-identical."""
    # Two graph-walk candidates with different salience: the higher-salience one sorts first.
    facts = {
        "targets": [
            {"id": "container:big", "container_id": "container:big",
             "container_name": "Big", "tags": []},
            {"id": "container:b1", "container_id": "container:b1", "container_name": "B1", "tags": []},
            {"id": "container:b2", "container_id": "container:b2", "container_name": "B2", "tags": []},
            {"id": "container:small", "container_id": "container:small",
             "container_name": "Small", "tags": []},
            {"id": "container:s1", "container_id": "container:s1", "container_name": "S1", "tags": []},
            {"id": "container:s2", "container_id": "container:s2", "container_name": "S2", "tags": []},
        ],
        "relationships": [
            {"source": "container:big", "target": "container:b1", "weight": 50,
             "evidence": [{"type": "project_ref", "detail": "x"}]},
            {"source": "container:big", "target": "container:b2", "weight": 50,
             "evidence": [{"type": "project_ref", "detail": "x"}]},
            # Small has a 2-step chain so it clears the >=2 quality bar but stays low-salience.
            {"source": "container:small", "target": "container:s1", "weight": 1,
             "evidence": [{"type": "project_ref", "detail": "x"}]},
            {"source": "container:s1", "target": "container:s2", "weight": 1,
             "evidence": [{"type": "project_ref", "detail": "x"}]},
        ],
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = _ws(repo)
    scenario_candidates.run(ws, facts, sources=["graph-walk"])
    rows = load_json(ws.scenario_candidates_dir / "index.json")["candidates"]
    keys = [r["key"] for r in rows]
    # both candidates present; the high-salience "Big" flow sorts ahead of "Small".
    assert keys == ["walk-big", "walk-small"], rows
    assert rows[0]["salience"] > rows[1]["salience"]
    # byte-identical on a second sweep (the sort is stable).
    b1 = (ws.scenario_candidates_dir / "index.json").read_bytes()
    scenario_candidates.run(ws, facts, sources=["graph-walk"])
    assert (ws.scenario_candidates_dir / "index.json").read_bytes() == b1


# ---------------------------------------------------------------------------
# §10.3 — per-source sweep: a `--source X` run preserves other sources' candidates
# ---------------------------------------------------------------------------

def test_per_source_sweep_preserves_other_sources(tmp_path: Path):
    """Running one source sweeps ONLY that source's subdir — candidates from a prior
    `arch scenarios --source Y` run stay in the inbox (index) until re-run/promote/discard
    (plan §10.3). The index is rebuilt from the FULL on-disk tree each run."""
    repo = _copy_fixture(tmp_path / "repo")
    ws = _ws(repo)
    frag = load_json(extract_runtime.run(ws))
    facts = _emission_facts(frag["deployment"])

    # 1) Source B (runtime) only.
    scenario_candidates.run(ws, facts, sources=["runtime"])
    idx = load_json(ws.scenario_candidates_dir / "index.json")
    runtime_keys = {r["key"] for r in idx["candidates"] if r["source"] == "runtime"}
    assert runtime_keys, "runtime should yield >=1 candidate on the Aspire fixture"

    # 2) Source C (graph-walk) only — must NOT wipe the runtime candidate(s).
    scenario_candidates.run(ws, facts, sources=["graph-walk"])
    keys2 = {(r["source"], r["key"]) for r in
             load_json(ws.scenario_candidates_dir / "index.json")["candidates"]}
    assert all(("runtime", k) in keys2 for k in runtime_keys), \
        "a graph-walk run must preserve the runtime candidates (per-source sweep)"
    assert (ws.scenario_candidates_dir / "runtime" / "runtime-webapp.json").exists()

    # 3) Re-running runtime re-sweeps ONLY runtime; any graph-walk candidates persist.
    gw_before = {(r[0], r[1]) for r in keys2 if r[0] == "graph-walk"}
    scenario_candidates.run(ws, facts, sources=["runtime"])
    keys3 = {(r["source"], r["key"]) for r in
             load_json(ws.scenario_candidates_dir / "index.json")["candidates"]}
    assert gw_before <= keys3, "re-running runtime must preserve graph-walk candidates"


# ---------------------------------------------------------------------------
# §8 — the `arch scenarios` CLI verb
# ---------------------------------------------------------------------------

def test_cli_scenarios_writes_index(tmp_path: Path):
    """`arch scenarios` loads curated facts, normalizes, sweeps, exits 0, writes index.json
    (the smoke path) — and routes through emission_normalized_facts, never scenarios.run."""
    from anon.cli import main as cli_main

    # A minimal curated-facts.json under the arch-dir so run_cli finds a fact model.
    repo = tmp_path / "repo"
    repo.mkdir()
    arch = tmp_path / "arch"
    ws = resolve_workspace(repo, arch_dir=arch)
    ws.curated_facts.parent.mkdir(parents=True, exist_ok=True)
    from anon.jsonio import dump_json
    dump_json(_walk_facts(), ws.curated_facts)

    # default sweep (Source B only): no Aspire wiring -> empty index, exit 0.
    rc = cli_main(["scenarios", "--repo", str(repo), "--arch-dir", str(arch), "--no-llm"])
    assert rc == 0
    idx = load_json(ws.scenario_candidates_dir / "index.json")
    assert idx["schema"] == "scenario-candidate-index/1"
    assert idx["candidates"] == []

    # opt-in graph-walk: the toy-like graph yields the walk-web candidate.
    rc = cli_main(["scenarios", "--repo", str(repo), "--arch-dir", str(arch),
                   "--source", "graph-walk", "--no-llm"])
    assert rc == 0
    rows = load_json(ws.scenario_candidates_dir / "index.json")["candidates"]
    assert [r["key"] for r in rows] == ["walk-web"]


def test_cli_scenarios_skips_combined_workspace(tmp_path: Path, caplog):
    """A combined (<repo>::) workspace is skipped with a logged note (plan §1 non-goals)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    arch = tmp_path / "arch"
    ws = resolve_workspace(repo, arch_dir=arch)
    ws.curated_facts.parent.mkdir(parents=True, exist_ok=True)
    from anon.jsonio import dump_json
    combined = {
        "targets": [{"id": "repoA::container:web", "container_id": "repoA::container:web",
                     "container_name": "Web", "tags": []}],
        "relationships": [],
    }
    dump_json(combined, ws.curated_facts)
    with caplog.at_level(logging.INFO, logger="anon.scenario_candidates"):
        summary = scenario_candidates.run_cli(ws, sources=["runtime"])
    assert summary["skipped"] == "combined"
    assert any("combined" in rec.getMessage().lower() for rec in caplog.records)


def test_cli_scenarios_no_facts_returns_none(tmp_path: Path):
    """No fact model on disk -> run_cli returns None (the CLI prints 'run arch run first')."""
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = resolve_workspace(repo, arch_dir=tmp_path / "arch")
    assert scenario_candidates.run_cli(ws, sources=["runtime"]) is None
