"""Tests for T1.4 — auto-grouping cold-start / P§7.1 Auto-propose (``anon.propose``).

Covers the four pure grouping signals, the connected-components clustering pass, the
combined :func:`build_proposal`, the banner-prefixed YAML rendering, and the
:func:`run` orchestration. The headline guarantees are **determinism** (byte-identical
across relationship/target reorderings — the §1.1#2 gate) and that the draft is
**advisory only** (never touches the curated/extracted facts).

All I/O is to pytest ``tmp_path``; no extractor/SDK/LLM needed. Synthetic facts dicts
keep each test cheap and self-contained (mirrors ``tests/test_curate.py`` style).
"""
from __future__ import annotations

import copy
import random
from pathlib import Path

import yaml

from anon import propose
from anon.jsonio import dump_json, load_json
from anon.paths import resolve_workspace


# --- synthetic facts builders --------------------------------------------------

def _t(tid: str, *, path: str = "", external: bool = False,
       tags=None, namespaces=None, name=None) -> dict:
    t: dict = {"id": tid, "name": name or tid.rsplit(":", 1)[-1].rsplit("/", 1)[-1],
               "type": "csproj", "language": "csharp", "external": external}
    if path:
        t["path"] = path
    if tags is not None:
        t["tags"] = tags
    if namespaces is not None:
        t["namespaces"] = namespaces
    return t


def _r(src: str, tgt: str) -> dict:
    return {"id": f"rel:{src}->{tgt}", "source": src, "target": tgt,
            "is_declared_dependency": True, "weight": 5,
            "evidence": [{"type": "project_ref", "detail": "<ProjectReference>"}]}


def _multi_project_facts() -> dict:
    """A repo with all four signals present so build_proposal makes N>1 groups."""
    return {
        "schema_version": "1.2",
        "provenance": {"extractors": []},
        "targets": [
            # two Modules projects sharing a slnfolder tag (high signal)
            _t("csharp:csproj:src/Modules/Blog/Blog.csproj", path="src/Modules/Blog",
               tags=["build:msbuild", "slnfolder:Modules"], namespaces=["App.Modules.Blog"]),
            _t("csharp:csproj:src/Modules/Wiki/Wiki.csproj", path="src/Modules/Wiki",
               tags=["build:msbuild", "slnfolder:Modules"], namespaces=["App.Modules.Wiki"]),
            # two Core projects sharing a directory root (medium directory/namespace signal)
            _t("csharp:csproj:src/Core/Domain/Domain.csproj", path="src/Core/Domain",
               tags=["build:msbuild"], namespaces=["Core.Domain"]),
            _t("csharp:csproj:src/Core/Infra/Infra.csproj", path="src/Core/Infra",
               tags=["build:msbuild"], namespaces=["Core.Infra"]),
            # two interconnected projects with no folder/dir/ns commonality -> cluster (low)
            _t("csharp:csproj:tools/A/A.csproj", path="tools/A", tags=["build:msbuild"]),
            _t("csharp:csproj:apps/B/B.csproj", path="apps/B", tags=["build:msbuild"]),
            # an external package — must never be grouped
            _t("csharp:package:Serilog", external=True, tags=["external", "nuget"]),
        ],
        "relationships": [
            _r("csharp:csproj:src/Modules/Blog/Blog.csproj",
               "csharp:csproj:src/Core/Domain/Domain.csproj"),
            _r("csharp:csproj:tools/A/A.csproj", "csharp:csproj:apps/B/B.csproj"),
            _r("csharp:csproj:src/Modules/Blog/Blog.csproj", "csharp:package:Serilog"),
        ],
    }


# --- connected_components ------------------------------------------------------

def test_connected_components_correctness():
    facts = {
        "targets": [_t(f"x:t:{n}", path=n) for n in ("A", "B", "C", "D", "E", "F")],
        "relationships": [_r("x:t:A", "x:t:B"), _r("x:t:B", "x:t:C"),  # A-B-C component
                          _r("x:t:E", "x:t:D")],                       # D-E component (reversed edge)
    }
    comps = propose.connected_components(facts)
    # F is isolated -> its own singleton component; A/B/C cluster; D/E cluster.
    assert comps == [["x:t:A", "x:t:B", "x:t:C"], ["x:t:D", "x:t:E"], ["x:t:F"]]


