"""Curation tests (plan §7.4) — Agent C vertical.

Covers grouping/exclusion/threshold, declared-never-dropped, relationship_kinds rename,
down_weight_private, boundary promotion, component_view carry-through, and the rich
curation-proposal content. All outputs go to pytest ``tmp_path``.
"""
from __future__ import annotations

import json
from pathlib import Path

from anon.config import MappingRules
from anon.jsonio import load_json
from anon.model import canonicalize
from anon.paths import resolve_workspace
from anon.stages import apply_mapping_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"
TOY_RULES = TOY / "architecture" / "rules"


# --- synthetic facts: cheap, self-contained, no extractor needed ----------------

def _facts() -> dict:
    """A small fact model exercising declared/non-declared, private, and a Publisher edge."""
    return canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": "csharp:csproj:src/Web/Web.csproj", "name": "Web", "type": "csproj",
             "language": "csharp", "path": "src/Web",
             "components": [{"id": "csharp:csproj:src/Web/Web.csproj/component:Web",
                             "name": "Web", "level": "component", "source": "namespace"}]},
            {"id": "csharp:csproj:src/Core/Core.csproj", "name": "Core", "type": "csproj",
             "language": "csharp", "path": "src/Core"},
            {"id": "csharp:csproj:src/Msg/Msg.csproj", "name": "Msg", "type": "csproj",
             "language": "csharp", "path": "src/Msg"},
            {"id": "csharp:csproj:tests/Web.Tests/Web.Tests.csproj", "name": "Web.Tests",
             "type": "csproj", "language": "csharp", "path": "tests/Web.Tests"},
            {"id": "csharp:package:Serilog", "name": "Serilog", "type": "package",
             "language": "csharp", "external": True, "tags": ["external", "nuget"]},
        ],
        "relationships": [
            # declared (project_ref) + symbol -> never weight-dropped
            {"id": "rel:csharp:csproj:src/Web/Web.csproj->csharp:csproj:src/Core/Core.csproj",
             "source": "csharp:csproj:src/Web/Web.csproj",
             "target": "csharp:csproj:src/Core/Core.csproj",
             "is_declared_dependency": True, "weight": 13, "confidence": "medium",
             "evidence": [{"type": "project_ref", "detail": "<ProjectReference>", "visibility": "public"},
                          {"type": "symbol_use", "detail": "Core.Thing", "count": 8}]},
            # declared but below threshold -> kept + suspicious-declared
            {"id": "rel:csharp:csproj:src/Web/Web.csproj->csharp:csproj:src/Msg/Msg.csproj",
             "source": "csharp:csproj:src/Web/Web.csproj",
             "target": "csharp:csproj:src/Msg/Msg.csproj",
             "is_declared_dependency": True, "weight": 1, "confidence": "low",
             "evidence": [{"type": "project_ref", "detail": "Publisher.cs <ProjectReference>",
                           "visibility": "private"}]},
            # non-declared, below threshold -> dropped
            {"id": "rel:csharp:csproj:src/Msg/Msg.csproj->csharp:csproj:src/Core/Core.csproj",
             "source": "csharp:csproj:src/Msg/Msg.csproj",
             "target": "csharp:csproj:src/Core/Core.csproj",
             "is_declared_dependency": False, "weight": 2, "confidence": "low",
             "evidence": [{"type": "symbol_use", "detail": "Core.Util", "count": 2}]},
            # non-declared package edge
            {"id": "rel:csharp:csproj:src/Web/Web.csproj->csharp:package:Serilog",
             "source": "csharp:csproj:src/Web/Web.csproj", "target": "csharp:package:Serilog",
             "is_declared_dependency": False, "weight": 1, "confidence": "low",
             "evidence": [{"type": "package_ref", "detail": "<PackageReference>"}]},
        ],
    })


def _rules(**extra) -> MappingRules:
    raw = {
        "defaults": {"min_relationship_weight": 3, "drop_external": True,
                     "down_weight_private": True},
        "exclude": {"targets": [{"glob": "*.Tests"}, {"glob": "tests/*"}]},
        "group": [
            {"container": "Web Frontend", "members": ["csharp:csproj:src/Web/Web.csproj"],
             "component_view": True},
            {"container": "Core Library", "members": ["csharp:csproj:src/Core/Core.csproj"]},
            {"container": "Messaging", "members": ["csharp:csproj:src/Msg/Msg.csproj"]},
        ],
        "technology": {"csproj": ".NET assembly"},
    }
    raw.update(extra)
    return MappingRules(raw=raw)


