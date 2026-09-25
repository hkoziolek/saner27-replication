"""CLI enforcement gates are connected and ACTIVE (not CI-only theater).

These tests close the Theme-2 "advertised gates are theater" debt by proving the gates
the architect curates actually change ``arch`` exit codes — exercised through the real
``cli.build_parser`` → ``args.func(args)`` path, the same code humans run:

  * ``run --strict`` on a clean model exits 0; the §6.5d schema-changelog gate is wired
    into the ``run``/``extract`` path (``validate(..., check_changelog=True)``);
  * ``on_unmapped: fail`` from mapping-rules.yaml blocks the run (and ``annotate`` does not);
  * ``drift --fail-on-violation`` exits non-zero on a layering VIOLATION in a non-advisory
    run, and exits 0 without the flag (advisory by default, §11.6).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from anon.cli import build_parser
from anon.jsonio import dump_json
from anon.paths import resolve_workspace
from anon.stages import validate_facts

from conftest import pristine_toy

# These gates run the real `arch run`/`extract` orchestrator on the C#-only toy and assert
# only on L0 behaviour (exit codes, coverage, drafts) — never on Roslyn output. Suppress
# the optional dotnet/Roslyn extractor so a dev box with the .NET SDK doesn't pay MSBuild
# cold-start per run (conftest.no_dotnet_extract; mirrors CI — §18.4/§19.5).
pytestmark = pytest.mark.usefixtures("no_dotnet_extract")


def _run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def _toy(tmp_path: Path) -> Path:
    """A pristine, writable toy-repo copy (the fixture itself must stay read-only)."""
    return pristine_toy(tmp_path / "toy-src")


def _write_rules(rules_dir: Path, mapping_yaml: str) -> Path:
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "mapping-rules.yaml").write_text(mapping_yaml, encoding="utf-8", newline="")
    return rules_dir


# --- run --strict on a clean model -------------------------------------------------

def test_run_strict_passes_on_clean_toy(tmp_path: Path) -> None:
    """`arch run --strict` exits 0 on the toy (no over-budget views, no layering violation)."""
    repo = _toy(tmp_path)
    code = _run(["run", "--repo", str(repo), "--arch-dir", str(tmp_path / "arch"),
                 "--rules-dir", str(repo / "architecture" / "rules"), "--no-llm", "--strict"])
    assert code == 0


# --- on_unmapped: fail is honored (was dead config) --------------------------------

def test_on_unmapped_fail_blocks_run(tmp_path: Path) -> None:
    """With no group rules every target is needs-curation; `on_unmapped: fail` -> non-zero."""
    repo = _toy(tmp_path)
    rules = _write_rules(tmp_path / "rules-fail", "defaults:\n  on_unmapped: fail\n")
    code = _run(["run", "--repo", str(repo), "--arch-dir", str(tmp_path / "arch-fail"),
                 "--rules-dir", str(rules), "--no-llm"])
    assert code == 3, "on_unmapped: fail must block the run when targets are unmapped"


def test_on_unmapped_annotate_allows_run(tmp_path: Path) -> None:
    """Same unmapped scenario, default `annotate` policy -> the run still succeeds (advisory)."""
    repo = _toy(tmp_path)
    rules = _write_rules(tmp_path / "rules-annotate", "defaults:\n  on_unmapped: annotate\n")
    code = _run(["run", "--repo", str(repo), "--arch-dir", str(tmp_path / "arch-annotate"),
                 "--rules-dir", str(rules), "--no-llm"])
    assert code == 0, "annotate must not block — proves the gate is the policy, not any unmapped"


# --- schema-version / CHANGELOG gate is wired into the run path --------------------

def test_schema_changelog_gate_is_wired_into_run(tmp_path: Path, monkeypatch) -> None:
    """`run` calls validate(check_changelog=True): a CHANGELOG problem aborts the run.

    Force ``check_schema_changelog`` to report a problem; the run's validate-extracted stage
    must surface it as a ValidationError (it would NOT if the gate were unwired)."""
    monkeypatch.setattr(validate_facts, "check_schema_changelog",
                        lambda *a, **k: ["forced: schema_version bumped without a CHANGELOG entry"])
    repo = _toy(tmp_path)
    with pytest.raises(validate_facts.ValidationError, match="schema-changelog gate failed"):
        _run(["run", "--repo", str(repo), "--arch-dir", str(tmp_path / "arch-cl"),
              "--rules-dir", str(repo / "architecture" / "rules"), "--no-llm"])


# --- drift --fail-on-violation as a CI gate ----------------------------------------

def _seed_violation_workspace(tmp_path: Path):
    """A minimal workspace whose curated facts contain a forbidden A->B container edge,
    with full L0 coverage so the run is NOT advisory (so the violation is gating)."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    arch = tmp_path / "arch"
    rules = tmp_path / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    ws = resolve_workspace(str(repo), str(arch), str(rules))
    facts = {
        "schema_version": "1.2",
        "provenance": {"coverage": {"a": {"L0": True}, "b": {"L0": True}}},
        "targets": [
            {"id": "a", "name": "A", "container_id": "a", "container_name": "A",
             "external": False, "path": "a"},
            {"id": "b", "name": "B", "container_id": "b", "container_name": "B",
             "external": False, "path": "b"},
        ],
        "relationships": [
            {"id": "a->b", "source": "a", "target": "b",
             "evidence": [{"type": "link", "detail": "a links b"}],
             "weight": 5, "is_declared_dependency": True, "confidence": "low"},
        ],
    }
    ws.curated_facts.parent.mkdir(parents=True, exist_ok=True)
    dump_json(facts, ws.curated_facts)
    ws.layering_rules.parent.mkdir(parents=True, exist_ok=True)
    ws.layering_rules.write_text('forbidden:\n  - from: "A"\n    to: "B"\n',
                                 encoding="utf-8", newline="")
    return repo, arch, rules