def test_connected_components_ignores_external_targets():
    facts = {
        "targets": [_t("x:t:A", path="A"), _t("x:t:B", path="B"),
                    _t("x:pkg:Hub", external=True)],
        # an external hub must NOT fuse A and B into one component.
        "relationships": [_r("x:t:A", "x:pkg:Hub"), _r("x:t:B", "x:pkg:Hub")],
    }
    comps = propose.connected_components(facts)
    assert comps == [["x:t:A"], ["x:t:B"]]


def test_connected_components_byte_identical_across_relationship_order():
    facts = _multi_project_facts()
    base = propose.connected_components(facts)
    rng = random.Random(1991)
    for _ in range(8):
        shuffled = copy.deepcopy(facts)
        rng.shuffle(shuffled["relationships"])
        rng.shuffle(shuffled["targets"])
        assert propose.connected_components(shuffled) == base


# --- propose_by_grouping_tag (the source-declared family signal) ----------------

def _tagged_facts() -> dict:
    """Two module-category families + one sdk-role:library (must be IGNORED by the signal)."""
    return {
        "targets": [
            _t("csharp:csproj:src/M/Email/Email.csproj", path="src/M/Email",
               tags=["module-category:Communication"]),
            _t("csharp:csproj:src/M/Sms/Sms.csproj", path="src/M/Sms",
               tags=["module-category:Communication"]),
            _t("csharp:csproj:src/M/Blog/Blog.csproj", path="src/M/Blog",
               tags=["module-category:Content"]),
            _t("csharp:csproj:src/M/Page/Page.csproj", path="src/M/Page",
               tags=["module-category:Content"]),
            _t("csharp:csproj:src/Lib/A/A.csproj", path="src/Lib/A", tags=["sdk-role:library"]),
            _t("csharp:csproj:src/Lib/B/B.csproj", path="src/Lib/B", tags=["sdk-role:library"]),
        ],
        "relationships": [],
    }


def test_propose_by_grouping_tag_buckets_by_value_emits_members_tag():
    groups = propose.propose_by_grouping_tag(_tagged_facts())
    by_name = {g["container"]: g for g in groups}
    assert set(by_name) == {"Communication", "Content"}  # sdk-role:library is NOT grouped
    g = by_name["Communication"]
    assert g["members"] == ["csharp:csproj:src/M/Email/Email.csproj",
                            "csharp:csproj:src/M/Sms/Sms.csproj"]
    assert g["members_tag"] == ["module-category:Communication"]
    assert g["confidence"] == "high"
    assert g["provenance"] == "grouping-tag:module-category:Communication"


def test_grouping_tag_excludes_sdk_role():
    # sdk-role:library is coarse (would over-merge) -> deliberately not an auto-grouping key.
    assert all("sdk-role" not in g["provenance"]
               for g in propose.propose_by_grouping_tag(_tagged_facts()))


def test_build_proposal_grouping_tag_wins_over_directory():
    # A target carrying BOTH a module-category tag and a shared directory must land in the
    # tag family (grouping-tag is precedence-0), and the emitted rule keeps members_tag.
    proposal = propose.build_proposal(_tagged_facts())
    by_name = {g["container"]: g for g in proposal["group"]}
    assert "Communication" in by_name and "Content" in by_name
    assert by_name["Communication"]["members_tag"] == ["module-category:Communication"]
    assert by_name["Communication"]["provenance"].startswith("grouping-tag:")


# --- propose_by_solution_folder ------------------------------------------------

