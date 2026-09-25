"""Stage — common scenario-candidate envelope + index writer (dynamic-view plan §2/§8).

Four static sources (test / runtime / graph-walk / llm) each emit a *raw proposal* — an
ordered list of ``(from, to)`` over the container vocabulary. This module wraps each raw
proposal in the ONE common ``scenario-candidate/1`` envelope and writes the advisory
sidecar tree under ``generated/scenario-candidates/`` plus an ``index.json`` for the GUI
inbox. **Producer logic stays thin and lives in the source generators**
(``extract_runtime.derive_runtime_scenarios`` for B, etc.); this module centralizes the
single validator call (:func:`scenarios.build_dynamic_view`), the quality bar (plan §1a#1),
salience (plan §1a#3, via the shared :mod:`anon.salience`), inline schema validation,
the deterministic sort, and the stale-file sweep.

**Determinism (plan §8/§11).** A/B/graph-walk candidates are byte-identical across runs:
candidate files sorted by ``(source, key)``, index rows by ``(source, -salience, slug)``
(slug breaks ties), everything through :func:`jsonio.dump_json`. The ``llm`` source is
excluded from the determinism gate (off by default, never in CI). The hashed fact-model
core is untouched — these are advisory sidecars, never normalized into ``extracted-facts``.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import jsonschema

from ..jsonio import dump_json, load_json
from ..paths import Workspace
from .. import salience as _salience
from . import scenarios
from .scenarios import build_dynamic_view, derived_from_hash, validate_dynamic_view
from .validate_facts import find_schema

log = logging.getLogger("anon.scenario_candidates")

SCENARIO_CANDIDATE_SCHEMA = "scenario-candidate/1"

# Altitude / readability quality bar (plan §1a#1): a candidate is DROPPED (with a logged
# count, never silently) when its post-validation `ok`-step count is <2 or >9.
MIN_OK_STEPS = 2
MAX_OK_STEPS = 9

# Source C (graph-walk) path/candidate cap (plan §6): applied AFTER the (source, key) sort,
# emitting a `notes.truncated: {cap, dropped}` field — never a silent truncation (§6.1).
# `cap`/`dropped` are the cap's own accounting and do NOT pre-count the downstream quality
# bar (which may drop more in `run()`), so the reported number is never overstated.
MAX_WALK_CANDIDATES = 25

# The inline "synthesized — not observed" marker Source C carries on EVERY step (plan §6):
# a structure-walk must never read as observed behavior, in the arrow itself (not only a
# tooltip).
_SYNTHESIZED_MARKER = "[synthesized — not observed]"


def _validate_envelope(envelope: dict[str, Any]) -> None:
    """Inline-validate a candidate envelope against schema/scenario-candidate.schema.json
    (plan §3.2) — the enrich_llm.py pattern (``find_schema`` + ``jsonschema.validate``); NOT
    wired into ``arch validate`` or the schema/CHANGELOG gate (advisory, core untouched).
    The inner ``validated_view`` is checked separately against the dynamic-view schema."""
    schema = load_json(find_schema("scenario-candidate.schema.json"))
    jsonschema.validate(instance=envelope, schema=schema)
    validate_dynamic_view(envelope["validated_view"])


# ---------------------------------------------------------------------------
# Source C — graph-walk synthesis (plan §6; default-OFF in `arch run`)
# ---------------------------------------------------------------------------

def _walk_slug(s: str) -> str:
    """Mirror scenarios._scenario_slug for the key suffix (lowercase, non-alnum -> '-')."""
    return scenarios._scenario_slug(s)


def _container_display_names(facts: dict[str, Any]) -> dict[str, str]:
    """container_id -> a human display name (mirrors extract_runtime._container_name)."""
    out: dict[str, str] = {}
    for t in facts.get("targets", []):
        cid = t.get("container_id", t.get("id"))
        cname = t.get("container_name") or t.get("name") or cid
        if cid and cid not in out:
            out[cid] = cname
    return out


def _is_infra_neighbor(facts: dict[str, Any], cid: str) -> bool:
    """A sink heuristic (plan §6): the container resolves to an ``infra:database`` element
    (deployment node) — a leaf the request flow terminates at."""
    deployment = facts.get("deployment") or {}
    for n in deployment.get("nodes", []):
        nid = n.get("id", "")
        if nid == cid and nid.startswith("infra:database"):
            return True
    return False


def synthesize_graph_walk_scenarios(
        facts: dict[str, Any],
        *, api_skeletons: dict[str, list[dict[str, Any]]] | None = None,
        ) -> list[dict[str, Any]]:
    """Source C — graph-walk scenario candidates (dynamic-view plan §6). Language-agnostic
    (covers C++ where A/B are weak). **Default-OFF in ``arch run``** — opt-in via
    ``arch scenarios --source graph-walk``.

    For each entry container (the SHARED :func:`salience.detect_entries` detector — so C and
    the guided tour agree on entries, plan §6), single-source BFS over the emission-normalized
    container graph (build ∪ runtime edges), neighbors expanded in **target-container-id
    order**, parent set on **first discovery only** (deterministically pins the chosen path).
    Each BFS depth level shares one ``order`` band so a gateway fan-out renders as a parallel
    group, NOT a linearized DFS chain (plan §6/§2). Each tree edge (shortest path over the
    graph) becomes one ordered step.

    Confidence is ``low`` and ``ordering_provenance: synthesized`` — every step is a real
    edge but the ORDERING is synthesized, never observed. Every step description is
    dependency-framed and carries the inline ``[synthesized — not observed]`` marker (plan
    §6) so a structure-walk never masquerades as observed behavior. ``key: walk-<entry-slug>``.

    The candidate cap (plan §6) is applied AFTER the ``key`` sort, recording
    ``notes.truncated: {cap, dropped}`` on every retained candidate — never a silent drop.
    ``cap`` is the cap limit and ``dropped`` the count the cap removed; these describe THIS
    producer's truncation only and deliberately do NOT claim how many candidates ultimately
    survive ``run()``'s downstream 2..9 ok-step quality bar (which may drop more — §6.1)."""
    # The combined container graph: build edges ∪ runtime `calls` edges (the same sets the
    # validator evidences against, so every emitted step is `ok`).
    build_edges = scenarios._build_container_edges(facts)
    runtime_edges, _cite = scenarios._runtime_container_edges(facts)
    cedges = build_edges | runtime_edges
    if not cedges:
        return []

    names = _container_display_names(facts)
    adj: dict[str, list[str]] = {}
    out_deg: dict[str, int] = {}
    nodes: set[str] = set()
    for (cs, ct) in cedges:
        adj.setdefault(cs, []).append(ct)
        out_deg[cs] = out_deg.get(cs, 0) + 1
        out_deg.setdefault(ct, out_deg.get(ct, 0))
        nodes.add(cs)
        nodes.add(ct)
    for cs in adj:
        adj[cs] = sorted(adj[cs])

    # Sinks (plan §6): zero out-degree, an infra:database neighbor, or a leaf library.
    sinks = {n for n in nodes if not out_deg.get(n) or _is_infra_neighbor(facts, n)}

    entries, entry_reasons = _salience.detect_entries(facts, api_skeletons=api_skeletons)
    # Restrict to entries that are actual graph nodes (an isolated container has no walk).
    entries = [e for e in entries if e in nodes]
    if not entries:
        # A pure-cycle component (no zero-in-degree, no high-conf root resolving to a node):
        # seed at the smallest container id so output is non-empty + deterministic.
        entries = [min(nodes)] if nodes else []

    candidates: list[dict[str, Any]] = []
    for entry in entries:
        # single-source BFS by depth level; parent set on FIRST discovery only.
        steps: list[dict[str, Any]] = []
        visited: set[str] = {entry}
        frontier = [entry]
        order = 1
        reached_sinks: set[str] = set()
        while frontier:
            level_edges: list[tuple[str, str]] = []
            for src in sorted(frontier):
                for tgt in adj.get(src, []):
                    if tgt in visited:
                        continue   # parent set on first discovery only (pins the path)
                    level_edges.append((src, tgt))
            if not level_edges:
                break
            next_frontier: list[str] = []
            # de-dup targets discovered at this level (first src in sorted order wins).
            claimed: set[str] = set()
            for (cs, ct) in sorted(level_edges):
                if ct in claimed:
                    continue
                claimed.add(ct)
                from_name = names.get(cs, cs)
                to_name = names.get(ct, ct)
                # Dependency-framed + the inline synthesized marker (plan §6).
                description = (f"{from_name} depends on {to_name} (synthesized order) "
                              f"{_SYNTHESIZED_MARKER}")
                steps.append({
                    "from": cs, "to": ct,
                    "from_name": from_name, "to_name": to_name,
                    "order": order, "description": description,
                })
                visited.add(ct)
                next_frontier.append(ct)
                if ct in sinks:
                    reached_sinks.add(ct)
            order += 1
            frontier = sorted(next_frontier)

        if not steps:
            continue
        entry_name = names.get(entry, entry)
        candidates.append({
            "key": "walk-" + _walk_slug(entry_name),
            "name": f"Graph-walk: {entry_name} dependency flow",
            "scope": "system",
            "source": "graph-walk",
            "confidence": "low",
            "ordering_provenance": "synthesized",
            "note": ("SYNTHESIZED dependency-graph walk — every step is a real build/runtime "
                     "edge but the ORDERING is synthesized, NOT observed (a shortest-path "
                     "BFS from the entry; plan §6). Never read as observed behavior."),
            "steps": steps,
            "notes": {"reached_sinks": sorted(reached_sinks),
                      "entry_reasons": entry_reasons.get(entry, [])},
        })

    # candidate cap AFTER the key sort, recording truncated:{cap,dropped} — never silent.
    # NOTE: `cap`/`dropped` describe the cap's own truncation only; the downstream quality
    # bar in `run()` may drop more, so this does NOT claim a final emitted count (§6.1).
    candidates.sort(key=lambda c: c["key"])
    total = len(candidates)
    if total > MAX_WALK_CANDIDATES:
        kept = candidates[:MAX_WALK_CANDIDATES]
        dropped = total - len(kept)
        for c in kept:
            c["notes"]["truncated"] = {"cap": MAX_WALK_CANDIDATES, "dropped": dropped}
        candidates = kept
    return candidates


