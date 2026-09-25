"""Metamorphic / property-style determinism tests (plan T3.2 / §6.4d / §18.4) — Agent B.

Anon's headline guarantee is **byte-identical, order-independent** output: "the build
graph is structural truth; same inputs -> byte-identical fact-model hash" (§1.1 #2, §18.4).
Today that is proven only by ONE golden fixture (``tests/test_golden.py``) plus single-machine
run-to-run equality. These tests add the missing *metamorphic* coverage of the two invariants
the design promises and CLAUDE.md flags as the usual regression class (dict-iteration order,
unsorted lists):

  * **order-independence** — neither *fragment-arrival* order (test 1) nor *within-fragment
    list* order (test 2) nor *argument* order to the evidence merge (test 3) may leak into the
    output; and ``canonicalize`` is a fixed point (test 5).
  * **graceful degradation** — ``merge`` never raises on empty / optional-key-missing /
    gracefully-degraded fragments, and still produces canonical output (test 4).

Hand-rolled harness, stdlib only (``itertools.permutations`` / ``random.Random(seed)`` with a
FIXED seed for reproducibility of the *test* / ``copy.deepcopy``). No ``hypothesis`` dependency
(the project's only test dep is ``pytest``). The functions under test are deterministic; the
fixed seed only makes the *shuffling* in test 2 reproducible run-to-run.
"""
from __future__ import annotations

import copy
import itertools
import random

from anon.stages import normalize_facts as nf
from anon import model

# Fixed seed: the shuffles below must be reproducible so a failure is debuggable.
_SEED = 1991


# --- fragment builders (mirror the gracefully-degraded ``.get(...)``-tolerant shape) -----

def _frag(extractor, *, version="1", targets=None, rels=None, coverage=None, deployment=None):
    """A minimal fact *fragment* as a gracefully-degraded extractor would emit it."""
    prov = {"extractors": [{"name": extractor, "version": version}]}
    if coverage is not None:
        prov["coverage"] = coverage
    frag = {"schema_version": "1.0", "provenance": prov,
            "targets": targets or [], "relationships": rels or []}
    if deployment is not None:
        frag["deployment"] = deployment
    return frag


def _target(tid, **kw):
    base = {"id": tid, "name": tid.split(":")[-1], "type": "csproj", "language": "csharp"}
    base.update(kw)
    return base


def _rel(src, tgt, evidence, **kw):
    rel = {"id": f"rel:{src}->{tgt}", "source": src, "target": tgt, "evidence": evidence}
    rel.update(kw)
    return rel


