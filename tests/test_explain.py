"""T2.6 — per-decision explainability (`arch explain`) + shadowed/empty-glob lint.

Three things are proven here:

  1. Curation stamps a ``grouped_by`` placement provenance on every kept target (members /
     members_glob / boundary / seam / singleton), citing the mapping-rules.yaml ``group:``
     index — and that provenance never leaks into the structural DSL (byte-identity holds).
  2. ``explain_target`` / ``explain_edge`` render that provenance + the cited edge evidence
     human-readable.
  3. ``lint_rules`` replays §7.4 first-match-wins grouping to flag the shadowed-glob footgun
     (a glob swept by an earlier ``*``), dead globs, and empty containers.
"""
from __future__ import annotations

from pathlib import Path

from anon import cli, explain
from anon.config import MappingRules
from anon.jsonio import load_json
from anon.stages.apply_mapping_rules import curate

from conftest import run_toy_pipeline

WEB = "csharp:csproj:src/Web/Toy.Web.csproj"
DOMAIN = "csharp:csproj:src/Domain/Toy.Domain.csproj"


def _facts(targets, rels=None):
    return {"schema_version": "1.2", "provenance": {}, "targets": targets,
            "relationships": rels or []}


def _run(argv: list[str]) -> int:
    # Route through cli.main so stdout is reconfigured to UTF-8 exactly as in production
    # (the rendered reports carry §/→/emoji); returns the command's exit code.
    return cli.main(argv)


# --- grouped_by provenance stamping (the ~2-3 additive lines in _group_for) ---------

def test_grouped_by_members_and_singleton() -> None:
    facts = _facts([
        {"id": WEB, "name": "Toy.Web", "type": "csproj", "language": "csharp", "path": "src/Web/Toy.Web.csproj"},
        {"id": "csharp:csproj:src/Lib/L.csproj", "name": "L", "type": "csproj",
         "language": "csharp", "path": "src/Lib/L.csproj"},
    ])
    rules = MappingRules(raw={"group": [{"container": "Web", "members": [WEB]}]})
    by_id = {t["id"]: t for t in curate(facts, rules)["targets"]}
    assert by_id[WEB]["grouped_by"] == {"signal": "members", "container": "Web", "rule_index": 0}
    # an unmatched target is a singleton — the provenance IS the needs-curation explanation.
    sing = by_id["csharp:csproj:src/Lib/L.csproj"]
    assert sing["grouped_by"] == {"signal": "singleton"}
    assert "needs-curation" in sing["tags"]


def test_grouped_by_glob_records_matched_pattern() -> None:
    facts = _facts([{"id": WEB, "name": "W", "type": "csproj", "language": "csharp",
                     "path": "src/Web/Toy.Web.csproj"}])
    rules = MappingRules(raw={"group": [{"container": "WebGroup", "members_glob": ["src/Web/*"]}]})
    gb = curate(facts, rules)["targets"][0]["grouped_by"]
    assert gb == {"signal": "members_glob", "container": "WebGroup",
                  "rule_index": 0, "pattern": "src/Web/*"}


def test_grouped_by_boundary_and_seam() -> None:
    # boundary: rule promotes a dep to an external system.
    db = {"id": "csharp:csproj:src/Db.csproj", "name": "Db", "type": "csproj",
          "language": "csharp", "path": "src/Db.csproj"}
    rules = MappingRules(raw={"boundary": {db["id"]: {"kind": "external", "name": "Database"}}})
    assert curate(_facts([db]), rules)["targets"][0]["grouped_by"] == {"signal": "boundary", "kind": "external"}

    # a native interop library is kept as a cross-language SEAM despite drop_external.
    native = {"id": "native:lib:foo", "name": "foo", "type": "unknown", "language": "native",
              "path": "", "external": True, "tags": ["native"]}
    kept = {t["id"]: t for t in curate(_facts([native]), MappingRules(raw={}))["targets"]}
    assert kept["native:lib:foo"]["grouped_by"] == {"signal": "seam", "kind": "external"}


def test_grouped_by_on_toy_does_not_leak_into_dsl(tmp_path: Path) -> None:
    """Every curated target carries grouped_by, but it NEVER reaches the structural DSL
    (the generator ignores it) — so the §1.1#2 byte-identity gate is unaffected."""
    tr = run_toy_pipeline(tmp_path / "architecture")
    curated = load_json(tr.ws.curated_facts)
    assert curated["targets"], "toy produced no targets"
    assert all("grouped_by" in t for t in curated["targets"])
    # rule indices cite the mapping-rules.yaml group: list (Web=0, Core Library=1, Messaging=2).
    by_id = {t["id"]: t for t in curated["targets"]}
    assert by_id[WEB]["grouped_by"] == {"signal": "members", "container": "Web Frontend", "rule_index": 0}
    assert b"grouped_by" not in tr.ws.components_dsl.read_bytes()
    assert b"grouped_by" not in tr.ws.relationships_dsl.read_bytes()


# --- explain rendering --------------------------------------------------------------

