"""ADR-mining plan §5 — the opt-in LLM / agentic miner (Tier 2).

Locked down here (with a STUB provider — no real network call):
  * mined candidates carry `provenance == "mined"` + `method ∈ {llm, agentic}`;
  * a key_point citing no real id is DROPPED (the §5.4 grounding gate);
  * a key_point whose text phrases WHY (causal/intent language) is DROPPED (the §0.1 intent
    filter) — asserted both end-to-end and directly via `adr._is_causal`;
  * a mined candidate has NO rationale/decision field (the structural §0.5 guard);
  * `run_mine(llm=True)` writes ONLY the soft sidecar, never `adr-candidate-decisions.json`;
  * `materialize_inbox` dedups a mined candidate duplicating a deterministic one IN FAVOUR of
    the deterministic row (§5.4);
  * `run_mine(llm=False)` is an alias for the deterministic detectors.
"""
from __future__ import annotations

from pathlib import Path

from anon import adr
from anon.jsonio import dump_json, load_json
from anon.model import canonicalize
from anon.paths import resolve_workspace

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"

# A sentinel the stub swaps for the element's own (always-resolvable) id at call time.
SELF = "__SELF__"


class StubMiner:
    """A narrative-shaped provider. `key_points` carry a `cites` list; the SELF sentinel is
    rewritten to the payload's own element_id so a grounded cite always resolves."""

    model_id = "stub-miner"

    def __init__(self, key_points):
        self._kps = key_points

    def complete(self, system_prompt, prompt, payload):
        eid = payload["element_id"]
        kps = []
        for kp in self._kps:
            kp = dict(kp)
            kp["cites"] = [eid if c == SELF else c for c in (kp.get("cites") or [])]
            kps.append(kp)
        return {"element_id": eid, "element_kind": payload.get("element_kind"),
                "abstract_md": "", "key_points": kps, "naming_confidence": "low"}


def _curated(targets=None, rels=None):
    return canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c",
                       "generated_at": "2026-01-01T00:00:00Z", "extractors": []},
        "targets": targets or [
            {"id": "csharp:csproj:src/Ordering/Ordering.csproj", "name": "Ordering",
             "type": "csproj", "language": "csharp", "technology": ".NET",
             "container_id": "container:ordering", "container_name": "Ordering"},
            {"id": "csharp:csproj:src/Catalog/Catalog.csproj", "name": "Catalog",
             "type": "csproj", "language": "csharp", "technology": ".NET",
             "container_id": "container:catalog", "container_name": "Catalog"},
        ],
        "relationships": rels if rels is not None else [
            {"source": "csharp:csproj:src/Ordering/Ordering.csproj",
             "target": "csharp:csproj:src/Catalog/Catalog.csproj", "weight": 5,
             "evidence": [{"type": "project_ref", "detail": "x"}]}],
    })


def _ws(tmp_path, *, curated=None):
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "arch", rules_dir=tmp_path / "rules")
    dump_json(curated if curated is not None else _curated(), ws.curated_facts)
    return ws


# --------------------------------------------------------------------------- provenance/method

def test_mined_candidates_carry_mined_provenance_and_method(tmp_path):
    ws = _ws(tmp_path)
    provider = StubMiner([{"text": "The system persists data in a relational store",
                           "cites": [SELF]}])
    cands = adr.mine_candidates(ws, provider=provider)
    assert cands, "a grounded, non-causal key_point yields a candidate"
    for c in cands:
        assert c["provenance"] == "mined"
        assert c["method"] in {"llm", "agentic"}
        assert c["kind"] == "mined-decision"
        # the candidate is lifted to container/system altitude (§0.3 #4)
        assert all(el.startswith("container:") for el in c["elements"])


# a single-container fixture so the #1 cross-container consolidation never triggers — these
# tests exercise the per-container anchor/title mechanics.
_ONE_CONTAINER = [
    {"id": "csharp:csproj:src/Ordering/Ordering.csproj", "name": "Ordering",
     "type": "csproj", "language": "csharp", "technology": ".NET",
     "container_id": "container:ordering", "container_name": "Ordering"},
]