def _nontrivial_fragments():
    """A non-trivial set of N=4 fragments exercising every merge path (§6.4a-d).

    Designed so the merge actually does work — order must NOT change the result:
      * overlapping target ids across fragments        -> ``_merge_target`` runs
      * overlapping relationships with different kinds  -> ``_merge_evidence`` sums/dedupes
      * targets carrying ``components[]``/``code[]``    -> ``_merge_components`` / ``_merge_code``
      * multiple extractors at different priorities     -> scalar-conflict ledger (§6.4c)
      * a ``deployment`` facet on one fragment          -> ``_merge_deployment`` (§4.4)
    """
    comp_id = "x:t:A/component:rules"
    return [
        # build-graph (priority 100): authoritative targets + a declared edge + a component tier
        _frag("csharp-build-graph",
              targets=[
                  _target("x:t:A", type="csproj", path="src/A",
                          namespaces=["Co.Orders", "Co.Billing"], tags=["seed", "core"],
                          depends_on=["x:t:B", "x:t:C"],
                          components=[{"id": comp_id, "name": "Rules", "level": "component",
                                       "source": "namespace", "tags": ["nsgroup"],
                                       "code": [{"id": comp_id + "/code:Z", "name": "Z", "level": "code"},
                                                {"id": comp_id + "/code:A", "name": "A", "level": "code"}]}]),
                  _target("x:t:B", path="src/B"),
                  _target("x:t:C", path="src/C", external=True),
              ],
              rels=[
                  # NOTE: a single, consistent `kind` per (source,target). `kind` is deliberately
                  # NOT in edge identity and is first-non-empty-wins (§6.4a, asserted in
                  # test_normalize.test_kind_not_in_identity_first_kind_kept), so supplying
                  # *conflicting* kinds on the same edge would be legitimately order-dependent —
                  # which is curation's concern, not the structural-determinism invariant tested
                  # here. We therefore keep kinds consistent so the structural hash is stable.
                  _rel("x:t:A", "x:t:B",
                       [{"type": "project_ref", "detail": "ref"},
                        {"type": "include", "detail": "b.h", "count": 2}], kind="uses"),
                  _rel("x:t:A", "x:t:C", [{"type": "link", "detail": "libc"}]),
              ],
              coverage={"x:t:A": {"L0": True, "L2": "5/20 files"}, "x:t:B": {"L0": True}}),
        # roslyn (priority 50): refines A's edge evidence + adds more component/code children.
        # A also carries `tags` here AND in the build-graph fragment above, so A is seen by 2+
        # fragments -> the merge's union-sort over `tags` runs and `tags` order is neutralized.
        _frag("roslyn-csharp", version="2",
              targets=[
                  _target("x:t:A", type="static_lib",  # equal/lower-prio scalar conflict on `type`
                          namespaces=["Co.Pricing"], tags=["refined", "alpha"],
                          components=[{"id": comp_id, "name": "Rules", "level": "component",
                                       "code": [{"id": comp_id + "/code:Y", "name": "Y", "level": "code"}]},
                                      {"id": "x:t:A/component:io", "name": "IO", "level": "component"}]),
              ],
              rels=[
                  _rel("x:t:A", "x:t:B",
                       [{"type": "include", "detail": "b.h", "count": 3},   # same (type,detail) -> summed
                        {"type": "symbol_use", "detail": "B.Foo"}], kind="uses"),
              ],
              coverage={"x:t:A": {"L2": "18/20 files", "L3": True}}),
        # clang (priority 50): a brand-new edge + a brand-new target
        _frag("clang-scan-deps",
              targets=[_target("x:t:D", path="src/D")],
              rels=[_rel("x:t:B", "x:t:D", [{"type": "include", "detail": "d.h", "count": 7}])]),
        # runtime (priority 40): a deployment facet only (§4.4) + dup of an existing edge's evidence
        _frag("runtime-topology",
              rels=[_rel("x:t:A", "x:t:C", [{"type": "runtime", "detail": "observed"}])],
              deployment={
                  "nodes": [{"id": "node:web", "name": "web", "tags": ["t2"], "ports": [443, 80]},
                            {"id": "node:db", "name": "db"}],
                  "edges": [{"source": "node:web", "target": "node:db", "kind": "connects",
                             "evidence": [{"type": "runtime", "detail": "tcp", "count": 1}]}],
              }),
    ]


# --- 1. fragment-order invariance of merge (§6.4d / §18.4) ---------------------------------

def test_merge_is_fragment_order_invariant():
    """INVARIANT (T3.2 / §6.4d): fragment-*arrival* order cannot leak into the output.

    For EVERY permutation of a non-trivial fragment list, ``content_hash(merge(perm))`` must
    equal the hash of the canonical (as-built) order. N=4 -> 4! = 24 permutations, all run.
    """
    fragments = _nontrivial_fragments()
    canonical_hash = model.content_hash(nf.merge(copy.deepcopy(fragments)))

    perms = list(itertools.permutations(range(len(fragments))))
    assert len(perms) == 24, "expected N=4 -> 24 permutations exercised"

    for perm in perms:
        ordered = [copy.deepcopy(fragments[i]) for i in perm]
        h = model.content_hash(nf.merge(ordered))
        assert h == canonical_hash, (
            f"fragment-order {perm} broke byte-identity: "
            f"got {h} != canonical {canonical_hash}"
        )


# --- 2. within-fragment list-order invariance (canonicalize + merge sorts) -----------------

def _shuffle_lists(frag, rng):
    """Deep-copy *frag* and shuffle every order-insensitive list it contains (fixed-seed rng).

    Covers exactly the lists ``canonicalize`` + the merge sorts normalize for a *single*
    fragment: ``targets`` / each target's ``namespaces``/``depends_on`` / each relationship's
    ``evidence`` / the ``components``/``code`` child tiers / the deployment ``nodes``/``edges``.

    Target-level ``tags`` is deliberately NOT shuffled here: ``canonicalize`` sorts a target's
    ``namespaces``/``depends_on`` but NOT its ``tags``, and the merge's ``tags`` union-sort only
    fires when the *same* target id is seen by 2+ fragments. So a single-fragment target's
    ``tags`` order is preserved verbatim. Its order-invariance is exercised separately via the
    supported multi-fragment union path in ``test_target_tags_union_is_order_invariant`` (which
    documents this gap). See the FINDING in the module / report.
    """
    frag = copy.deepcopy(frag)
    rng.shuffle(frag["targets"])
    rng.shuffle(frag["relationships"])
    for t in frag["targets"]:
        for key in ("namespaces", "depends_on"):
            if isinstance(t.get(key), list):
                rng.shuffle(t[key])
        if isinstance(t.get("components"), list):
            for comp in t["components"]:
                if isinstance(comp.get("code"), list):
                    rng.shuffle(comp["code"])
            rng.shuffle(t["components"])
    for r in frag["relationships"]:
        if isinstance(r.get("evidence"), list):
            rng.shuffle(r["evidence"])
    if isinstance(frag.get("deployment"), dict):
        for key in ("nodes", "edges"):
            if isinstance(frag["deployment"].get(key), list):
                rng.shuffle(frag["deployment"][key])
    return frag


