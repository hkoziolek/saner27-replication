"""Workspace path resolution.

The pipeline reads source from a *target repo* (``--repo``) and writes all its
artifacts under an *architecture directory* (``--arch-dir``, default
``<repo>/architecture``). For OSS fixtures the clone stays pristine and the arch-dir
is redirected out-of-tree to ``out/<id>/architecture`` (plan §19.4), with rules
overlaid from ``overlays/<id>/architecture/rules`` (``--rules-dir``).

The §12 layout is implemented here, in one place, so every stage agrees on where
things live.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    """Resolved locations for one pipeline run (plan §12 layout)."""

    repo: Path          # the target working tree (source of truth; never modified)
    arch_dir: Path      # where artifacts are written (default <repo>/architecture)
    rules_dir: Path     # rules/ (may be overlaid out-of-tree for fixtures)

    # --- machine-owned, committed (overwritten every run) — generated/ ---
    @property
    def generated(self) -> Path:
        return self.arch_dir / "generated"

    @property
    def fragments(self) -> Path:
        """Extractor fact-fragments (machine-local, gitignored under .cache/)."""
        return self.arch_dir / ".cache" / "fragments"

    @property
    def cache(self) -> Path:
        return self.arch_dir / ".cache"

    @property
    def cmake_build_dir(self) -> Path:
        """The CMake configure dir shared by the C++ extractors (L0/L1 + Doxygen attribution).

        ``ANON_CMAKE_BUILD_DIR`` overrides the default ``.cache/cmake-build`` for
        projects that hard-cap the build-dir path length (e.g. ITK's 50-char FATAL_ERROR
        check) — a host-environment knob like ``ANON_CMAKE_ARGS``; it changes only
        WHERE configure runs, never what is extracted. Defined here so every stage that
        touches the File API reply agrees on the location.
        """
        env_dir = os.environ.get("ANON_CMAKE_BUILD_DIR")
        return Path(env_dir) if env_dir else self.cache / "cmake-build"

    # individual generated artifacts (plan §12)
    @property
    def extracted_facts(self) -> Path:
        return self.generated / "extracted-facts.json"

    @property
    def curated_facts(self) -> Path:
        return self.generated / "curated-facts.json"

    @property
    def enriched_facts(self) -> Path:
        return self.generated / "enriched-facts.json"

    @property
    def curation_proposal_yaml(self) -> Path:
        return self.generated / "curation-proposal.yaml"

    @property
    def curation_proposal_md(self) -> Path:
        return self.generated / "curation-proposal.md"

    @property
    def name_review_tsv(self) -> Path:
        return self.generated / "name-review.tsv"

    @property
    def components_dsl(self) -> Path:
        return self.generated / "generated-components.dsl"

    @property
    def relationships_dsl(self) -> Path:
        return self.generated / "generated-relationships.dsl"

    # --- P7 machine-owned generated DSL (regenerated every run, §4.4/§9.3/§9.4) ---
    @property
    def deployment_dsl(self) -> Path:
        """Deployment MODEL block (deploymentEnvironment …), !included in workspace.dsl model{} (§4.4)."""
        return self.generated / "generated-deployment.dsl"

    @property
    def deployment_views_dsl(self) -> Path:
        """Deployment + container-runtime VIEW defs, !included in views.dsl views{} (§9.3)."""
        return self.generated / "generated-deployment-views.dsl"

    @property
    def dynamic_dsl(self) -> Path:
        """Dynamic (behavioral) VIEW defs from scenarios.yaml, !included in views.dsl views{} (§9.4)."""
        return self.generated / "generated-dynamic.dsl"

    @property
    def generated_views_dsl(self) -> Path:
        """Canonical/diagnostic VIEW defs compiled from view-intents.yaml (§6.1), !included in views.dsl."""
        return self.generated / "generated-views.dsl"

    @property
    def drift_md(self) -> Path:
        return self.generated / "architecture-drift.md"

    @property
    def conformance_md(self) -> Path:
        """T1.5 `arch verify` architecture-as-tests conformance report (machine-owned)."""
        return self.generated / "architecture-conformance.md"

    @property
    def abstention_report(self) -> Path:
        """T1.3 `arch abstain` calibrated-abstention report: per-element confidence +
        risk-coverage curve (machine-owned, advisory)."""
        return self.generated / "abstention-report.md"

    @property
    def scorecard_md(self) -> Path:
        """T2.5 `arch scorecard` coverage-honest fidelity scorecard: fidelity-relevant model
        composition decomposed by evidence-coverage stratum (machine-owned, advisory)."""
        return self.generated / "coverage-scorecard.md"

    @property
    def run_report(self) -> Path:
        return self.generated / "run-report.json"

    @property
    def proposed_mapping_rules(self) -> Path:
        """§7.1 Auto-propose DRAFT grouping (machine-owned, advisory; never auto-applied).

        Written to generated/ on purpose so it can be regenerated every run WITHOUT clobbering
        the hand-owned, reviewed rules/mapping-rules.yaml. The architect copies the blocks they
        want into the real rules file and locks them (T1.4)."""
        return self.generated / "proposed-mapping-rules.yaml"

    @property
    def egress_preview(self) -> Path:
        """§8.3/T1.2 exact post-redaction bytes that WOULD leave for the LLM (no provider call)."""
        return self.generated / "egress-preview.json"

    @property
    def egress_manifest(self) -> Path:
        """§8.3/T1.2 signed egress manifest written AFTER a real LLM run (fields+hashes, not raw)."""
        return self.generated / "egress-manifest.json"

    @property
    def discovered_artifacts(self) -> Path:
        """§4.5.1 read-only discovery report (machine-owned, advisory; not the fact-model contract)."""
        return self.generated / "discovered-artifacts.json"

    @property
    def adr_candidates_md(self) -> Path:
        """§4.5.4 ADR-derived *candidate* layering rules surfaced for human promotion (advisory)."""
        return self.generated / "adr-candidate-rules.md"

    @property
    def views_export(self) -> Path:
        return self.generated / "views"

    @property
    def docs_dir(self) -> Path:
        """Machine-owned generated documentation (Markdown sections), attached to the software
        system via ``!docs`` so the ``model.softwaresystem.documentation`` inspection passes (§9)."""
        return self.generated / "docs"

    @property
    def adrs_dir(self) -> Path:
        """Machine-owned generated architecture decision records, attached via ``!adrs`` so the
        ``model.softwaresystem.decisions`` inspection passes (§9)."""
        return self.generated / "adrs"

    # --- engine-plan advisory artifacts (engine §1/§2a/§3/§6; echoed by `arch serve`) ---
    # All machine-owned, on-demand, under generated/ — never part of the byte-identical
    # golden set (engine §9). Centralized here per the paths.py single-source rule even
    # though some producers (datasheet/risk/adr-check stages) are still to be built.
    @property
    def datasheets_json(self) -> Path:
        """Engine §1 per-container datasheets (machine-readable; `GET /datasheets`)."""
        return self.docs_dir / "datasheets.json"

    @property
    def datasheets_tour_json(self) -> Path:
        """Engine §1 deterministic guided-tour walk + critical-seams list."""
        return self.docs_dir / "datasheets" / "tour.json"

    @property
    def risk_report_json(self) -> Path:
        """Engine §3 ranked, cited structural-risk findings (`arch risk`)."""
        return self.generated / "risk-report.json"

    @property
    def adr_conformance_json(self) -> Path:
        """Engine §2a per-ADR conformance verdicts (`arch adr check`)."""
        return self.generated / "adr-conformance.json"

    @property
    def queries_dir(self) -> Path:
        """Engine §6.6 persisted query answers (`arch query --out`)."""
        return self.generated / "queries"

    @property
    def derived_views_dir(self) -> Path:
        """Engine §6 query-derived views (`--emit-view` / `POST /views/derive`)."""
        return self.generated / "views" / "derived"

    @property
    def dynamic_views_dir(self) -> Path:
        """Engine §6.9(c) evidence-validated scenario step lists for the GUI sequence strip."""
        return self.generated / "views" / "dynamic"

    @property
    def scenario_candidates_dir(self) -> Path:
        """Dynamic-view plan §0.4#4 advisory scenario candidates (`<source>/<slug>.json` +
        `index.json`) — machine-owned, overwritten every run; never part of the hashed core."""
        return self.generated / "scenario-candidates"

    @property
    def layout_dir(self) -> Path:
        """Engine §6.9 deterministic layout-coordinate artifacts (`arch export --layout`)."""
        return self.generated / "layout"

    @property
    def layout_view_index(self) -> Path:
        """The view-availability index `run_all_views` writes alongside the per-view layout
        artifacts — the sorted list of emitted view keys, so `GET /model/views` answers the
        GUI's grey-out/drill affordance with ONE small read instead of parsing every layout
        file. Advisory, machine-owned; a leading `_` can never collide with a `view_slug`
        (which strips non-alphanumerics), so it is inert to the per-view glob."""
        return self.layout_dir / "_views.json"

    @property
    def whatif_dir(self) -> Path:
        """Engine §7.1 what-if simulation PROPOSALS (`arch whatif`) — advisory, never applied."""
        return self.generated / "whatif"

    @property
    def timeline_json(self) -> Path:
        """Engine §7.2 architectural timeline over committed fact snapshots (`arch timeline`)."""
        return self.generated / "architecture-timeline.json"

    @property
    def fleet_dir(self) -> Path:
        """Engine §7.3 portfolio/fleet report over a combined model (`arch fleet`)."""
        return self.generated / "fleet"

    @property
    def adr_stubs_dir(self) -> Path:
        """Engine §7.4 ADR-CANDIDATE stubs — evidence filled, rationale a literal human TODO."""
        return self.generated / "adr-stubs"

    @property
    def adr_candidate_decisions_json(self) -> Path:
        """Engine §7.4 machine-readable decision candidates (`arch adr stubs`)."""
        return self.generated / "adr-candidate-decisions.json"

    @property
    def adr_mined_candidates_json(self) -> Path:
        """ADR-mining plan §5 SOFT-tier LLM/agentic candidate sidecar (`arch adr mine --llm`).
        Non-deterministic, written only on demand — never in `arch run`, never in CI."""
        return self.generated / "adr-mined-candidates.json"

    @property
    def adr_merged_candidates_json(self) -> Path:
        """SOFT-tier sidecar for human-fused candidates ("Merge selected" in the inbox): one
        combined candidate per merge, superseding its constituents. On-demand only, never in
        `arch run`, never hashed/CI — same lifecycle as the mined sidecar."""
        return self.generated / "adr-merged-candidates.json"

    @property
    def adr_candidate_inbox_json(self) -> Path:
        """ADR-mining plan §9 engine-pre-materialized inbox: union of the deterministic
        decisions + the soft mined/merged sidecars, deduped + dismissal-filtered (serve echoes
        this, computes nothing — GUI §2.1 iron rule)."""
        return self.generated / "adr-candidate-inbox.json"

    @property
    def measurements_json(self) -> Path:
        """Class-V volatile measurements sidecar (`arch measure`, datasheet plan §5):
        build-artifact sizes, timestamped BY DESIGN, never part of any hashed core."""
        return self.generated / "measurements.json"

    # --- evaluation-plan artifacts (eval §14.1; `arch export --graph`) ---
    @property
    def graph_dir(self) -> Path:
        """Eval §14.1 canonical RSF dependency-graph export consumed by every baseline."""
        return self.generated / "graph"

    def graph_rsf(self, granularity: str = "target") -> Path:
        """`depend` tuples at *granularity* — target: from the EXTRACTED (pre-curation)
        facts; file: from the merged per-file sidecars (:mod:`anon.filedeps`)."""
        return self.graph_dir / f"graph-{granularity}.rsf"

    def clusters_rsf(self, granularity: str = "target") -> Path:
        """The pipeline's own decomposition as `contain` tuples (curated container_id;
        file granularity induces file → build-target → container, eval §4.1)."""
        return self.graph_dir / f"clusters-{granularity}.rsf"

    def graph_meta(self, granularity: str = "target") -> Path:
        """Provenance sidecar for the RSF export (source hashes, counts, dropped edges)."""
        return self.graph_dir / f"graph-meta-{granularity}.json"

    # --- hand-owned / scaffold-once (committed) ---
    @property
    def workspace_dsl(self) -> Path:
        return self.arch_dir / "workspace.dsl"

    @property
    def views_dsl(self) -> Path:
        return self.arch_dir / "views.dsl"

    @property
    def workspace_json(self) -> Path:
        return self.arch_dir / "workspace.json"

    # --- rules/ (reviewed, committed) ---
    @property
    def mapping_rules(self) -> Path:
        return self.rules_dir / "mapping-rules.yaml"

    @property
    def layering_rules(self) -> Path:
        return self.rules_dir / "layering-rules.yaml"

    @property
    def payload_redaction(self) -> Path:
        return self.rules_dir / "payload-redaction.yaml"

    @property
    def human_id_bindings(self) -> Path:
        return self.rules_dir / "human_id_bindings.yaml"

    @property
    def view_intents(self) -> Path:
        return self.rules_dir / "view-intents.yaml"

    @property
    def seed_rules(self) -> Path:
        """§7.4 reviewable seed proposal written by `arch curate --seed-from` (one-time bootstrap)."""
        return self.rules_dir / "seed-rules.yaml"

    @property
    def scenarios_yaml(self) -> Path:
        """Hand-authored behavioral scenarios for dynamic views (§9.4)."""
        return self.rules_dir / "scenarios.yaml"

    @property
    def members_yaml(self) -> Path:
        """Multi-repo aggregator manifest: member repos + pinned commits (§21.4)."""
        return self.rules_dir / "members.yaml"

    @property
    def layout_overrides(self) -> Path:
        """GUI §5.10 hand-pinned layout positions (per view, keyed by stable element id),
        consumed deterministically by the engine §6.9 layout stage."""
        return self.rules_dir / "layout-overrides.yaml"

    @property
    def datasheet_overrides(self) -> Path:
        """Hand-owned datasheet narratives (datasheet plan §4.4): per-container
        `abstract_md` + cited `key_points` that WIN over any LLM proposal, survive every
        run, and are never machine-rejected (unresolvable citations become a warning)."""
        return self.rules_dir / "datasheet-overrides.yaml"

    @property
    def adr_candidates_yaml(self) -> Path:
        """ADR-mining plan §3.1 hand-owned dismissal state: a `dismissed:` list with
        per-candidate `evidence_hash`-keyed expiry. Edited only through the comment-preserving
        line-editor write surface; filters the inbox view, never the deterministic core."""
        return self.rules_dir / "adr-candidates.yaml"


def resolve_workspace(
    repo: str | Path,
    arch_dir: str | Path | None = None,
    rules_dir: str | Path | None = None,
) -> Workspace:
    """Build a :class:`Workspace`, applying the §12/§19.4 defaults.

    - ``arch_dir`` defaults to ``<repo>/architecture``.
    - ``rules_dir`` defaults to ``<arch_dir>/rules``.
    """
    repo_p = Path(repo).resolve()
    arch_p = Path(arch_dir).resolve() if arch_dir else (repo_p / "architecture")
    rules_p = Path(rules_dir).resolve() if rules_dir else (arch_p / "rules")
    return Workspace(repo=repo_p, arch_dir=arch_p, rules_dir=rules_p)