def test_mined_candidate_carries_anchor_and_named_title(tmp_path):
    """A mined record names its subject (GUI inbox readability): `anchor` is the mined
    container (one of `elements`), the display `title` is led by that container's name, and
    `statement` stays the verbatim model prose."""
    ws = _ws(tmp_path, curated=_curated(targets=_ONE_CONTAINER, rels=[]))
    provider = StubMiner([{"text": "Uses a RabbitMQ event bus", "cites": [SELF]}])
    cands = adr.mine_candidates(ws, provider=provider)
    assert cands
    for c in cands:
        assert c["anchor"] in c["elements"]
        assert c["anchor"].startswith("container:")
        assert c["statement"] == "Uses a RabbitMQ event bus"   # prose kept verbatim
        # the title leads with the subject container's display name, then the statement
        assert " — " in c["title"]
        assert c["title"].endswith("Uses a RabbitMQ event bus")
        assert c["title"] != c["statement"]


def test_named_statement_is_not_double_prefixed(tmp_path):
    """When the model already opens the statement with the element's name (the refined prompt
    asks it to), the heading doesn't double it up into 'Ordering — Ordering …'."""
    ws = _ws(tmp_path, curated=_curated(targets=_ONE_CONTAINER, rels=[]))
    provider = StubMiner([{"text": "Ordering exposes a gRPC service", "cites": [SELF]}])
    cands = adr.mine_candidates(ws, provider=provider)
    ordering = [c for c in cands if c["anchor"] == "container:ordering"]
    assert ordering
    for c in ordering:
        assert c["title"] == "Ordering exposes a gRPC service"   # name not repeated


def test_agentic_method_when_deep(tmp_path):
    """A container flagged `deep` runs the agentic augment path and records method=agentic."""
    ws = _ws(tmp_path)
    provider = StubMiner([{"text": "Uses a relational store", "cites": [SELF]}])
    cands = adr.mine_candidates(ws, provider=provider, deep={"container:ordering"})
    deep = [c for c in cands if "container:ordering" in c["elements"]]
    assert deep and all(c["method"] == "agentic" for c in deep)


# --------------------------------------------------------------------------- grounding gate (§5.4)

def test_uncited_keypoint_is_dropped(tmp_path):
    """A key_point citing only an id NOT in the cite universe is dropped (§5.4)."""
    ws = _ws(tmp_path)
    provider = StubMiner([{"text": "Uses gRPC for transport",
                           "cites": ["nosuch:element:id"]}])
    assert adr.mine_candidates(ws, provider=provider) == []


def test_keypoint_with_no_cites_is_dropped(tmp_path):
    ws = _ws(tmp_path)
    provider = StubMiner([{"text": "Some decision was made", "cites": []}])
    assert adr.mine_candidates(ws, provider=provider) == []


# --------------------------------------------------------------------------- intent filter (§0.1)

def test_causal_keypoint_is_dropped_end_to_end(tmp_path):
    """A grounded key_point whose TEXT phrases WHY is dropped — the mined tier may state THAT,
    never WHY (§0.1). The cite is real, so only the causal filter can drop it."""
    ws = _ws(tmp_path)
    provider = StubMiner([{"text": "Chose EF Core in order to ensure consistency",
                           "cites": [SELF]}])
    assert adr.mine_candidates(ws, provider=provider) == []


def test_only_the_causal_keypoint_is_dropped(tmp_path):
    """A causal key_point is dropped while a grounded WHAT-only sibling survives."""
    ws = _ws(tmp_path)
    provider = StubMiner([
        {"text": "The system persists data in a relational store", "cites": [SELF]},
        {"text": "EF Core was chosen because of team familiarity", "cites": [SELF]},
    ])
    cands = adr.mine_candidates(ws, provider=provider)
    assert cands, "the WHAT-only key_point survives"
    assert all(not adr._is_causal(c["statement"]) for c in cands)
    assert all("because" not in c["statement"] for c in cands)