def test_merge_is_within_fragment_list_order_invariant():
    """INVARIANT (T3.2 / §6.4d): the order of lists *inside* a fragment cannot leak out.

    Shuffling (fixed seed) a fragment's ``targets`` / each target's ``namespaces``/``depends_on``
    / each relationship's ``evidence`` / the ``components``/``code`` child lists / the deployment
    ``nodes``/``edges`` must NOT change ``content_hash(merge([frag]))`` — ``canonicalize`` + the
    merge's child/evidence sorts neutralize input list order. (Target ``tags`` is covered by the
    multi-fragment union test below; see ``_shuffle_lists`` for why.)
    """
    rng = random.Random(_SEED)
    for original in _nontrivial_fragments():
        baseline = model.content_hash(nf.merge([copy.deepcopy(original)]))
        # several independent shuffles, each must reproduce the baseline hash
        for attempt in range(5):
            shuffled = _shuffle_lists(original, rng)
            h = model.content_hash(nf.merge([shuffled]))
            assert h == baseline, (
                f"shuffling lists inside fragment {original['provenance']['extractors']} "
                f"(attempt {attempt}) broke byte-identity: got {h} != {baseline}"
            )


def test_target_tags_union_is_order_invariant():
    """INVARIANT (T3.2 / §6.4c-d): a target's ``tags`` are union-merged + sorted, so their
    incoming order across colliding fragments cannot leak into the output.

    NOTE / FINDING: this invariant holds via the *merge's* ``_UNION_LIST_FIELDS`` sort, which
    fires only when the same target id is seen by ≥2 fragments. ``model.canonicalize`` sorts a
    target's ``namespaces``/``depends_on`` but NOT its ``tags``; consequently a target seen by a
    *single* fragment keeps its ``tags`` verbatim. We assert the supported (multi-fragment) path
    here and document the single-fragment gap rather than assert a guarantee the code lacks.
    """
    def _two(order_a, order_b):
        return [
            _frag("csharp-build-graph", targets=[_target("x:t:A", tags=list(order_a))]),
            _frag("roslyn-csharp", targets=[_target("x:t:A", tags=list(order_b))]),
        ]

    base = model.content_hash(nf.merge(_two(["alpha", "beta"], ["beta", "gamma"])))
    for order_a in itertools.permutations(["alpha", "beta"]):
        for order_b in itertools.permutations(["beta", "gamma"]):
            for frags in (_two(order_a, order_b), list(reversed(_two(order_a, order_b)))):
                h = model.content_hash(nf.merge(frags))
                assert h == base, (
                    f"tags input order {order_a}/{order_b} leaked: got {h} != {base}"
                )
    # and the union is the sorted set, independent of arrival order
    merged = nf.merge(_two(["beta", "alpha"], ["gamma", "beta"]))
    assert merged["targets"][0]["tags"] == ["alpha", "beta", "gamma"]


# --- 3. evidence-merge commutativity (§6.4b) -----------------------------------------------

def test_merge_evidence_is_commutative():
    """INVARIANT (§6.4b): ``_merge_evidence`` is commutative once sorted.

    De-dupe is keyed by ``(type, detail)`` and ``count`` is summed, so ``_merge_evidence(a,b)``
    and ``_merge_evidence(b,a)`` — after ``model._sort_evidence`` — are identical evidence sets
    with identical summed counts, regardless of argument order.
    """
    a = [
        {"type": "include", "detail": "x.h", "count": 2},
        {"type": "link", "detail": "libx"},
        {"type": "symbol_use", "detail": "X.Foo", "count": 4},
    ]
    b = [
        {"type": "include", "detail": "x.h", "count": 5},   # same key as a[0] -> summed (7)
        {"type": "project_ref", "detail": "ref"},           # new key -> concatenated
        {"type": "symbol_use", "detail": "X.Foo", "count": 1},  # same key as a[2] -> summed (5)
    ]
    ab = model._sort_evidence(nf._merge_evidence(a, b))
    ba = model._sort_evidence(nf._merge_evidence(b, a))
    assert ab == ba, f"_merge_evidence not commutative: {ab} != {ba}"

    summed = {(e["type"], e.get("detail")): e.get("count") for e in ab}
    assert summed[("include", "x.h")] == 7         # 2 + 5
    assert summed[("symbol_use", "X.Foo")] == 5    # 4 + 1
    assert ("link", "libx") in summed              # uncounted evidence preserved
    assert ("project_ref", "ref") in summed        # concatenated, not lost