def test_explain_target_cites_the_rule_and_lists_edges() -> None:
    facts = _facts(
        [{"id": WEB, "name": "Toy.Web", "type": "csproj", "language": "csharp", "path": "src/Web/Toy.Web.csproj"},
         {"id": DOMAIN, "name": "Toy.Domain", "type": "csproj", "language": "csharp", "path": "src/Domain/Toy.Domain.csproj"}],
        rels=[{"id": f"rel:{WEB}->{DOMAIN}", "source": WEB, "target": DOMAIN, "kind": "uses",
               "weight": 11, "is_declared_dependency": True, "confidence": "medium",
               "evidence_strength": "project_ref + symbol_use corroborate",
               "evidence": [{"type": "project_ref", "detail": "<ProjectReference/>"}]}])
    rules = MappingRules(raw={"group": [{"container": "Web Frontend", "members": [WEB]}]})
    curated = curate(facts, rules)
    expl = explain.explain_target(curated, WEB, rules)
    assert "group #0" in expl["placement_reason"]
    assert expl["container_name"] == "Web Frontend"
    assert [e["target"] for e in expl["out_edges"]] == [DOMAIN]
    txt = explain.render_target(expl)
    assert "Web Frontend" in txt and "project_ref" in txt


def test_explain_edge_renders_cited_evidence() -> None:
    facts = _facts(
        [{"id": WEB, "name": "W", "type": "csproj", "language": "csharp", "path": "a"},
         {"id": DOMAIN, "name": "D", "type": "csproj", "language": "csharp", "path": "b"}],
        rels=[{"id": f"rel:{WEB}->{DOMAIN}", "source": WEB, "target": DOMAIN, "weight": 6,
               "is_declared_dependency": True, "confidence": "medium",
               "evidence": [{"type": "project_ref", "detail": "<ProjectReference Include='Domain'/>",
                             "visibility": "public"},
                            {"type": "symbol_use", "detail": "Toy.Domain", "count": 6}]}])
    edge = explain.explain_edge(facts, WEB, DOMAIN)
    assert {e["type"] for e in edge["evidence"]} == {"project_ref", "symbol_use"}
    # each cited evidence carries its engine-computed stratum (confidence._LAYER_OF), and the
    # edge summarises the distinct strata it spans — the GUI's per-edge layer badges (§5.8).
    by_type = {e["type"]: e for e in edge["evidence"]}
    assert by_type["project_ref"]["layer"] == "L0"
    assert by_type["symbol_use"]["layer"] == "L2"
    assert edge["evidence_layers"] == ["L0", "L2"]   # sorted, deduped, engine-single-sourced
    txt = explain.render_edge(edge)
    assert "ProjectReference" in txt and "Toy.Domain" in txt
    assert explain.explain_edge(facts, DOMAIN, WEB) is None  # no reverse edge


# --- the shadowed/empty-glob lint (preventive twin) ---------------------------------

def test_lint_flags_shadowed_dead_and_empty() -> None:
    """A specific glob below a broad `src/*` is shadowed → its container is silently empty;
    a glob matching nothing is dead. The exact footgun the plan calls out (P§7.1)."""
    rules = MappingRules(raw={"group": [
        {"container": "All", "members_glob": ["src/*"]},
        {"container": "Specific", "members_glob": ["src/Web/*"]},   # shadowed by All
        {"container": "Ghost", "members_glob": ["nope/*"]},          # matches nothing
    ]})
    facts = _facts([{"id": "x", "name": "W", "path": "src/Web/W.csproj"},
                    {"id": "y", "name": "D", "path": "src/Domain/D.csproj"}])
    finds = {(f["kind"], f["container"]) for f in explain.lint_rules(facts, rules)}
    assert ("shadowed-glob", "Specific") in finds
    assert ("empty-container", "Specific") in finds
    assert ("dead-glob", "Ghost") in finds
    # deterministic ordering: identical inputs -> identical finding list.
    assert explain.lint_rules(facts, rules) == explain.lint_rules(facts, rules)


def test_lint_clean_when_every_rule_binds() -> None:
    rules = MappingRules(raw={"group": [{"container": "Web", "members": ["x"]}]})
    facts = _facts([{"id": "x", "name": "W", "path": "src/Web/W.csproj"}])
    assert explain.lint_rules(facts, rules) == []


def test_lint_clean_on_toy(tmp_path: Path) -> None:
    """The toy uses explicit `members:` (no globs) — lint must be silent."""
    tr = run_toy_pipeline(tmp_path / "architecture")
    from anon.config import load_mapping_rules
    extracted = load_json(tr.ws.extracted_facts)
    rules = load_mapping_rules(tr.ws.mapping_rules)
    assert explain.lint_rules(extracted, rules) == []


# --- CLI wiring (the path humans run) -----------------------------------------------

def test_cli_explain_id_lint_and_decision_log(tmp_path: Path) -> None:
    tr = run_toy_pipeline(tmp_path / "architecture")
    base = ["explain", "--repo", str(tr.ws.repo), "--arch-dir", str(tr.ws.arch_dir),
            "--rules-dir", str(tr.ws.rules_dir)]
    assert _run(base + ["--id", WEB]) == 0                         # explain a target
    assert _run(base + ["--id", f"{WEB}->{DOMAIN}"]) == 0          # explain an edge
    assert _run(base + ["--lint"]) == 0                            # lint only
    assert _run(base) == 0                                         # decision log + advisory lint


def test_cli_explain_unknown_id_is_graceful(tmp_path: Path) -> None:
    tr = run_toy_pipeline(tmp_path / "architecture")
    # a non-existent id is reported, not a crash (exit 0).
    assert _run(["explain", "--repo", str(tr.ws.repo), "--arch-dir", str(tr.ws.arch_dir),
                 "--rules-dir", str(tr.ws.rules_dir),
                 "--id", "csharp:csproj:does/not/Exist.csproj"]) == 0