def test_drift_fail_on_violation_exits_nonzero(tmp_path: Path) -> None:
    repo, arch, rules = _seed_violation_workspace(tmp_path)
    code = _run(["drift", "--repo", str(repo), "--arch-dir", str(arch),
                 "--rules-dir", str(rules), "--fail-on-violation"])
    assert code == 3, "a non-advisory layering VIOLATION must gate `arch drift`"


def test_drift_violation_without_flag_exits_zero(tmp_path: Path) -> None:
    """Default behavior is advisory: the same violation does not gate without the flag."""
    repo, arch, rules = _seed_violation_workspace(tmp_path)
    code = _run(["drift", "--repo", str(repo), "--arch-dir", str(arch), "--rules-dir", str(rules)])
    assert code == 0


# ============================================================================
# v2 feature wiring (T1.2 egress · T2.7 reconcile · T1.4 propose) — connected & active
# ============================================================================

def test_run_emits_auto_propose_draft(tmp_path: Path) -> None:
    """`arch run` writes the advisory T1.4 draft grouping to generated/ (never auto-applied)."""
    import yaml
    repo = _toy(tmp_path)
    arch = tmp_path / "arch"
    code = _run(["run", "--repo", str(repo), "--arch-dir", str(arch),
                 "--rules-dir", str(repo / "architecture" / "rules"), "--no-llm"])
    assert code == 0
    draft = arch / "generated" / "proposed-mapping-rules.yaml"
    assert draft.exists(), "auto-propose draft not written"
    doc = yaml.safe_load(draft.read_text(encoding="utf-8"))
    assert "group" in doc and doc["group"], "draft has no group blocks"
    # advisory only: it lives in generated/, NOT in the hand-owned rules dir.
    assert not (repo / "architecture" / "rules" / "proposed-mapping-rules.yaml").exists()


def test_propose_standalone(tmp_path: Path) -> None:
    repo = _toy(tmp_path)
    arch = tmp_path / "arch"
    _run(["run", "--repo", str(repo), "--arch-dir", str(arch),
          "--rules-dir", str(repo / "architecture" / "rules"), "--no-llm"])
    code = _run(["propose", "--repo", str(repo), "--arch-dir", str(arch),
                 "--rules-dir", str(repo / "architecture" / "rules")])
    assert code == 0
    assert (arch / "generated" / "proposed-mapping-rules.yaml").exists()