def test_is_causal_direct():
    """The §0.1 filter directly: intent/motive language is causal; a bare WHAT-statement is not."""
    for causal in ("decided because of X", "in order to ensure Y", "to ensure Z",
                   "we chose EF Core", "deliberately uses a single store",
                   "designed to scale", "the rationale was performance"):
        assert adr._is_causal(causal), causal
    for plain in ("The system uses EF Core for persistence",
                  "Ordering depends on Catalog",
                  "12 of 14 containers reference Serilog"):
        assert not adr._is_causal(plain), plain


# --------------------------------------------------------------------------- §0.5 structural guard

def test_mined_candidate_has_no_rationale_field(tmp_path):
    ws = _ws(tmp_path)
    provider = StubMiner([{"text": "Uses a relational store", "cites": [SELF]}])
    cands = adr.mine_candidates(ws, provider=provider)
    assert cands
    for c in cands:
        for banned in ("decision", "rationale", "alternatives", "rejected_alternatives"):
            assert banned not in c, f"a mined candidate must not carry {banned!r} (§0.5)"


# --------------------------------------------------------------------------- soft-tier placement

def test_run_mine_llm_writes_only_the_soft_sidecar(tmp_path):
    """`run_mine(llm=True)` writes the soft sidecar + refreshes the inbox, but NEVER the
    deterministic `adr-candidate-decisions.json` (§0.2 — the determinism story stays crisp)."""
    ws = _ws(tmp_path)
    provider = StubMiner([{"text": "Uses a relational store", "cites": [SELF]}])
    out = adr.run_mine(ws, llm=True, provider=provider)
    assert out["schema"] == adr.MINED_SCHEMA
    assert ws.adr_mined_candidates_json.exists()
    assert not ws.adr_candidate_decisions_json.exists(), \
        "the LLM miner must never touch the deterministic artifact"
    assert ws.adr_candidate_inbox_json.exists(), "the inbox is re-materialized after mining"
    mined = load_json(ws.adr_mined_candidates_json)
    assert all(c["provenance"] == "mined" for c in mined["candidates"])


def test_run_mine_refused_egress_returns_error_no_sidecar(tmp_path):
    """An INERT redaction policy fails the egress gate; run_mine returns an error envelope and
    writes no candidates (fail-closed — the proprietary-code guarantee)."""
    ws = _ws(tmp_path)
    ws.payload_redaction.parent.mkdir(parents=True, exist_ok=True)
    ws.payload_redaction.write_text("secret_patterns: []\ndeny_list: []\n",
                                    encoding="utf-8", newline="")
    provider = StubMiner([{"text": "Uses a relational store", "cites": [SELF]}])
    out = adr.run_mine(ws, llm=True, provider=provider)
    assert out["schema"] == adr.MINED_SCHEMA
    assert out["candidates"] == []
    assert "error" in out and "--i-accept-unredacted-egress" in out["error"]


# --------------------------------------------------------------------------- llm=False alias

def test_run_mine_without_llm_is_deterministic_alias(tmp_path):
    """`run_mine(llm=False)` (or no provider) just re-runs the Tier-1 deterministic detectors
    and writes the deterministic decisions artifact, never the mined sidecar (§5.6)."""
    ws = _ws(tmp_path)
    out = adr.run_mine(ws, llm=False)
    assert out["schema"] == adr.STUBS_SCHEMA
    assert ws.adr_candidate_decisions_json.exists()
    assert not ws.adr_mined_candidates_json.exists()


# --------------------------------------------------------------------------- dedup (§5.4)

