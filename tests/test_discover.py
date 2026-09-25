"""Tests for §4.5.1 existing-architecture-artifact discovery (read-only).

Builds a tiny fake repo under ``tmp_path`` and asserts:
  - hand-crafted models are detected and format-classified (DSL, C4-PlantUML),
  - a layout-only ``workspace.json`` is distinguished from a full-model one,
  - ADR id / title / status are extracted (both Nygard and MADR),
  - the pipeline's own ``arch_dir`` (and ``generated/``) is excluded,
  - discovery mutates nothing and re-running yields byte-identical JSON.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anon.discover import discover, run
from anon.jsonio import dumps_json
from anon.paths import resolve_workspace


# --------------------------------------------------------------------------- #
# Fixture repo                                                                  #
# --------------------------------------------------------------------------- #

WORKSPACE_DSL = """\
workspace "Shop" {
    model {
        user = person "User"
        sys = softwareSystem "Shop" {
            web = container "Web"
        }
        user -> sys "uses"
    }
    views {
        systemContext sys { include * }
    }
}
"""

# A FULL hand-authored Structurizr model JSON (has model elements).
FULL_MODEL_JSON = {
    "name": "Shop",
    "model": {
        "people": [{"id": "1", "name": "User"}],
        "softwareSystems": [
            {"id": "2", "name": "Shop", "containers": [{"id": "3", "name": "Web"}]}
        ],
    },
    "views": {"systemContextViews": [{"key": "ctx"}]},
}

# A LAYOUT-ONLY JSON like the pipeline itself writes (§9.1): only element positions.
LAYOUT_ONLY_JSON = {
    "views": {
        "systemContextViews": [
            {
                "key": "ctx",
                "elements": [
                    {"id": "2", "x": 100, "y": 200},
                    {"id": "3", "x": 400, "y": 200},
                ],
            }
        ]
    },
    "configuration": {"lastSavedView": "ctx"},
}

C4_PUML = """\
@startuml
!include https://raw.githubusercontent.com/.../C4_Container.puml
Container(web, "Web", "ASP.NET")
@enduml
"""

# A .puml that is NOT C4-PlantUML (no C4 include) — must be ignored.
PLAIN_PUML = """\
@startuml
Alice -> Bob: hi
@enduml
"""

NYGARD_ADR = """\
# 1. Use MQTT for device telemetry

Date: 2026-01-02

## Status

Accepted

## Context

Devices need a lightweight pub/sub transport.

## Decision

We will use MQTT.

## Consequences

A broker must be operated.
"""

MADR_ADR = """\
---
status: proposed
date: 2026-02-03
---

# Adopt Postgres for the primary store

## Context and Problem Statement

We need a relational store.

## Decision Outcome

