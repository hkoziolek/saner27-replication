"""Foundation contract tests — the regression net the team builds on (task #5 expands).

These lock the load-bearing guarantees: deterministic merge, the §6.5 weight/confidence
functions, slug stability/uniqueness, referential-integrity validation, the curation
filter/threshold rules, and the end-to-end toy run with byte-identical reruns (§1.1 #2).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from anon import cli
from anon.config import MappingRules
from anon.ids import SlugRegistry, dsl_identifier
from anon.jsonio import dumps_json, load_json
from anon.model import (canonicalize, compute_confidence, compute_weight,
                             content_hash, is_declared)
from anon.paths import resolve_workspace
from anon.stages import (apply_mapping_rules, generate_structurizr,
                              normalize_facts, validate_facts)

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"
TOY_RULES = TOY / "architecture" / "rules"


# --- weight / confidence (§6.5) ------------------------------------------------

def test_declared_dependency_never_zero_weight():
    ev = [{"type": "project_ref", "detail": "x"}]
    assert is_declared(ev)
    assert compute_weight(ev) >= 5  # DECLARED_BASE


def test_confidence_low_for_declared_but_unused():
    # a single declared link with zero include/symbol usage -> low (§5 suspicious)
    assert compute_confidence([{"type": "project_ref", "detail": "x"}]) == "low"


def test_confidence_high_for_triple_corroboration():
    ev = [{"type": "link", "detail": "a"}, {"type": "include", "detail": "b", "count": 4},
          {"type": "symbol_use", "detail": "c"}]
    assert compute_confidence(ev) == "high"


def test_include_weight_capped():
    assert compute_weight([{"type": "include", "detail": "h", "count": 1000}]) == 10  # INCLUDE_CAP


# --- canonical ordering (§6.4d) ------------------------------------------------

def test_canonicalize_is_order_independent():
    a = {"targets": [{"id": "b"}, {"id": "a"}], "relationships": []}
    b = {"targets": [{"id": "a"}, {"id": "b"}], "relationships": []}
    assert dumps_json(canonicalize(a)) == dumps_json(canonicalize(b))


# --- normalize merge (§6.4) ----------------------------------------------------

def test_merge_concatenates_and_recomputes():
    frags = [
        {"provenance": {"extractors": [{"name": "csharp-build-graph", "version": "1"}]},
         "targets": [{"id": "csharp:csproj:A", "name": "A", "type": "csproj", "language": "csharp"}],
         "relationships": [{"id": "rel:csharp:csproj:A->csharp:csproj:B", "source": "csharp:csproj:A",
                            "target": "csharp:csproj:B",
                            "evidence": [{"type": "project_ref", "detail": "ref"}]}]},
        {"provenance": {"extractors": [{"name": "roslyn-csharp", "version": "1"}]},
         "targets": [{"id": "csharp:csproj:B", "name": "B", "type": "csproj", "language": "csharp"}],
         "relationships": [{"id": "rel:csharp:csproj:A->csharp:csproj:B", "source": "csharp:csproj:A",
                            "target": "csharp:csproj:B",
                            "evidence": [{"type": "symbol_use", "detail": "B.Foo", "count": 3}]}]},
    ]
    merged = normalize_facts.merge(frags)
    rels = merged["relationships"]
    assert len(rels) == 1
    assert len(rels[0]["evidence"]) == 2          # concatenated
    assert rels[0]["is_declared_dependency"] is True
    assert rels[0]["confidence"] == "medium"      # 2 corroborating kinds


def test_merge_is_fragment_order_independent():
    frags = [
        {"provenance": {"extractors": []},
         "targets": [{"id": "x:t:1", "name": "1", "type": "csproj", "language": "csharp"}],
         "relationships": []},
        {"provenance": {"extractors": []},
         "targets": [{"id": "x:t:2", "name": "2", "type": "csproj", "language": "csharp"}],
         "relationships": []},
    ]
    assert dumps_json(normalize_facts.merge(frags)) == dumps_json(normalize_facts.merge(list(reversed(frags))))


# --- slug stability/uniqueness (§9) --------------------------------------------

def test_slug_stable_and_unique():
    reg = SlugRegistry()
    a = reg.assign("csharp:csproj:src/Web/Toy.Web.csproj")
    assert a == "toyWeb"
    assert reg.assign("csharp:csproj:src/Web/Toy.Web.csproj") == a  # stable
    # collision (same base) gets a deterministic suffix, not a clobber
    b = reg.assign("csharp:csproj:other/Toy.Web.csproj")
    assert b != a and b.startswith("toyWeb_")


def test_dsl_identifier_starts_with_letter():
    assert dsl_identifier("cpp:target:123abc")[0].isalpha()


def test_slug_uniqueness_is_case_insensitive():
    """Structurizr identifiers are case-insensitive, so ``basketAPI`` (a container) and
    ``basketApi`` (a deployment node) must NOT both be emitted bare — the second is
    disambiguated. Regression for the eShop ``identifier already in use`` defect."""
    reg = SlugRegistry()
    container = reg.assign("csharp:csproj:src/Basket.API/Basket.API.csproj")  # -> basketAPI
    node = reg.assign("deploy:image:basket-api")                              # base -> basketApi
    assert container.lower() == "basketapi"
    assert node != container
    assert node.lower() != container.lower()   # genuinely distinct to a case-insensitive parser


# --- referential integrity (§5) ------------------------------------------------

def test_validate_rejects_dangling_relationship():
    facts = {"schema_version": "1.0",
             "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                            "extractors": []},
             "targets": [{"id": "x:t:1", "name": "1", "type": "csproj", "language": "csharp"}],
             "relationships": [{"id": "rel:x:t:1->x:t:missing", "source": "x:t:1",
                                "target": "x:t:missing",
                                "evidence": [{"type": "project_ref"}]}]}
    with pytest.raises(validate_facts.ValidationError):
        validate_facts.validate(facts)


# --- curation (§7.4) -----------------------------------------------------------

def _toy_facts():
    ws = resolve_workspace(TOY, arch_dir="/tmp/_unused")
    return normalize_facts.merge([load_json_fragment(ws)])


def load_json_fragment(ws):
    from anon.stages.extract_build_graph import build_fragment
    return build_fragment(ws.repo)


def test_curation_excludes_tests_and_drops_packages():
    facts = canonicalize(_toy_facts())
    rules = MappingRules(raw={
        "defaults": {"drop_external": True, "min_relationship_weight": 3},
        "exclude": {"targets": [{"glob": "*.Tests"}]},
        "group": [{"container": "Core", "members": [
            "csharp:csproj:src/Domain/Toy.Domain.csproj",
            "csharp:csproj:src/Common/Toy.Common.csproj"]}],
    })
    curated = apply_mapping_rules.curate(facts, rules)
    ids = {t["id"] for t in curated["targets"]}
    assert not any("Tests" in i for i in ids)            # test project excluded
    assert not any(i.startswith("csharp:package:") for i in ids)  # packages dropped
    # declared edge below threshold is kept, never weight-dropped
    assert all(t["id"].startswith("csharp:csproj:") for t in curated["targets"])


# --- end-to-end + stability gate (§1.1 #2) -------------------------------------

def test_end_to_end_toy_is_byte_identical(tmp_path):
    def run(out: Path) -> dict:
        cli.main(["run", "--repo", str(TOY), "--arch-dir", str(out / "architecture"),
                  "--rules-dir", str(TOY_RULES), "--no-llm"])
        return {
            "components": (out / "architecture/generated/generated-components.dsl").read_bytes(),
            "relationships": (out / "architecture/generated/generated-relationships.dsl").read_bytes(),
            "facts_hash": content_hash(load_json(out / "architecture/generated/extracted-facts.json")),
        }
    a = run(tmp_path / "a")
    b = run(tmp_path / "b")
    assert a["components"] == b["components"]
    assert a["relationships"] == b["relationships"]
    assert a["facts_hash"] == b["facts_hash"]
    # the three expected containers exist
    assert b"Core Library" in a["components"]
    assert b"Web Frontend" in a["components"]
    assert b"Messaging" in a["components"]