def test_materialize_inbox_dedups_mined_in_favour_of_deterministic(tmp_path):
    """A mined candidate with the SAME (kind, elements) as a deterministic one is dropped in
    favour of the cited, byte-identical deterministic row; a non-duplicate mined row survives."""
    ws = _ws(tmp_path)
    det_row = {"slug": "det-1", "kind": "mined-decision", "category": "technology",
               "provenance": "detector", "method": "deterministic", "elements": ["container:x"],
               "evidence_hash": "h1", "title": "t", "statement": "s", "evidence": [],
               "n_sources": 1, "total_weight": 1, "linked_adrs": []}
    dump_json({"schema": adr.STUBS_SCHEMA, "candidates": [det_row]},
              ws.adr_candidate_decisions_json)
    mined_dup = {**det_row, "slug": "mined-dup", "provenance": "mined", "method": "llm",
                 "evidence_hash": "h2"}
    mined_new = {**det_row, "slug": "mined-new", "provenance": "mined", "method": "llm",
                 "elements": ["container:y"], "evidence_hash": "h3"}
    dump_json({"schema": adr.MINED_SCHEMA, "candidates": [mined_dup, mined_new]},
              ws.adr_mined_candidates_json)
    inbox = adr.materialize_inbox(ws)
    slugs = {c["slug"] for c in inbox["candidates"]}
    assert "det-1" in slugs, "the deterministic row wins the duplicate"
    assert "mined-dup" not in slugs, "the duplicate mined row is dropped"
    assert "mined-new" in slugs, "a non-duplicate mined row survives"


# --------------------------------------------------------------------------- clear / merge

def _row(slug, **kw):
    # distinct elements per slug by default, so the materialize dedup (keyed on
    # (category, elements)) never collapses two fixture rows together.
    base = {"slug": slug, "kind": "mined-decision", "category": "technology",
            "provenance": "mined", "method": "llm", "elements": ["container:" + slug],
            "evidence_hash": "h-" + slug, "title": slug, "statement": slug + ".",
            "evidence": [], "n_sources": 1, "total_weight": 1, "linked_adrs": []}
    base.update(kw)
    return base


def test_clear_candidates_drops_selected_then_all(tmp_path):
    """Transient Clear drops slugs from the generated artifacts WITHOUT recording a dismissal;
    `slugs=None` empties the inbox entirely."""
    ws = _ws(tmp_path)
    dump_json({"schema": adr.STUBS_SCHEMA,
               "candidates": [_row("det-a", provenance="detector", method="deterministic")]},
              ws.adr_candidate_decisions_json)
    dump_json({"schema": adr.MINED_SCHEMA, "candidates": [_row("mined-a"), _row("mined-b")]},
              ws.adr_mined_candidates_json)
    adr.materialize_inbox(ws)

    inbox = adr.clear_candidates(ws, slugs=["mined-a"])
    slugs = {c["slug"] for c in inbox["candidates"]}
    assert "mined-a" not in slugs
    assert {"det-a", "mined-b"} <= slugs
    assert inbox["dismissed"] == []          # Clear is NOT a dismissal

    inbox = adr.clear_candidates(ws, slugs=None)
    assert inbox["candidates"] == []


def test_merge_candidates_supersedes_and_persists(tmp_path):
    """Merge fuses ≥2 candidates into one persisted (sidecar) candidate — union of elements,
    verbatim concatenated statements — and supersedes the originals; the merged row authors
    through the normal find_candidate lookup."""
    ws = _ws(tmp_path)
    a = _row("mined-a", elements=["container:ordering"], statement="Uses RabbitMQ.",
             evidence=[{"type": "citation", "detail": "cites `container:ordering`"}])
    b = _row("mined-b", elements=["container:catalog"], statement="Uses PostgreSQL.",
             evidence=[{"type": "citation", "detail": "cites `container:catalog`"}])
    dump_json({"schema": adr.MINED_SCHEMA, "candidates": [a, b]},
              ws.adr_mined_candidates_json)
    adr.materialize_inbox(ws)

    merged = adr.merge_candidates(ws, ["mined-a", "mined-b"])
    assert merged is not None
    assert merged["kind"] == "merged-decision"
    assert merged["merged_from"] == ["mined-a", "mined-b"]
    assert set(merged["elements"]) == {"container:ordering", "container:catalog"}
    assert "Uses RabbitMQ." in merged["statement"]
    assert "Uses PostgreSQL." in merged["statement"]

    inbox = load_json(ws.adr_candidate_inbox_json)
    shown = {c["slug"] for c in inbox["candidates"]}
    assert merged["slug"] in shown
    assert not ({"mined-a", "mined-b"} & shown)        # originals superseded
    assert adr.find_candidate(ws, merged["slug"]) is not None


