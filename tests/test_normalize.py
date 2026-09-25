"""Normalize/merge hardening tests (plan §6.4) — Agent B.

Self-contained: every test builds its own in-memory fragments and (where it touches
disk) writes only under ``tmp_path``. Covers evidence merge + derived-field recompute,
the scalar-conflict ledger, child-tier (component/code) merging, coverage-map union,
and fragment-order independence (byte-identical, §6.4d / §1.1 #2).
"""
from __future__ import annotations

from anon.jsonio import dumps_json
from anon.stages import normalize_facts as nf


def _frag(extractor, *, targets=None, rels=None, coverage=None):
    prov = {"extractors": [{"name": extractor, "version": "1"}]}
    if coverage is not None:
        prov["coverage"] = coverage
    return {"schema_version": "1.0", "provenance": prov,
            "targets": targets or [], "relationships": rels or []}


def _target(tid, **kw):
    base = {"id": tid, "name": tid.split(":")[-1], "type": "csproj", "language": "csharp"}
    base.update(kw)
    return base


def _rel(src, tgt, evidence):
    return {"id": f"rel:{src}->{tgt}", "source": src, "target": tgt, "evidence": evidence}


# --- (b) evidence merge + recompute --------------------------------------------------

def test_evidence_merged_deduped_and_count_summed():
    frags = [
        _frag("csharp-build-graph",
              targets=[_target("x:t:A"), _target("x:t:B")],
              rels=[_rel("x:t:A", "x:t:B",
                         [{"type": "project_ref", "detail": "ref"},
                          {"type": "include", "detail": "h", "count": 2}])]),
        _frag("roslyn-csharp",
              rels=[_rel("x:t:A", "x:t:B",
                         [{"type": "include", "detail": "h", "count": 3},     # same (type,detail) -> summed
                          {"type": "symbol_use", "detail": "B.Foo"}])]),      # new kind -> concatenated
    ]
    merged = nf.merge(frags)
    rel = merged["relationships"][0]
    by = {(e["type"], e.get("detail")): e for e in rel["evidence"]}
    assert by[("include", "h")]["count"] == 5            # summed across fragments
    assert ("symbol_use", "B.Foo") in by                  # concatenated, not lost
    # derived fields RECOMPUTED from merged evidence (§6.4b), not first-fragment-wins
    assert rel["is_declared_dependency"] is True          # project_ref present
    assert rel["confidence"] == "high"                    # link/proj + include + symbol = 3 kinds
    assert rel["weight"] == 5 + 5 + 1                      # DECLARED_BASE + min(5,cap) + symbol(1)
    assert "corroborate" in rel["evidence_strength"]


def test_kind_not_in_identity_first_kind_kept():
    # same (source,target) edge, differing kind: kind is curated later, stays out of identity
    frags = [
        _frag("a", targets=[_target("x:t:A"), _target("x:t:B")],
              rels=[{**_rel("x:t:A", "x:t:B", [{"type": "project_ref", "detail": "r"}]), "kind": "uses"}]),
        _frag("b", rels=[{**_rel("x:t:A", "x:t:B", [{"type": "symbol_use", "detail": "s"}]), "kind": "calls"}]),
    ]
    merged = nf.merge(frags)
    assert len(merged["relationships"]) == 1               # merged on (source,target) only
    assert merged["relationships"][0]["kind"] == "uses"    # first non-empty kind kept


# --- (c) scalar conflict ledger ------------------------------------------------------

def test_scalar_conflict_recorded_priority_wins():
    # build-graph (priority 100) and roslyn (50) disagree on `type`; build-graph wins,
    # conflict recorded, never silently dropped (§6.4c).
    frags = [
        _frag("roslyn-csharp", targets=[_target("x:t:A", type="static_lib", path="old/path")]),
        _frag("csharp-build-graph", targets=[_target("x:t:A", type="csproj", path="new/path")]),
    ]
    merged = nf.merge(frags)
    t = merged["targets"][0]
    assert t["type"] == "csproj"                            # higher-priority extractor wins
    assert t["path"] == "new/path"
    conflicts = {c["field"]: c for c in t["conflicts"]}
    assert conflicts["type"]["kept"] == "csproj"
    assert conflicts["type"]["dropped"] == "static_lib"
    assert conflicts["type"]["winner_priority"] > conflicts["type"]["loser_priority"]