def build_candidate(raw: dict[str, Any], facts_emitted: dict[str, Any]) -> dict[str, Any] | None:
    """Wrap one raw proposal in the ``scenario-candidate/1`` envelope, or return ``None``
    when the quality bar drops it (plan §1a#1; the caller logs the counted drop).

    *raw* is a source-generator proposal: ``key`` / ``name`` / ``scope`` / ``source`` /
    ``confidence`` / ``ordering_provenance`` / ``note`` / ``steps[]`` (stable ``from`` /
    ``to`` container ids + ``from_name`` / ``to_name`` labels + ``order`` + ``description``)
    plus an optional ``notes`` sub-dict. The proposal's ``from`` / ``to`` are the only
    promotable endpoints and MUST be stable ids (plan §0.4#6).

    Validation flows through the ONE shared :func:`scenarios.build_dynamic_view` against the
    emission-normalized *facts_emitted*, so a candidate that validates clean here validates
    identically when promoted (no inbox bait-and-switch, plan §0.4#1)."""
    scenario = {
        "key": raw["key"],
        "name": raw.get("name", raw["key"]),
        "scope": raw.get("scope", "system"),
        "ordering_provenance": raw.get("ordering_provenance", scenarios.DEFAULT_ORDERING_PROVENANCE),
        "steps": raw.get("steps", []),
    }
    view = build_dynamic_view(scenario, facts_emitted)

    # Quality bar (plan §1a#1): post-validation `ok`-step count must be 2..9.
    if view["valid_steps"] < MIN_OK_STEPS or view["valid_steps"] > MAX_OK_STEPS:
        return None

    sal, sal_reasons = _salience.candidate_salience(view, facts_emitted)
    envelope: dict[str, Any] = {
        "schema": SCENARIO_CANDIDATE_SCHEMA,
        "source": raw["source"],
        "confidence": raw.get("confidence", "low"),
        "ordering_provenance": scenario["ordering_provenance"],
        "salience": sal,
        "salience_reasons": sal_reasons,
        "derived_from_hash": derived_from_hash(facts_emitted),
        "proposal": {
            "key": raw["key"],
            "name": scenario["name"],
            "scope": scenario["scope"],
            "steps": raw.get("steps", []),
        },
        "validated_view": view,
        "note": raw.get("note", ""),
    }
    if raw.get("notes"):
        envelope["notes"] = raw["notes"]
    return envelope