Chosen: Postgres, because it is well understood.
"""


def _build_repo(root: Path) -> None:
    """Create a tiny fake repo tree with hand-crafted artifacts."""
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "architecture").mkdir(parents=True, exist_ok=True)

    # Hand-crafted models at the repo root / docs.
    (root / "architecture" / "workspace.dsl").write_text(WORKSPACE_DSL, encoding="utf-8", newline="")
    (root / "docs" / "container.puml").write_text(C4_PUML, encoding="utf-8", newline="")
    (root / "docs" / "sequence.puml").write_text(PLAIN_PUML, encoding="utf-8", newline="")

    # Full-model workspace.json lives in a hand-authored location.
    (root / "model").mkdir(parents=True, exist_ok=True)
    (root / "model" / "workspace.json").write_text(
        json.dumps(FULL_MODEL_JSON, indent=2), encoding="utf-8", newline=""
    )

    # ADRs — Nygard + MADR.
    adr_dir = root / "docs" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)
    (adr_dir / "adr-001-use-mqtt.md").write_text(NYGARD_ADR, encoding="utf-8", newline="")
    (adr_dir / "0002-adopt-postgres.md").write_text(MADR_ADR, encoding="utf-8", newline="")
    # A non-ADR markdown in the same dir must be ignored.
    (adr_dir / "README.md").write_text("# ADR index\n", encoding="utf-8", newline="")


# --------------------------------------------------------------------------- #
# discover() — pure function                                                   #
# --------------------------------------------------------------------------- #

def test_discover_finds_models_and_classifies_formats(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    report = discover(tmp_path)

    by_path = {m["path"]: m for m in report["models"]}

    # Structurizr DSL detected.
    assert "architecture/workspace.dsl" in by_path
    assert by_path["architecture/workspace.dsl"]["format"] == "structurizr-dsl"
    assert by_path["architecture/workspace.dsl"]["parse_ok"] is True

    # C4-PlantUML detected; plain PlantUML ignored.
    assert "docs/container.puml" in by_path
    assert by_path["docs/container.puml"]["format"] == "c4-plantuml"
    assert "docs/sequence.puml" not in by_path


def test_discover_full_model_workspace_json(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    report = discover(tmp_path)
    by_path = {m["path"]: m for m in report["models"]}

    assert "model/workspace.json" in by_path
    entry = by_path["model/workspace.json"]
    assert entry["format"] == "structurizr-json"
    assert entry["kind"] == "model"  # full model, not layout
    assert entry["parse_ok"] is True


def test_discover_distinguishes_layout_only_json(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    # Add a layout-only workspace.json in a separate hand location.
    (tmp_path / "layout").mkdir()
    (tmp_path / "layout" / "workspace.json").write_text(
        json.dumps(LAYOUT_ONLY_JSON, indent=2), encoding="utf-8", newline=""
    )

    report = discover(tmp_path)
    by_path = {m["path"]: m for m in report["models"]}

    assert by_path["layout/workspace.json"]["kind"] == "layout"
    assert by_path["model/workspace.json"]["kind"] == "model"


def test_discover_adr_extraction(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    report = discover(tmp_path)
    by_path = {a["path"]: a for a in report["adrs"]}

    # Nygard.
    nygard = by_path["docs/adr/adr-001-use-mqtt.md"]
    assert nygard["id"] == "001"
    assert nygard["title"] == "1. Use MQTT for device telemetry"
    assert nygard["status"] == "Accepted"

    # MADR (front-matter status).
    madr = by_path["docs/adr/0002-adopt-postgres.md"]
    assert madr["id"] == "0002"
    assert madr["title"] == "Adopt Postgres for the primary store"
    assert madr["status"] == "proposed"

    # README.md is not an ADR.
    assert "docs/adr/README.md" not in by_path


def test_discover_excludes_arch_dir_and_generated(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    # Drop generated artifacts into the arch_dir that LOOK like hand-crafted models.
    gen = tmp_path / "architecture" / "generated"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "generated-relationships.dsl").write_text(WORKSPACE_DSL, encoding="utf-8", newline="")
    (gen / "workspace.json").write_text(
        json.dumps(FULL_MODEL_JSON, indent=2), encoding="utf-8", newline=""
    )

    report = discover(tmp_path, exclude_dirs=[tmp_path / "architecture"])
    paths = {m["path"] for m in report["models"]}

    # The hand-crafted workspace.dsl under architecture/ is excluded too (whole arch_dir).
    assert not any(p.startswith("architecture/") for p in paths)
    # Nothing from generated/ leaked in.
    assert not any("generated" in p for p in paths)


def test_discover_empty_repo_is_valid_report(tmp_path: Path) -> None:
    report = discover(tmp_path)
    assert report == {
        "models": [],
        "adrs": [],
        "counts": {"models": 0, "adrs": 0, "models_by_format": {}, "adrs_with_status": 0},
    }


def test_discover_missing_repo_does_not_raise(tmp_path: Path) -> None:
    report = discover(tmp_path / "does-not-exist")
    assert report["models"] == []
    assert report["adrs"] == []


def test_discover_counts(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    report = discover(tmp_path)
    counts = report["counts"]
    assert counts["models"] == len(report["models"])
    assert counts["adrs"] == 2
    assert counts["adrs_with_status"] == 2
    # Histogram is deterministic and sums to the model count.
    assert sum(counts["models_by_format"].values()) == counts["models"]


# --------------------------------------------------------------------------- #
# Determinism                                                                  #
# --------------------------------------------------------------------------- #

def test_discover_is_deterministic(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    r1 = discover(tmp_path)
    r2 = discover(tmp_path)
    assert dumps_json(r1) == dumps_json(r2)


def test_paths_are_repo_relative_and_sorted(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    report = discover(tmp_path)
    model_paths = [m["path"] for m in report["models"]]
    adr_paths = [a["path"] for a in report["adrs"]]
    # Forward-slash, relative (no drive/backslash), and sorted.
    for p in model_paths + adr_paths:
        assert "\\" not in p
        assert not Path(p).is_absolute()
    assert model_paths == sorted(model_paths)
    assert adr_paths == sorted(adr_paths)


# --------------------------------------------------------------------------- #
# run(ws) — writes the artifact, mutates nothing else                          #
# --------------------------------------------------------------------------- #

def test_run_writes_report_and_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_repo(repo)
    arch_dir = tmp_path / "out" / "architecture"
    ws = resolve_workspace(repo, arch_dir)

    report = run(ws)
    out = ws.generated / "discovered-artifacts.json"
    assert out.exists()

    # The returned report equals what was written.
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written == report

    # Re-running yields byte-identical JSON (determinism gate).
    first_bytes = out.read_bytes()
    run(ws)
    assert out.read_bytes() == first_bytes


def test_run_excludes_its_own_arch_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_repo(repo)
    # arch_dir defaults to <repo>/architecture — the same place a hand-crafted
    # workspace.dsl lives in our fixture. run() must exclude it.
    ws = resolve_workspace(repo)
    report = run(ws)
    paths = {m["path"] for m in report["models"]}
    assert not any(p.startswith("architecture/") for p in paths)
    # Non-arch-dir models still found.
    assert "docs/container.puml" in paths
    assert "model/workspace.json" in paths


def test_run_does_not_mutate_source_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_repo(repo)
    dsl = repo / "architecture" / "workspace.dsl"
    before = dsl.read_bytes()
    ws = resolve_workspace(repo, tmp_path / "out")
    run(ws)
    assert dsl.read_bytes() == before
