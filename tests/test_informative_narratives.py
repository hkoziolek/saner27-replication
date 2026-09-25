"""Informative-llm-narratives plan (2026-06-15) — the §2 D/C/ADR inputs, §4 length bands,
§5 differential gate, §3.1 agentic deep tier, and the §6 on-demand serve surface.

These lock the NEW behaviour that makes the §4 narrative worth reading:
  * (D) the Roslyn ``apiSkeleton`` block lifts into an ``api-skeleton/1`` sidecar and is
    stripped from the fact fragment (outside the determinism hash, like file-deps);
  * (C) anchor selection is a pure, hashable function of facts + file-deps;
  * ADR linkage feeds ``adr:<id>`` cites that resolve in the validator;
  * the band is deterministic and sets the payload's output ceiling;
  * the differential gate drops a key point that merely restates a deterministic field;
  * the deep tier records ``method: agentic``;
  * the §6 on-demand narrate/egress-preview engine + serve routes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import run_toy_pipeline

from anon import anchors, apiskeleton
from anon.jsonio import dump_json, load_json
from anon.stages import enrich_llm
from anon.paths import resolve_workspace


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    return run_toy_pipeline(tmp_path_factory.mktemp("infonarr") / "architecture")


REDACTION = {"secret_patterns": [], "deny_list": [], "max_snippet_chars": 280}


# --------------------------------------------------------------- (D) api skeleton

def test_apiskeleton_lift_writes_sidecar_and_strips(tmp_path):
    """The helper's ``apiSkeleton`` block lifts into an ``api-skeleton/1`` sidecar and is
    stripped from the fact fragment (mirrors the file-deps lift)."""
    from anon.stages.extract_csharp_facts import _lift_apiskeleton_sidecar
    ws = resolve_workspace(str(tmp_path), str(tmp_path / "architecture"))
    ws.fragments.mkdir(parents=True, exist_ok=True)
    frag = ws.fragments / "roslyn-csharp.json"
    dump_json({
        "schema_version": "1.0",
        "targets": [{"id": "csharp:csproj:src/A/A.csproj", "type": "csproj"}],
        "apiSkeleton": {
            "csharp:csproj:src/A/A.csproj": [
                {"name": "OrderRepository", "kind": "class", "base": "DbContext",
                 "interfaces": ["IOrderRepository"], "attributes": ["ApiController"],
                 "doc": "Persists orders.",
                 "members": [{"sig": "Task<int> SaveAsync(CancellationToken ct)"}]},
            ],
        },
    }, frag)
    _lift_apiskeleton_sidecar(ws, frag)
    # the block is stripped from the (determinism-hashed) fact fragment
    assert "apiSkeleton" not in load_json(frag)
    # and a sidecar carries it, keyed `skeletons` (NOT `targets`, which normalize iterates)
    sidecars = apiskeleton.load_sidecars(ws)
    assert len(sidecars) == 1 and "targets" not in sidecars[0]
    skel = apiskeleton.load(ws)
    types = skel["csharp:csproj:src/A/A.csproj"]
    assert types[0]["name"] == "OrderRepository"
    assert types[0]["interfaces"] == ["IOrderRepository"]


def test_apiskeleton_sidecar_does_not_break_normalize(tmp_path):
    """An api-skeleton sidecar is globbed by normalize_facts like any *.json fragment; with
    no ``targets`` key it passes through harmlessly (the bug that the rename fixed)."""
    from anon.stages import normalize_facts
    ws = resolve_workspace(str(tmp_path), str(tmp_path / "architecture"))
    ws.fragments.mkdir(parents=True, exist_ok=True)
    apiskeleton.write_sidecar(ws, apiskeleton.make_sidecar(
        extractor={"name": "roslyn-csharp", "version": "0.3.0"}, language="cs",
        skeletons={"csharp:csproj:src/A/A.csproj": [{"name": "X", "kind": "class"}]}))
    # must not raise (sidecar has no `targets` list to iterate)
    facts = normalize_facts.merge(normalize_facts.load_fragments(ws))
    assert facts.get("targets") == []


# --------------------------------------------------------------- (C) anchors (§2.2)

def test_select_anchors_is_pure_and_ranked():
    members = {"docs/README.md", "src/Program.cs", "src/Core.cs", "src/Util.cs"}
    edges = {("src/Program.cs", "src/Core.cs"), ("src/Util.cs", "src/Core.cs"),
             ("src/Program.cs", "src/Util.cs")}
    sel = anchors.select_anchors(members, edges)
    kinds = [s["kind"] for s in sel]
    assert kinds[0] == "readme" and kinds[1] == "entrypoint"
    # Core.cs has the highest in-element fan-in (2) → the top centrality pick
    centrality = [s["path"] for s in sel if s["kind"] == "centrality"]
    assert centrality[0] == "src/Core.cs"


def test_anchor_excerpts_reads_head_and_respects_budget(tmp_path):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "README.md").write_text("# Title\n" + "x" * 5000, encoding="utf-8")
    (tmp_path / "src" / "Program.cs").write_text("class P { static void Main(){} }\n",
                                                 encoding="utf-8")
    members = {"README.md", "src/Program.cs"}
    out = anchors.anchor_excerpts(tmp_path, members, set(), budget_chars=900)
    assert out and out[0]["kind"] == "readme"
    # head-capped (never the whole 5000-char file)
    assert all(len(e["text"]) <= anchors.HEAD_CHARS for e in out)


# --------------------------------------------------------------- ADR linkage (§2.3)

def test_adr_inputs_from_tags(toy):
    curated = load_json(toy.ws.curated_facts)
    # stamp a synthetic adr:<id> tag on one target (as adr.run would)
    tid = curated["targets"][0]["id"]
    curated["targets"][0].setdefault("tags", []).append("adr:0002")
    per_target, _titles = enrich_llm.adr_inputs(toy.ws, curated)
    assert per_target.get(tid) == [{"id": "0002", "title": "", "status": ""}]


# --------------------------------------------------------------- length bands (§4)

def test_bands_from_scores_percentile():
    bands = enrich_llm._bands_from_scores({"a": 1.0, "b": 5.0, "c": 9.0})
    assert bands == {"a": "leaf", "b": "standard", "c": "hub"}
    # a lone element bands as standard (no percentile to place it)
    assert enrich_llm._bands_from_scores({"only": 3.0}) == {"only": "standard"}


def test_compute_size_bands_covers_containers_and_targets(toy):
    curated = load_json(toy.ws.curated_facts)
    containers, cedges, _ = enrich_llm.narrative_context(curated)
    bands = enrich_llm.compute_size_bands(curated, containers, cedges)
    assert set(bands.values()) <= {"leaf", "standard", "hub"}
    assert "container:coreLibrary" in bands
    # every member target is also banded
    assert all(t["id"] in bands for t in curated["targets"])


# --------------------------------------------------------------- differential gate (§5)

@pytest.mark.parametrize("text,restates", [
    ("Depends on Catalog, Basket, and Ordering.", True),
    ("Built with ASP.NET and EF Core.", True),
    ("Has 4 components.", True),
    ("Owns the ordering domain so that order lifecycle invariants stay in one place.", False),
    ("Coordinates checkout across services, the saga pattern.", False),
    ("Depends on Catalog so that pricing stays authoritative.", False),
    # regression (2026-06-16 review): composition/behaviour leads describe responsibilities,
    # not deterministic-field restatements — they must NOT be dropped.
    ("Includes retry and idempotency handling for resilience.", False),
    ("Provides a repository abstraction over the order aggregate.", False),
    ("Contains the checkout saga and basket reconciliation logic.", False),
    ("Uses the mediator pattern to decouple command handlers.", False),
])
def test_restates_deterministic_field(text, restates):
    assert enrich_llm._restates_deterministic_field(text) is restates


def test_differential_gate_drops_restating_point():
    band = "standard"
    rec = {"element_id": "container:x", "abstract_md": "ok",
           "key_points": [{"text": "Depends on A, B, C.", "cites": ["container:x"]},
                          {"text": "Owns the X domain so that invariants hold.",
                           "cites": ["container:x"]}],
           "naming_confidence": "high"}
    shaped = enrich_llm._apply_band_and_differential(rec, band)
    assert len(shaped["key_points"]) == 1
    assert "Owns" in shaped["key_points"][0]["text"]
    assert shaped["size_band"] == "standard"
    # if EVERY point restates, the record degrades to honest absence (None)
    only_restate = dict(rec, key_points=[rec["key_points"][0]])
    assert enrich_llm._apply_band_and_differential(only_restate, band) is None


def test_band_truncates_overlong_abstract():
    rec = {"element_id": "c", "abstract_md": "word " * 100,
           "key_points": [{"text": "Owns it because reasons.", "cites": ["c"]}],
           "naming_confidence": "high"}
    shaped = enrich_llm._apply_band_and_differential(rec, "leaf")
    assert len(shaped["abstract_md"]) <= enrich_llm.NARRATIVE_BANDS["leaf"]["max_abstract"]


# --------------------------------------------------------------- payload augmentation

def test_container_payload_carries_band_skeleton_adr(toy):
    curated = load_json(toy.ws.curated_facts)
    containers, cedges, _ = enrich_llm.narrative_context(curated)
    ctx = enrich_llm.NarrativeInputs(toy.ws, curated, containers, cedges)
    # inject a synthetic skeleton + ADR for a member of coreLibrary
    cid = "container:coreLibrary"
    member = sorted(containers[cid].members)[0]
    ctx.api_skeleton = {member: [{"name": "Foo", "kind": "class",
                                  "interfaces": ["IRequestHandler"]}]}
    ctx.adrs_by_target = {member: [{"id": "0001", "title": "Event log is private",
                                    "status": "accepted"}]}
    p = enrich_llm.container_payload(cid, containers, cedges, curated, REDACTION,
                                     ctx=ctx, band="standard")
    assert p["size_band"] == "standard"
    assert p["max_key_points"] == 3
    assert p["api_skeleton"][0]["name"] == "Foo"
    assert p["adrs"] == [{"id": "0001", "title": "Event log is private"}]
    assert "adr:0001" in p["citable_ids"]


# --------------------------------------------------------------- agentic deep tier (§3.1)

class _Provider:
    model_id = "mock-1"

    def complete(self, system_prompt, prompt, facts):
        return {"element_id": facts["element_id"],
                "element_kind": facts.get("element_kind"),
                "abstract_md": "Owns the domain so that invariants hold.",
                "key_points": [{"text": "Owns it because domain.",
                                "cites": [facts["citable_ids"][0]]}],
                "naming_confidence": "high"}


def test_agentic_deep_marks_method(toy):
    curated = load_json(toy.ws.curated_facts)
    containers, cedges, _ = enrich_llm.narrative_context(curated)
    ctx = enrich_llm.NarrativeInputs(toy.ws, curated, containers, cedges)
    deep = {"container:coreLibrary"}
    recs, _cache = enrich_llm.propose_narratives(curated, _Provider(), REDACTION,
                                                 ctx=ctx, deep=deep)
    assert recs["container:coreLibrary"]["method"] == "agentic"
    assert recs["container:messaging"]["method"] == "deterministic"


def test_agentic_augment_adds_oracle_context(toy):
    """The §3.1 deep tier folds read-only oracle context (blast-radius + dependents) into
    the payload and flags it agentic."""
    curated = load_json(toy.ws.curated_facts)
    containers, cedges, _ = enrich_llm.narrative_context(curated)
    ctx = enrich_llm.NarrativeInputs(toy.ws, curated, containers, cedges)
    base = enrich_llm.container_payload("container:coreLibrary", containers, cedges,
                                        curated, REDACTION, ctx=ctx, band="standard")
    aug = enrich_llm._agentic_augment(base, "container:coreLibrary", curated, ctx, REDACTION)
    assert aug["method"] == "agentic"
    # coreLibrary is depended-on by other containers → oracle context is present
    assert "oracle" in aug and "blast_radius" in aug["oracle"]


def test_default_method_is_deterministic(toy):
    curated = load_json(toy.ws.curated_facts)
    recs, _ = enrich_llm.propose_narratives(curated, _Provider(), REDACTION)
    assert all(r.get("method") == "deterministic" for r in recs.values())
    assert all(r.get("size_band") in {"leaf", "standard", "hub"} for r in recs.values())


# --------------------------------------------------------------- on-demand (§6.5)

def test_narrate_element_patches_enriched(toy):
    # seed a no-llm enriched file, then narrate ONE container on demand
    enrich_llm.run(toy.ws, "no-llm")
    rec = enrich_llm.narrate_element(toy.ws, _Provider(), "container:messaging", "container")
    assert rec and rec["abstract_md"]
    enriched = load_json(toy.ws.enriched_facts)
    assert "container:messaging" in enriched["containers_narrative"]
    assert "container:messaging" in enriched["provenance"]["egress_container_ids"]
    # restore
    enrich_llm.run(toy.ws, "no-llm")


def test_narrate_datasheet_reemit_keeps_manually_grouped_container(tmp_path):
    """Regression: generating a narrative for a container that was manually grouped AFTER
    the last enrich must not strand its datasheet. The enriched snapshot pre-dates the new
    grouping, so re-emitting datasheets off the enrich-preferring default drops the row and
    ``GET /datasheets/{id}`` 404s right after the job succeeds. The narrate path rebuilds
    from the current curated structure (like the curate fast-loop) and carries the fresh
    narrative forward (informative-llm-narratives §6.5 / curate-fastloop datasheet fix)."""
    from anon.stages import generate_docs

    tr = run_toy_pipeline(tmp_path / "architecture")
    NEW = "container:nodeManager"

    # Simulate a Curate-tab regrouping done after enrich: move a target into a brand-new
    # container that the (now-stale) enriched snapshot has never seen.
    curated = load_json(tr.ws.curated_facts)
    curated["targets"][0]["container_id"] = NEW
    curated["targets"][0]["container_name"] = "Node Manager"
    dump_json(curated, tr.ws.curated_facts)

    rec = enrich_llm.narrate_element(tr.ws, _Provider(), NEW, "container")
    assert rec and rec["abstract_md"]
    # enriched really is structurally stale — it never got the new container.
    stale = load_json(tr.ws.enriched_facts)
    assert not any(t.get("container_id") == NEW for t in stale.get("targets", []))

    # Bug witness: the enrich-preferring default strands the new container's sheet.
    generate_docs.run_datasheets(tr.ws)
    bare = load_json(tr.ws.datasheets_json)
    assert not any(r["id"] == NEW for r in bare["containers"]), \
        "the enrich-preferring default should (still) drop the manually-grouped container"

    # Fix: rebuild from curated + the just-written narrative → sheet present AND narrated.
    generate_docs.run_datasheets(
        tr.ws, facts=enrich_llm.narrated_facts_for_datasheets(tr.ws))
    fixed = load_json(tr.ws.datasheets_json)
    row = next((r for r in fixed["containers"] if r["id"] == NEW), None)
    assert row is not None, "manually-grouped container must get a datasheet"
    assert row["narrative"]["source"] == "llm"
    assert row["narrative"]["stale"] is False, "the fresh narrative must not read as stale"


def test_element_egress_preview(toy):
    pv = enrich_llm.element_egress_preview(toy.ws, "container:coreLibrary", "container")
    assert pv["kind"] == "container"
    assert pv["size_band"] in {"leaf", "standard", "hub"}
    assert pv["estimated_tokens"] >= 1
    assert pv["payload"]["element_id"] == "container:coreLibrary"
    with pytest.raises(KeyError):
        enrich_llm.element_egress_preview(toy.ws, "container:nope", "container")


def test_deep_egress_preview_matches_deep_send(toy):
    """The §7 governance invariant: a deep (agentic) preview shows the SAME oracle-augmented
    payload the deep send will use — never understates egress."""
    shallow = enrich_llm.element_egress_preview(toy.ws, "container:coreLibrary", "container")
    deep = enrich_llm.element_egress_preview(toy.ws, "container:coreLibrary", "container",
                                             deep=True)
    assert deep["deep"] is True and shallow["deep"] is False
    assert "oracle" in deep["payload"] and "oracle" not in shallow["payload"]
    assert deep["payload"]["method"] == "agentic"
    # the deep payload is at least as large as the shallow one (hub band + oracle)
    assert deep["estimated_tokens"] >= shallow["estimated_tokens"]


def test_is_narratable(toy):
    assert enrich_llm.is_narratable(toy.ws, "container:coreLibrary", "container")
    assert not enrich_llm.is_narratable(toy.ws, "container:ghost", "container")
    tid = load_json(toy.ws.curated_facts)["targets"][0]["id"]
    assert enrich_llm.is_narratable(toy.ws, tid, "component")
    assert not enrich_llm.is_narratable(toy.ws, "csharp:csproj:ghost", "component")


# --------------------------------------------------------------- §6 serve surface

def _served(tmp_path):
    pytest.importorskip("fastapi", reason="[serve] extra not installed")
    from fastapi.testclient import TestClient
    from anon.serve import create_app
    tr = run_toy_pipeline(tmp_path / "architecture")
    return tr.ws, TestClient(create_app(tr.ws, token="tok", allowed_hosts=None))


def test_serve_narrative_egress_preview_route(tmp_path):
    _ws, c = _served(tmp_path)
    r = c.get("/api/v1/datasheets/container:coreLibrary/narrative/egress-preview"
              "?kind=container")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["kind"] == "container" and body["size_band"] in {"leaf", "standard", "hub"}
    assert body["estimated_tokens"] >= 1
    # unknown element → 404, not swallowed by the datasheet catch-all
    r2 = c.get("/api/v1/datasheets/container:ghost/narrative/egress-preview?kind=container")
    assert r2.status_code == 404


def test_serve_narrative_generate_requires_provider(tmp_path, monkeypatch):
    """With no LLM provider configured the generate job is refused 412 (never a 500 / silent
    network call). Monkeypatched so the test is deterministic regardless of local env."""
    import anon.cli as cli
    monkeypatch.setattr(cli, "_resolve_provider", lambda mode: (None, "no LLM config"))
    _ws, c = _served(tmp_path)
    r = c.post("/api/v1/datasheets/container:messaging/narrative/generate",
               headers={"X-Anon-Token": "tok"}, json={"kind": "container"})
    assert r.status_code == 412
    assert r.json()["error"]["code"] in {"no_llm_provider", "egress_refused"}
    # a bad kind is a 400
    r2 = c.post("/api/v1/datasheets/container:messaging/narrative/generate",
                headers={"X-Anon-Token": "tok"}, json={"kind": "bogus"})
    assert r2.status_code == 400


def test_serve_narrative_generate_token_gated(tmp_path):
    _ws, c = _served(tmp_path)
    r = c.post("/api/v1/datasheets/container:messaging/narrative/generate",
               json={"kind": "container"})
    assert r.status_code == 403  # no token


def test_serve_narrative_generate_unknown_id_is_404(tmp_path, monkeypatch):
    """A typo'd element id is a synchronous 404 (plan §6.5), not a 202 + opaque job failure."""
    import anon.cli as cli
    monkeypatch.setattr(cli, "_resolve_provider", lambda mode: (object(), "mock"))
    _ws, c = _served(tmp_path)
    r = c.post("/api/v1/datasheets/container:ghost/narrative/generate",
               headers={"X-Anon-Token": "tok"}, json={"kind": "container"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_element"


def test_serve_deep_egress_preview_route(tmp_path):
    _ws, c = _served(tmp_path)
    r = c.get("/api/v1/datasheets/container:coreLibrary/narrative/egress-preview"
              "?kind=container&deep=true")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["deep"] is True and "oracle" in body["payload"]


def test_serve_system_narrative_accept(tmp_path):
    """The system narrative uses the SAME accept path (plan §6.3): an edited body splices
    into datasheet-overrides.yaml under the system id and re-emits as source 'human'."""
    ws, c = _served(tmp_path)
    from anon.stages.generate_docs import system_identity
    sid, _name = system_identity(load_json(ws.curated_facts))
    r = c.post(f"/api/v1/datasheets/{sid}/narrative/accept",
               headers={"X-Anon-Token": "tok"},
               json={"abstract_md": "The whole system, hand-written.",
                     "key_points": [{"text": "kp", "cites": [sid]}]})
    assert r.status_code == 200 and r.json()["accepted"] == sid
    # the accept re-emits datasheets, so the system sheet now renders the human override
    after = c.get("/api/v1/datasheets/system").json()["data"]
    assert after["narrative"]["source"] == "human"
    assert after["narrative"]["abstract_md"] == "The whole system, hand-written."


def test_serve_narrative_reset(tmp_path):
    """Reset-to-proposal (plan §6.1) removes the hand override; 404 when none exists."""
    ws, c = _served(tmp_path)
    from anon.stages.generate_docs import system_identity
    sid, _name = system_identity(load_json(ws.curated_facts))
    # no override yet → reset is a 404
    r0 = c.post(f"/api/v1/datasheets/{sid}/narrative/reset",
                headers={"X-Anon-Token": "tok"})
    assert r0.status_code == 404
    # accept one, then reset removes it (reset needs If-Match now the file exists)
    acc = c.post(f"/api/v1/datasheets/{sid}/narrative/accept",
                 headers={"X-Anon-Token": "tok"},
                 json={"abstract_md": "x", "key_points": [{"text": "k", "cites": [sid]}]})
    sha = acc.json()["sha256"]
    r1 = c.post(f"/api/v1/datasheets/{sid}/narrative/reset",
                headers={"X-Anon-Token": "tok", "If-Match": sha})
    assert r1.status_code == 200 and r1.json()["accepted"] == sid
    after = c.get("/api/v1/datasheets/system").json()["data"]
    assert after["narrative"]["source"] != "human"