def test_equal_priority_external_disagreement_resolves_to_internal_both_orders():
    # Two equal-authority build-graph extractors (priority 100) disagree on `external`
    # for the SAME target id. The principled policy (§6.4c) resolves to external=False
    # (internal) ON PURPOSE — first-party build evidence wins — and the conflict is
    # recorded, never silently dropped. Must be order-independent.
    def _frags_in_order(first_external, second_external):
        return [
            _frag("csharp-build-graph", targets=[_target("x:t:A", external=first_external)]),
            _frag("cmake-graph", targets=[_target("x:t:A", external=second_external)]),
        ]

    for first, second in ((True, False), (False, True)):
        merged = nf.merge(_frags_in_order(first, second))
        t = merged["targets"][0]
        # (a) principled + order-independent: always resolves to internal.
        assert t["external"] is False
        # (b) the disagreement is recorded in the conflict ledger.
        conflicts = {c["field"]: c for c in t.get("conflicts", [])}
        assert "external" in conflicts
        # the kept value is the principled False; the dropped value is the True listing.
        assert conflicts["external"]["kept"] is False
        assert conflicts["external"]["dropped"] is True


def test_namespaces_unioned_not_conflicted():
    frags = [
        _frag("csharp-build-graph", targets=[_target("x:t:A", namespaces=["Co.Orders"])]),
        _frag("roslyn-csharp", targets=[_target("x:t:A", namespaces=["Co.Pricing"])]),
    ]
    merged = nf.merge(frags)
    t = merged["targets"][0]
    assert t["namespaces"] == ["Co.Orders", "Co.Pricing"]  # unioned + sorted
    assert "conflicts" not in t                              # union is not a conflict


# --- ADD: child-tier merging (components[] / code[]) --------------------------------

def test_component_and_code_tiers_merged_by_child_id():
    comp_id = "x:t:A/component:rules"
    code_id = comp_id + "/code:PriceCalc"
    frags = [
        _frag("csharp-build-graph", targets=[_target("x:t:A")]),
        _frag("roslyn-csharp", targets=[_target("x:t:A", components=[
            {"id": comp_id, "name": "Rules", "level": "component", "source": "namespace",
             "code": [{"id": code_id, "name": "PriceCalc", "level": "code"}]}])]),
        _frag("roslyn-csharp", targets=[_target("x:t:A", components=[
            {"id": comp_id, "name": "Rules", "level": "component",
             "code": [{"id": comp_id + "/code:Validator", "name": "Validator", "level": "code"}]},
            {"id": "x:t:A/component:io", "name": "IO", "level": "component"}])]),
    ]
    merged = nf.merge(frags)
    t = next(t for t in merged["targets"] if t["id"] == "x:t:A")
    comps = {c["id"]: c for c in t["components"]}
    assert set(comps) == {comp_id, "x:t:A/component:io"}     # union of components by id
    code_ids = {c["id"] for c in comps[comp_id]["code"]}
    assert code_ids == {code_id, comp_id + "/code:Validator"}  # union of code by id


def test_coverage_map_union_keeps_max_partial():
    cov_a = {"x:t:A": {"L0": True, "L2": "5/20 files"}}
    cov_b = {"x:t:A": {"L0": True, "L2": "18/20 files", "L3": True}}
    frags = [
        _frag("csharp-build-graph", targets=[_target("x:t:A")], coverage=cov_a),
        _frag("roslyn-csharp", targets=[_target("x:t:A")], coverage=cov_b),
    ]
    merged = nf.merge(frags)
    cov = merged["provenance"]["coverage"]["x:t:A"]
    assert cov["L0"] is True
    assert cov["L2"] == "18/20 files"                       # max-coverage descriptor wins
    assert cov["L3"] is True                                 # unioned across layers


# --- (d) deterministic output: any fragment order -> byte-identical ------------------

def test_fragment_order_independent_byte_identical():
    comp = {"id": "x:t:A/component:c", "name": "C", "level": "component",
            "code": [{"id": "x:t:A/component:c/code:Z", "name": "Z", "level": "code"},
                     {"id": "x:t:A/component:c/code:Y", "name": "Y", "level": "code"}]}
    frags = [
        _frag("csharp-build-graph",
              targets=[_target("x:t:B"), _target("x:t:A", components=[comp])],
              rels=[_rel("x:t:A", "x:t:B", [{"type": "project_ref", "detail": "r"}])],
              coverage={"x:t:A": {"L0": True}, "x:t:B": {"L0": True}}),
        _frag("roslyn-csharp",
              targets=[_target("x:t:A")],
              rels=[_rel("x:t:A", "x:t:B", [{"type": "include", "detail": "h", "count": 4}])],
              coverage={"x:t:A": {"L2": "4/4 files"}}),
    ]
    forward = dumps_json(nf.merge(frags))
    reverse = dumps_json(nf.merge(list(reversed(frags))))
    assert forward == reverse                                # byte-identical (§6.4d)
    # and the child tiers/coverage actually survived the merge
    import json
    obj = json.loads(forward)
    t = next(t for t in obj["targets"] if t["id"] == "x:t:A")
    assert [c["id"] for c in t["components"][0]["code"]] == [
        "x:t:A/component:c/code:Y", "x:t:A/component:c/code:Z"]  # code sorted by id