def _index_row(envelope: dict[str, Any]) -> dict[str, Any]:
    """One ``index.json`` row for the GUI inbox (plan §8)."""
    view = envelope["validated_view"]
    runtime_only = any(s.get("status") == "ok" and not s.get("dsl")
                       for s in view.get("steps", []))
    return {
        "source": envelope["source"],
        "confidence": envelope["confidence"],
        "key": envelope["proposal"]["key"],
        "name": envelope["proposal"]["name"],
        "slug": view["slug"],
        "valid_steps": view["valid_steps"],
        "flagged_steps": view["flagged_steps"],
        "dsl_steps": view["dsl_steps"],
        "has_runtime_only": runtime_only,
        "salience": envelope["salience"],
        "salience_reasons": envelope["salience_reasons"],
    }


def _sweep_sources(ws: Workspace, sources: list[str]) -> None:
    """Remove stale candidate files ONLY for the *sources* regenerated this run, so candidates
    from OTHER sources (a prior ``arch scenarios --source X``) are PRESERVED in the inbox until
    the user re-runs that source, promotes, or discards them (plan §10.3). The candidate dir is
    machine-owned, but each invocation owns only the source subdir(s) it sweeps; ``index.json``
    is rebuilt from the full on-disk tree afterwards (:func:`_reindex`)."""
    root = ws.scenario_candidates_dir
    if not root.exists():
        return
    for src in sources:
        sub = root / src
        if sub.exists():
            for f in sorted(sub.rglob("*.json")):
                f.unlink()


