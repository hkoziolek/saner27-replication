"""Shared-contract / codegen-contract extraction tests (plan §16#14 / §21.3).

Covers:
  - a repo with a shared ``.proto`` + two projects that reference it -> a first-class
    ``contract:proto:orders`` element (``type:"contract"``, ``language:"idl"``) plus
    medium-confidence ``package_ref`` ``uses contract`` edges from the referencing
    targets, tagged ``contract`` + ``cross-language``.
  - graceful no-op: a repo with no contract definition files -> ``run`` returns None.
  - schema validation of the produced contract target against the live 1.2 schema.
  - determinism: two runs produce byte-identical fragments.

Run just this file:

    .venv/Scripts/python.exe -m pytest tests/test_contracts.py -q
"""
from __future__ import annotations

from pathlib import Path

from anon.jsonio import dump_json, dumps_json, load_json
from anon.model import canonicalize, make_relationship
from anon.paths import resolve_workspace
from anon.stages import extract_contracts, normalize_facts, validate_facts

CONTRACT_ID = "contract:proto:orders"


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _make_repo(root: Path) -> Path:
    """A repo: proto/orders.proto + an Api csproj and a Worker dir that reference it."""
    (root / "proto").mkdir(parents=True)
    (root / "proto" / "orders.proto").write_text(
        'syntax = "proto3";\n'
        "package shop;\n"
        "message Orders {\n"
        "  int64 id = 1;\n"
        "}\n",
        encoding="utf-8",
    )

    # A C# API project whose source references the generated Orders type / imports the proto.
    (root / "src" / "Api").mkdir(parents=True)
    (root / "src" / "Api" / "Api.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"></Project>', encoding="utf-8"
    )
    (root / "src" / "Api" / "OrderService.cs").write_text(
        "using Grpc.Orders;\n"
        "namespace Api;\n"
        "public class OrderService {\n"
        "  public Orders Create() => new Orders();\n"
        "}\n",
        encoding="utf-8",
    )

    # A worker dir (no csproj alongside the .cs but a csproj at its root) that also references it.
    (root / "src" / "Worker").mkdir(parents=True)
    (root / "src" / "Worker" / "Worker.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"></Project>', encoding="utf-8"
    )
    (root / "src" / "Worker" / "Pump.cs").write_text(
        "namespace Worker;\n"
        "public class Pump {\n"
        "  // consumes orders.proto messages\n"
        "  public void Handle(Orders o) {}\n"
        "}\n",
        encoding="utf-8",
    )
    return root


def _target_by_id(fragment: dict, tid: str) -> dict:
    return next(t for t in fragment["targets"] if t["id"] == tid)


def _rel_by(fragment: dict, source: str, target: str) -> dict:
    return next(
        r for r in fragment["relationships"]
        if r["source"] == source and r["target"] == target
    )


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------

def test_run_writes_fragment(tmp_path: Path):
    repo = _make_repo(tmp_path / "repo")
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    out = extract_contracts.run(ws)
    assert out is not None
    assert out.exists()
    assert out.name == "contracts.json"


def test_contract_element_is_first_class(tmp_path: Path):
    repo = _make_repo(tmp_path / "repo")
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    out = extract_contracts.run(ws)
    assert out is not None
    frag = load_json(out)
    c = _target_by_id(frag, CONTRACT_ID)
    assert c["type"] == "contract"
    assert c["language"] == "idl"
    assert c["external"] is True
    assert c["name"] == "orders"
    assert c["path"] == "proto/orders.proto"
    assert "contract" in c["tags"]
    assert "external" in c["tags"]


def test_uses_contract_edges_from_referencing_targets(tmp_path: Path):
    repo = _make_repo(tmp_path / "repo")
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    out = extract_contracts.run(ws)
    assert out is not None
    frag = load_json(out)

    api_id = "csharp:csproj:src/Api/Api.csproj"
    worker_id = "csharp:csproj:src/Worker/Worker.csproj"

    for src in (api_id, worker_id):
        rel = _rel_by(frag, src, CONTRACT_ID)
        assert rel["kind"] == "uses contract"
        assert rel["evidence"][0]["type"] == "package_ref"
        assert "orders" in rel["evidence"][0]["detail"]
        assert rel["confidence"] == "medium"  # §21.3 shared-contract medium confidence
        assert "contract" in rel["tags"]
        assert "cross-language" in rel["tags"]