def test_merge_candidates_needs_two(tmp_path):
    """Fewer than two resolvable slugs is a no-op (returns None)."""
    ws = _ws(tmp_path)
    dump_json({"schema": adr.MINED_SCHEMA, "candidates": [_row("only-one")]},
              ws.adr_mined_candidates_json)
    adr.materialize_inbox(ws)
    assert adr.merge_candidates(ws, ["only-one"]) is None
    assert adr.merge_candidates(ws, ["only-one", "nonexistent"]) is None


# --------------------------------------------------------------- #2 bare-dependency drop

def test_is_bare_dependency_direct():
    """A dependency verb whose subject AND object are both containers, with no technology named,
    merely restates a C4 edge (#2) — dropped. A technology or pattern statement is kept."""
    names = ["Ordering", "Catalog", "Building Blocks"]
    assert adr._is_bare_dependency("Ordering depends on Catalog", names)
    assert adr._is_bare_dependency("Catalog uses Building Blocks", names)
    assert adr._is_bare_dependency("Webhooks depends on the Building Blocks library",
                                   ["Webhooks", "Building Blocks"])
    # a real technology / pattern / non-dependency statement is NOT trivial
    assert not adr._is_bare_dependency("Ordering uses RabbitMQ", names)
    assert not adr._is_bare_dependency("Ordering implements the CQRS pattern", names)
    assert not adr._is_bare_dependency("Ordering owns the order lifecycle", names)
    # a pattern phrased with a needle the OLD inline allowlist lacked is still kept — the guard
    # now reuses the single _CAT_HINTS pattern list via _infer_category, so it can't drift (#4)
    assert not adr._is_bare_dependency("Ordering relies on Catalog using a unit of work", names)
    assert not adr._is_bare_dependency("Ordering depends on Catalog via an MVC controller", names)
    # a dependency on a single container (object not a model container) is not the 2-container
    # restatement shape — keep it (it may carry a real boundary decision)
    assert not adr._is_bare_dependency("Ordering depends on an external gateway", names)


def test_bare_dependency_dropped_end_to_end(tmp_path):
    """A mined 'X depends on Y' between two containers is dropped; a non-dependency WHAT survives."""
    ws = _ws(tmp_path)
    provider = StubMiner([
        {"text": "Ordering depends on Catalog", "cites": [SELF]},
        {"text": "Ordering owns the order lifecycle", "cites": [SELF]},
    ])
    cands = adr.mine_candidates(ws, provider=provider)
    assert cands, "the non-dependency statement survives"
    assert all("depends on" not in c["statement"].lower() for c in cands)
    assert any("owns the order lifecycle" in c["statement"] for c in cands)


# --------------------------------------------------------------- #1 consolidation (deterministic)

def test_consolidation_folds_duplicate_tech(tmp_path):
    """The per-container 'uses RabbitMQ' findings across both containers fold into ONE
    'N containers use RabbitMQ' candidate, preserving each per-container statement as evidence."""
    ws = _ws(tmp_path)   # the default fixture has two containers (Ordering, Catalog)
    provider = StubMiner([{"text": "uses RabbitMQ for messaging", "cites": [SELF]}])
    cands = adr.mine_candidates(ws, provider=provider)
    assert len(cands) == 1, "the duplicate per-container tech choices are consolidated"
    c = cands[0]
    assert c["method"] == "consolidated"
    assert c["provenance"] == "mined"
    assert c["category"] == "technology"
    assert "RabbitMQ" in c["title"]
    assert c["n_sources"] == 2
    assert set(c["elements"]) >= {"container:ordering", "container:catalog"}
    details = {e["detail"] for e in c["evidence"]}
    assert details == {"uses RabbitMQ for messaging"}     # every constituent kept, verbatim
    assert "anchor" not in c                                # a consolidated row has no single anchor