def _reindex(ws: Workspace) -> list[dict[str, Any]]:
    """Rebuild ``index.json`` from EVERY candidate body present on disk — the source(s) just
    written PLUS the preserved ones — so the inbox accumulates across per-source runs (plan
    §10.3). Pure function of the on-disk tree; rows sorted ``(source, -salience, slug)``."""
    root = ws.scenario_candidates_dir
    rows: list[dict[str, Any]] = []
    if root.exists():
        for f in sorted(root.glob("*/*.json")):   # one level deep: <source>/<slug>.json
            try:
                rows.append(_index_row(load_json(f)))
            except Exception as exc:  # noqa: BLE001 - a corrupt body never breaks the index
                log.warning("scenario candidates: skipping unreadable candidate %s (%s)", f, exc)
    rows.sort(key=lambda r: (r["source"], -r["salience"], r["slug"]))
    dump_json({"schema": "scenario-candidate-index/1", "candidates": rows}, root / "index.json")
    return rows


def _disambiguator(envelope: dict[str, Any]) -> str:
    """A stable per-candidate suffix source for slug-collision disambiguation. Distinct
    entry containers that share a display name produce the SAME proposal key (and thus the
    same bare slug); the entry container id is what actually differs, so hash key + entry
    container id — colliding candidates then get distinct suffixes, deterministically."""
    proposal = envelope.get("proposal", {})
    steps = proposal.get("steps") or []
    entry = steps[0].get("from", "") if steps else ""
    return f"{proposal.get('key', '')}|{entry}"