# --- grouping / exclusion / drop_external --------------------------------------

def test_grouping_excludes_tests_and_drops_packages():
    curated = apply_mapping_rules.curate(_facts(), _rules())
    ids = {t["id"] for t in curated["targets"]}
    assert "csharp:csproj:tests/Web.Tests/Web.Tests.csproj" not in ids   # excluded
    assert "csharp:package:Serilog" not in ids                           # drop_external
    names = {t["container_name"] for t in curated["targets"]}
    assert names == {"Web Frontend", "Core Library", "Messaging"}


# --- members_tag selector (§7.4, the module-category grouping path) -------------

def test_members_tag_groups_target_by_tag():
    target = {"id": "csharp:csproj:src/M/M.csproj", "name": "M", "path": "src/M",
              "tags": ["build:msbuild", "module-category:Notifications"]}
    rules = MappingRules(raw={"group": [
        {"container": "Notifications", "members_tag": ["module-category:Notifications"]},
    ]})
    explicit, glob_groups, _ = apply_mapping_rules._group_index(rules)
    got = apply_mapping_rules._group_for(target["id"], target, explicit, glob_groups)
    assert got is not None
    _cid, cname, grouped_by = got
    assert cname == "Notifications"
    assert grouped_by["signal"] == "members_tag"
    assert grouped_by["pattern"] == "module-category:Notifications"


def test_members_glob_does_not_match_a_target_by_its_tag_text():
    # The separation guarantee: a path/name glob must NEVER claim a target via its tags, or a
    # glob like *Content* would silently swallow a `module-category:ContentManagement` target.
    target = {"id": "csharp:csproj:src/M/M.csproj", "name": "M", "path": "src/M",
              "tags": ["module-category:ContentManagement"]}
    rules = MappingRules(raw={"group": [
        {"container": "Content", "members_glob": ["*ContentManagement*"]},
    ]})
    explicit, glob_groups, _ = apply_mapping_rules._group_index(rules)
    assert apply_mapping_rules._group_for(target["id"], target, explicit, glob_groups) is None


def test_members_tag_first_matching_rule_wins_in_order():
    target = {"id": "csharp:csproj:src/M/M.csproj", "name": "M",
              "path": "src/OrchardCore.Modules/M", "tags": ["module-category:Search"]}
    # the structural glob is listed FIRST, so it claims the target before the category tag rule.
    rules = MappingRules(raw={"group": [
        {"container": "All Modules", "members_glob": ["src/OrchardCore.Modules/*"]},
        {"container": "Search", "members_tag": ["module-category:Search"]},
    ]})
    explicit, glob_groups, _ = apply_mapping_rules._group_index(rules)
    _cid, cname, grouped_by = apply_mapping_rules._group_for(
        target["id"], target, explicit, glob_groups)
    assert cname == "All Modules"
    assert grouped_by["signal"] == "members_glob"


# --- threshold + declared-never-dropped (§7.4) ---------------------------------

def test_declared_below_threshold_kept_and_tagged():
    curated = apply_mapping_rules.curate(_facts(), _rules())
    rels = {r["id"]: r for r in curated["relationships"]}
    msg = "rel:csharp:csproj:src/Web/Web.csproj->csharp:csproj:src/Msg/Msg.csproj"
    assert msg in rels                                  # declared, kept despite weight 1
    assert "suspicious-declared" in rels[msg]["tags"]


def test_nondeclared_below_threshold_dropped():
    curated = apply_mapping_rules.curate(_facts(), _rules())
    ids = {r["id"] for r in curated["relationships"]}
    # Msg->Core (symbol_use only, weight 2 < 3) dropped; Serilog edge gone w/ package.
    assert "rel:csharp:csproj:src/Msg/Msg.csproj->csharp:csproj:src/Core/Core.csproj" not in ids
    assert curated["provenance"]["edges_dropped"] >= 2


# --- relationship_kinds rename (§7.4) -----------------------------------------

def test_relationship_kinds_rename_by_evidence_pattern():
    rules = _rules(relationship_kinds=[
        {"when_evidence_contains": "Publisher", "kind": "Publishes and consumes messages"}])
    curated = apply_mapping_rules.curate(_facts(), rules)
    rels = {r["id"]: r for r in curated["relationships"]}
    msg = "rel:csharp:csproj:src/Web/Web.csproj->csharp:csproj:src/Msg/Msg.csproj"
    assert rels[msg]["kind"] == "Publishes and consumes messages"
    assert curated["provenance"]["kinds_renamed"] == 1


