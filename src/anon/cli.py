"""The ``arch`` command-line interface (plan §18.2).

One entry point wraps the stage modules (which still exist for CI granularity, §10).
Implemented: ``run | extract | curate | generate | validate | drift | verify | explain |
abstain | scorecard | diff | fetch | export | combine | discover | doctor | bootstrap |
propose | reconcile | egress-preview | risk | datasheet | adr | query | serve | mcp |
whatif | timeline | fleet``.
The engine-§7 verbs are on-demand and advisory: ``whatif`` (§7.1) simulates
extract/merge/move/cut as a PROPOSAL under ``generated/whatif/`` (never applied);
``timeline`` (§7.2) narrates committed fact snapshots ("history unavailable" is a
normal answer); ``fleet`` (§7.3) reports cross-system facts over the ``combine``
output; ``adr stubs`` (§7.4) emits ADR-CANDIDATE stubs with rationale ``TODO (human)``.
``query`` (engine plan §6) is the deterministic, cited oracle over the curated model;
``serve`` (GUI plan §7) is the thin read-only ``/api/v1`` HTTP layer behind the optional
``[serve]`` extra; ``risk`` (engine §3) / ``datasheet`` (engine §1) / ``adr check``
(engine §2a) emit the advisory architecture-intelligence artifacts the GUI echoes. ``abstain`` (T1.3) scores per-element confidence +
abstains the weak tail (risk-coverage curve); ``scorecard`` (T2.5) decomposes the model by
evidence-coverage stratum. ``propose`` (T1.4) emits an advisory draft grouping; ``reconcile
--accept-all`` (T2.7) bulk-confirms id bindings; ``egress-preview`` (T1.2) shows the exact
post-redaction bytes; ``explain`` (T2.6) cites the rule behind a placement / the evidence
behind an edge (and ``--lint`` flags shadowed/empty globs); ``verify`` (T1.5) is the
architecture-as-tests conformance gate (forbidden-dependency fitness functions as PASS/FAIL/
SKIP, merge-blocking exit code, with the §11.6 coverage-conditioned no-cry-wolf SKIP).
Live-LLM ``run``/``generate`` are fail-closed on an inert redaction policy unless
``--i-accept-unredacted-egress`` is passed (T1.2). ``run`` supports ``--no-llm`` /
``--llm-propose`` / ``--llm-accepted`` (§8.5), ``--incremental`` (§10.1), ``--diff`` (the
§9.1.1 content-vs-layout dry-run preview), ``--strict`` (turn the advisory readability +
layering gates into hard CI failures), and ``--resolve-deployment`` (opt into
environment-conditional Helm/Kustomize rendering — off by default so the hashed core stays
PATH-independent, T1.1). ``drift --fail-on-violation`` makes layering a non-zero CI gate.

Enforcement note (§A.2): ``run`` / ``extract`` run the §6.5d schema-version/CHANGELOG gate
via ``validate_facts.validate(..., check_changelog=True)``; ``run`` honors ``on_unmapped:
fail`` from mapping-rules.yaml; these are no longer CI-only theater.
"""
from __future__ import annotations

import argparse
import importlib.util
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import (adr, confidence, discover, explain, export_views, fetch, preview,
               propose, query, reconcile, scorecard)
from .config import load_mapping_rules
from .jsonio import dump_json, load_json
from .logutil import setup_console_logging, ts as _ts
from .paths import Workspace, resolve_workspace
from .runreport import RunReport
from .stages import (apply_mapping_rules, drift_report, enrich_llm,
                     extract_autotools, extract_build_graph, extract_clang_deps, extract_cmake,
                     extract_contracts, extract_cpp_facts, extract_csharp_facts,
                     extract_csproj_sdk, extract_deployment, extract_interop,
                     extract_msbuild_cpp, extract_orchardcore_manifest,
                     extract_plugin_namespace, extract_runtime,
                     generate_docs, generate_structurizr,
                     normalize_facts, risk, scenario_candidates,
                     validate_facts, whatif)

# Verbs whose module is OPTIONAL in a distribution: the on-demand ops / GUI surface that
# nothing on the `run → export --graph` path imports (serve + GUI bundle, mcp, combine,
# fleet, timeline, measure, verify). The anonymized replication package
# (baselines/make_review_artifact.py, PRUNED_REL) omits these modules; `_shipped` lets the
# matching subcommand simply not register there instead of failing at import time, and their
# `cmd_*` functions import the module lazily for the same reason. Everything else stays
# eagerly imported — the pipeline itself is never optional.
OPTIONAL_VERB_MODULES = {
    "serve": "serve", "mcp": "mcp", "combine": "combine", "verify": "verify",
    "fleet": "stages.fleet", "timeline": "stages.timeline", "measure": "stages.measure",
}


def _shipped(verb: str) -> bool:
    """True when the optional module behind ``verb`` is present in this installation."""
    return importlib.util.find_spec(f"{__package__}.{OPTIONAL_VERB_MODULES[verb]}") is not None

LLM_FLAG_TO_MODE = {"no_llm": "no-llm", "llm_propose": "llm-propose", "llm_accepted": "llm-accepted"}