def test_reconcile_accept_all_promotes_to_confirmed(tmp_path: Path) -> None:
    """`arch reconcile --accept-all high` flips proposed bindings to confirmed (T2.7)."""
    import yaml
    repo = _toy(tmp_path)
    rules = tmp_path / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "human_id_bindings.yaml").write_text(
        "bindings:\n"
        "  orderProcessor:\n"
        "    extracted_id: csharp:csproj:src/Orders/Op.csproj\n"
        "    status: proposed\n"
        "    confidence: high\n"
        "  weakMatch:\n"
        "    extracted_id: csharp:csproj:src/Other/X.csproj\n"
        "    status: proposed\n"
        "    confidence: low\n",
        encoding="utf-8", newline="")
    code = _run(["reconcile", "--repo", str(repo), "--arch-dir", str(tmp_path / "arch"),
                 "--rules-dir", str(rules), "--accept-all", "high"])
    assert code == 0
    doc = yaml.safe_load((rules / "human_id_bindings.yaml").read_text(encoding="utf-8"))
    assert doc["bindings"]["orderProcessor"]["status"] == "confirmed"   # >= high promoted
    assert doc["bindings"]["weakMatch"]["status"] == "proposed"         # low not promoted


def test_egress_preview_writes_post_redaction_payload(tmp_path: Path) -> None:
    repo = _toy(tmp_path)
    arch = tmp_path / "arch"
    _run(["run", "--repo", str(repo), "--arch-dir", str(arch),
          "--rules-dir", str(repo / "architecture" / "rules"), "--no-llm"])
    code = _run(["egress-preview", "--repo", str(repo), "--arch-dir", str(arch),
                 "--rules-dir", str(repo / "architecture" / "rules")])
    assert code == 0
    assert (arch / "generated" / "egress-preview.json").exists()


def test_egress_fail_closed_refuses_inert_policy(tmp_path: Path, monkeypatch) -> None:
    """Live-LLM `generate` with an INERT redaction policy refuses (exit 4), fail-closed (T1.2)."""
    from anon import cli
    repo = _toy(tmp_path)
    rules = tmp_path / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "payload-redaction.yaml").write_text("secret_patterns: []\ndeny_list: []\n",
                                                  encoding="utf-8", newline="")
    # force a (fake) provider so the egress gate is the thing being exercised, not provider-missing.
    monkeypatch.setattr(cli, "_resolve_provider", lambda mode: (object(), "stub:model"))
    code = _run(["generate", "--repo", str(repo), "--arch-dir", str(tmp_path / "arch"),
                 "--rules-dir", str(rules), "--llm-propose"])
    assert code == 4, "fail-closed egress must refuse an inert policy without the override"


def test_egress_override_bypasses_gate(tmp_path: Path, monkeypatch) -> None:
    """`--i-accept-unredacted-egress` lets an inert-policy run proceed past the gate (T1.2)."""
    from anon import cli
    from anon.stages import enrich_llm, generate_structurizr
    repo = _toy(tmp_path)
    rules = tmp_path / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "payload-redaction.yaml").write_text("secret_patterns: []\ndeny_list: []\n",
                                                  encoding="utf-8", newline="")
    monkeypatch.setattr(cli, "_resolve_provider", lambda mode: (object(), "stub:model"))
    monkeypatch.setattr(enrich_llm, "run", lambda *a, **k: None)
    monkeypatch.setattr(enrich_llm, "write_egress_manifest", lambda *a, **k: tmp_path / "m.json")
    monkeypatch.setattr(generate_structurizr, "run", lambda ws: {"containers": 0})
    code = _run(["generate", "--repo", str(repo), "--arch-dir", str(tmp_path / "arch"),
                 "--rules-dir", str(rules), "--llm-propose", "--i-accept-unredacted-egress"])
    assert code == 0, "override should let the run proceed past the egress gate"