# --- down_weight_private (§6.2/§7.4) -------------------------------------------

def test_down_weight_private_tags_private_link():
    curated = apply_mapping_rules.curate(_facts(), _rules())
    rels = {r["id"]: r for r in curated["relationships"]}
    msg = "rel:csharp:csproj:src/Web/Web.csproj->csharp:csproj:src/Msg/Msg.csproj"
    assert "private-link" in rels[msg]["tags"]   # private project_ref evidence


def test_down_weight_private_can_drop_nondeclared():
    # a non-declared private edge above raw threshold gets pushed below it and dropped.
    facts = _facts()
    facts["relationships"].append({
        "id": "rel:csharp:csproj:src/Core/Core.csproj->csharp:csproj:src/Msg/Msg.csproj",
        "source": "csharp:csproj:src/Core/Core.csproj",
        "target": "csharp:csproj:src/Msg/Msg.csproj",
        "is_declared_dependency": False, "weight": 4, "confidence": "low",
        "evidence": [{"type": "include", "detail": "x", "count": 4, "visibility": "private"}]})
    facts = canonicalize(facts)
    kept = {r["id"] for r in apply_mapping_rules.curate(facts, _rules())["relationships"]}
    # weight 4 - 2*1 private = 2 < 3 -> dropped
    assert "rel:csharp:csproj:src/Core/Core.csproj->csharp:csproj:src/Msg/Msg.csproj" not in kept


# --- boundary promotion (§7.4) -------------------------------------------------

def test_boundary_promotion_keeps_dep_as_external():
    rules = _rules(boundary={"csharp:package:Serilog": {"kind": "external", "name": "Serilog Sink"}})
    curated = apply_mapping_rules.curate(_facts(), rules)
    promoted = {t["id"]: t for t in curated["targets"]}
    assert "csharp:package:Serilog" in promoted          # kept, not dropped
    t = promoted["csharp:package:Serilog"]
    assert t["external"] is True and t["boundary_kind"] == "external"
    assert t["name"] == "Serilog Sink"
    assert curated["provenance"]["boundary_promoted"] == 1


# --- legacy component_view reinterpretation (D§6.2 -> §9) -----------------------

def test_component_view_reinterprets_as_expand_on_member_targets():
    """The deprecated ``component_view: true`` group flag becomes an ``expand`` stamp on
    each member (D§6.2: the architect wanted depth here, and the deep path is its closest
    equivalent), with a legible provenance deprecation note — never silent."""
    curated = apply_mapping_rules.curate(_facts(), _rules())
    web = next(t for t in curated["targets"] if t["id"] == "csharp:csproj:src/Web/Web.csproj")
    assert web.get("expand") is True
    assert not web.get("component_view")    # the legacy flag itself is no longer stamped
    core = next(t for t in curated["targets"] if t["id"] == "csharp:csproj:src/Core/Core.csproj")
    assert not core.get("expand")
    notes = curated.get("provenance", {}).get("deprecations", [])
    assert any("component_view" in n for n in notes)


def test_expand_rules_stamp_targets_with_classes():
    """The explicit D§6.1 ``expand:`` surface: a top-level glob row stamps ``expand``;
    a ``classes: true`` row also stamps ``expand_classes``; per-group rows scope to that
    group's members only."""
    rules = _rules(expand=[{"target": "csharp:csproj:src/Core/*", "classes": True}])
    curated = apply_mapping_rules.curate(_facts(), rules)
    core = next(t for t in curated["targets"] if t["id"] == "csharp:csproj:src/Core/Core.csproj")
    assert core.get("expand") is True and core.get("expand_classes") is True
    # per-group scoping: an expand row under "Messaging" never matches another group's member
    rules2 = _rules()
    for g in rules2.raw["group"]:
        if g["container"] == "Messaging":
            g["expand"] = ["csharp:csproj:src/Core/*"]
    curated2 = apply_mapping_rules.curate(_facts(), rules2)
    core2 = next(t for t in curated2["targets"] if t["id"] == "csharp:csproj:src/Core/Core.csproj")
    assert not core2.get("expand")


# --- needs-curation gate (§7.3) ------------------------------------------------

def test_unmapped_target_gets_needs_curation():
    rules = _rules()
    # drop the Messaging group so Msg becomes unmapped
    rules.raw["group"] = [g for g in rules.raw["group"] if g["container"] != "Messaging"]
    curated = apply_mapping_rules.curate(_facts(), rules)
    msg = next(t for t in curated["targets"] if t["id"] == "csharp:csproj:src/Msg/Msg.csproj")
    assert "needs-curation" in msg["tags"]


