"""Source F — LLM-ordered scenario candidates (dynamic-view plan §7).

Provider-INJECTED unit tests (NO network — the same mock-provider discipline as
``test_enrich.py``). They assert the F-specific contract:

  - Unevidenced LLM steps (edges NOT in the build ∪ runtime set) are HARD-FILTERED by the
    validator — prompt hope is not a control — and DROPPED from the rendered strip, surfaced
    as a collapsed ``notes.rejected_by_validator: N`` count (plan §7, F-only). The evidenced
    steps still flow through the ONE validator (``build_dynamic_view``) as ``ok``.
  - F is honestly absent on ``--no-llm`` (no provider injected -> no candidates).
  - F is EXCLUDED from the determinism gate: it is off by default (the default sweep is
    Source B only), so a default ``arch run`` / ``scenario_candidates.run`` writes no llm
    candidate, and F runs only under an explicitly injected provider (the ``--llm-propose``
    path the CLI gates).
  - The egress prompt is built from the SAME redacted payload the egress preview uses
    (``infra:`` / ``deploy:image:`` ids are scrubbed/omitted; container→container only).
"""
from __future__ import annotations

from pathlib import Path

from anon.jsonio import load_json
from anon.paths import resolve_workspace
from anon.stages import scenario_candidates, scenario_candidates_llm

# A toy-like container graph (build edges): Web -> {Domain, Messaging}; Domain -> Common.
_WEB = "container:web"
_DOMAIN = "container:domain"
_MESSAGING = "container:messaging"
_COMMON = "container:common"
# A container that EXISTS but has no edge to Web — a step Web -> Solo is unevidenced.
_SOLO = "container:solo"
# A container id the LLM might INVENT entirely (not in the vocabulary at all).
_GHOST = "container:ghost"


def _facts() -> dict:
    def tgt(cid, name):
        return {"id": cid, "container_id": cid, "container_name": name, "tags": [],
                "responsibilities": [f"{name} responsibility"]}

    def rel(s, t):
        return {"source": s, "target": t, "weight": 5,
                "evidence": [{"type": "project_ref", "detail": s}]}

    return {
        "targets": [tgt(_WEB, "Web"), tgt(_DOMAIN, "Domain"),
                    tgt(_MESSAGING, "Messaging"), tgt(_COMMON, "Common"),
                    tgt(_SOLO, "Solo")],
        "relationships": [rel(_WEB, _DOMAIN), rel(_WEB, _MESSAGING),
                          rel(_DOMAIN, _COMMON)],
    }


# A non-inert redaction policy so egress_gate would pass and tests exercise real scrubbing.
_REDACTION = {"secret_patterns": [r"sk-[A-Za-z0-9]{8,}"], "deny_list": [], "max_snippet_chars": 280}


class HallucinatingProvider:
    """Returns one coherent use case mixing EVIDENCED edges with HALLUCINATED ones:
      - Web -> Domain (evidenced), Domain -> Common (evidenced)  -> KEPT
      - Web -> Solo  (no edge in the set)                        -> DROPPED (rejected)
      - Web -> Ghost (invented container id)                     -> DROPPED (rejected)
    """
    model_id = "mock-llm-halluc-1"

    def __init__(self):
        self.calls = []

    def complete(self, system_prompt, prompt, element_facts):
        self.calls.append(element_facts)
        return {"scenarios": [{
            "name": "Order intake flow",
            "steps": [
                {"from": _WEB, "to": _DOMAIN, "description": "submits the order"},
                {"from": _DOMAIN, "to": _COMMON, "description": "loads shared model"},
                {"from": _WEB, "to": _SOLO, "description": "HALLUCINATED — no edge"},
                {"from": _WEB, "to": _GHOST, "description": "HALLUCINATED — invented id"},
            ],
        }]}


class AllHallucinatedProvider:
    """Every step is over a non-existent edge -> no candidate survives."""
    model_id = "mock-llm-allbad-1"

    def complete(self, system_prompt, prompt, element_facts):
        return {"scenarios": [{
            "name": "Phantom flow",
            "steps": [
                {"from": _WEB, "to": _SOLO, "description": "no edge"},
                {"from": _SOLO, "to": _GHOST, "description": "no edge"},
            ],
        }]}


# ---------------------------------------------------------------------------
# §7 — hard filter: unevidenced LLM steps are dropped + counted, evidenced kept
# ---------------------------------------------------------------------------

def test_unevidenced_llm_steps_dropped_and_counted():
    """The validator (not prompt hope) is the control: unevidenced LLM steps are DROPPED
    from the strip and surfaced as a collapsed rejected_by_validator count (plan §7)."""
    facts = _facts()
    raws = scenario_candidates_llm.propose_llm_scenarios(
        facts, HallucinatingProvider(), redaction=_REDACTION)
    assert len(raws) == 1, raws
    raw = raws[0]
    assert raw["source"] == "llm"
    assert raw["confidence"] == "medium"
    assert raw["ordering_provenance"] == "llm-proposed"
    assert raw["key"].startswith("llm-")
    # only the two EVIDENCED edges survive; the two hallucinations are dropped.
    kept = {(s["from"], s["to"]) for s in raw["steps"]}
    assert kept == {(_WEB, _DOMAIN), (_DOMAIN, _COMMON)}, kept
    # the two hallucinated steps are surfaced as the collapsed count (not hidden, not inlined).
    assert raw["notes"]["rejected_by_validator"] == 2