# --- 4. graceful-degradation invariant: no-raise on degraded / empty / malformed input -----

def test_merge_empty_and_degraded_fragments_do_not_raise():
    """INVARIANT (T3.2 / graceful degradation, §6.4): ``merge`` never raises on degraded input.

    A gracefully-degraded extractor emits a valid-but-sparse fragment (optional keys absent).
    ``merge`` over [], over an entirely-empty fragment, and over fragments missing
    ``relationships`` / ``provenance`` / ``targets`` must not raise and must still produce
    canonical output (an idempotent fixed point of ``canonicalize``).
    """
    # (a) no fragments at all -> a valid empty-ish fact model, never an exception.
    empty = nf.merge([])
    assert empty["targets"] == []
    assert empty["relationships"] == []
    assert empty == model.canonicalize(empty)            # canonical (fixed point)
    assert isinstance(model.content_hash(empty), str)    # hashable -> well-formed

    # (b) fragments with optional keys missing (mirror `.get(...)`-tolerant degraded emits).
    degraded = [
        {},                                                      # totally empty fragment
        {"targets": [{"id": "x:t:A", "name": "A"}]},             # no relationships/provenance
        {"relationships": []},                                    # no targets/provenance
        {"provenance": {"extractors": [{"name": "csharp-build-graph"}]}},  # no targets/rels
        {"targets": [], "relationships": [], "provenance": {}},  # empty everything
    ]
    merged = nf.merge(copy.deepcopy(degraded))               # must NOT raise
    assert merged == model.canonicalize(merged)              # still canonical
    assert any(t["id"] == "x:t:A" for t in merged["targets"])
    # order-independent over degraded input too (the graceful path must be deterministic)
    assert model.content_hash(merged) == model.content_hash(
        nf.merge(list(reversed(copy.deepcopy(degraded))))
    )


def test_extract_contracts_graceful_noop_returns_none(tmp_path):
    """BONUS (T3.2 / graceful degradation): an extractor no-ops on a repo with no inputs.

    ``extract_contracts.run`` over a repo with no ``.proto``/IDL/WSDL must return ``None`` and
    write nothing — the graceful-degradation contract every extractor honours (CLAUDE.md).
    """
    from anon.paths import resolve_workspace
    from anon.stages import extract_contracts

    repo = tmp_path / "empty-repo"
    repo.mkdir()
    ws = resolve_workspace(repo, arch_dir=tmp_path / "arch")
    assert extract_contracts.run(ws) is None
    # nothing written to the fragments cache
    assert not (ws.fragments.exists() and any(ws.fragments.glob("*.json")))


# --- 5. idempotency of canonicalize (§6.4d) ------------------------------------------------

def test_canonicalize_is_idempotent():
    """INVARIANT (§6.4d / §18.4): ``canonicalize`` is a fixed point.

    ``canonicalize(canonicalize(facts)) == canonicalize(facts)`` — a second pass over already
    canonical data is a no-op, the precondition for byte-identical regeneration.
    """
    facts = nf.merge(_nontrivial_fragments())   # merge already canonicalizes once
    once = model.canonicalize(facts)
    twice = model.canonicalize(once)
    assert twice == once, "canonicalize is not idempotent (second pass changed the model)"
    # and the byte-image is identical too (the actual §18.4 guarantee)
    from anon.jsonio import dumps_json
    assert dumps_json(twice) == dumps_json(once)


def test_single_fragment_target_tags_are_canonicalized() -> None:
    """A target seen by only ONE fragment must still have its `tags` sorted (§6.4d).

    Guards the model.canonicalize fix that closed the latent gap where the merge's
    union-sort only neutralizes tag order for multi-fragment targets — a lone fragment's
    extractor-emitted tag order would otherwise survive verbatim into the hashed output.
    """
    frag = {
        "schema_version": model.__dict__.get("SCHEMA_VERSION", "1.2"),
        "provenance": {"extractors": [{"name": "csharp-build-graph", "version": "1"}]},
        "targets": [{"id": "t1", "name": "T1", "external": False,
                     "tags": ["zeta", "alpha", "needs-curation"]}],
        "relationships": [],
    }
    merged = nf.merge([copy.deepcopy(frag)])
    tags = merged["targets"][0]["tags"]
    assert tags == sorted(tags), f"single-fragment target tags not canonicalized: {tags}"
    assert tags == ["alpha", "needs-curation", "zeta"]