# --- curation proposal content (§7.1/§7.2B) -----------------------------------

def test_proposal_md_and_json_content(tmp_path):
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "arch", rules_dir=TOY_RULES)
    rules = _rules(rationale={"Core Library": {"reason": "reusable core",
                                               "decided_by": "architect", "date": "2026-06-04",
                                               "confidence": "high"}})
    curated = apply_mapping_rules.curate(_facts(), rules)
    apply_mapping_rules.write_proposal(ws, curated, rules)
    md = ws.curation_proposal_md.read_text(encoding="utf-8")
    assert "3 targets -> 3 containers" in md or "3 targets" in md
    assert "edges below threshold dropped" in md
    assert "Web Frontend" in md and "Core Library" in md
    assert "Rationale" in md and "reusable core" in md
    machine = load_json(ws.curation_proposal_yaml)
    assert machine["summary"]["containers"] == 3
    assert "Web Frontend" in machine["containers"]
    # machine file written via jsonio (LF, canonical) to the §12 .yaml path; JSON is a
    # valid YAML subset, so it both loads as JSON and parses with a YAML loader.
    raw = ws.curation_proposal_yaml.read_bytes()
    assert b"\r" not in raw
    assert json.loads(raw)["summary"]["targets"] == 3


def test_proposal_suspected_rename_with_baseline(tmp_path):
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "arch", rules_dir=TOY_RULES)
    rules = _rules()
    curated = apply_mapping_rules.curate(_facts(), rules)
    baseline = {"targets": [{"id": "csharp:csproj:src/Web/OldWeb.csproj"}]}
    apply_mapping_rules.write_proposal(ws, curated, rules, baseline=baseline)
    md = ws.curation_proposal_md.read_text(encoding="utf-8")
    assert "Suspected renames" in md
    assert "OldWeb.csproj" in md


# --- §7.4 / §16#10b component-tier curation (exclude_namespaces + aggregate_depth) ---

def _ns_components(parent: str, names: list[str]) -> list[dict]:
    return [{"id": f"{parent}/component:{n}", "name": n, "key": n,
             "level": "component", "source": "namespace"} for n in names]


def test_transform_components_noop_without_policy():
    parent = "csharp:csproj:src/Serilog/Serilog.csproj"
    comps = _ns_components(parent, ["Serilog", "Serilog.Core"])
    # absent a policy the same list is returned untouched (keeps unaffected fixtures byte-identical)
    assert apply_mapping_rules._transform_components(comps, [], None) is comps


def test_transform_components_excludes_and_aggregates():
    parent = "csharp:csproj:src/Serilog/Serilog.csproj"
    comps = _ns_components(parent, [
        "JetBrains.Annotations", "System", "System.Diagnostics",
        "Serilog", "Serilog.Core", "Serilog.Core.Sinks", "Serilog.Core.Sinks.Batching",
        "Serilog.Formatting", "Serilog.Formatting.Json",
    ])
    out = apply_mapping_rules._transform_components(comps, ["System*", "JetBrains*"], 2)
    # noise dropped; the deep Serilog.* namespaces collapse to their depth-2 prefix
    assert [c["key"] for c in out] == ["Serilog", "Serilog.Core", "Serilog.Formatting"]
    assert out[1]["id"] == f"{parent}/component:Serilog.Core"   # id stays parent-qualified (§6.3)
    assert out[1]["name"] == "Serilog.Core"                     # display follows the aggregated key


def test_transform_components_merges_code_children():
    parent = "p:t:x"
    comps = [
        {"id": f"{parent}/component:A.B", "name": "A.B", "key": "A.B",
         "code": [{"id": f"{parent}/component:A.B/code:One", "name": "One"}]},
        {"id": f"{parent}/component:A.C", "name": "A.C", "key": "A.C",
         "code": [{"id": f"{parent}/component:A.C/code:Two", "name": "Two"}]},
    ]
    out = apply_mapping_rules._transform_components(comps, [], 1)
    assert [c["key"] for c in out] == ["A"]            # both collapse to depth-1 "A"
    assert len(out[0]["code"]) == 2                    # children of both merged namespaces folded in


def test_component_policy_reads_block_and_defaults():
    rules = MappingRules(raw={"components": {"exclude_namespaces": ["System*"], "aggregate_depth": 2}})
    assert apply_mapping_rules._component_policy(rules) == (["System*"], 2)
    assert apply_mapping_rules._component_policy(MappingRules(raw={})) == ([], None)
    # an invalid (<1 / non-int) depth degrades to the no-op default
    assert apply_mapping_rules._component_policy(
        MappingRules(raw={"components": {"aggregate_depth": 0}})) == ([], None)