def write_candidates(ws: Workspace, envelopes: list[dict[str, Any]], *,
                     sources: list[str] | None = None) -> dict[str, Any]:
    """Sweep, validate, and write the candidate sidecar tree + ``index.json`` (plan §8).

    *envelopes* is the full list across all sources for this run. Files are written to
    ``generated/scenario-candidates/<source>/<slug>.json`` sorted by ``(source, key)``; the
    index rows are sorted by ``(source, -salience, slug)``. Returns a small summary dict.

    Two robustness guarantees keep files and index always consistent:
      * **Unique slug per source** — distinct entry containers can share a display name and
        thus collide on the bare slug; on a collision a short stable sha256 suffix
        (mirroring :func:`scenarios._write_dynamic_views`) is appended and applied to BOTH
        the on-disk filename AND the index row, so an inbox row never points at the wrong
        body (the second writer would otherwise overwrite the first).
      * **Per-candidate fail-soft** — validate+write is wrapped per candidate; a single bad
        envelope is logged + skipped (no index row), and ``index.json`` is ALWAYS written
        from the successfully-written candidates only, so the tree never goes index-less."""
    root = ws.scenario_candidates_dir
    # Sweep ONLY the sources we are regenerating (default: those present in *envelopes*), so a
    # prior run's OTHER-source candidates stay in the inbox (preserve-until-promote/discard).
    swept = sources if sources is not None else sorted({e["source"] for e in envelopes})
    _sweep_sources(ws, swept)
    root.mkdir(parents=True, exist_ok=True)

    # deterministic file order: (source, proposal key)
    ordered = sorted(envelopes, key=lambda e: (e["source"], e["proposal"]["key"]))
    written: list[dict[str, Any]] = []
    seen_slugs: set[tuple[str, str]] = set()   # (source, final slug) — files live per source
    write_failures = 0
    for env in ordered:
        source = env["source"]
        slug = env["validated_view"]["slug"]
        # Guarantee a unique, deterministic slug per source (sorted iteration above pins the
        # bare slug to the first-by-(source,key) candidate; collisions get a stable suffix).
        if (source, slug) in seen_slugs:
            suffix = hashlib.sha256(_disambiguator(env).encode("utf-8")).hexdigest()[:8]
            slug = f"{slug}-{suffix}"
        seen_slugs.add((source, slug))
        # Apply the FINAL slug to both the on-disk body and the index row, then validate.
        env["validated_view"]["slug"] = slug
        try:
            _validate_envelope(env)
            dump_json(env, root / source / f"{slug}.json")
        except Exception as exc:   # noqa: BLE001 - fail-soft: skip one bad candidate, continue
            write_failures += 1
            seen_slugs.discard((source, slug))   # the file was not written; free the slug
            log.warning("scenario candidates: SKIPPING candidate %s/%s — validate/write "
                        "failed (%s); omitting its index row (plan §8)",
                        source, env.get("proposal", {}).get("key", "?"), exc)
            continue
        written.append(env)

    if write_failures:
        log.warning("scenario candidates: skipped %d candidate(s) that failed "
                    "validate/write — see warnings above", write_failures)

    # Rebuild index.json from the FULL on-disk tree (the sources just written + any PRESERVED
    # from prior per-source runs) so the inbox accumulates across runs (plan §10.3).
    rows = _reindex(ws)

    per_source: dict[str, int] = {}
    for e in written:
        per_source[e["source"]] = per_source.get(e["source"], 0) + 1
    summary: dict[str, Any] = {"candidates": len(written), "per_source": per_source,
                               "index_total": len(rows)}
    if write_failures:
        summary["write_failures"] = write_failures
    return summary


def run(ws: Workspace, facts_emitted: dict[str, Any], *,
        sources: list[str] | None = None,
        provider: Any = None) -> dict[str, Any]:
    """Generate candidate sidecars for *facts_emitted* (the emission-normalized facts, plan
    §0.4#1) and write the index. Called from ``generate_structurizr.run``'s advisory pass and
    from ``arch scenarios``.

    *sources* selects which producers run. Default ``["runtime"]`` — Source B (the only
    source the ``arch run`` advisory pass runs by default, plan §8). Source C (``graph-walk``)
    is **opt-in** (``arch scenarios --source graph-walk``, plan §6); Source F (``llm``) is
    opt-in and non-deterministic (``arch scenarios --source llm --llm-propose``, plan §7) — it
    runs only when *provider* is injected (the CLI gates egress first); ``test`` is deferred
    (plan §4). Each raw proposal flows through :func:`build_candidate` (validate + quality bar
    + salience). Quality-bar drops are counted and logged, never silent (§1a#1).
    """
    from . import extract_runtime  # local import: avoid a stages import cycle at module load

    if sources is None:
        sources = ["runtime"]

    raw_proposals: list[dict[str, Any]] = []
    if "runtime" in sources:
        raw_proposals.extend(extract_runtime.derive_runtime_scenarios(facts_emitted))
    if "graph-walk" in sources:
        # Source C reuses the SHARED entry detector; the api-skeleton signal (plan §6) is an
        # optional sharper-entry input loaded best-effort from the workspace (honest absence).
        from .. import apiskeleton
        try:
            api_skeletons = apiskeleton.load(ws)
        except Exception:  # pragma: no cover - enrichment input, never fatal
            api_skeletons = {}
        raw_proposals.extend(
            synthesize_graph_walk_scenarios(facts_emitted, api_skeletons=api_skeletons))
    if "llm" in sources:
        # Source F — LLM-ordered (plan §7). The ONLY non-deterministic source: opt-in via
        # `--llm-propose` (the CLI gates egress + injects a provider before reaching here),
        # honestly absent on `--no-llm` (provider is None -> []). Excluded from the
        # determinism gate, never in CI.
        from . import scenario_candidates_llm
        raw_proposals.extend(
            scenario_candidates_llm.propose_llm_scenarios(
                facts_emitted, provider, ws=ws))
    # test source is added in a later follow-up (plan §4/§14 — A is deferred).

    envelopes: list[dict[str, Any]] = []
    dropped = 0
    for raw in raw_proposals:
        env = build_candidate(raw, facts_emitted)
        if env is None:
            dropped += 1
            continue
        envelopes.append(env)

    if dropped:
        log.info("scenario candidates: dropped %d candidate(s) outside the %d..%d "
                 "ok-step quality bar (plan §1a#1)", dropped, MIN_OK_STEPS, MAX_OK_STEPS)

    summary = write_candidates(ws, envelopes, sources=sources)
    summary["dropped_quality_bar"] = dropped
    log.info("scenario candidates: wrote %d candidate(s) %s -> %s",
             summary["candidates"], summary.get("per_source", {}),
             ws.scenario_candidates_dir / "index.json")
    return summary


