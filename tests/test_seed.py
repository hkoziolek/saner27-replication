"""Tests for adopt-as-seed (§4.5.2 / §7.4) — the ``arch curate --seed-from`` bootstrap.

Covers the three pure functions in ``anon.seed``:
  - ``parse_model`` on a hand-crafted ``workspace.dsl`` (elements, group nesting,
    descriptions, verbatim ``views {}`` / ``styles {}`` capture) and on a full
    ``workspace.json`` model.
  - ``build_seed_rules`` (the ``name_overrides`` / ``group`` / ``seed`` / ``descriptions``
    shape).
  - ``to_yaml`` determinism (same input -> identical string).

All I/O is to pytest ``tmp_path``; no extractor/SDK/LLM needed.
"""
from __future__ import annotations

import json
from pathlib import Path

from anon import seed

# A small hand-crafted Structurizr DSL: a softwareSystem with two containers, one inside a
# group "Backend"; plus verbatim views {} and styles {} blocks and an !include to skip.
_DSL = """\
workspace "Toy" "A hand-crafted model" {
    !include shared.dsl

    model {
        operator = person "Operator" "Runs the system"
        toy = softwareSystem "Toy System" "The system under design" {
            web = container "Web Frontend" "User-facing UI" "React"
            group "Backend" {
                api = container "API Service" "Handles requests" "C#"
            }
        }
    }

    views {
        systemContext toy "Context" {
            include *
            autolayout lr
        }
    }

    styles {
        element "Person" {
            shape Person
        }
    }
}
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8", newline="")
    return p


# --------------------------------------------------------------------------- DSL parse

def test_parse_dsl_extracts_elements_and_group(tmp_path: Path) -> None:
    path = _write(tmp_path, "workspace.dsl", _DSL)
    parsed = seed.parse_model(path)

    by_key = {e["key"]: e for e in parsed.elements}
    # all four element declarations captured (person + softwareSystem + 2 containers).
    assert set(by_key) == {"operator", "toy", "web", "api"}

    # display names, descriptions, technology lifted off the declarations.
    assert by_key["web"]["name"] == "Web Frontend"
    assert by_key["web"]["description"] == "User-facing UI"
    assert by_key["web"]["technology"] == "React"
    assert by_key["operator"]["name"] == "Operator"

    # the container declared inside `group "Backend"` records that group.
    assert by_key["api"]["group"] == "Backend"
    # the container declared directly under the softwareSystem records the parent name.
    assert by_key["web"]["group"] == "Toy System"

    assert "Backend" in parsed.groups


def test_parse_dsl_captures_views_and_styles_verbatim(tmp_path: Path) -> None:
    path = _write(tmp_path, "workspace.dsl", _DSL)
    parsed = seed.parse_model(path)

    assert "systemContext toy" in parsed.views_text
    assert "autolayout lr" in parsed.views_text
    # the !include line must NOT leak into captured view text.
    assert "!include" not in parsed.views_text

    assert 'element "Person"' in parsed.styles_text
    assert "shape Person" in parsed.styles_text


def test_parse_dsl_skips_include_lines(tmp_path: Path) -> None:
    path = _write(tmp_path, "workspace.dsl", _DSL)
    parsed = seed.parse_model(path)
    # no element should have been synthesized from the `!include shared.dsl` line.
    assert all("shared" not in e["name"].lower() for e in parsed.elements)


def test_parse_dsl_is_deterministically_sorted(tmp_path: Path) -> None:
    path = _write(tmp_path, "workspace.dsl", _DSL)
    a = seed.parse_model(path)
    b = seed.parse_model(path)
    assert a.elements == b.elements
    # sorted by (group, key, name): empty-group elements first, then "Backend", "Toy System".
    groups_in_order = [e["group"] for e in a.elements]
    assert groups_in_order == sorted(groups_in_order)


# ----------------------------------------------------------------------- build_seed_rules

def test_build_seed_rules_shape(tmp_path: Path) -> None:
    path = _write(tmp_path, "workspace.dsl", _DSL)
    parsed = seed.parse_model(path)
    rules = seed.build_seed_rules(parsed, source="docs/architecture/workspace.dsl")

    # name_overrides keyed by the element's own hand-crafted key (NOT a real stable id, §4.5.3).
    assert rules["name_overrides"]["web"] == "Web Frontend"
    assert rules["name_overrides"]["api"] == "API Service"
    assert rules["name_overrides"]["operator"] == "Operator"

    # group block: one {container, members: []} row per group/nesting label, members empty.
    containers = {g["container"]: g for g in rules["group"]}
    assert "Backend" in containers
    assert containers["Backend"]["members"] == []

    # seed provenance block — exactly per §7.4 example.
    assert rules["seed"] == {
        "from": "docs/architecture/workspace.dsl",
        "imported_views": True,
        "id_reconciliation": "pending",
    }

    # descriptions surfaced for §8 enrichment seeding.
    assert rules["descriptions"]["web"] == "User-facing UI"
    assert rules["descriptions"]["api"] == "Handles requests"


def test_build_seed_rules_imported_views_false_without_views() -> None:
    parsed = seed.ParsedModel(
        elements=[{"key": "a", "name": "A", "description": "", "technology": "", "group": ""}],
        groups=[],
        views_text="",
        styles_text="",
    )
    rules = seed.build_seed_rules(parsed, source="x.dsl")
    assert rules["seed"]["imported_views"] is False
    assert rules["group"] == []


# --------------------------------------------------------------------------- to_yaml

def test_to_yaml_is_deterministic(tmp_path: Path) -> None:
    path = _write(tmp_path, "workspace.dsl", _DSL)
    parsed = seed.parse_model(path)
    rules = seed.build_seed_rules(parsed, source="workspace.dsl")

    out1 = seed.to_yaml(rules)
    out2 = seed.to_yaml(rules)
    assert out1 == out2
    # LF-only, no CRLF (cross-platform byte stability gate, §1.1 metric 2).
    assert "\r" not in out1
    # round-trips back to the same dict.
    import yaml
    assert yaml.safe_load(out1) == rules
    # block style (sorted keys), not flow style.
    assert "seed:" in out1
    assert "{" not in out1.splitlines()[0]


# --------------------------------------------------------------------------- workspace.json

def test_parse_workspace_json_full_model(tmp_path: Path) -> None:
    ws = {
        "model": {
            "people": [
                {"id": "1", "name": "Operator", "description": "Runs it"}
            ],
            "softwareSystems": [
                {
                    "id": "2",
                    "name": "Toy System",
                    "description": "The system",
                    "containers": [
                        {"id": "3", "name": "Web Frontend",
                         "description": "UI", "technology": "React"},
                        {"id": "4", "name": "API Service",
                         "description": "Backend", "technology": "C#",
                         "group": "Backend"},
                    ],
                }
            ],
        },
        "views": {
            "systemContextViews": [{"key": "ctx"}],
        },
        "configuration": {
            "styles": {"elements": [{"tag": "Person", "shape": "Person"}]},
        },
    }
    path = _write(tmp_path, "workspace.json", json.dumps(ws))
    parsed = seed.parse_model(path)

    names = {e["name"] for e in parsed.elements}
    assert {"Operator", "Toy System", "Web Frontend", "API Service"} <= names

    by_name = {e["name"]: e for e in parsed.elements}
    assert by_name["API Service"]["group"] == "Backend"
    assert by_name["Web Frontend"]["technology"] == "React"
    assert "Backend" in parsed.groups

    # views/styles surfaced (as stable JSON text for the full-model case).
    assert parsed.views_text
    assert "Person" in parsed.styles_text

    rules = seed.build_seed_rules(parsed, source="workspace.json")
    # keyed by the json element id here (the human-authored id), §4.5.3.
    assert rules["name_overrides"]["3"] == "Web Frontend"
    assert rules["seed"]["id_reconciliation"] == "pending"
    assert rules["seed"]["imported_views"] is True