def test_endpoints_bind_to_real_csproj_ids(tmp_path: Path):
    """Referencing .cs files resolve to the same csharp:csproj:<path> the build graph uses."""
    repo = _make_repo(tmp_path / "repo")
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    out = extract_contracts.run(ws)
    assert out is not None
    frag = load_json(out)
    sources = {r["source"] for r in frag["relationships"]}
    assert "csharp:csproj:src/Api/Api.csproj" in sources
    assert "csharp:csproj:src/Worker/Worker.csproj" in sources
    # no dir: placeholder should be needed here — every .cs has an enclosing .csproj.
    assert not any(s.startswith("dir:") for s in sources)


def test_dir_placeholder_when_no_project_marker(tmp_path: Path):
    """A .proto referenced by a .cs with NO enclosing project -> dir: placeholder endpoint."""
    repo = tmp_path / "loose"
    (repo / "idl").mkdir(parents=True)
    (repo / "idl" / "ping.proto").write_text(
        'syntax = "proto3";\nmessage Ping {}\n', encoding="utf-8"
    )
    (repo / "loosecode").mkdir()
    (repo / "loosecode" / "use.cs").write_text(
        'using Grpc.Ping;\nclass C { Ping p; }\n', encoding="utf-8"
    )
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    out = extract_contracts.run(ws)
    assert out is not None
    frag = load_json(out)
    sources = {r["source"] for r in frag["relationships"]}
    assert "dir:loosecode" in sources


# ---------------------------------------------------------------------------
# Post-merge dir: rebind (normalize_facts.rebind_contract_dirs) — §16#14
#
# The contract extractor can emit a ``dir:<path>`` placeholder endpoint when no enclosing
# project marker is found. Those placeholders are not declared targets, so the extract-stage
# referential-integrity gate (run by cmd_extract BEFORE curate) would crash the run. The
# post-merge rebind pass in normalize must either bind the placeholder to a real target or
# prune the unresolvable edge so validation passes.
# ---------------------------------------------------------------------------

def _contract_rel(source: str) -> dict:
    """A contract package_ref edge exactly as build_fragment emits it (with a dir: source)."""
    rel = make_relationship(
        source=source,
        target=CONTRACT_ID,
        evidence=[{"type": "package_ref", "detail": "references orders"}],
        kind="uses contract",
    )
    rel["tags"] = ["contract", "cross-language"]
    rel["confidence"] = "medium"
    return rel


def _contract_target() -> dict:
    return {
        "id": CONTRACT_ID, "name": "orders", "type": "contract", "language": "idl",
        "external": True, "path": "proto/orders.proto", "tags": ["contract", "external"],
    }


def _write_fragments(ws, *fragments: dict) -> None:
    """Write each fragment as its own JSON file under the workspace fragments dir."""
    ws.fragments.mkdir(parents=True, exist_ok=True)
    for i, frag in enumerate(fragments):
        dump_json(frag, ws.fragments / f"frag-{i}.json")


def test_unresolvable_dir_contract_edge_is_pruned_not_crashed(tmp_path: Path):
    """An unbindable dir: contract source -> edge dropped, validate() does NOT raise (§16#14)."""
    # Build-graph fragment declares a real project; the contract fragment references a
    # directory (dir:loosecode) that no target's path covers -> unresolvable -> pruned.
    build_frag = {
        "schema_version": "1.2",
        "provenance": {"extractors": [{"name": "csharp-build-graph", "version": "1"}]},
        "targets": [{
            "id": "csharp:csproj:src/Api/Api.csproj", "name": "Api", "type": "csproj",
            "language": "csharp", "external": False, "path": "src/Api/Api.csproj",
        }],
        "relationships": [],
    }
    contract_frag = {
        "schema_version": "1.2",
        "provenance": {"extractors": [extract_contracts.EXTRACTOR]},
        "targets": [_contract_target()],
        "relationships": [_contract_rel("dir:loosecode")],
    }
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "architecture")
    _write_fragments(ws, build_frag, contract_frag)

    facts = normalize_facts.run(ws, repo="t", commit="c", generated_at="1970-01-01T00:00:00Z")

    # No dir: endpoint survives, and the dangling contract edge was pruned (counted).
    assert not any(
        r["source"].startswith("dir:") or r["target"].startswith("dir:")
        for r in facts["relationships"]
    )
    assert facts["provenance"]["contract_dirs_dropped"] >= 1
    # The crash is fixed: referential integrity + schema validation now pass.
    validate_facts.validate(facts)