# ---------------------------------------------------------------------------
# `arch scenarios` — net-new on-demand verb (plan §8)
# ---------------------------------------------------------------------------
# The four candidate sources. The CLI default is `all` MINUS `llm` MINUS `graph-walk`
# (C is opt-in, §6; F is opt-in, §7) — so the default sweep is Source B only today (A is
# deferred, §4). `--source graph-walk|all` is required to run C.
ALL_SOURCES = ("test", "runtime", "graph-walk", "llm")
DEFAULT_SOURCES = ("runtime",)   # `all` minus llm minus graph-walk minus deferred test


def _facts_path(ws: Workspace):
    """The latest curated/enriched facts on disk (enriched wins, then curated)."""
    if ws.enriched_facts.exists():
        return ws.enriched_facts
    if ws.curated_facts.exists():
        return ws.curated_facts
    return None


def _is_combined(facts: dict[str, Any]) -> bool:
    """A combined (multi-repo) workspace stamps every id with a ``<repo>::`` prefix
    (combine.py). ``arch scenarios`` skips it with a logged note — multi-repo is out of
    scope for the first cut (plan §1 non-goals)."""
    return any("::" in (t.get("id") or "") for t in facts.get("targets", []))


def resolve_sources(selection: str | None) -> list[str]:
    """Map the ``--source`` flag to the effective source list (plan §8).

    ``all`` -> every source MINUS ``llm`` MINUS ``graph-walk`` (C is opt-in, F is opt-in);
    a single named source -> exactly that source. ``None`` defaults to the same as ``all``."""
    if selection in (None, "all"):
        return list(DEFAULT_SOURCES)
    return [selection]


def revalidate_proposal(ws: Workspace, proposal: dict[str, Any], *,
                        source: str | None = None,
                        ordering_provenance: str | None = None) -> dict[str, Any] | None:
    """Re-derive a candidate ``proposal``'s ``validated_view`` SERVER-SIDE against the CURRENT
    on-disk facts — the §7 promote TOCTOU closure.

    The promote path (``POST /rules/scenarios/add``) must never ride a *client-supplied*
    ``validated_view``: the candidate envelope the GUI loaded from the echo endpoint was
    validated against the facts as they stood THEN, but the only thing the promote's
    ``If-Match`` pins is the ``scenarios.yaml`` file bytes — NOT the curated facts. A curation
    edit between echo and promote can leave a step unevidenced while the client still holds the
    stale "all-ok" view. So we re-run the ONE validator (:func:`scenarios.build_dynamic_view`)
    over the freshly emission-normalized facts here, and the route uses THIS result as the
    authoritative ``validated_view`` for the write decision (the contiguity rule then honestly
    catches a now-unevidenced step instead of silently promoting it).

    Loads the latest curated/enriched facts via :func:`_facts_path` (enriched wins, then
    curated); returns ``None`` when no fact model exists (run ``arch run`` first) or when the
    workspace is a combined multi-repo model (out of scope, plan §1) — both mapped by the route
    to ``409 no_analysis``. Mirrors :func:`generate_structurizr.emission_normalized_facts`
    (the MANDATORY single shared mapping, §0.4#1) and the write op's ``source`` ->
    ``ordering_provenance`` derivation (``rules_edit._SOURCE_TO_PROVENANCE``) so the re-derived
    view carries the same provenance the promoted entry will."""
    path = _facts_path(ws)
    if path is None:
        return None
    facts = load_json(path)
    if _is_combined(facts):
        return None

    # The MANDATORY single shared emission-normalized facts shape (plan §0.4#1/§8) — the same
    # target→container mapping `generate_structurizr.run` uses, so the re-derived view matches
    # what a fresh `arch run` would emit (never a second mapping path).
    from ..config import load_mapping_rules
    from ..rules_edit import _SOURCE_TO_PROVENANCE
    from .generate_structurizr import emission_normalized_facts
    rules = load_mapping_rules(ws.mapping_rules)
    facts_emitted = emission_normalized_facts(facts, rules)

    provenance = (ordering_provenance
                  or _SOURCE_TO_PROVENANCE.get(str(source), "hand-authored"))
    scenario = {
        "key": proposal.get("key"),
        "name": proposal.get("name"),
        "scope": proposal.get("scope"),
        "steps": proposal.get("steps") or [],
        "ordering_provenance": provenance,
    }
    return scenarios.build_dynamic_view(scenario, facts_emitted)