def _git_commit(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _llm_mode(args: argparse.Namespace) -> str:
    for flag, mode in LLM_FLAG_TO_MODE.items():
        if getattr(args, flag, False):
            return mode
    return "no-llm"  # MVP-0/1 default (plan §8.5 / §16#18)


def _resolve_provider(mode: str):
    """Build the Azure AI Foundry provider for a live mode (plan §16#5).

    Returns ``(provider_or_None, message)``. For ``no-llm`` -> ``(None, None)``. When a
    live mode is requested but no usable config is found, ``provider`` is ``None`` and
    ``message`` explains what is missing; otherwise ``message`` is a ``client:model``
    descriptor for the run summary. No network call happens here — only config resolution.
    """
    if mode == "no-llm":
        return None, None
    from . import llm_provider
    cfg = llm_provider.load_llm_config()
    if cfg is None:
        return None, ("no LLM config — set ANON_LLM_* env vars or create llm.local.yaml "
                      "(see llm.example.yaml)")
    ok, why = cfg.usable()
    if not ok:
        return None, why
    return llm_provider.make_provider(cfg), f"{cfg.client_kind}:{cfg.model_id}"


def _workspace(args: argparse.Namespace) -> Workspace:
    return resolve_workspace(args.repo, getattr(args, "arch_dir", None), getattr(args, "rules_dir", None))


def _egress_gate(ws: Workspace, args: argparse.Namespace, mode: str, provider) -> int | None:
    """T1.2 fail-closed egress gate — only when a provider will ACTUALLY be called.

    Returns 4 (refuse) if a live-LLM run would egress under an inert redaction policy without
    ``--i-accept-unredacted-egress``; else None. No-op when ``provider is None`` (e.g.
    ``--llm-accepted`` serving pinned names only never reaches the provider, so nothing leaves)."""
    if provider is None:
        return None
    ok, msg = enrich_llm.egress_gate(ws, mode, getattr(args, "accept_unredacted", False))
    if not ok:
        print(f"[anon {_ts()}] {msg}", file=sys.stderr)
        return 4
    return None


def _write_egress_manifest(ws: Workspace, provider, prov_msg: str | None) -> None:
    """T1.2 signed egress manifest, only after a real provider run. Splits the ``client:model``
    descriptor so the manifest records ``provider`` (endpoint) and ``model_id`` distinctly."""
    if provider is None:
        return
    model_id = prov_msg.split(":", 1)[1] if (prov_msg and ":" in prov_msg) else (prov_msg or "")
    enrich_llm.write_egress_manifest(ws, provider_desc=prov_msg or "", model_id=model_id)


def _extractor_registry(resolve_deployment: bool = False):
    """``(run_fn, fragment_filename, version, input_globs)`` per extractor — the §10.1
    per-extractor incremental cache (item 5). Each ``run(ws)`` writes ``fragment_filename``
    under ``ws.fragments`` and returns its Path (or None on a graceful no-op). The globs are
    a deliberate **superset** of each extractor's real inputs: over-inclusion only forces a
    harmless re-extract, while under-inclusion would risk a stale skip — so we err broad.

    *resolve_deployment* is threaded explicitly into the deployment extractor (no process-global
    env var) AND folded into its cache version, so a rendered (environment-conditional) fragment
    can never be served from cache for a default resolve-off run, or vice-versa (T1.1)."""
    cs = ["**/*.cs", "**/*.csproj", "**/*.slnx", "**/*.sln"]
    cpp = ["**/*.cpp", "**/*.cc", "**/*.cxx", "**/*.h", "**/*.hpp", "**/*.hxx"]
    cmake = ["**/CMakeLists.txt", "**/*.cmake", "**/compile_commands.json"]
    deploy = ["**/Dockerfile", "**/docker-compose*.y*ml", "**/*.yaml", "**/*.yml",
              "**/*.tf", "**/*.bicep", "**/*.json", "**/Chart.yaml", "**/kustomization.y*ml"]
    contracts = ["**/*.proto", "**/*.thrift", "**/*.fbs", "**/*.wsdl", "**/*.avsc", "**/*.idl"]
    return [
        (extract_build_graph.run, "csharp-build-graph.json", "1",
         ["**/*.sln", "**/*.slnx", "**/*.csproj", "**/*.props", "**/*.targets"]),
        (extract_cmake.run,       "cmake-graph.json",  "1", cmake),
        (extract_msbuild_cpp.run, "msbuild-cpp.json",  "1", ["**/*.vcxproj", "**/*.props", "**/*.targets", "**/*.binlog"]),
        (extract_autotools.run,   "autotools-graph.json", "1",
         ["**/Makefile.am", "**/Makefile.in", "**/configure", "**/configure.ac", "**/configure.in"]),
        (extract_csharp_facts.run, "roslyn-csharp.json", "1", cs),
        (extract_orchardcore_manifest.run, "orchardcore-manifest.json", "1", cs),
        (extract_csproj_sdk.run, "csproj-sdk-role.json", "1", cs),
        (extract_plugin_namespace.run, "plugin-namespace.json", "1", cs),

        (extract_cpp_facts.run,   "doxygen-xml.json",  "1", cpp + ["**/CMakeLists.txt"]),
        (extract_clang_deps.run,  "clang-scan-deps.json", "1", cpp + ["**/compile_commands.json"]),
        (extract_interop.run,     "csharp-interop.json", "1", cs + cpp + ["**/*.vcxproj"]),
        (extract_runtime.run,     "runtime.json",      "1", cs),          # §5 runtime topology
        (lambda ws: extract_deployment.run(ws, resolve=resolve_deployment),
         "deployment.json", "1+resolve" if resolve_deployment else "1", deploy),
        (extract_contracts.run,   "contracts.json",    "1", contracts + cs + cpp + ["**/*.go", "**/*.py"]),  # §16#14
    ]


def _cached_extractor(ws: Workspace, fc, run_fn, frag_name: str, version: str,
                      globs: list[str]) -> None:
    """Run one extractor with §10.1 per-extractor caching: restore its cached fragment when
    its inputs are unchanged, else re-extract and re-cache. A miss/corrupt cache falls
    through to a full, correct extract — incrementality changes speed, never the facts."""
    from .cache import decide
    key, changed = decide(ws.repo, frag_name, version, globs, cache_dir=fc.dir)
    if not changed:
        cached = fc.get(key)
        if cached is not None:
            dump_json(cached, ws.fragments / frag_name)   # restore — skip the extractor
            return
    path = run_fn(ws)
    if path is not None:
        fc.put(key, load_json(path))


def cmd_extract(ws: Workspace, report: RunReport, repo_name: str, commit: str,
                incremental: bool = False, resolve_deployment: bool = False,
                max_evidence_layer: str | None = None) -> dict:
    # §10.1 incremental extraction (item 5): cache PER EXTRACTOR (finer than whole-phase) so a
    # C#-only edit re-runs only the C#/interop/runtime extractors, not the CMake/Doxygen chain.
    fc = None
    if incremental:
        from .cache import FragmentCache
        fc = FragmentCache.for_workspace(ws)
    # Reset the fragments dir so NO prior run's output can leak into this run's facts: a
    # degraded extractor (e.g. cmake on a C#-only repo) writes nothing and would otherwise
    # leave its previous fragment behind, and normalize globs the whole dir — that is how a
    # stale abseil `cmake-graph.json` contaminated eShop with phantom C++ targets. Fragments
    # are a machine-local cache regenerated every run, so wiping is safe and deterministic;
    # the incremental FragmentCache lives in its own dir and still restores by key below.
    ws.fragments.mkdir(parents=True, exist_ok=True)
    for stale in ws.fragments.glob("*.json"):
        stale.unlink()
    with report.stage("extract"):
        for run_fn, frag_name, version, globs in _extractor_registry(resolve_deployment):
            # One substep per extractor (named after its fragment): breaks the extract
            # stage's wall-clock down per extractor in run-report/SSE/console, and puts
            # a visible bracket around each extractor's warnings — a bare "cmake
            # configure failed" is attributable to the extractor that was running.
            with report.substep("extract", frag_name.removesuffix(".json")):
                if incremental and fc is not None:
                    _cached_extractor(ws, fc, run_fn, frag_name, version, globs)
                else:
                    run_fn(ws)   # default path: byte-identical, no caching
    with report.stage("normalize"):
        facts = normalize_facts.run(ws, repo=repo_name, commit=commit,
                                    generated_at=datetime.now(timezone.utc).isoformat(),
                                    max_evidence_layer=max_evidence_layer)
    with report.stage("validate-extracted"):
        # check_changelog=True activates the §6.5d schema-version/CHANGELOG gate on the
        # path humans actually run (previously only the commented CI sketch called it).
        validate_facts.validate(facts, check_changelog=True)
    return facts


def _console_progress(event: dict) -> None:
    """Default RunReport sink for direct CLI runs: narrate live progress to stderr.

    Before this, `arch run` was silent for the entire extract/normalize minutes — the
    ▶/✓ stage lines existed only in the GUI's SSE feed. Substeps print their *start*
    line always (so the transcript shows which extractor is running when a warning —
    or a hang — appears) and a ✓ duration line only when they took ≥1s; every duration
    lands in run-report.json regardless."""
    kind = event.get("event")
    if kind == "stage_start":
        print(f"[anon {_ts()}] ▶ {event['stage']}", file=sys.stderr)
    elif kind == "stage_finish":
        print(f"[anon {_ts()}] ✓ {event['stage']} ({event['seconds']}s)", file=sys.stderr)
    elif kind == "substep_start":
        print(f"[anon {_ts()}]   · {event['substep']}", file=sys.stderr)
    elif kind == "substep_finish" and event.get("seconds", 0) >= 1.0:
        print(f"[anon {_ts()}]   ✓ {event['substep']} ({event['seconds']}s)", file=sys.stderr)


def cmd_run(args: argparse.Namespace, *, report: RunReport | None = None) -> int:
    """End-to-end pipeline run. *report* is injectable (GUI §7.5): the serve job manager
    passes a RunReport wired with an SSE sink + a cooperative cancel_event; a cancel
    raises JobCancelled at the next stage boundary and the completed stages are still
    recorded in run-report.json (stages write their artifacts atomically)."""
    ws = _workspace(args)
    mode = _llm_mode(args)
    # Resolve the LLM provider up front (cheap, no network) so a misconfigured propose run
    # fails before the expensive extract, rather than degrading every element (§8.5).
    provider, prov_msg = _resolve_provider(mode)
    if mode == "llm-propose" and provider is None:
        print(f"[anon {_ts()}] --llm-propose needs a model provider: {prov_msg}", file=sys.stderr)
        return 2
    if mode == "llm-accepted" and provider is None:
        print(f"[anon {_ts()}] --llm-accepted: {prov_msg}; serving pinned names only, "
              f"unreviewed elements -> needs-curation (§8.5).", file=sys.stderr)
    # T1.2 fail-closed egress: refuse a live-LLM run whose effective redaction policy is INERT,
    # unless --i-accept-unredacted-egress. Checked up front (before the expensive extract) so a
    # compliance refusal fails fast; only gates when a provider will actually be called.
    refusal = _egress_gate(ws, args, mode, provider)
    if refusal is not None:
        return refusal
    repo_name = ws.repo.name
    commit = _git_commit(ws.repo)
    if report is None:
        report = RunReport(sink=_console_progress)
    from .logutil import reset_warnings
    reset_warnings()  # per-run scope for the summary line's "N warning(s)" counter

    # For --diff, capture the previously generated curated model BEFORE we overwrite it,
    # so we can preview content vs layout-affecting changes (§9.1.1).
    prev_curated = load_json(ws.curated_facts) if (args.diff and ws.curated_facts.exists()) else None

    # §4.5.1 existing-model discovery (read-only) runs before extraction; advisory and
    # never fails the run — a repo with no hand-crafted models/ADRs is a no-op.
    try:
        discover.run(ws)
    except Exception as exc:  # noqa: BLE001 - discovery is advisory; never break the run
        print(f"[anon {_ts()}] discovery skipped: {exc}", file=sys.stderr)

    facts = cmd_extract(ws, report, repo_name, commit, getattr(args, "incremental", False),
                        resolve_deployment=getattr(args, "resolve_deployment", False),
                        max_evidence_layer=getattr(args, "max_evidence_layer", None))

    # §4.5.4 ADR adoption: tag named elements `adr:<id>` + surface candidate layering rules.
    # Runs after extraction (it annotates extracted-facts), before curation; no-op without ADRs.
    try:
        adr_summary = adr.run(ws)
        if adr_summary.get("tagged"):
            print(f"[anon {_ts()}] ADR adoption: tagged {adr_summary['tagged']} element(s) from "
                  f"{adr_summary['adrs']} ADR(s); {adr_summary['candidates']} candidate rule(s) "
                  f"(§4.5.4)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - ADR adoption is advisory; never break the run
        print(f"[anon {_ts()}] ADR adoption skipped: {exc}", file=sys.stderr)

    with report.stage("curate"):
        curated = apply_mapping_rules.run(ws)
    # D§6.2: legacy curation flags were reinterpreted — say so once per run, never silently.
    for note in curated.get("provenance", {}).get("deprecations", []) or []:
        print(f"[anon {_ts()}] DEPRECATION: {note}", file=sys.stderr)
    # container-groups plan §3.3: parent-label hygiene, advisory only.
    for note in curated.get("provenance", {}).get("parent_warnings", []) or []:
        print(f"[anon {_ts()}] WARNING parent: {note}", file=sys.stderr)
    # container-groups plan §4.4: the chosen grouping picture stays legible per run.
    parented = {t.get("container_id"): t.get("container_parent")
                for t in curated.get("targets", []) if t.get("container_parent")}
    if parented:
        print(f"[anon {_ts()}] parent groups: {len(parented)} container(s) under "
              f"{len(set(parented.values()))} parent group(s)", file=sys.stderr)
    with report.stage("validate-curated"):
        validate_facts.validate(curated)
    # T1.4: emit the advisory auto-propose DRAFT grouping (human-gated, never auto-applied;
    # written to generated/, so it never clobbers the hand-owned rules/mapping-rules.yaml).
    try:
        prop = propose.run(ws)
        if prop.get("groups"):
            print(f"[anon {_ts()}] auto-propose: {prop['groups']} draft group(s) covering "
                  f"{prop.get('targets_grouped', 0)} target(s) -> {ws.proposed_mapping_rules} "
                  f"(advisory; review & copy into rules, T1.4)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - proposal is advisory; never break the run
        print(f"[anon {_ts()}] auto-propose skipped: {exc}", file=sys.stderr)
    with report.stage("enrich"):
        enrich_llm.run(ws, mode, provider)
        _write_egress_manifest(ws, provider, prov_msg)
    with report.stage("generate"):
        gen = generate_structurizr.run(ws)
    with report.stage("drift"):
        drift = drift_report.evaluate(ws, commit=commit)

    # T1.3 + T2.5 advisory reporting instruments: the calibrated-abstention report (per-element
    # confidence + risk-coverage curve) and the coverage-honest fidelity scorecard. Both are pure,
    # deterministic functions of the curated facts (no schema change, never gate the run); wrapped
    # so a reporting hiccup never breaks the pipeline — they are dashboards, not gates.
    abst: dict = {}
    try:
        abst = confidence.run(ws)            # writes generated/abstention-report.md (T1.3)
        scorecard.run(ws)                    # writes generated/coverage-scorecard.md (T2.5)
        if abst.get("abstained"):
            print(f"[anon {_ts()}] abstention (T1.3): {abst['abstained']}/{abst['elements']} "
                  f"element(s) below τ={abst['threshold']:.2f} -> needs-curation; mean confidence "
                  f"{abst['mean_confidence']:.2f} -> {ws.abstention_report}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - reporting is advisory; never break the run
        print(f"[anon {_ts()}] abstention/scorecard skipped: {exc}", file=sys.stderr)

    # The advisory engine artifacts the GUI renders (GUI §7.3): the deterministic canvas
    # layout (engine §6.9), the risk report (§3), and ADR conformance (§2a; the
    # first-violating-commit history walk stays on-demand in `arch adr check`).
    # Re-emitted every run so the GUI never shows a picture or a verdict computed
    # against yesterday's facts; advisory — a failure never breaks the run.
    try:
        from . import layout
        views = layout.run_all_views(ws)
        risk.run(ws)
        adr.run_check(ws, history=False)
        # Decision archaeology (ADR-mining plan §8/§0.3#3): the DETERMINISTIC detectors refresh
        # every run so the inbox is never stale; run_stubs also pre-materializes the unioned,
        # dismissal-filtered inbox view (§9). The LLM miner stays OFF here (soft tier).
        adr.run_stubs(ws)
        # Datasheets LAST among the advisory artifacts (engine §1.4 + enrichment plan
        # §7): they ECHO the risk report just written above, so building them earlier
        # would echo yesterday's findings against today's facts.
        generate_docs.run_datasheets(ws)
        print(f"[anon {_ts()}] GUI artifacts refreshed: {len(views)} layout view(s) + "
              f"risk-report + adr-conformance + adr-candidates + datasheets (advisory, "
              f"engine §6.9/§3/§2a/§7.4/§1)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - advisory; never break the run
        print(f"[anon {_ts()}] layout/risk/adr artifacts skipped: {exc}", file=sys.stderr)

    # trust dashboard (§10.2)
    cov = drift_report._coverage_l0(curated)
    needs_curation = sum(1 for t in curated.get("targets", []) if "needs-curation" in t.get("tags", []))
    report.set_trust(
        coverage_pct={"L0": round(cov, 2)},
        unmapped_targets=needs_curation,
        over_budget_views=gen["over_budget_views"],
        edges_dropped=curated.get("provenance", {}).get("edges_dropped", 0),
        # T1.3: surface the calibrated-abstention summary so a CI step / fleet view can read it.
        mean_confidence=abst.get("mean_confidence"),
        abstained_targets=abst.get("abstained"),
    )
    report.write(ws)

    via = f" via {prov_msg}" if provider is not None else ""
    # A degraded run must not end on a healthy-looking summary while its warnings
    # scrolled past minutes earlier — count them into the last line.
    from .logutil import warnings_seen
    warned = f" | {warnings_seen()} warning(s) — see log above" if warnings_seen() else ""
    print(f"[anon {_ts()}] {repo_name}: {gen['containers']} containers, {gen['edges']} edges "
          f"(mode={mode}{via}) | {report.summary_line()}{warned}")
    # D§2.3 legibility: the chosen model shape + the numbers it was chosen from, so a
    # threshold-hover reshape (N crossing MODEL_Y_MAX) is never silent.
    shape_src = "pinned in mapping-rules" if gen.get("model_shape_pinned") \
        else f"auto: N={gen.get('targets_n')} targets, threshold {gen.get('model_y_max')}"
    print(f"[anon {_ts()}]   model shape {str(gen.get('model_shape', 'x')).upper()} ({shape_src}); "
          f"{gen.get('component_edges', 0)} component edge(s), "
          f"{gen.get('component_views', 0)} component view(s)")
    if gen.get("deployment_nodes") or gen.get("dynamic_views"):
        bits = []
        if gen.get("deployment_nodes"):
            envs = ", ".join(gen.get("deployment_envs", []))
            bits.append(f"{gen['deployment_nodes']} deployment nodes ({envs})")
        if gen.get("dynamic_views"):
            bits.append(f"{gen['dynamic_views']} dynamic view(s)")
        print(f"[anon {_ts()}]   P7: {'; '.join(bits)}")
    if gen["over_budget_views"]:
        print(f"[anon {_ts()}] WARNING views over budget: {', '.join(gen['over_budget_views'])}", file=sys.stderr)
    if args.diff:
        new_curated = load_json(ws.curated_facts)
        diff = preview.compute(prev_curated, new_curated)
        preview_md = preview.render(diff)
        (ws.generated / "run-diff.md").write_text(preview_md, encoding="utf-8", newline="")
        print("\n" + preview_md)

    # --- enforcement gates (previously advisory-only — the §A.2 "gates are theater" debt) ---
    exit_code = 0
    rules = load_mapping_rules(ws.mapping_rules)
    # on_unmapped: fail is an explicit per-repo curation policy (§7.3) — honor it always (it was
    # dead config before). Default 'annotate' keeps the run advisory, so the toy fixture is unaffected.
    if rules.defaults.get("on_unmapped", "annotate") == "fail" and needs_curation:
        print(f"[anon {_ts()}] FAIL (on_unmapped: fail): {needs_curation} target(s) unmapped / "
              f"needs-curation — assign a group or exclude them in mapping-rules.yaml (§7.3).",
              file=sys.stderr)
        exit_code = 3
    # --strict turns the advisory readability + layering gates into hard failures (for CI).
    if getattr(args, "strict", False):
        if gen["over_budget_views"]:
            print(f"[anon {_ts()}] FAIL (--strict): views over element budget: "
                  f"{', '.join(gen['over_budget_views'])} (§1.1#1/§9.1).", file=sys.stderr)
            exit_code = 3
        if drift.get("gating"):
            viol = ", ".join(f"{v['from']} -/-> {v['to']}" for v in drift["violations"])
            print(f"[anon {_ts()}] FAIL (--strict): layering violation(s): {viol} "
                  f"(§11.2; non-advisory run). See {ws.drift_md}.", file=sys.stderr)
            exit_code = 3
    return exit_code


def cmd_extract_only(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    report = RunReport(sink=_console_progress)
    cmd_extract(ws, report, ws.repo.name, _git_commit(ws.repo),
                max_evidence_layer=getattr(args, "max_evidence_layer", None))
    print(f"[anon {_ts()}] extracted facts -> {ws.extracted_facts}")
    return 0


def _import_seed_views(ws: Workspace, src_str: str, parsed) -> None:
    """Write a hand-crafted model's views/styles to a SEPARATE seed-views.dsl (§4.5.2).

    Imported once, never regenerated; we deliberately do NOT splice into the scaffolded
    views.dsl (which already owns a ``views {}`` block) to avoid producing a duplicate-block
    workspace. The architect reviews and merges the views/styles they want, then deletes it."""
    out = ws.arch_dir / "seed-views.dsl"
    if out.exists():
        return
    parts = [f"// Seeded from {src_str} (§4.5.2). HAND-OWNED, imported once, never regenerated.",
             "// Review and merge the views/styles you want into views.dsl, then delete this file.",
             ""]
    if parsed.views_text:
        parts += ["views {", parsed.views_text, "}", ""]
    if parsed.styles_text:
        parts += ["styles {", parsed.styles_text, "}", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8", newline="")


def _seed_from(ws: Workspace, src_str: str) -> None:
    """`arch curate --seed-from`: adopt a hand-crafted model as a curation seed (§4.5.2/§7.4)."""
    import yaml

    from . import reconcile, seed
    parsed = seed.parse_model(Path(src_str))
    seed_rules = seed.build_seed_rules(parsed, source=src_str)
    # (a) the reviewable seed proposal artifact (always written).
    ws.seed_rules.parent.mkdir(parents=True, exist_ok=True)
    ws.seed_rules.write_text(seed.to_yaml(seed_rules), encoding="utf-8", newline="")
    # (b) merge into mapping-rules.yaml only as a ONE-TIME bootstrap (never clobber a `seed:`d
    #     or hand-locked file — §7.1 propose->lock).
    existing: dict = {}
    if ws.mapping_rules.exists():
        existing = yaml.safe_load(ws.mapping_rules.read_text(encoding="utf-8-sig")) or {}
    if "seed" not in existing:
        merged = {**existing, **seed_rules}
        ws.mapping_rules.parent.mkdir(parents=True, exist_ok=True)
        ws.mapping_rules.write_text(seed.to_yaml(merged), encoding="utf-8", newline="")
    # (c) import the hand-crafted views/styles once (separate file, §4.5.2).
    if parsed.views_text or parsed.styles_text:
        _import_seed_views(ws, src_str, parsed)
    # (d) propose human-id -> stable-id bindings against extracted facts when available (§7.5).
    if ws.extracted_facts.exists():
        targets = load_json(ws.extracted_facts).get("targets", [])
        proposals = reconcile.propose_bindings(parsed.elements, targets)
        if not ws.human_id_bindings.exists():
            ws.human_id_bindings.parent.mkdir(parents=True, exist_ok=True)
            ws.human_id_bindings.write_text(reconcile.bindings_to_yaml(proposals),
                                            encoding="utf-8", newline="")
        ws.generated.mkdir(parents=True, exist_ok=True)
        (ws.generated / "id-reconciliation.md").write_text(
            reconcile.render_reconciliation_table(proposals), encoding="utf-8", newline="")
        matched = sum(1 for p in proposals if p.get("extracted_id"))
        print(f"[anon {_ts()}] seed: proposed {matched}/{len(proposals)} id binding(s) -> "
              f"{ws.human_id_bindings} (review `proposed`->`confirmed`, §7.5)", file=sys.stderr)
    else:
        print(f"[anon {_ts()}] seed: no extracted-facts yet — run `arch extract` first to propose "
              "id bindings (§7.5).", file=sys.stderr)
    print(f"[anon {_ts()}] seeded curation from {src_str} -> {ws.seed_rules} (review & lock, §7.1)")


def cmd_curate(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if getattr(args, "seed_from", None):
        _seed_from(ws, args.seed_from)
    curated = apply_mapping_rules.run(ws)
    validate_facts.validate(curated)
    print(f"[anon {_ts()}] curated -> {ws.curated_facts}; proposal -> {ws.curation_proposal_md}")
    if args.names:
        n = enrich_llm.emit_name_review_from_curated(ws)
        print(f"[anon {_ts()}] name review ({n} rows) -> {ws.name_review_tsv} (edit the llm_name column, §8.4)")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """`arch discover` (§4.5.1): read-only report of existing C4/Structurizr models + ADRs."""
    ws = _workspace(args)
    report = discover.run(ws)
    c = report["counts"]
    print(f"[anon {_ts()}] discovered {c['models']} model artifact(s), {c['adrs']} ADR(s) "
          f"-> {ws.discovered_artifacts}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    mode = _llm_mode(args)
    provider, prov_msg = _resolve_provider(mode)
    if mode == "llm-propose" and provider is None:
        print(f"[anon {_ts()}] --llm-propose needs a model provider: {prov_msg}", file=sys.stderr)
        return 2
    refusal = _egress_gate(ws, args, mode, provider)
    if refusal is not None:
        return refusal
    enrich_llm.run(ws, mode, provider)
    _write_egress_manifest(ws, provider, prov_msg)
    gen = generate_structurizr.run(ws)
    print(f"[anon {_ts()}] generated {gen['containers']} containers -> {ws.components_dsl}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    try:
        validate_facts.run(ws, args.which)
    except validate_facts.ValidationError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    print("facts OK (schema + referential integrity)")
    if args.workspace:
        # §10 gate 4 / §14 P3: validate the generated workspace.dsl with the Structurizr
        # CLI. Skips gracefully if the CLI/Java is absent; FAILS the command on a real
        # DSL rejection so CI can gate on it.
        res = export_views.validate_workspace(ws)
        if not res["validated"] and not res["skipped"]:
            return 1
    return 0


def _reconcile_baseline(ws: Workspace, model_path: str) -> None:
    """§11.5 baseline reconciliation: diff extracted code facts against an adopted model.

    Honors ONLY `confirmed` human_id_bindings (§7.5/§11.6 — no confirmed binding, no finding).
    Advisory, never auto-applied."""
    from . import reconcile, seed
    parsed = seed.parse_model(Path(model_path))
    current = load_json(ws.curated_facts if ws.curated_facts.exists() else ws.extracted_facts)
    conf = reconcile.confirmed(reconcile.load_bindings(ws.human_id_bindings))
    recon = drift_report.reconcile_baseline(current, parsed.elements, conf)
    md = drift_report.render_reconciliation(recon, model_path)
    out = ws.generated / "baseline-reconciliation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8", newline="")
    print(f"[anon {_ts()}] baseline reconciliation -> {out} "
          f"({len(recon['model_not_in_code'])} model-not-in-code, "
          f"{len(recon['code_not_in_model'])} code-not-in-model, "
          f"{len(recon['unreconciled'])} unreconciled, §11.5)")


def cmd_drift(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    result = drift_report.evaluate(ws, baseline_path=args.baseline, commit=_git_commit(ws.repo))
    print(f"[anon {_ts()}] drift report -> {ws.drift_md}")
    if getattr(args, "reconcile_baseline", None):
        _reconcile_baseline(ws, args.reconcile_baseline)
    # `--fail-on-violation` makes `arch drift` a real CI gate (§11.2/§11.6): exit non-zero on a
    # layering VIOLATION, but only on a non-advisory run (a degraded run never gates, §11.6).
    if getattr(args, "fail_on_violation", False) and result.get("gating"):
        viol = ", ".join(f"{v['from']} -/-> {v['to']}" for v in result["violations"])
        print(f"[anon {_ts()}] FAIL (--fail-on-violation): layering violation(s): {viol} "
              f"(§11.2; non-advisory run).", file=sys.stderr)
        return 3
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """`arch verify` (T1.5): architecture-as-tests conformance gate (§11.2/§11.6).

    Runs the ``forbidden:`` fitness functions in layering-rules.yaml as a test suite and
    exits non-zero on a real (non-advisory) violation — a merge-blocking CI gate that runs
    alongside unit tests. A degraded run (coverage below floor) reports but never gates
    (the §11.6 no-cry-wolf SKIP)."""
    from . import verify
    ws = _workspace(args)
    report = verify.run(ws, baseline_path=getattr(args, "baseline", None),
                        commit=_git_commit(ws.repo), junit_path=getattr(args, "junit", None))
    if getattr(args, "format", "text") == "json":
        from .jsonio import dumps_json
        print(dumps_json(report), end="")
    else:
        print(verify.render(report), end="")
    print(f"[anon {_ts()}] conformance report -> {ws.conformance_md}", file=sys.stderr)
    # Merge-blocking: a non-advisory forbidden-dependency violation fails the gate (exit 3),
    # consistent with `drift --fail-on-violation` / `run --strict` (§11.2/§11.6).
    if report["gating"]:
        return 3
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """`arch explain` (T2.6): cite the rule behind a placement / evidence behind an edge.

    ``--id ID`` explains a target id (its container placement provenance + the edges that
    touch it) or an edge given as ``source->target`` (its cited evidence). ``--lint`` flags
    shadowed/dead globs + empty containers in mapping-rules.yaml. With neither, prints the
    full curation decision log + an advisory lint."""
    ws = _workspace(args)
    result = explain.run(ws, element=getattr(args, "id", None), lint=getattr(args, "lint", False))
    print(result["text"], end="")
    return 0


def cmd_abstain(args: argparse.Namespace) -> int:
    """`arch abstain` (T1.3): calibrated abstention over the curated model.

    Scores every element with a continuous, evidence-grounded confidence, abstains on the
    weak tail (below τ → needs-curation), and reports the selective-prediction risk-coverage
    curve + AURC. Advisory (exit 0) — it is a dashboard, not a gate; the risk axis is an
    explicit model-internal proxy (the calibrated ECE/AURC measurement is out-of-tree)."""
    ws = _workspace(args)
    summary = confidence.run(ws, threshold=getattr(args, "threshold", confidence.DEFAULT_ABSTAIN_THRESHOLD))
    if getattr(args, "format", "text") == "json":
        from .jsonio import dumps_json
        print(dumps_json(summary), end="")
    else:
        print(confidence.render(summary), end="")
    print(f"[anon {_ts()}] abstention report -> {ws.abstention_report}", file=sys.stderr)
    return 0


def cmd_scorecard(args: argparse.Namespace) -> int:
    """`arch scorecard` (T2.5): coverage-honest fidelity scorecard.

    Decomposes the curated model by evidence-coverage stratum (L0 build graph → L1 → L2/L3 →
    interop/runtime) with mean confidence + an abstention/partial column, plus the edge
    groundedness invariant. Advisory (exit 0) — the reporting instrument that lets a
    superiority claim be pre-registered on the high-coverage stratum (T2.5)."""
    ws = _workspace(args)
    card = scorecard.run(ws, threshold=getattr(args, "threshold", confidence.DEFAULT_ABSTAIN_THRESHOLD))
    if getattr(args, "format", "text") == "json":
        from .jsonio import dumps_json
        print(dumps_json(card), end="")
    else:
        print(scorecard.render(card), end="")
    print(f"[anon {_ts()}] coverage scorecard -> {ws.scorecard_md}", file=sys.stderr)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    if args.validate_only:
        report = fetch.validate_manifest(args.manifest)
        for err in report.get("errors", []):
            print(f"ERROR: {err}", file=sys.stderr)
        for warn in report.get("warnings", []):
            print(f"WARNING: {warn}", file=sys.stderr)
        if report.get("errors"):
            return 1
        print(f"[anon {_ts()}] manifest OK ({report.get('count', '?')} repos)")
        return 0
    result = fetch.run(args.manifest, args.ids or None)
    for line in result.get("status", []) if isinstance(result, dict) else []:
        print(f"[anon {_ts()}] {line}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if getattr(args, "graph", False):
        # eval §14.1: the canonical RSF dependency graph every SAR baseline consumes
        # (one input, many techniques — the §6.4 fairness control).
        from . import export_graph
        try:
            res = export_graph.run(ws, granularity=getattr(args, "granularity", "target"),
                                   include_external=getattr(args, "include_external", False))
        except export_graph.ExportGraphError as exc:
            print(f"[anon {_ts()}] export --graph: {exc}", file=sys.stderr)
            return 2
        if res is None:
            print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
            return 2
        g = res["meta"]["graph"]
        dropped = ", ".join(f"{v} {k.replace('_', '-')}" for k, v in sorted(g["dropped"].items()))
        print(f"[anon {_ts()}] graph ({res['meta']['granularity']}): {g['nodes']} nodes, "
              f"{g['edges']} edges, {g['isolated_nodes']} isolated "
              f"(dropped: {dropped}) -> {res['graph']}")
        if res["clusters"] is not None:
            c = res["meta"]["clusters"]
            print(f"[anon {_ts()}] clusters: {c['clusters']} cluster(s), {c['members']} member(s) "
                  f"-> {res['clusters']}")
        else:
            print(f"[anon {_ts()}] no curated-facts.json — clusters file skipped "
                  "(run `arch curate` for the pipeline decomposition).", file=sys.stderr)
        return 0
    if getattr(args, "layout", False):
        # engine §6.9: the deterministic layout-coordinate artifact the canvas renders
        # (GUI §2.1 — layout is engine data, never client computation).
        from . import layout
        try:
            # update_index: keep _views.json (the GET /model/views fast path) current for
            # this incrementally-exported view without requiring a full `arch run`.
            res = layout.run(ws, view=getattr(args, "view", "containers"),
                             update_index=True)
        except layout.LayoutError as exc:
            print(f"[anon {_ts()}] layout: {exc}", file=sys.stderr)
            return 2
        if res is None:
            print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
            return 2
        artifact, path = res
        m = artifact["metrics"]
        print(f"[anon {_ts()}] layout ({artifact['view']}, {artifact['engine']}): "
              f"{m['nodes']} nodes, {m['edges']} edges, "
              f"{m['edge_crossings']} crossing(s), {m['node_overlaps']} overlap(s) "
              f"-> {path}")
        if artifact["stale_pins"]:
            print(f"[anon {_ts()}] WARNING: {len(artifact['stale_pins'])} stale layout "
                  f"override(s) in {ws.layout_overrides.name} (element gone from the "
                  f"view, pin not applied): {', '.join(artifact['stale_pins'])} (§5.10)",
                  file=sys.stderr)
        return 0
    res = export_views.run(ws, args.format, render=getattr(args, "render", False))
    print(f"[anon {_ts()}] export ({args.format}): {res}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Release-to-release architecture diff between two committed fact snapshots (§11.5)."""
    facts_a = load_json(args.ref_a)
    facts_b = load_json(args.ref_b)
    snap = drift_report.diff_snapshots(facts_a, facts_b)
    print(drift_report.render_snapshots(snap, args.ref_a, args.ref_b))
    return 0


def cmd_combine(args: argparse.Namespace) -> int:
    """Multi-repo combine: UNION per-repo fact models into one (§21)."""
    from . import combine
    out = Path(args.out)
    combined = combine.run(args.members, out)
    n_t = len(combined.get("targets", []))
    n_r = len(combined.get("relationships", []))
    members = combined.get("provenance", {}).get("members", [])
    print(f"[anon {_ts()}] combined {len(members)} repo(s) -> {n_t} targets, {n_r} relationships "
          f"-> {out}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only tooling check against scripts/tooling.lock (§22.2)."""
    from . import doctor
    report = doctor.run(getattr(args, "lock", None))
    print(doctor.render(report))
    # T1.2: also run the LLM egress preflight (provider/auth + redaction policy together).
    # Advisory — arch doctor's exit stays tied to the tooling.lock check (LLM enrichment is
    # opt-in). Pass a workspace when --repo is given so the redaction half can resolve the
    # user/effective policy; without it, only the provider/auth half is checked.
    ws = _workspace(args) if getattr(args, "repo", None) else None
    print(doctor.render_llm_preflight(doctor.llm_preflight(ws)))
    return 0 if report["ok"] else 1


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Report tooling gaps + the platform bootstrap command (§22.2).

    The Python CLI never installs (boundary, §22.2) — actual provisioning is the
    OS-specific scripts/bootstrap.ps1 / bootstrap.sh. ``arch bootstrap`` runs doctor
    and prints the remediation, then points at the script.
    """
    from . import doctor
    report = doctor.run(getattr(args, "lock", None))
    print(doctor.render(report))
    print()
    print(doctor.bootstrap_advice(report))
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    """`arch propose` (T1.4 / P§7.1): emit an advisory DRAFT grouping, human-gated, never applied."""
    ws = _workspace(args)
    summary = propose.run(ws)
    print(f"[anon {_ts()}] auto-propose: {summary.get('groups', 0)} draft group(s) covering "
          f"{summary.get('targets_grouped', 0)} target(s) -> {ws.proposed_mapping_rules} "
          f"(advisory; review & copy the blocks you want into rules/mapping-rules.yaml, T1.4)")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """`arch reconcile --accept-all <band>` (T2.7 / §7.5): bulk-confirm proposed id bindings."""
    import yaml
    ws = _workspace(args)
    bindings = reconcile.load_bindings(ws.human_id_bindings)
    table = bindings.get("bindings", {}) if isinstance(bindings, dict) else {}
    if not table:
        print(f"[anon {_ts()}] no human_id_bindings to reconcile at {ws.human_id_bindings} "
              f"(run `arch curate --seed-from <model>` first, §7.5).", file=sys.stderr)
        return 1
    band = getattr(args, "accept_all", None)
    if not band:
        print(f"[anon {_ts()}] reconcile: {len(table)} binding(s) at {ws.human_id_bindings}; "
              f"pass --accept-all <high|medium|low> to bulk-confirm proposed ones (T2.7).")
        return 0
    new_bindings, n = reconcile.bulk_accept(bindings, band)
    ws.human_id_bindings.write_text(
        yaml.safe_dump(new_bindings, sort_keys=True, default_flow_style=False, allow_unicode=True),
        encoding="utf-8", newline="")
    print(f"[anon {_ts()}] reconcile: promoted {n} proposed binding(s) (confidence >= {band}) to "
          f"confirmed -> {ws.human_id_bindings} (now honored by curate, T2.7).")
    return 0


def cmd_egress_preview(args: argparse.Namespace) -> int:
    """`arch egress-preview` (T1.2): write the EXACT post-redaction bytes that would leave for the LLM."""
    ws = _workspace(args)
    out = enrich_llm.write_egress_preview(ws)
    print(f"[anon {_ts()}] egress preview (exact post-redaction payload, NO provider call) -> {out}")
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    """`arch measure` (datasheet plan §5): stat build artifacts into the Class-V
    timestamped sidecar generated/measurements.json. On-demand on purpose — never part
    of `arch run` (measuring implies trusting a build the pipeline didn't make). After
    measuring, re-runs the datasheets so the footprint block joins the fresh sidecar."""
    from .stages import measure
    ws = _workspace(args)
    result = measure.run(ws)
    if result is None:
        print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
        return 2
    print(f"[anon {_ts()}] measured {result['found']}/{result['targets']} build "
          f"artifact(s), {result['total_bytes']} byte(s) -> {result['path']}")
    if result["found"] == 0:
        print(f"[anon {_ts()}] note: found:false everywhere usually means nothing is built "
              "(or it built out-of-tree) — that is a normal answer, not an error.",
              file=sys.stderr)
    ds = generate_docs.run_datasheets(ws)
    if ds:
        print(f"[anon {_ts()}] datasheets refreshed with footprint -> "
              f"{ds['datasheets_json']}")
    return 0


def cmd_scenarios(args: argparse.Namespace) -> int:
    """`arch scenarios` (dynamic-view plan §8): re-emit the advisory scenario candidates
    on demand (mirrors `arch risk` / `arch measure` — no full run needed).

    Loads the latest curated/enriched facts, passes them through the SHARED
    `emission_normalized_facts` helper (so a candidate validates identically when promoted —
    no inbox bait-and-switch, §0.4#1), then sweeps the selected sources into
    `generated/scenario-candidates/<source>/*.json` + `index.json`. `--source` default is
    `all` MINUS `llm` MINUS `graph-walk` (C/F are opt-in, §6/§7; A is deferred, §4) — Source
    B only today. A combined multi-repo workspace is skipped with a logged note (§1)."""
    ws = _workspace(args)
    mode = _llm_mode(args)
    provider, prov_msg = _resolve_provider(mode)
    # A misconfigured propose run must fail loudly (exit 2), not silently no-op with 0 LLM
    # candidates — consistent with `cmd_run` / `cmd_generate` (§8.5).
    if mode == "llm-propose" and provider is None:
        print(f"[anon {_ts()}] --llm-propose needs a model provider: {prov_msg}", file=sys.stderr)
        return 2
    sources = scenario_candidates.resolve_sources(getattr(args, "source", None))
    llm_requested = "llm" in sources
    # F is a Phase-6 hook: the `llm` source no-ops today, but wire the egress gate now so a
    # live mode under an inert redaction policy fails closed exactly like `run`/`generate`.
    if llm_requested:
        gate = _egress_gate(ws, args, mode, provider)
        if gate is not None:
            return gate
    summary = scenario_candidates.run_cli(
        ws, sources=sources, provider=provider)
    # A real provider run that requested the llm source egresses — record the signed manifest
    # exactly as `cmd_generate` does (no-op when provider is None, §T1.2).
    if llm_requested:
        _write_egress_manifest(ws, provider, prov_msg)
    if summary is None:
        print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
        return 2
    if summary.get("skipped") == "combined":
        print(f"[anon {_ts()}] scenarios: skipped combined (<repo>::) workspace — multi-repo is "
              "out of scope for the first cut (plan §1).", file=sys.stderr)
        return 0
    total = summary.get("index_total", summary["candidates"])
    print(f"[anon {_ts()}] {summary['candidates']} scenario candidate(s) "
          f"{summary.get('per_source', {})} this run; inbox now holds {total} across all "
          f"sources (other sources preserved) -> {ws.scenario_candidates_dir / 'index.json'} "
          f"(advisory; {summary['dropped_quality_bar']} dropped by the quality bar)",
          file=sys.stderr)
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    """`arch risk` (engine §3): the ranked, cited structural maintainability-risk report.

    Explicitly NOT ATAM — the header states it provides cited structural inputs a risk
    review may consult. Advisory exit 0 by default; `--fail-on <severity>` makes it a CI
    gate (non-advisory findings at/above the threshold → exit 3). Detectors whose
    optional input is absent (git history, deployment facet, layering rules) report
    `not_evaluated`, never a silent "none found"."""
    ws = _workspace(args)
    report = risk.run(ws, top=getattr(args, "top", None),
                      with_scenarios=getattr(args, "with_scenarios", False))
    if report is None:
        print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
        return 2
    if getattr(args, "format", "md") == "json":
        from .jsonio import dumps_json
        print(dumps_json(report), end="")
    else:
        print(risk.render(report), end="")
    print(f"[anon {_ts()}] risk report -> {ws.risk_report_json}", file=sys.stderr)
    threshold = getattr(args, "fail_on", None)
    if threshold is not None:
        gating = [f for f in report["findings"]
                  if f["severity"] >= threshold and not f.get("advisory")]
        if gating:
            print(f"[anon {_ts()}] FAIL (--fail-on {threshold}): {len(gating)} "
                  "non-advisory finding(s) at/above threshold.", file=sys.stderr)
            return 3
    return 0


def cmd_datasheet(args: argparse.Namespace) -> int:
    """`arch datasheet` (engine §1): cited reference-manual entries per container +
    the guided tour. The headline value is the always-available L0/L1 content; the
    public-surface field is honestly "L2 not extracted" when L2 did not run."""
    ws = _workspace(args)
    result = generate_docs.run_datasheets(ws)
    if result is None:
        print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
        return 2
    from .jsonio import dumps_json
    sheets = load_json(ws.datasheets_json)
    if getattr(args, "id", None):
        rows = [r for r in sheets["containers"] if r["id"] == args.id
                or r["name"] == args.id]
        if not rows:
            known = ", ".join(r["id"] for r in sheets["containers"])
            print(f"[anon {_ts()}] no datasheet for '{args.id}' (known: {known})",
                  file=sys.stderr)
            return 2
        print(dumps_json(rows[0]) if args.format == "json"
              else generate_docs.render_datasheet(rows[0]), end="")
    elif getattr(args, "tour", False):
        tour = load_json(ws.datasheets_tour_json)
        print(dumps_json(tour) if args.format == "json"
              else generate_docs.render_tour(tour), end="")
    elif args.format == "json":
        print(dumps_json(sheets), end="")
    else:
        for row in sheets["containers"]:
            print(generate_docs.render_datasheet(row))
    print(f"[anon {_ts()}] {result['containers']} datasheet(s) -> {ws.datasheets_json} "
          f"(+ tour, {result['tour_hops']} hop(s), "
          f"{result['critical_seams']} critical seam(s))", file=sys.stderr)
    return 0


def cmd_adr_check(args: argparse.Namespace) -> int:
    """`arch adr check` (engine §2a): check hand-authored ADRs against the live model.

    Constraints are extracted from title AND body text; conformance runs the identical
    `check_layering` engine as `arch verify`. Advisory exit 0; `--strict` makes a
    non-advisory VIOLATED merge-blocking (exit 3), like a hand-written layering rule."""
    ws = _workspace(args)
    report = adr.run_check(ws, history=not getattr(args, "no_history", False),
                           baseline_path=getattr(args, "baseline", None),
                           since=getattr(args, "since", None))
    if report is None:
        print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
        return 2
    if getattr(args, "format", "text") == "json":
        from .jsonio import dumps_json
        print(dumps_json(report), end="")
    else:
        print(adr.render_conformance(report), end="")
    print(f"[anon {_ts()}] conformance report -> {ws.adr_conformance_json}",
          file=sys.stderr)
    if getattr(args, "strict", False) and report["gating"]:
        print(f"[anon {_ts()}] FAIL (--strict): {report['summary']['VIOLATED']} ADR(s) "
              "VIOLATED on a non-degraded run (engine §2a.4).", file=sys.stderr)
        return 3
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    """`arch query` (engine §6): the deterministic, cited oracle primitives.

    Every answer carries ``derived_from_hash`` + the coverage caveat; "no path found" /
    "unknown id (did you mean …?)" are first-class answers. ``conformance
    --fail-on-violation`` reuses the verify exit-3 semantics so an agent's proposed edge
    can be gated in CI (engine §6.4)."""
    ws = _workspace(args)
    result = query.run(ws, args.primitive, ident=getattr(args, "id", None),
                       to=getattr(args, "to", None),
                       transitive=getattr(args, "transitive", False),
                       limit=getattr(args, "limit", query.DEFAULT_LIMIT),
                       out=getattr(args, "out", False),
                       emit_view=getattr(args, "emit_view", False),
                       slug=getattr(args, "slug", None))
    if result is None:
        print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
        return 2
    if getattr(args, "format", "text") == "json":
        from .jsonio import dumps_json
        print(dumps_json(result), end="")
    else:
        print(query.render(result), end="")
    if result.get("written_to"):
        print(f"[anon {_ts()}] query result -> {result['written_to']}", file=sys.stderr)
    if result.get("view"):
        print(f"[anon {_ts()}] derived view '{result['view']['slug']}' -> "
              f"{result['view']['view_path']} (+ layout twin "
              f"{result['view']['layout_path']}) (§5.6)", file=sys.stderr)
    if result.get("error"):
        return 2
    # CI gate: a FORBIDDEN conformance answer is merge-blocking like `verify` (exit 3).
    if getattr(args, "fail_on_violation", False) and result.get("verdict") == "FORBIDDEN":
        print(f"[anon {_ts()}] FAIL (--fail-on-violation): the queried edge is forbidden "
              "by a layering rule (engine §6.4).", file=sys.stderr)
        return 3
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """`arch serve` (GUI plan §6/§7): the thin, read-only HTTP/JSON API over this
    workspace (Stage 1 — read endpoints + the oracle; writes/jobs arrive in Stage 2).

    Security posture (GUI §7.8): localhost bind by default; a per-session token is
    ALWAYS issued and required on state-changing requests (CSRF defence — localhost is
    not implicitly safe); Host/Origin are validated against the bind address
    (DNS-rebinding defence); ``--read-only`` disables every state-changing endpoint;
    a non-localhost ``--bind`` requires the token on reads too (bearer)."""
    ws = _workspace(args)
    try:
        from .serve import ServeUnavailable, create_app
        import uvicorn
    except ModuleNotFoundError:
        print(f'[anon {_ts()}] `arch serve` needs the [serve] extra: '
              'pip install -e ".[serve]"', file=sys.stderr)
        return 2
    host = args.bind
    port = args.port
    token = args.token or secrets.token_urlsafe(24)
    localhost = host in ("127.0.0.1", "localhost", "::1")
    allowed = {"127.0.0.1", "localhost", "::1"} if localhost else None
    extra_origins = tuple(getattr(args, "allow_origin", None) or ())
    try:
        app = create_app(ws, token=token, read_only=args.read_only,
                         allowed_hosts=allowed,
                         require_token_for_reads=not localhost,
                         extra_origins=extra_origins)
    except ServeUnavailable as exc:
        print(f"[anon {_ts()}] {exc}", file=sys.stderr)
        return 2
    print(f"[anon {_ts()}] serve → http://{host}:{port}/  (API http://{host}:{port}/api/v1, "
          f"read-only: {args.read_only})")
    print(f"[anon {_ts()}] session token: {token}")
    print(f"[anon {_ts()}] state-changing requests need header  X-Anon-Token: <token>"
          + ("  (reads too — non-localhost bind)" if not localhost else ""))
    if extra_origins:
        print(f"[anon {_ts()}] CORS granted to: {', '.join(extra_origins)} "
              "(token still required on writes, GUI §4.1)")
    # Ctrl+C with SSE streams held open (the GUI keeps /events open indefinitely and
    # EventSource auto-reconnects): on the exit signal we broadcast a shutdown
    # sentinel (app.state.anon_begin_shutdown) so every stream ends ITSELF and
    # graceful shutdown completes cleanly — no "waiting for connections" hang, no
    # force-cancel CancelledError tracebacks. timeout_graceful_shutdown stays as the
    # backstop for anything that still lingers (a slow in-flight request).
    class _Server(uvicorn.Server):
        def handle_exit(self, sig, frame):  # runs in the signal handler
            begin = getattr(app.state, "anon_begin_shutdown", None)
            if begin is not None:
                begin()
            super().handle_exit(sig, frame)

    server = _Server(uvicorn.Config(app, host=host, port=port, log_level="info",
                                    timeout_graceful_shutdown=5))
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # uvicorn re-raises the captured SIGINT after a clean shutdown
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """`arch mcp` (engine §6.4): the read-only MCP server exposing the query oracle to
    coding agents over stdio — the agent-facing twin of `arch serve`, sharing
    `query.dispatch` so the two surfaces can never disagree.

    Read-only by contract (engine §6.7): no tool mutates `rules/` or `generated/`.
    Stdlib-only (no [mcp] extra — deviation recorded in mcp.py); stdout carries the
    protocol, logs go to stderr."""
    from . import mcp
    return mcp.serve_stdio(_workspace(args))


def cmd_whatif(args: argparse.Namespace) -> int:
    """`arch whatif` (engine §7.1): simulate extract/merge/move/cut on an in-memory
    copy of the curated facts — a PROPOSAL for human evaluation, never applied.
    Writes only generated/whatif/<scenario>.{md,json}."""
    ws = _workspace(args)
    try:
        report = whatif.run(ws, args.op, scenario=getattr(args, "scenario", None),
                            members=getattr(args, "members", None),
                            ids=getattr(args, "ids", None),
                            name=getattr(args, "name", None),
                            frm=getattr(args, "frm", None),
                            to=getattr(args, "to", None))
    except whatif.WhatifError as exc:
        print(f"[anon {_ts()}] whatif: {exc}", file=sys.stderr)
        return 2
    if report is None:
        print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
        return 2
    if getattr(args, "format", "md") == "json":
        from .jsonio import dumps_json
        print(dumps_json(report), end="")
    else:
        print(whatif.render(report), end="")
    print(f"[anon {_ts()}] what-if proposal -> "
          f"{ws.whatif_dir / (report['scenario'] + '.json')} (advisory, never applied)",
          file=sys.stderr)
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """`arch timeline` (engine §7.2): narrate structural change across COMMITTED
    extracted-facts.json snapshots (§0.6 — never re-extract history). "history
    unavailable" is the normal answer on out-of-tree pilots, not an error."""
    from .stages import timeline
    ws = _workspace(args)
    report = timeline.run(ws, max_snapshots=getattr(args, "max_snapshots", None),
                          since=getattr(args, "since", None))
    if getattr(args, "format", "md") == "json":
        from .jsonio import dumps_json
        print(dumps_json(report), end="")
    else:
        print(timeline.render(report), end="")
    if report["snapshots"]:
        print(f"[anon {_ts()}] timeline ({len(report['snapshots'])} snapshot(s), "
              f"{len(report['events'])} event(s)) -> {ws.timeline_json}",
              file=sys.stderr)
    return 0


def cmd_fleet(args: argparse.Namespace) -> int:
    """`arch fleet` (engine §7.3): cross-system structural facts over the multi-repo
    `combine` output — library dependents, shared infrastructure, trust-boundary
    crossings — with per-member coverage so a missing repo is never a silent clean
    bill of health."""
    from .stages import fleet
    ws = _workspace(args)
    report = fleet.run(ws, combined_path=getattr(args, "combined", None),
                       package=getattr(args, "package", None))
    if report is None:
        print(f"[anon {_ts()}] fleet: no combined fact model — run `arch combine` first "
              "(or pass --combined <path>).", file=sys.stderr)
        return 2
    if getattr(args, "format", "md") == "json":
        from .jsonio import dumps_json
        print(dumps_json(report), end="")
    else:
        print(fleet.render(report), end="")
    print(f"[anon {_ts()}] fleet report -> {ws.fleet_dir / 'fleet-report.json'}",
          file=sys.stderr)
    return 0


def cmd_adr_stubs(args: argparse.Namespace) -> int:
    """`arch adr stubs` (engine §7.4): decision archaeology — ADR-CANDIDATE stubs with
    the EVIDENCE filled and the rationale a literal `TODO (human)`. Never invents
    rationale or rejected alternatives (§0.5); capped + thresholded so it stays
    triageable."""
    ws = _workspace(args)
    report = adr.run_stubs(ws, cap=getattr(args, "cap", None))
    if report is None:
        print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
        return 2
    if getattr(args, "format", "text") == "json":
        from .jsonio import dumps_json
        print(dumps_json(report), end="")
    else:
        print(adr.render_stub_summary(report), end="")
    print(f"[anon {_ts()}] {len(report['candidates'])} ADR candidate(s) -> "
          f"{ws.adr_stubs_dir} + {ws.adr_candidate_decisions_json} "
          f"(rationale = TODO (human), engine §7.4)", file=sys.stderr)
    return 0


def cmd_adr_mine(args: argparse.Namespace) -> int:
    """`arch adr mine` (ADR-mining plan §5): without `--llm`, an alias for the deterministic
    detectors (`adr stubs`); with `--llm`, the opt-in governed miner — egress-gated fail-closed,
    soft sidecar only, NEVER in CI. `--deep <id,…>` runs the agentic deep tier for those
    elements (tool-capable provider; off by default)."""
    ws = _workspace(args)
    mode = _llm_mode(args)
    llm = mode != "no-llm"
    if getattr(args, "consolidate", False) and not llm:
        print(f"[anon {_ts()}] --consolidate has no effect without --llm (the deterministic "
              "detectors don't run the consolidation pass); ignoring it.", file=sys.stderr)
    provider, prov_msg = (None, None)
    if llm:
        provider, prov_msg = _resolve_provider(mode)
        if provider is None:
            print(f"[anon {_ts()}] {prov_msg or 'no LLM provider configured'} — "
                  "mine without --llm to re-run the deterministic detectors.", file=sys.stderr)
            return 2
        gate = _egress_gate(ws, args, mode, provider)
        if gate is not None:
            return gate
    deep = {s.strip() for s in (getattr(args, "deep", None) or "").split(",") if s.strip()}
    report = adr.run_mine(ws, llm=llm, provider=provider, deep=deep or None,
                          cap=getattr(args, "cap", None),
                          llm_consolidate=getattr(args, "consolidate", False),
                          accept_unredacted=getattr(args, "accept_unredacted", False))
    if report is None:
        print(f"[anon {_ts()}] no fact model — run `arch run` first.", file=sys.stderr)
        return 2
    if report.get("error"):
        print(f"[anon {_ts()}] {report['error']}", file=sys.stderr)
        return 4
    if getattr(args, "format", "text") == "json":
        from .jsonio import dumps_json
        print(dumps_json(report), end="")
    n = len(report.get("candidates", []))
    where = (ws.adr_mined_candidates_json if llm else ws.adr_candidate_decisions_json)
    print(f"[anon {_ts()}] {n} {'mined' if llm else 'deterministic'} ADR candidate(s) -> {where} "
          f"(rationale = TODO (human); soft tier, never CI)" if llm
          else f"[anon {_ts()}] {n} ADR candidate(s) -> {where}", file=sys.stderr)
    return 0


def cmd_adr_draft(args: argparse.Namespace) -> int:
    """`arch adr draft <slug>` (ADR-mining plan §6.1): structure the architect's own sectioned
    notes into a well-formed ADR with Evidence + Linked-C4 pre-filled. Reads sections from
    `--input <yaml/json>` (keys: context/decision/rationale/consequences/alternatives); empty
    sections stay a literal `TODO (human)`. Prints the draft (and writes `--out` if given). Does
    NOT promote — the rationale is and stays the human's."""
    ws = _workspace(args)
    sections: dict[str, str] = {}
    inp = getattr(args, "input", None)
    if inp:
        from .config import load_yaml
        data = load_yaml(inp)
        raw = data.get("sections", data) if isinstance(data, dict) else {}
        sections = {k: str(v) for k, v in (raw or {}).items() if v is not None}
    provider = None
    mode = _llm_mode(args)
    if mode != "no-llm":
        provider, prov_msg = _resolve_provider(mode)
        if provider is None:
            print(f"[anon {_ts()}] {prov_msg} — drafting deterministically instead.",
                  file=sys.stderr)
        elif _egress_gate(ws, args, mode, provider) is not None:
            return 4
    accept = [s.strip() for s in (getattr(args, "accept_constraint", None) or [])]
    result = adr.draft_adr(ws, args.slug, sections, provider=provider,
                           accept_constraints=accept or None)
    if result is None:
        print(f"[anon {_ts()}] no candidate '{args.slug}' — see `arch adr stubs`/`mine`.",
              file=sys.stderr)
        return 2
    out_path = getattr(args, "out", None)
    if out_path:
        from pathlib import Path
        with open(Path(out_path), "w", encoding="utf-8", newline="") as fh:
            fh.write(result["draft_md"])
    print(result["draft_md"], end="")
    if result["offered_constraints"]:
        print("\n[anon] offered constraint(s) (accept with --accept-constraint):",
              file=sys.stderr)
        for o in result["offered_constraints"]:
            print(f"  - {o['sentence']}  [{o['verdict']}]", file=sys.stderr)
    return 0


def cmd_adr_promote(args: argparse.Namespace) -> int:
    """`arch adr promote <slug>` (ADR-mining plan §6.2): write the reviewed, human-authored body
    (`--from <draft.md>`, else the engine stub) into the repo's ADR directory as the next
    numbered file. Scaffold-once / never-overwrite / hand-owned; refreshes discovery."""
    ws = _workspace(args)
    body = None
    frm = getattr(args, "from_file", None)
    if frm:
        from pathlib import Path
        body = Path(frm).read_text(encoding="utf-8")
    try:
        result = adr.promote_adr(ws, args.slug, body, title=getattr(args, "title", None))
    except FileExistsError as exc:
        print(f"[anon {_ts()}] {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"[anon {_ts()}] {exc}", file=sys.stderr)
        return 2
    if result is None:
        print(f"[anon {_ts()}] no candidate '{args.slug}' and no --from body to promote.",
              file=sys.stderr)
        return 2
    print(f"[anon {_ts()}] promoted ADR -> {result['path']} (hand-owned; refreshed discovery). "
          "Fill any TODO (human) sections, then `arch adr check`.", file=sys.stderr)
    print(result["path"])
    return 0


def cmd_adr_dismiss(args: argparse.Namespace) -> int:
    """`arch adr dismiss <slug>` (ADR-mining plan §3.1): mark a candidate not-worth-an-ADR. Writes
    a hand-owned `rules/adr-candidates.yaml` `dismissed:` entry keyed by the candidate's current
    evidence_hash (so it resurfaces only if its evidence materially changes), then refreshes the
    inbox. Audited: the candidate stays computed in adr-candidate-decisions.json."""
    ws = _workspace(args)
    candidate = adr.find_candidate(ws, args.slug)
    if candidate is None:
        print(f"[anon {_ts()}] no candidate '{args.slug}' to dismiss.", file=sys.stderr)
        return 2
    from . import rules_edit
    op = {"op": "dismiss_candidate", "slug": args.slug,
          "reason": getattr(args, "reason", "") or "",
          "evidence_hash": candidate.get("evidence_hash", "")}
    old = ws.adr_candidates_yaml.read_text(encoding="utf-8") if ws.adr_candidates_yaml.exists() else ""
    try:
        new_text = rules_edit.apply_ops(old, [op])
    except rules_edit.RulesEditError as exc:
        print(f"[anon {_ts()}] unsafe edit: {exc.message}", file=sys.stderr)
        return 2
    ws.adr_candidates_yaml.parent.mkdir(parents=True, exist_ok=True)
    with open(ws.adr_candidates_yaml, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    adr.materialize_inbox(ws)
    print(f"[anon {_ts()}] dismissed '{args.slug}' -> {ws.adr_candidates_yaml} "
          "(resurfaces only if its evidence changes; still audited in "
          "adr-candidate-decisions.json).", file=sys.stderr)
    return 0


def cmd_not_implemented(name: str, task: str):
    def _inner(args: argparse.Namespace) -> int:
        print(f"[anon {_ts()}] `arch {name}` is not yet implemented ({task}).", file=sys.stderr)
        return 2
    return _inner


def _add_common(p: argparse.ArgumentParser, rules: bool = False) -> None:
    p.add_argument("--repo", required=True, help="target working tree (source of truth)")
    p.add_argument("--arch-dir", help="artifact dir (default <repo>/architecture)")
    if rules:
        p.add_argument("--rules-dir", help="rules dir (default <arch-dir>/rules)")


def _add_llm_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--no-llm", dest="no_llm", action="store_true", help="baseline, no model call (default)")
    g.add_argument("--llm-propose", dest="llm_propose", action="store_true", help="names into review file")
    g.add_argument("--llm-accepted", dest="llm_accepted", action="store_true", help="reviewed/pinned names")
    # T1.2 fail-closed override: proceed even when the effective redaction policy is INERT (no
    # secret patterns / deny-list). NOT in the mutex group — it modifies any live-LLM mode.
    p.add_argument("--i-accept-unredacted-egress", dest="accept_unredacted", action="store_true",
                   help="explicitly proceed with an inert redaction policy (T1.2 fail-closed override)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="arch", description="Anon architecture pipeline (plan §18.2).")
    sub = ap.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="the whole pipeline end-to-end (default)")
    _add_common(p_run, rules=True)
    _add_llm_flags(p_run)
    p_run.add_argument("--incremental", action="store_true", help="re-extract only changed targets (§10.1)")
    p_run.add_argument("--diff", action="store_true", help="preview model/layout changes (§9.1.1)")
    p_run.add_argument("--strict", action="store_true",
                       help="turn advisory readability + layering gates into hard failures "
                            "(non-zero exit) for CI (§1.1#1/§11.2)")
    p_run.add_argument("--resolve-deployment", dest="resolve_deployment", action="store_true",
                       help="render Helm/Kustomize via local tooling — environment-conditional, "
                            "off by default to keep the hashed core PATH-independent (T1.1)")
    p_run.add_argument("--max-evidence-layer", dest="max_evidence_layer",
                       choices=["L0", "L1", "L2", "L3"], default=None,
                       help="restrict facts to evidence up to this layer (eval §7 A1: L0=build "
                            "graph only, +L1 declared refs, +L2 symbol/include); default = all")
    p_run.set_defaults(func=cmd_run)

    p_ex = sub.add_parser("extract", help="stages 1–2 only -> extracted-facts.json")
    _add_common(p_ex)
    p_ex.add_argument("--incremental", action="store_true")
    p_ex.add_argument("--max-evidence-layer", dest="max_evidence_layer",
                      choices=["L0", "L1", "L2", "L3"], default=None,
                      help="restrict facts to evidence up to this layer (eval §7 A1); default = all")
    p_ex.set_defaults(func=cmd_extract_only)

    p_cur = sub.add_parser("curate", help="apply mapping rules -> curated-facts.json + proposal")
    _add_common(p_cur, rules=True)
    p_cur.add_argument("--names", action="store_true", help="emit name-review.tsv (§8.4)")
    p_cur.add_argument("--seed-from", metavar="PATH",
                       help="adopt a hand-crafted Structurizr model (.dsl/workspace.json) "
                            "as a one-time curation seed (§4.5.2/§7.4)")
    p_cur.set_defaults(func=cmd_curate)

    p_gen = sub.add_parser("generate", help="stage 6 -> DSL fragments")
    _add_common(p_gen, rules=True)
    _add_llm_flags(p_gen)
    p_gen.set_defaults(func=cmd_generate)

    p_val = sub.add_parser("validate", help="schema + referential-integrity checks (+ optional Structurizr DSL)")
    _add_common(p_val)
    p_val.add_argument("--which", default="extracted", choices=["extracted", "curated"])
    p_val.add_argument("--workspace", action="store_true",
                       help="also run Structurizr `validate` on workspace.dsl (§10 gate 4; needs Java/CLI)")
    p_val.set_defaults(func=cmd_validate)

    p_dr = sub.add_parser("drift", help="code-vs-model drift + layering report (§11.1/§11.2)")
    _add_common(p_dr, rules=True)   # needs the rules dir to read layering-rules.yaml (§11.2)
    p_dr.add_argument("--baseline", help="committed baseline facts to diff against")
    p_dr.add_argument("--reconcile-baseline", metavar="MODEL",
                      help="adopted hand-crafted model (.dsl/workspace.json) to reconcile code "
                           "facts against, honoring confirmed human_id_bindings only (§11.5)")
    p_dr.add_argument("--fail-on-violation", dest="fail_on_violation", action="store_true",
                      help="exit non-zero on a layering VIOLATION in a non-advisory run "
                           "(makes `arch drift` a CI gate, §11.2/§11.6)")
    p_dr.set_defaults(func=cmd_drift)

    if _shipped("verify"):
        p_ver = sub.add_parser("verify",
                               help="architecture-as-tests conformance gate: forbidden-dependency "
                                    "fitness functions as PASS/FAIL/SKIP, merge-blocking (T1.5/§11.2)")
        _add_common(p_ver, rules=True)   # needs the rules dir to read layering-rules.yaml (§11.2)
        p_ver.add_argument("--baseline", help="committed baseline facts to diff against")
        p_ver.add_argument("--format", default="text", choices=["text", "json"],
                           help="report format (default text)")
        p_ver.add_argument("--junit", metavar="PATH",
                           help="also write a JUnit XML report so the gate slots into CI alongside "
                                "unit tests (deterministic, no timestamps, T1.5)")
        p_ver.set_defaults(func=cmd_verify)

    p_expl = sub.add_parser("explain",
                            help="cite the rule behind a placement / evidence behind an edge; "
                                 "--lint flags shadowed/empty globs (T2.6/§7.4)")
    _add_common(p_expl, rules=True)
    p_expl.add_argument("--id", metavar="ID",
                        help="a target id to explain, or an edge as `source->target`")
    p_expl.add_argument("--lint", action="store_true",
                        help="lint mapping-rules.yaml for shadowed/dead globs + empty containers")
    p_expl.set_defaults(func=cmd_explain)

    p_abst = sub.add_parser("abstain",
                            help="calibrated abstention: per-element confidence + risk-coverage "
                                 "curve; abstain the weak tail to needs-curation (T1.3)")
    _add_common(p_abst, rules=True)
    p_abst.add_argument("--threshold", type=float, default=confidence.DEFAULT_ABSTAIN_THRESHOLD,
                        help=f"abstention threshold τ on the [0,1] confidence "
                             f"(default {confidence.DEFAULT_ABSTAIN_THRESHOLD})")
    p_abst.add_argument("--format", default="text", choices=["text", "json"])
    p_abst.set_defaults(func=cmd_abstain)

    p_sc = sub.add_parser("scorecard",
                          help="coverage-honest fidelity scorecard: model composition decomposed "
                               "by evidence-coverage stratum + abstention/partial column (T2.5)")
    _add_common(p_sc, rules=True)
    p_sc.add_argument("--threshold", type=float, default=confidence.DEFAULT_ABSTAIN_THRESHOLD,
                      help="abstention threshold τ used for the abstained column")
    p_sc.add_argument("--format", default="text", choices=["text", "json"])
    p_sc.set_defaults(func=cmd_scorecard)

    p_fetch = sub.add_parser("fetch", help="clone/pin test repos from the manifest (§19.4)")
    p_fetch.add_argument("ids", nargs="*", help="fixture ids to fetch (default: all)")
    p_fetch.add_argument("--manifest", default="manifest.yaml")
    p_fetch.add_argument("--validate-only", action="store_true", help="validate the manifest, don't clone")
    p_fetch.set_defaults(func=cmd_fetch)

    p_exp = sub.add_parser("export", help="export curated views to PlantUML/Mermaid/DOT (§10.3) "
                                          "or deterministic layout coordinates (engine §6.9)")
    _add_common(p_exp, rules=True)
    p_exp.add_argument("--format", default="plantuml", choices=["plantuml", "mermaid", "dot"])
    p_exp.add_argument("--render", action="store_true",
                       help="also rasterize the exported text to views/*.svg on-prem (§10.3 step 2)")
    p_exp.add_argument("--layout", action="store_true",
                       help="emit the deterministic layout-coordinate artifact "
                            "generated/layout/<view>.json the canvas renders (engine §6.9; "
                            "consumes rules/layout-overrides.yaml pins, GUI §5.10)")
    p_exp.add_argument("--view", default="containers",
                       help="layout view key: containers (default) | component:<container-id>")
    p_exp.add_argument("--graph", action="store_true",
                       help="emit the canonical RSF dependency graph + pipeline clusters "
                            "under generated/graph/ — the single input every SAR baseline "
                            "consumes (eval plan §14.1/§6.4)")
    p_exp.add_argument("--granularity", default="target", choices=["target", "file"],
                       help="graph entity granularity (eval §4.1). 'file' merges the "
                            "per-file sidecars the C++ extractors emit (Doxygen / "
                            "clang-scan-deps); without one it fails honestly (C# Roslyn "
                            "per-file is an open eval work item)")
    p_exp.add_argument("--include-external", dest="include_external", action="store_true",
                       help="keep external/package targets and their edges in the graph "
                            "(default: first-party only, matching SAR ground truths)")
    p_exp.set_defaults(func=cmd_export)

    p_disc = sub.add_parser("discover", help="read-only report of existing C4/Structurizr models + ADRs (§4.5.1)")
    _add_common(p_disc)
    p_disc.set_defaults(func=cmd_discover)

    p_prop = sub.add_parser("propose", help="emit an advisory DRAFT grouping (P§7.1 Auto-propose, T1.4)")
    _add_common(p_prop, rules=True)
    p_prop.set_defaults(func=cmd_propose)

    p_rec = sub.add_parser("reconcile", help="bulk-confirm proposed human_id_bindings (§7.5 / T2.7)")
    _add_common(p_rec, rules=True)
    p_rec.add_argument("--accept-all", dest="accept_all", choices=["high", "medium", "low"], metavar="BAND",
                       help="promote all proposed bindings with confidence >= BAND to confirmed")
    p_rec.set_defaults(func=cmd_reconcile)

    p_egp = sub.add_parser("egress-preview",
                           help="write the exact post-redaction bytes that would leave for the LLM (T1.2; no call)")
    _add_common(p_egp, rules=True)
    p_egp.set_defaults(func=cmd_egress_preview)

    p_diff = sub.add_parser("diff", help="release-to-release architecture diff of two fact snapshots (§11.5)")
    p_diff.add_argument("ref_a", help="path to baseline facts JSON")
    p_diff.add_argument("ref_b", help="path to current facts JSON")
    p_diff.set_defaults(func=cmd_diff)

    if _shipped("combine"):
        p_comb = sub.add_parser("combine", help="UNION per-repo fact models into one (multi-repo, §21)")
        p_comb.add_argument("--members", required=True, help="members.yaml manifest (repos + pinned commits)")
        p_comb.add_argument("--out", required=True, help="path to write the combined extracted-facts.json")
        p_comb.set_defaults(func=cmd_combine)

    p_doc = sub.add_parser("doctor", help="read-only tooling check vs scripts/tooling.lock (§22.2)")
    p_doc.add_argument("--lock", help="path to tooling.lock (default: scripts/tooling.lock)")
    p_doc.add_argument("--repo", help="optional: target repo, to also check its redaction policy (T1.2)")
    p_doc.add_argument("--arch-dir")
    p_doc.add_argument("--rules-dir")
    p_doc.set_defaults(func=cmd_doctor)

    p_risk = sub.add_parser("risk",
                            help="ranked, cited structural maintainability-risk report (engine §3; NOT ATAM)")
    _add_common(p_risk, rules=True)
    p_risk.add_argument("--top", type=int, help="emit only the N highest-severity findings")
    p_risk.add_argument("--format", choices=["md", "json"], default="md")
    p_risk.add_argument("--with-scenarios", dest="with_scenarios", action="store_true",
                        help="attach §7.1 what-if proposals when that engine ships (no-op today)")
    p_risk.add_argument("--fail-on", dest="fail_on", type=float,
                        help="CI gate: exit 3 on a non-advisory finding at/above this severity")
    p_risk.set_defaults(func=cmd_risk)

    p_ds = sub.add_parser("datasheet",
                          help="cited per-container reference-manual entries + guided tour (engine §1)")
    _add_common(p_ds, rules=True)
    p_ds.add_argument("--id", help="emit one container's datasheet (id or name)")
    p_ds.add_argument("--tour", action="store_true", help="emit the guided-tour walk + critical seams")
    p_ds.add_argument("--format", choices=["md", "json"], default="md")
    p_ds.set_defaults(func=cmd_datasheet)

    if _shipped("measure"):
        p_meas = sub.add_parser("measure",
                                help="stat build artifacts into the timestamped "
                                     "generated/measurements.json sidecar (datasheet plan §5; "
                                     "on-demand, never in `arch run`)")
        _add_common(p_meas, rules=True)
        p_meas.set_defaults(func=cmd_measure)

    p_scen = sub.add_parser("scenarios",
                            help="re-emit advisory dynamic-view scenario candidates on demand "
                                 "(dynamic-view plan §8; B by default, C/F opt-in)")
    _add_common(p_scen, rules=True)
    p_scen.add_argument("--source", choices=["test", "runtime", "graph-walk", "llm", "all"],
                        default="all",
                        help="which candidate source(s) to sweep (default: all minus llm "
                             "minus graph-walk — Source B only; --source graph-walk|all "
                             "required for C, §6)")
    _add_llm_flags(p_scen)
    p_scen.set_defaults(func=cmd_scenarios)

    p_adr = sub.add_parser("adr", help="ADR umbrella: check (conformance §2a) / stubs / mine / "
                                       "draft / promote / dismiss (decision archaeology §7.4 + mining)")
    adr_sub = p_adr.add_subparsers(dest="adr_cmd", required=True)
    p_adrc = adr_sub.add_parser("check",
                                help="check hand-authored ADRs against the live model (title+body constraints)")
    _add_common(p_adrc, rules=True)
    p_adrc.add_argument("--strict", action="store_true",
                        help="exit 3 on a non-advisory VIOLATED ADR (merge-blocking, like verify)")
    p_adrc.add_argument("--format", choices=["text", "json"], default="text")
    p_adrc.add_argument("--baseline",
                        help="check the ADRs against this facts snapshot instead of the current model")
    p_adrc.add_argument("--since",
                        help="bound the first-violating-commit walk to <since>..HEAD (committed snapshots only)")
    p_adrc.add_argument("--no-history", dest="no_history", action="store_true",
                        help="skip the first-violating-commit walk over committed fact snapshots")
    p_adrc.set_defaults(func=cmd_adr_check)
    p_adrs = adr_sub.add_parser("stubs",
                                help="decision archaeology: ADR-CANDIDATE stubs, evidence filled, "
                                     "rationale TODO (human) (engine §7.4)")
    _add_common(p_adrs, rules=True)
    p_adrs.add_argument("--cap", type=int, default=None,
                        help="max candidates emitted (noise control, engine §7.4; default 8)")
    p_adrs.add_argument("--format", choices=["text", "json"], default="text")
    p_adrs.set_defaults(func=cmd_adr_stubs)

    p_adrm = adr_sub.add_parser("mine",
                                help="mine ADR candidates: deterministic detectors, or the opt-in "
                                     "governed LLM/agentic miner with --llm (ADR-mining plan §5)")
    _add_common(p_adrm, rules=True)
    _add_llm_flags(p_adrm)
    p_adrm.add_argument("--deep", help="comma-separated element ids for the agentic deep tier "
                                       "(tool-capable provider; off by default, never CI)")
    p_adrm.add_argument("--consolidate", action="store_true",
                        help="run the optional second LLM pass that fuses related technology "
                             "choices across containers (#1 hybrid; extra call, --llm only)")
    p_adrm.add_argument("--cap", type=int, default=None, help="max candidates emitted")
    p_adrm.add_argument("--format", choices=["text", "json"], default="text")
    p_adrm.set_defaults(func=cmd_adr_mine)

    p_adrd = adr_sub.add_parser("draft",
                                help="structure your OWN notes into an ADR draft with evidence + "
                                     "C4 links pre-filled (ADR-mining plan §6.1)")
    _add_common(p_adrd, rules=True)
    _add_llm_flags(p_adrd)
    p_adrd.add_argument("slug", help="candidate slug to author from (see `arch adr stubs`/`mine`)")
    p_adrd.add_argument("--input", help="YAML/JSON file with a `sections:` map (context/decision/"
                                        "rationale/consequences/alternatives) — the human's words")
    p_adrd.add_argument("--accept-constraint", dest="accept_constraint", action="append",
                        help="include an offered structural constraint sentence (repeatable)")
    p_adrd.add_argument("--out", help="also write the draft markdown to this path")
    p_adrd.set_defaults(func=cmd_adr_draft)

    p_adrp = adr_sub.add_parser("promote",
                                help="promote a reviewed, human-authored ADR into the repo's ADR "
                                     "directory (scaffold-once, never overwritten; §6.2)")
    _add_common(p_adrp, rules=True)
    p_adrp.add_argument("slug", help="candidate slug to promote")
    p_adrp.add_argument("--from", dest="from_file",
                        help="reviewed ADR markdown body to write (else the engine stub)")
    p_adrp.add_argument("--title", help="override the ADR title")
    p_adrp.set_defaults(func=cmd_adr_promote)

    p_adrx = adr_sub.add_parser("dismiss",
                                help="mark a candidate not-worth-an-ADR (hand-owned dismissal "
                                     "state, resurfaces only on evidence change; §3.1)")
    _add_common(p_adrx, rules=True)
    p_adrx.add_argument("slug", help="candidate slug to dismiss")
    p_adrx.add_argument("--reason", help="why this candidate is intentionally not documented")
    p_adrx.set_defaults(func=cmd_adr_dismiss)

    p_q = sub.add_parser("query",
                         help="deterministic, cited oracle queries over the curated model (engine §6)")
    _add_common(p_q, rules=True)
    p_q.add_argument("primitive", choices=list(query.PRIMITIVES),
                     help="dependents | dependencies | blast-radius | path | cycles | "
                          "conformance | explain")
    p_q.add_argument("--id", help="element: target id, container id/name, or component id")
    p_q.add_argument("--to", help="destination element (path / conformance / edge explain)")
    p_q.add_argument("--transitive", action="store_true",
                     help="transitive closure (dependents/dependencies)")
    p_q.add_argument("--limit", type=int, default=query.DEFAULT_LIMIT,
                     help="cap large transitive answers (ranked by weight, engine §6.7)")
    p_q.add_argument("--format", choices=["text", "json"], default="text")
    p_q.add_argument("--out", action="store_true",
                     help="also persist the cited answer under generated/queries/ (engine §6.6)")
    p_q.add_argument("--emit-view", dest="emit_view", action="store_true",
                     help="materialize the answer as a derived view under generated/views/derived/ "
                          "+ its §6.9 layout twin — the same artifact POST /views/derive and the "
                          "MCP tool produce (§5.6)")
    p_q.add_argument("--slug", help="derived-view name (default: <primitive>-<query-hash>)")
    p_q.add_argument("--fail-on-violation", dest="fail_on_violation", action="store_true",
                     help="exit 3 when a conformance answer is FORBIDDEN (CI gate, engine §6.4)")
    p_q.set_defaults(func=cmd_query)

    if _shipped("serve"):
        p_srv = sub.add_parser("serve",
                               help="thin read-only HTTP/JSON API over this workspace (GUI plan §6/§7; [serve] extra)")
        _add_common(p_srv, rules=True)
        p_srv.add_argument("--bind", default="127.0.0.1",
                           help="bind address (default 127.0.0.1; non-localhost requires the token on reads too)")
        p_srv.add_argument("--port", type=int, default=8765)
        p_srv.add_argument("--token", help="session token override (default: a fresh secret per start)")
        p_srv.add_argument("--read-only", dest="read_only", action="store_true",
                           help="disable every state-changing endpoint (auditor/stakeholder mode, GUI §7.8)")
        p_srv.add_argument("--allow-origin", dest="allow_origin", action="append", metavar="ORIGIN",
                           help="grant CORS to an extra origin (exact, or scheme://* prefix) — "
                                "the VS Code webview shell passes vscode-webview://* (GUI §4.1); "
                                "the token is still required on every write. Repeatable.")
        p_srv.set_defaults(func=cmd_serve)

    if _shipped("mcp"):
        p_mcp = sub.add_parser("mcp",
                               help="read-only MCP server: the query oracle as agent tools over stdio (engine §6)")
        _add_common(p_mcp, rules=True)
        p_mcp.add_argument("--stdio", action="store_true",
                           help="stdio transport (the default and only transport)")
        p_mcp.set_defaults(func=cmd_mcp)

    p_boot = sub.add_parser("bootstrap", help="report tooling gaps + the platform bootstrap command (§22.2)")
    p_boot.add_argument("--lock", help="path to tooling.lock (default: scripts/tooling.lock)")
    p_boot.set_defaults(func=cmd_bootstrap)

    p_wi = sub.add_parser("whatif",
                          help="simulate extract/merge/move/cut on the curated graph — "
                               "a PROPOSAL, never applied (engine §7.1)")
    p_wi.add_argument("op", choices=list(whatif.OPS),
                      help="extract: members -> a new container | merge: containers -> one | "
                           "move: members -> an existing container | cut: hypothetically remove "
                           "a container edge")
    _add_common(p_wi, rules=True)
    p_wi.add_argument("--members", action="append", metavar="TARGET_ID",
                      help="member build-target id (repeatable; extract/move)")
    p_wi.add_argument("--ids", action="append", metavar="CONTAINER",
                      help="container id or name (repeatable; merge)")
    p_wi.add_argument("--name", help="display name for the new container (extract/merge)")
    p_wi.add_argument("--from", dest="frm", metavar="CONTAINER",
                      help="source container (cut)")
    p_wi.add_argument("--to", metavar="CONTAINER",
                      help="destination container (move) / edge target (cut)")
    p_wi.add_argument("--scenario", help="artifact slug (default: <op>-<param-hash>)")
    p_wi.add_argument("--format", choices=["md", "json"], default="md")
    p_wi.set_defaults(func=cmd_whatif)

    if _shipped("timeline"):
        p_tl = sub.add_parser("timeline",
                              help="narrate structural change across COMMITTED fact snapshots "
                                   "(engine §7.2; 'history unavailable' is a normal answer)")
        _add_common(p_tl, rules=True)
        p_tl.add_argument("--since", help="bound the snapshot walk to <since>..HEAD (a git ref)")
        p_tl.add_argument("--max-snapshots", dest="max_snapshots", type=int,
                          help="cap the number of snapshots walked (most recent kept; default 200)")
        p_tl.add_argument("--format", choices=["md", "json"], default="md")
        p_tl.set_defaults(func=cmd_timeline)

    if _shipped("fleet"):
        p_fl = sub.add_parser("fleet",
                              help="cross-system structural facts over the multi-repo combine "
                                   "output (engine §7.3)")
        _add_common(p_fl, rules=True)
        p_fl.add_argument("--combined", metavar="FACTS_JSON",
                          help="path to a combined fact model (default: "
                               "<arch-dir>/generated/combined-facts.json)")
        p_fl.add_argument("--package", action="append", metavar="NAME",
                          help="also answer 'which systems declare a dependency on this "
                               "package' (honest package_ref match, NOT CVE matching; repeatable)")
        p_fl.add_argument("--format", choices=["md", "json"], default="md")
        p_fl.set_defaults(func=cmd_fleet)

    return ap


def main(argv: list[str] | None = None) -> int:
    # plan §22.3: UTF-8 console output on any OS code page. Shared with every stage's
    # `python -m` entry point via anon.force_utf8_stdio.
    from . import force_utf8_stdio
    force_utf8_stdio()
    # One timestamped console format for BOTH streams — the [anon …] narration and
    # the extractors' logging records (level via ANON_LOG, default info). Without
    # this, extractor warnings printed bare and their INFO context never showed.
    setup_console_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