def test_singleton_tech_is_not_consolidated(tmp_path):
    """A technology named in only ONE container is left as a per-container candidate (#1 needs
    ≥2 anchors) — consolidation never fabricates a 'standardization' from a single user."""
    ws = _ws(tmp_path, curated=_curated(targets=_ONE_CONTAINER, rels=[]))
    provider = StubMiner([{"text": "uses RabbitMQ for messaging", "cites": [SELF]}])
    cands = adr.mine_candidates(ws, provider=provider)
    assert len(cands) == 1
    assert cands[0]["method"] == "llm"
    assert cands[0].get("anchor") == "container:ordering"


# --------------------------------------------------------------- #1 consolidation (optional LLM)

class _Residue:
    """A provider that answers the CONSOLIDATE prompt with one fused-group key_point."""
    model_id = "stub-residue"

    def __init__(self, key_points):
        self._kps = key_points

    def complete(self, system_prompt, prompt, payload):
        assert system_prompt == adr.CONSOLIDATE_SYSTEM_PROMPT
        return {"key_points": self._kps}


def test_llm_consolidate_merges_residue():
    """The optional second pass fuses leftover mined candidates the deterministic step could not
    (no taxonomy match) into one, replacing the constituents; non-merged rows are untouched."""
    a = _row("mined-a", elements=["container:a"], statement="Service A queues work via the bus.")
    b = _row("mined-b", elements=["container:b"], statement="Service B queues work via the bus.")
    provider = _Residue([{"text": "Services A and B queue work via the shared bus.",
                          "merge": ["mined-a", "mined-b"],
                          "cites": ["container:a", "container:b"]}])
    out = adr._consolidate_llm(
        [a, b], provider, {"container:a": "A", "container:b": "B"},
        {"container:a", "container:b"}, {"container:a", "container:b"}, {}, derived_hash="h")
    assert len(out) == 1
    c = out[0]
    assert c["method"] == "llm-consolidated"
    assert c["slug"].startswith("mined-merged")
    assert set(c["elements"]) == {"container:a", "container:b"}
    assert {e["detail"] for e in c["evidence"]} == {a["statement"], b["statement"]}


def test_llm_consolidate_degrades_when_provider_returns_nothing():
    """A consolidate call returning no mergeable groups leaves the candidate list unchanged."""
    a = _row("mined-a", statement="A.")
    b = _row("mined-b", statement="B.")
    out = adr._consolidate_llm([a, b], _Residue([]), {}, set(), set(), {}, derived_hash="h")
    assert {c["slug"] for c in out} == {"mined-a", "mined-b"}


# --------------------------------------------------------------- #4 pattern category + #5 description

def test_pattern_statement_buckets_as_pattern_and_splits_description(tmp_path):
    """A pattern statement is categorised `pattern` (#4); a multi-sentence WHAT splits into a
    headline `statement` + an elaboration `description`, both verbatim (#5)."""
    ws = _ws(tmp_path, curated=_curated(targets=_ONE_CONTAINER, rels=[]))
    provider = StubMiner([{
        "text": "Ordering implements the CQRS pattern via MediatR. "
                "Commands flow through IRequestHandler types.",
        "cites": [SELF]}])
    cands = adr.mine_candidates(ws, provider=provider)
    assert len(cands) == 1
    c = cands[0]
    assert c["category"] == "pattern"
    assert c["statement"] == "Ordering implements the CQRS pattern via MediatR."
    assert c["description"] == "Commands flow through IRequestHandler types."


def test_split_statement_direct():
    s, d = adr._split_statement("First sentence. Second one. Third.")
    assert s == "First sentence."
    assert d == "Second one. Third."
    assert adr._split_statement("no terminal punctuation") == ("no terminal punctuation", "")


def test_split_statement_skips_abbreviations_and_leading_punct():
    # an abbreviation's '.' is not a sentence end (#10)
    s, d = adr._split_statement("Ordering uses gRPC vs. REST for sync. Async elsewhere.")
    assert s == "Ordering uses gRPC vs. REST for sync."
    assert d == "Async elsewhere."
    # 'e.g.' is skipped too
    s2, _d2 = adr._split_statement("Persists to Redis, e.g. for sessions. More.")
    assert s2 == "Persists to Redis, e.g. for sessions."
    # a leading terminator never yields a one-char headline
    assert adr._split_statement(". Foo bar")[0] != "."