def test_propose_by_solution_folder_resolves_explicit_member_ids():
    groups = propose.propose_by_solution_folder(_multi_project_facts())
    by_name = {g["container"]: g for g in groups}
    assert "Modules" in by_name
    g = by_name["Modules"]
    # explicit real member ids resolved HERE (members_glob can't match tags).
    assert g["members"] == ["csharp:csproj:src/Modules/Blog/Blog.csproj",
                            "csharp:csproj:src/Modules/Wiki/Wiki.csproj"]
    assert g["confidence"] == "high"
    assert g["provenance"] == "solution-folder:Modules"
    assert "members_glob" not in g  # solution-folder groups use explicit ids


def test_propose_by_solution_folder_skips_singletons():
    facts = {
        "targets": [_t("x:t:A", path="A", tags=["slnfolder:Solo"])],  # only one member
        "relationships": [],
    }
    assert propose.propose_by_solution_folder(facts) == []


# --- propose_by_directory / propose_by_namespace -------------------------------

def test_propose_by_directory_emits_members_glob_on_path():
    facts = {
        "targets": [_t("x:t:A", path="src/Core/Domain"), _t("x:t:B", path="src/Core/Infra")],
        "relationships": [],
    }
    groups = propose.propose_by_directory(facts)
    g = next(g for g in groups if g["container"] == "Core")
    assert g["confidence"] == "medium"
    assert g["provenance"] == "directory:src/Core"
    # globs match the project PATH (which the curation matcher supports).
    assert "src/Core/*" in g["members_glob"]


def test_propose_by_namespace_buckets_on_root():
    facts = {
        "targets": [_t("x:t:A", path="a", namespaces=["Co.Orders"]),
                    _t("x:t:B", path="b", namespaces=["Co.Billing"]),
                    _t("x:t:C", path="c")],  # no namespaces -> not bucketed
        "relationships": [],
    }
    groups = propose.propose_by_namespace(facts)
    co = next(g for g in groups if g["container"] == "Co")
    assert sorted(co["members"]) == ["x:t:A", "x:t:B"]
    assert co["confidence"] == "medium"
    assert co["provenance"] == "namespace:Co"


def test_propose_by_namespace_empty_without_l2():
    facts = {"targets": [_t("x:t:A", path="a"), _t("x:t:B", path="b")], "relationships": []}
    assert propose.propose_by_namespace(facts) == []


# --- propose_by_cluster --------------------------------------------------------

def test_propose_by_cluster_one_group_per_nontrivial_component():
    facts = {
        "targets": [_t("tools:A", path="tools/A"), _t("apps:B", path="apps/B"),
                    _t("solo:C", path="solo/C")],
        "relationships": [_r("tools:A", "apps:B")],
    }
    groups = propose.propose_by_cluster(facts)
    # only the A-B cluster is non-trivial; the isolated C is not emitted.
    assert len(groups) == 1
    assert sorted(groups[0]["members"]) == ["apps:B", "tools:A"]
    assert groups[0]["confidence"] == "low"
    assert groups[0]["provenance"] == "dependency-cluster"


# --- build_proposal ------------------------------------------------------------

def test_build_proposal_produces_multiple_groups_not_singletons():
    proposal = propose.build_proposal(_multi_project_facts())
    groups = proposal["group"]
    assert len(groups) > 1  # a real decomposition, NOT N singletons
    # the high-confidence solution-folder group reads first (sorted by confidence desc).
    assert groups[0]["provenance"] == "solution-folder:Modules"
    # rationale is emitted per container.
    assert set(proposal["rationale"]) == {g["container"] for g in groups}


def test_build_proposal_each_target_in_at_most_one_group():
    proposal = propose.build_proposal(_multi_project_facts())
    seen: list[str] = []
    for g in proposal["group"]:
        seen.extend(g["members"])
    assert len(seen) == len(set(seen))  # no target appears in two groups
    # and no external package leaked into any group.
    assert "csharp:package:Serilog" not in seen


def test_build_proposal_deterministic_across_reorderings():
    facts = _multi_project_facts()
    base_yaml = propose.to_proposal_yaml(propose.build_proposal(facts))
    rng = random.Random(7)
    for _ in range(8):
        shuffled = copy.deepcopy(facts)
        rng.shuffle(shuffled["targets"])
        rng.shuffle(shuffled["relationships"])
        out = propose.to_proposal_yaml(propose.build_proposal(shuffled))
        assert out == base_yaml  # byte-identical regardless of input order