def test_curate_applies_component_policy_end_to_end():
    facts = _facts()
    parent = "csharp:csproj:src/Web/Web.csproj"
    for t in facts["targets"]:
        if t["id"] == parent:
            t["components"] = _ns_components(parent, [
                "System", "Web", "Web.Controllers", "Web.Controllers.Api"])
    rules = _rules(components={"exclude_namespaces": ["System*"], "aggregate_depth": 2})
    curated = apply_mapping_rules.curate(facts, rules)
    web = next(t for t in curated["targets"] if t["id"] == parent)
    assert [c["key"] for c in web["components"]] == ["Web", "Web.Controllers"]


def _ns_edge(parent: str, a: str, b: str) -> dict:
    s, d = f"{parent}/component:{a}", f"{parent}/component:{b}"
    return {"id": f"rel:{s}->{d}", "source": s, "target": d,
            "is_declared_dependency": False, "weight": 5, "confidence": "medium",
            "evidence": [{"type": "symbol_use", "detail": f"{a}->{b}", "count": 5}]}


def test_namespace_edges_follow_component_curation():
    """The `…/component:<ns>` namespace edges stay in sync with the curated component tier:
    an edge to an EXCLUDED namespace is dropped, an edge into an AGGREGATED namespace is
    remapped to the survivor (and a now-self edge dropped) — so curated facts never carry a
    dangling component endpoint (the §6 referential-integrity gate). Regression for the bug
    `exclude_namespaces` exposed (curate dropped the component but kept its edges)."""
    facts = _facts()
    parent = "csharp:csproj:src/Web/Web.csproj"
    for t in facts["targets"]:
        if t["id"] == parent:
            t["components"] = _ns_components(parent, [
                "System", "Web", "Web.Controllers", "Web.Controllers.Api"])
    facts["relationships"] = facts["relationships"] + [
        _ns_edge(parent, "Web", "System"),                          # -> excluded endpoint: drop
        _ns_edge(parent, "System", "Web"),                          # excluded endpoint ->: drop
        _ns_edge(parent, "Web", "Web.Controllers"),                 # both survive: keep
        _ns_edge(parent, "Web.Controllers", "Web.Controllers.Api"), # depth-2 collapses to self: drop
    ]
    facts = canonicalize(facts)
    rules = _rules(components={"exclude_namespaces": ["System*"], "aggregate_depth": 2})
    curated = apply_mapping_rules.curate(facts, rules)

    comp_ids = {c["id"] for t in curated["targets"] for c in (t.get("components") or [])}
    ns_rels = [(r["source"], r["target"]) for r in curated["relationships"]
               if "/component:" in r["source"] and "/component:" in r["target"]]
    assert ns_rels == [(f"{parent}/component:Web", f"{parent}/component:Web.Controllers")]
    # referential integrity: every surviving component-edge endpoint is a real curated component
    for s, t in ns_rels:
        assert s in comp_ids and t in comp_ids


# --- T2.7 seed/adopt confirmed-binding rekey (§7.4/§7.5) -----------------------

ORDER_ID = "csharp:csproj:src/Orders/OrderProcessor.csproj"


def _seed_facts() -> dict:
    """One extracted target whose stable id a seeded rule will reach only via a binding."""
    return canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": ORDER_ID, "name": "OrderProcessor", "type": "csproj",
             "language": "csharp", "path": "src/Orders"},
        ],
        "relationships": [],
    })


def _seed_rules(**extra) -> MappingRules:
    """A SEEDED mapping-rules.yaml: human-keyed members + name_override (the §4.5.3 shape)."""
    raw = {
        "defaults": {"min_relationship_weight": 3, "drop_external": True},
        "group": [{"container": "Orders", "members": ["orderProcessor"]}],
        "name_overrides": {"orderProcessor": "Order Processor"},
    }
    raw.update(extra)
    return MappingRules(raw=raw)


def test_rekey_rules_rewrites_human_keys_to_extracted_ids():
    confirmed = {"orderProcessor": ORDER_ID}
    new_rules, stats = apply_mapping_rules.rekey_rules(_seed_rules(), confirmed)
    # name_overrides key rewritten human -> extracted id, value preserved.
    assert new_rules.name_overrides == {ORDER_ID: "Order Processor"}
    assert "orderProcessor" not in new_rules.name_overrides
    # group member rewritten too.
    assert new_rules.groups[0]["members"] == [ORDER_ID]
    assert stats["rekeyed"] == 1
    assert stats["collisions"] == []