def test_statement_techs_word_boundary():
    # a real technology token is matched, with its cased display
    assert ("rabbitmq", "messaging", "RabbitMQ") in adr._statement_techs("Catalog uses RabbitMQ")
    assert adr._statement_techs("persists via StackExchange.Redis")  # dotted needle matches
    # a needle embedded inside a longer word is NOT a false hit (#5)
    assert adr._statement_techs("the team had to smarten the API") == set()  # not 'marten'
    assert adr._statement_techs("Ordering owns the order lifecycle") == set()


# --------------------------------------------------------------- #3 inbox cap

def test_inbox_cap_clamps_and_counts(tmp_path):
    """`materialize_inbox(max_candidates=N)` shows the top-N by salience and reports the hidden
    overflow as `counts.capped` — without dismissing it (#3)."""
    ws = _ws(tmp_path)
    rows = [_row("det-a", provenance="detector", method="deterministic"),
            _row("det-b", provenance="detector", method="deterministic"),
            _row("det-c", provenance="detector", method="deterministic")]
    dump_json({"schema": adr.STUBS_SCHEMA, "candidates": rows},
              ws.adr_candidate_decisions_json)

    capped = adr.materialize_inbox(ws, max_candidates=2)
    assert capped["counts"]["shown"] == 2
    assert capped["counts"]["capped"] == 1
    assert len(capped["candidates"]) == 2

    uncapped = adr.materialize_inbox(ws, max_candidates=0)   # 0 = unlimited
    assert uncapped["counts"]["shown"] == 3
    assert uncapped["counts"]["capped"] == 0


def test_inbox_cap_keeps_global_salience_not_category_order(tmp_path):
    """The cap keeps the top-N by GLOBAL salience, so a high-salience candidate in an
    alpha-late category (technology) survives over a low-salience one in an early category
    (framework) — it must not starve whole categories by display order (#3)."""
    ws = _ws(tmp_path)
    rows = [{**_row("fw"), "category": "framework", "n_sources": 1, "total_weight": 1},
            {**_row("tech"), "category": "technology", "n_sources": 9, "total_weight": 9}]
    dump_json({"schema": adr.STUBS_SCHEMA, "candidates": rows},
              ws.adr_candidate_decisions_json)
    inbox = adr.materialize_inbox(ws, max_candidates=1)
    assert [c["slug"] for c in inbox["candidates"]] == ["tech"]
    assert inbox["counts"]["capped"] == 1


def test_render_stub_includes_description(tmp_path):
    """The mined WHAT-only elaboration (#5) is rendered into the durable stub, not just the
    inbox; a candidate without a description renders cleanly (no stray 'None')."""
    c = {"title": "t", "statement": "Headline decision.",
         "description": "Supporting WHAT detail.",
         "evidence": [{"type": "citation", "detail": "cites x"}]}
    md = adr.render_stub(c)
    assert "Headline decision." in md and "Supporting WHAT detail." in md
    bare = adr.render_stub({"title": "t", "statement": "Only.", "evidence": []})
    assert "None" not in bare


def test_resolve_inbox_cap_prefers_override_then_setting(tmp_path, monkeypatch):
    """The explicit override wins (0 = unlimited); otherwise the persistent setting is read."""
    ws = _ws(tmp_path)
    sp = tmp_path / "settings.local.yaml"
    monkeypatch.setenv("ANON_SETTINGS", str(sp))
    assert adr._resolve_inbox_cap(ws, 5) == 5
    assert adr._resolve_inbox_cap(ws, 0) is None        # explicit unlimited
    assert adr._resolve_inbox_cap(ws, None) is None     # no setting file yet
    sp.write_text("anon:\n  adr_max_candidates: 7\n", encoding="utf-8", newline="")
    assert adr._resolve_inbox_cap(ws, None) == 7
    assert adr._resolve_inbox_cap(ws, 3) == 3           # override still wins
