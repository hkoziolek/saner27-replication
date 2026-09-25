"""The optional-verb mechanism in ``anon.cli`` (``OPTIONAL_VERB_MODULES`` / ``_shipped``).

The anonymized replication package (``baselines/make_review_artifact.py``, ``PRUNED_REL``)
omits the on-demand ops / GUI modules nothing on the ``run → export --graph`` path imports.
Their subcommands must then simply not register — the CLI must not fail at import time, and
every pipeline verb the evaluation harness shells out to must still be there.
"""
from __future__ import annotations

import argparse
import importlib.util

import pytest

from anon import cli

HARNESS_VERBS = ("run", "extract", "curate", "generate", "drift", "diff", "propose", "export",
                 "fetch", "doctor", "validate")


def _choices(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:  # noqa: SLF001 - argparse has no public accessor
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return set(action.choices)
    raise AssertionError("no subparsers")


def test_shipped_mirrors_the_installed_modules():
    """Install-agnostic on purpose: this file also runs inside the pruned replication package,
    where the optional modules are absent — `_shipped` must then say so, not fail."""
    for verb, module in cli.OPTIONAL_VERB_MODULES.items():
        present = importlib.util.find_spec(f"{cli.__package__}.{module}") is not None
        assert cli._shipped(verb) == present, verb


def test_registered_verbs_match_what_ships():
    choices = _choices(cli.build_parser())
    shipped = {v for v in cli.OPTIONAL_VERB_MODULES if cli._shipped(v)}
    assert set(cli.OPTIONAL_VERB_MODULES) & choices == shipped
    assert set(HARNESS_VERBS) <= choices


def test_pruned_install_drops_only_the_optional_verbs(monkeypatch):
    monkeypatch.setattr(cli, "_shipped", lambda verb: False)
    choices = _choices(cli.build_parser())
    assert not (set(cli.OPTIONAL_VERB_MODULES) & choices)
    assert set(HARNESS_VERBS) <= choices


def test_optional_verbs_are_not_imported_eagerly():
    """The CLI module must not hold the optional modules as module-level names — otherwise a
    pruned package fails at `import anon.cli` before `_shipped` can help."""
    for module in cli.OPTIONAL_VERB_MODULES.values():
        assert not hasattr(cli, module.split(".")[-1]), module


@pytest.mark.parametrize("verb", sorted(cli.OPTIONAL_VERB_MODULES))
def test_pruned_verb_is_rejected_by_argparse_not_importerror(monkeypatch, verb, capsys):
    monkeypatch.setattr(cli, "_shipped", lambda v: v != verb)
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args([verb, "--repo", "."])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