def test_build_proposal_empty_facts_is_valid_empty_draft():
    proposal = propose.build_proposal({})
    assert proposal["group"] == []
    text = propose.to_proposal_yaml(proposal)
    assert "DRAFT auto-proposed grouping (T1.4)" in text
    assert "\r" not in text
    assert yaml.safe_load(text) == proposal  # round-trips


# --- to_proposal_yaml ----------------------------------------------------------

def test_to_proposal_yaml_has_banner_and_is_lf():
    text = propose.to_proposal_yaml(propose.build_proposal(_multi_project_facts()))
    assert text.startswith("# DRAFT auto-proposed grouping (T1.4)")
    assert "NEVER auto-applied" in text
    assert "\r" not in text
    # the body round-trips back to the proposal dict (banner is comments, ignored by YAML).
    loaded = yaml.safe_load(text)
    assert "group" in loaded and isinstance(loaded["group"], list)


# --- run(ws) -------------------------------------------------------------------

def test_run_writes_advisory_draft_and_leaves_facts_untouched(tmp_path: Path):
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    ws.generated.mkdir(parents=True, exist_ok=True)
    facts = _multi_project_facts()
    dump_json(facts, ws.extracted_facts)
    extracted_before = ws.extracted_facts.read_bytes()

    summary = propose.run(ws)

    # the draft was written to the machine-owned generated/ path.
    assert ws.proposed_mapping_rules.exists()
    raw = ws.proposed_mapping_rules.read_bytes()
    assert b"\r" not in raw  # LF only
    loaded = yaml.safe_load(raw)
    assert "group" in loaded and len(loaded["group"]) > 1

    # advisory only: the extracted facts file is byte-for-byte unchanged.
    assert ws.extracted_facts.read_bytes() == extracted_before
    # and no curated-facts file was created by the proposer.
    assert not ws.curated_facts.exists()

    # summary shape.
    assert summary["groups"] == len(loaded["group"])
    assert summary["targets_grouped"] > 0
    assert isinstance(summary["signals"], dict)


def test_run_prefers_curated_facts_when_present(tmp_path: Path):
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    ws.generated.mkdir(parents=True, exist_ok=True)
    # extracted has the full set; curated has only one (no-group) target -> proposer should
    # read curated (fewer groups) proving it prefers curated_facts.
    dump_json(_multi_project_facts(), ws.extracted_facts)
    dump_json({"targets": [_t("x:t:Solo", path="solo")], "relationships": []},
              ws.curated_facts)
    propose.run(ws)
    loaded = yaml.safe_load(ws.proposed_mapping_rules.read_bytes())
    assert loaded["group"] == []  # the single curated target yields no groups


def test_run_graceful_on_absent_facts(tmp_path: Path):
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    # no extracted/curated facts at all -> still writes a valid empty draft, no crash.
    summary = propose.run(ws)
    assert summary == {"groups": 0, "targets_grouped": 0, "signals": {}}
    assert ws.proposed_mapping_rules.exists()
    loaded = yaml.safe_load(ws.proposed_mapping_rules.read_bytes())
    assert loaded["group"] == []


def test_run_draft_round_trips_as_valid_yaml(tmp_path: Path):
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    ws.generated.mkdir(parents=True, exist_ok=True)
    dump_json(_multi_project_facts(), ws.extracted_facts)
    propose.run(ws)
    loaded = yaml.safe_load(ws.proposed_mapping_rules.read_text(encoding="utf-8"))
    assert isinstance(loaded["group"], list)
    for g in loaded["group"]:
        assert "container" in g and "confidence" in g and "provenance" in g


def test_run_is_idempotent_byte_identical(tmp_path: Path):
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    ws.generated.mkdir(parents=True, exist_ok=True)
    dump_json(_multi_project_facts(), ws.extracted_facts)
    propose.run(ws)
    first = ws.proposed_mapping_rules.read_bytes()
    propose.run(ws)
    assert ws.proposed_mapping_rules.read_bytes() == first