def test_rekey_rules_leaves_unconfirmed_keys_as_is():
    # `billing` is NOT in confirmed -> left untouched (may already be a real id / unmatched).
    rules = _seed_rules(name_overrides={"orderProcessor": "Order Processor", "billing": "Billing"})
    new_rules, _ = apply_mapping_rules.rekey_rules(rules, {"orderProcessor": ORDER_ID})
    assert new_rules.name_overrides == {ORDER_ID: "Order Processor", "billing": "Billing"}


def test_rekey_rules_does_not_mutate_input():
    rules = _seed_rules()
    apply_mapping_rules.rekey_rules(rules, {"orderProcessor": ORDER_ID})
    # original .raw is untouched (deep copy) -> humans-touch-keys invariant is non-destructive.
    assert rules.name_overrides == {"orderProcessor": "Order Processor"}
    assert rules.groups[0]["members"] == ["orderProcessor"]


def test_rekey_rules_is_deterministic_over_member_order():
    a = _seed_rules(group=[{"container": "Orders",
                            "members": ["orderProcessor", "billing", "shipping"]}])
    b = _seed_rules(group=[{"container": "Orders",
                            "members": ["shipping", "billing", "orderProcessor"]}])
    confirmed = {"orderProcessor": ORDER_ID}
    ra, _ = apply_mapping_rules.rekey_rules(a, confirmed)
    rb, _ = apply_mapping_rules.rekey_rules(b, confirmed)
    # member order is preserved (curation order can matter); rekeying itself is deterministic.
    # name_overrides (the dict-keyed block) must be byte-identical regardless of input order.
    assert ra.name_overrides == rb.name_overrides
    # and rekeying twice is identical.
    assert apply_mapping_rules.rekey_rules(a, confirmed)[0].raw == ra.raw


def test_rekey_rules_name_override_block_order_independent():
    # two name_overrides dicts with the same content but different insertion order rekey
    # to byte-identical .raw (no dependence on dict order).
    r1 = _seed_rules(name_overrides={"orderProcessor": "OP", "shipping": "Ship"})
    r2 = _seed_rules(name_overrides={"shipping": "Ship", "orderProcessor": "OP"})
    confirmed = {"orderProcessor": ORDER_ID, "shipping": "csharp:csproj:src/Ship/Ship.csproj"}
    out1, _ = apply_mapping_rules.rekey_rules(r1, confirmed)
    out2, _ = apply_mapping_rules.rekey_rules(r2, confirmed)
    assert out1.name_overrides == out2.name_overrides


def test_rekey_rules_collision_deterministic_winner():
    # two distinct human keys both confirmed to the SAME extracted id in name_overrides.
    rules = _seed_rules(name_overrides={"orderProcessor": "From OP", "orders": "From Orders"})
    confirmed = {"orderProcessor": ORDER_ID, "orders": ORDER_ID}
    new_rules, stats = apply_mapping_rules.rekey_rules(rules, confirmed)
    # sorted human id wins: "orderProcessor" < "orders" -> its value survives.
    assert new_rules.name_overrides == {ORDER_ID: "From OP"}
    assert len(stats["collisions"]) == 1
    col = stats["collisions"][0]
    assert col["extracted_id"] == ORDER_ID
    assert col["winner"] == "orderProcessor"
    assert col["losers"] == ["orders"]


def _write_seed_workspace(tmp_path, *, status: str, rules_raw=None):
    """A workspace with seeded rules + a bindings file at the given status, ready for run()."""
    from anon.jsonio import dump_json
    import yaml
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    ws.generated.mkdir(parents=True, exist_ok=True)
    ws.rules_dir.mkdir(parents=True, exist_ok=True)
    dump_json(_seed_facts(), ws.extracted_facts)
    if rules_raw is None:
        rules_raw = _seed_rules().raw
    ws.mapping_rules.write_text(yaml.safe_dump(rules_raw), encoding="utf-8", newline="")
    bindings = {"bindings": {"orderProcessor": {
        "extracted_id": ORDER_ID, "status": status, "confidence": "high"}}}
    ws.human_id_bindings.write_text(yaml.safe_dump(bindings), encoding="utf-8", newline="")
    return ws