def run_cli(ws: Workspace, *, sources: list[str] | None = None,
            provider: Any = None) -> dict[str, Any] | None:
    """The ``arch scenarios`` handler (plan §8): load the latest curated/enriched facts, pass
    them through the SHARED :func:`generate_structurizr.emission_normalized_facts` helper
    (NOT a second mapping path — the inbox bait-and-switch §0.4#1 exists to prevent), THEN run
    the candidate sweep. It MUST NOT call ``scenarios.run(ws, facts, slugs)`` directly.

    Returns the sweep summary, or ``None`` when there is no fact model (run ``arch run``
    first) or when the workspace is a combined multi-repo model (skipped with a logged note,
    plan §1)."""
    path = _facts_path(ws)
    if path is None:
        return None
    facts = load_json(path)
    if _is_combined(facts):
        log.info("scenario candidates: SKIPPING combined (<repo>::) workspace — multi-repo "
                 "is out of scope for the first cut (plan §1 non-goals)")
        return {"candidates": 0, "per_source": {}, "dropped_quality_bar": 0, "skipped": "combined"}

    # The MANDATORY single shared emission-normalized facts shape (plan §0.4#1/§8): the same
    # target→container mapping `generate_structurizr.run` uses for dynamic-view generation.
    from ..config import load_mapping_rules
    from .generate_structurizr import emission_normalized_facts
    rules = load_mapping_rules(ws.mapping_rules)
    facts_emitted = emission_normalized_facts(facts, rules)
    return run(ws, facts_emitted, sources=sources, provider=provider)


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover
    import argparse

    from .. import force_utf8_stdio
    from ..paths import resolve_workspace
    force_utf8_stdio()   # the §22.3 reconfig the arch CLI applies — needed here too
    ap = argparse.ArgumentParser(prog="anon.stages.scenario_candidates", description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    ap.add_argument("--rules-dir")
    ap.add_argument("--source", choices=[*ALL_SOURCES, "all"], default="all",
                    help="which candidate source(s) to sweep (default: all minus llm minus "
                         "graph-walk — Source B only, §8)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--no-llm", dest="no_llm", action="store_true",
                   help="baseline, no model call (default)")
    g.add_argument("--llm-propose", dest="llm_propose", action="store_true",
                   help="opt into the LLM-ordered source F (Phase 6 hook)")
    g.add_argument("--llm-accepted", dest="llm_accepted", action="store_true",
                   help="reviewed/pinned LLM scenarios (Phase 6 hook)")
    args = ap.parse_args(argv)
    ws = resolve_workspace(args.repo, args.arch_dir, args.rules_dir)
    summary = run_cli(ws, sources=resolve_sources(args.source))
    if summary is None:
        print("[scenarios] no fact model — run `arch run` first.")
        return 2
    print(f"[scenarios] wrote {summary['candidates']} candidate(s) {summary.get('per_source', {})} "
          f"-> {ws.scenario_candidates_dir / 'index.json'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