def test_resolvable_dir_contract_edge_rebinds_to_target_id(tmp_path: Path):
    """A dir: source under a target's path is rebound to that target id; edge kept (§16#14)."""
    build_frag = {
        "schema_version": "1.2",
        "provenance": {"extractors": [{"name": "csharp-build-graph", "version": "1"}]},
        "targets": [{
            "id": "csharp:csproj:src/Worker/Worker.csproj", "name": "Worker", "type": "csproj",
            "language": "csharp", "external": False, "path": "src/Worker",
        }],
        "relationships": [],
    }
    contract_frag = {
        "schema_version": "1.2",
        "provenance": {"extractors": [extract_contracts.EXTRACTOR]},
        "targets": [_contract_target()],
        # dir:src/Worker/sub is under the Worker target's path (src/Worker) -> rebinds.
        "relationships": [_contract_rel("dir:src/Worker/sub")],
    }
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "architecture")
    _write_fragments(ws, build_frag, contract_frag)

    facts = normalize_facts.run(ws, repo="t", commit="c", generated_at="1970-01-01T00:00:00Z")

    sources = {r["source"] for r in facts["relationships"]}
    assert "csharp:csproj:src/Worker/Worker.csproj" in sources   # rebound to the real id
    assert not any(s.startswith("dir:") for s in sources)        # placeholder gone
    # the edge was KEPT (not dropped), so no drop count is recorded.
    assert "contract_dirs_dropped" not in facts["provenance"]
    validate_facts.validate(facts)


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

def test_no_contracts_returns_none(tmp_path: Path):
    repo = tmp_path / "plain"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Class1.cs").write_text(
        "namespace P; class Class1 {}", encoding="utf-8"
    )
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    result = extract_contracts.run(ws)
    assert result is None
    assert not (ws.fragments / "contracts.json").exists()


def test_generated_outputs_are_not_contracts(tmp_path: Path):
    """A generated .pb.cc / generated dir must NOT be picked up as a contract source."""
    repo = tmp_path / "gen"
    (repo / "proto").mkdir(parents=True)
    (repo / "proto" / "orders.proto").write_text(
        'syntax = "proto3";\nmessage Orders {}\n', encoding="utf-8"
    )
    # generated output dir + generated file — both must be ignored.
    (repo / "generated").mkdir()
    (repo / "generated" / "orders.pb.cc").write_text("// generated", encoding="utf-8")
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    out = extract_contracts.run(ws)
    assert out is not None
    frag = load_json(out)
    ids = {t["id"] for t in frag["targets"]}
    # only the real contract definition is an element, never the generated output.
    assert ids == {CONTRACT_ID}


# ---------------------------------------------------------------------------
# Schema validation (live 1.2 schema) + determinism
# ---------------------------------------------------------------------------

def test_contract_target_validates_against_live_schema(tmp_path: Path):
    repo = _make_repo(tmp_path / "repo")
    ws = resolve_workspace(repo, arch_dir=tmp_path / "architecture")
    out = extract_contracts.run(ws)
    assert out is not None
    frag = load_json(out)
    # Inject the runner-supplied provenance fields so the full provenance def validates.
    frag["provenance"]["repo"] = "test/contract-repo"
    frag["provenance"]["commit"] = "test"
    frag["provenance"]["generated_at"] = "2026-01-01T00:00:00Z"
    # validate() runs the JSON-Schema gate; referential integrity is N/A here because the
    # dir:/csproj: endpoints are not declared as targets in this isolated fragment, so we
    # validate the contract TARGET shape directly against the schema instead.
    schema = load_json(validate_facts.find_schema())
    import jsonschema
    jsonschema.validate(instance=frag, schema=schema)
    # the contract target satisfies the closed 1.2 enums (type:contract / language:idl).
    c = _target_by_id(frag, CONTRACT_ID)
    assert c["type"] == "contract" and c["language"] == "idl"


def test_two_runs_byte_identical(tmp_path: Path):
    repo_a = _make_repo(tmp_path / "a")
    repo_b = _make_repo(tmp_path / "b")
    ws_a = resolve_workspace(repo_a, arch_dir=tmp_path / "a-arch")
    ws_b = resolve_workspace(repo_b, arch_dir=tmp_path / "b-arch")
    out_a = extract_contracts.run(ws_a)
    out_b = extract_contracts.run(ws_b)
    assert out_a is not None and out_b is not None
    # same relative layout under two roots -> identical canonical fragment content.
    fa = canonicalize(load_json(out_a))
    fb = canonicalize(load_json(out_b))
    assert dumps_json(fa) == dumps_json(fb)