def test_run_confirmed_binding_groups_real_target_0_to_100(tmp_path):
    """0% -> 100%: a confirmed binding makes the seeded rule bind the real extracted target."""
    # Group-only rules (no competing name_override) so the container NAME isolates grouping.
    rules_raw = {"defaults": {"min_relationship_weight": 3},
                 "group": [{"container": "Orders", "members": ["orderProcessor"]}]}
    ws = _write_seed_workspace(tmp_path, status="confirmed", rules_raw=rules_raw)
    curated = apply_mapping_rules.run(ws)
    target = next(t for t in curated["targets"] if t["id"] == ORDER_ID)
    assert target["container_name"] == "Orders"            # grouped via rekeyed member
    assert target["container_id"] == apply_mapping_rules._container_id_for_group("Orders")
    assert "needs-curation" not in target.get("tags", [])  # no longer a provisional singleton


def test_run_proposed_binding_is_not_applied_gate_holds(tmp_path):
    """Gate: WITHOUT a confirmed binding (proposed) the seeded rule binds NOTHING."""
    ws = _write_seed_workspace(tmp_path, status="proposed")
    curated = apply_mapping_rules.run(ws)
    target = next(t for t in curated["targets"] if t["id"] == ORDER_ID)
    # falls to a provisional singleton (not grouped into "Orders").
    assert target["container_name"] != "Orders"
    assert "needs-curation" in target["tags"]
    assert target["container_id"] == ORDER_ID


def test_run_name_override_reflects_via_rekey(tmp_path):
    """The rekeyed name_override pins the display name even on a singleton container."""
    from anon.jsonio import dump_json
    import yaml
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    ws.generated.mkdir(parents=True, exist_ok=True)
    ws.rules_dir.mkdir(parents=True, exist_ok=True)
    dump_json(_seed_facts(), ws.extracted_facts)
    # only a name_override, no group -> singleton whose display name comes from the override.
    rules_raw = {"defaults": {"min_relationship_weight": 3},
                 "name_overrides": {"orderProcessor": "Order Processor"}}
    ws.mapping_rules.write_text(yaml.safe_dump(rules_raw), encoding="utf-8", newline="")
    bindings = {"bindings": {"orderProcessor": {
        "extracted_id": ORDER_ID, "status": "confirmed", "confidence": "high"}}}
    ws.human_id_bindings.write_text(yaml.safe_dump(bindings), encoding="utf-8", newline="")
    curated = apply_mapping_rules.run(ws)
    target = next(t for t in curated["targets"] if t["id"] == ORDER_ID)
    assert target["container_name"] == "Order Processor"   # rekeyed override applied


def test_run_no_bindings_is_noop(tmp_path):
    """No bindings file -> rekey is skipped entirely (byte-identical no-op path)."""
    from anon.jsonio import dump_json
    import yaml
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    ws.generated.mkdir(parents=True, exist_ok=True)
    ws.rules_dir.mkdir(parents=True, exist_ok=True)
    dump_json(_seed_facts(), ws.extracted_facts)
    ws.mapping_rules.write_text(yaml.safe_dump(_seed_rules().raw), encoding="utf-8", newline="")
    # no human_id_bindings.yaml at all.
    curated = apply_mapping_rules.run(ws)
    target = next(t for t in curated["targets"] if t["id"] == ORDER_ID)
    # human-keyed member "orderProcessor" never resolves to the real id -> singleton.
    assert target["container_name"] != "Orders"


def test_run_stale_confirmed_binding_no_crash(tmp_path):
    """A confirmed binding to a non-existent extracted id -> no crash; member simply absent."""
    from anon.jsonio import dump_json
    import yaml
    ws = resolve_workspace(tmp_path / "repo", arch_dir=tmp_path / "arch",
                           rules_dir=tmp_path / "rules")
    ws.generated.mkdir(parents=True, exist_ok=True)
    ws.rules_dir.mkdir(parents=True, exist_ok=True)
    dump_json(_seed_facts(), ws.extracted_facts)
    ws.mapping_rules.write_text(yaml.safe_dump(_seed_rules().raw), encoding="utf-8", newline="")
    stale_id = "csharp:csproj:src/Gone/Gone.csproj"  # not in facts
    bindings = {"bindings": {"orderProcessor": {
        "extracted_id": stale_id, "status": "confirmed", "confidence": "high"}}}
    ws.human_id_bindings.write_text(yaml.safe_dump(bindings), encoding="utf-8", newline="")
    curated = apply_mapping_rules.run(ws)  # must not raise
    ids = {t["id"] for t in curated["targets"]}
    assert stale_id not in ids                         # dropped by curate's kept_ids check
    target = next(t for t in curated["targets"] if t["id"] == ORDER_ID)
    # the real target falls to a singleton (its member id was rekeyed away to the stale id).
    assert target["container_name"] != "Orders"