def test_kept_steps_validate_ok_through_the_one_validator():
    """The surviving steps flow through build_dynamic_view as `ok` (build evidence), so a
    Source-F candidate is a real, validated candidate — not pre-asserted by the producer."""
    facts = _facts()
    raws = scenario_candidates_llm.propose_llm_scenarios(
        facts, HallucinatingProvider(), redaction=_REDACTION)
    env = scenario_candidates.build_candidate(raws[0], facts)
    assert env is not None, "2 ok steps clears the 2..9 quality bar"
    view = env["validated_view"]
    ok = [s for s in view["steps"] if s["status"] == "ok"]
    assert len(ok) == 2
    assert all(s["evidence_layer"] == "build" and s["dsl"] is True for s in ok)
    assert env["source"] == "llm"
    assert env["ordering_provenance"] == "llm-proposed"
    # the hallucination count rides through to the envelope notes for the GUI.
    assert env["notes"]["rejected_by_validator"] == 2


def test_all_hallucinated_yields_no_candidate():
    """A scenario whose every step is unevidenced produces NO raw proposal (nothing the
    validator would keep) — the F-only drop removes all steps."""
    facts = _facts()
    raws = scenario_candidates_llm.propose_llm_scenarios(
        facts, AllHallucinatedProvider(), redaction=_REDACTION)
    assert raws == []


# ---------------------------------------------------------------------------
# §7 — not promotable by default / honest absence on --no-llm
# ---------------------------------------------------------------------------

def test_no_provider_is_honestly_absent():
    """On --no-llm there is no provider -> F yields nothing (honest absence, plan §7)."""
    facts = _facts()
    assert scenario_candidates_llm.propose_llm_scenarios(facts, None, redaction=_REDACTION) == []


def test_f_excluded_from_default_sweep(tmp_path: Path):
    """F is NOT promotable / NOT produced by default: the default `run` sweep (Source B only)
    writes no llm candidate even when a provider is injected — `llm` must be an explicit
    source (the --llm-propose path). This is what excludes F from the determinism gate."""
    facts = _facts()
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = resolve_workspace(repo, arch_dir=repo / "_arch")
    # default sources (runtime only) — a provider is injected but llm is not selected.
    summary = scenario_candidates.run(ws, facts, provider=HallucinatingProvider())
    assert summary["per_source"].get("llm", 0) == 0
    assert not (ws.scenario_candidates_dir / "llm").exists()
    index = load_json(ws.scenario_candidates_dir / "index.json")
    assert all(r["source"] != "llm" for r in index["candidates"])


def test_f_runs_only_under_explicit_llm_source(tmp_path: Path):
    """With `--source llm` + an injected provider, F writes a candidate under
    scenario-candidates/llm/ — the only path that produces a non-deterministic candidate."""
    facts = _facts()
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = resolve_workspace(repo, arch_dir=repo / "_arch")
    summary = scenario_candidates.run(ws, facts, sources=["llm"],
                                      provider=HallucinatingProvider())
    assert summary["per_source"].get("llm") == 1
    cand_path = next((ws.scenario_candidates_dir / "llm").glob("*.json"))
    cand = load_json(cand_path)
    assert cand["schema"] == "scenario-candidate/1"
    assert cand["source"] == "llm"
    assert cand["confidence"] == "medium"
    assert cand["notes"]["rejected_by_validator"] == 2


# ---------------------------------------------------------------------------
# §7 — egress: prompt built from the redacted payload (no infra/deploy ids)
# ---------------------------------------------------------------------------

def test_prompt_payload_is_container_list_plus_evidenced_edges():
    """The prompt input is the container catalog (names + descriptions) + the build∪runtime
    edge set — the SAME sets the validator uses (plan §7)."""
    facts = _facts()
    payload = scenario_candidates_llm.build_prompt_payload(facts, _REDACTION)
    # one row per distinct container, scrubbed names.
    cids = {c["id"] for c in payload["containers"]}
    assert cids == {_WEB, _DOMAIN, _MESSAGING, _COMMON, _SOLO}
    # the edge set is exactly the build container edges (no infra/deploy ids).
    edges = {(e["from"], e["to"]) for e in payload["edges"]}
    assert edges == {(_WEB, _DOMAIN), (_WEB, _MESSAGING), (_DOMAIN, _COMMON)}
    assert all(not e["to"].startswith(("infra:", "deploy:image:")) for e in payload["edges"])


def test_redaction_scrubs_container_names():
    """A secret-shaped token in a container description is scrubbed by the effective policy
    before it reaches the prompt (plan §7 egress)."""
    facts = _facts()
    facts["targets"][0]["responsibilities"] = ["leaked sk-ABCDEFGH12345 token"]
    payload = scenario_candidates_llm.build_prompt_payload(facts, _REDACTION)
    web = next(c for c in payload["containers"] if c["id"] == _WEB)
    assert "sk-ABCDEFGH12345" not in web["description"]
    assert "[REDACTED]" in web["description"]