def test_component_altitude_edges_survive_curation_and_surface_in_the_drill():
    """Roslyn namespace->namespace symbol_use edges (`…/component:` endpoints) live one
    zoom BELOW the curation grid (drill-down plan D§4 / fix #3): curation keeps them when
    their owning target survives — un-thresholded, un-rebound — and the namespace drill
    then renders them, even at a weight the container threshold would drop."""
    from anon import layout
    facts = _facts()
    web_id = "csharp:csproj:src/Web/Web.csproj"
    web = next(t for t in facts["targets"] if t["id"] == web_id)
    web["components"].append({"id": f"{web_id}/component:Web.Api", "name": "Web.Api",
                              "level": "component", "source": "namespace"})
    facts["relationships"].append({
        "id": "rel:web-api->web", "source": f"{web_id}/component:Web.Api",
        "target": f"{web_id}/component:Web", "weight": 1,
        "is_declared_dependency": False,
        "evidence": [{"type": "symbol_use", "detail": "Web.Helper", "count": 1}]})
    curated = apply_mapping_rules.curate(canonicalize(facts), _rules())
    nse = [r for r in curated["relationships"] if "/component:" in r["source"]]
    assert len(nse) == 1, "the namespace edge survived (weight 1 < threshold 3)"
    assert nse[0]["source"] == f"{web_id}/component:Web.Api"
    assert nse[0]["target"] == f"{web_id}/component:Web"
    # the namespace drill (target -> its namespaces) renders exactly that edge
    kind, items, rels = layout.component_drilldown(curated, web_id)
    assert kind == "namespace"
    assert {(r["source"], r["target"]) for r in rels} == {
        (f"{web_id}/component:Web.Api", f"{web_id}/component:Web")}


# --- cosmetic parent tier (container-groups plan §3) -----------------------------

def test_parent_stamped_on_grouped_members():
    """A `parent:` label on a group rule lands as `container_parent` on every member
    target of that group — and nowhere else (plan §3.2)."""
    rules = _rules()
    rules.raw["group"][0]["parent"] = "Frontend Tier"
    rules.raw["group"][2]["parent"] = "Middleware"
    curated = apply_mapping_rules.curate(_facts(), rules)
    by_id = {t["id"]: t for t in curated["targets"]}
    assert by_id["csharp:csproj:src/Web/Web.csproj"]["container_parent"] == "Frontend Tier"
    assert by_id["csharp:csproj:src/Msg/Msg.csproj"]["container_parent"] == "Middleware"
    assert "container_parent" not in by_id["csharp:csproj:src/Core/Core.csproj"]
    assert "parent_warnings" not in curated["provenance"]


def test_parent_absent_without_rules_key():
    """Byte-identity guard: parent-free rules stamp neither the per-target key nor the
    provenance warnings block (plan §10)."""
    curated = apply_mapping_rules.curate(_facts(), _rules())
    assert all("container_parent" not in t for t in curated["targets"])
    assert "parent_warnings" not in curated["provenance"]


def test_parent_warnings_are_advisory():
    """Label hygiene (plan §3.3): parent == container name and case-fold near-duplicates
    warn in provenance but never block the stamping."""
    rules = _rules()
    rules.raw["group"][0]["parent"] = "Messaging"    # collides with a container name
    rules.raw["group"][1]["parent"] = "middleware"
    rules.raw["group"][2]["parent"] = "Middleware"   # case-fold near-duplicate
    curated = apply_mapping_rules.curate(_facts(), rules)
    warns = curated["provenance"]["parent_warnings"]
    assert any("'Messaging' is also a container name" in w for w in warns)
    assert any("differ only by case" in w for w in warns)
    by_id = {t["id"]: t for t in curated["targets"]}
    assert by_id["csharp:csproj:src/Core/Core.csproj"]["container_parent"] == "middleware"


def test_parent_non_string_or_blank_ignored():
    """Curation must degrade, never crash: a non-string or blank `parent:` is inert."""
    rules = _rules()
    rules.raw["group"][0]["parent"] = 123
    rules.raw["group"][1]["parent"] = "   "
    curated = apply_mapping_rules.curate(_facts(), rules)
    assert all("container_parent" not in t for t in curated["targets"])
    assert "parent_warnings" not in curated["provenance"]
