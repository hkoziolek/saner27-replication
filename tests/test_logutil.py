"""Console observability (§10.2): timestamped logging + per-extractor substeps.

Covers the three pieces that make a run transcript diagnosable:

- :mod:`anon.logutil` — one timestamped console format for BOTH output streams
  (the ``[anon …]`` narration and the extractors' :mod:`logging` records, which
  previously fell through to Python's bare last-resort handler);
- :meth:`RunReport.substep` — per-extractor timing/events inside the ``extract`` stage
  (run-report ``substeps``, ``substep_start/finish`` on the SSE sink, cooperative
  cancellation at substep boundaries);
- ``cli._console_progress`` — the default CLI narration sink (a direct ``arch run``
  used to be silent between start and summary).

Console output only — no generated artifact carries a wall-clock stamp, so the
determinism guarantee is untouched (the golden suite enforces that independently).
"""
from __future__ import annotations

import logging
import re
import threading

import pytest

from conftest import pristine_toy
from anon import cli, logutil
from anon.paths import resolve_workspace
from anon.runreport import JobCancelled, RunReport

TS_RE = r"\d{2}:\d{2}:\d{2}"


# ------------------------------------------------------------------- logutil format

def _fresh_logger_tree():
    """Detach any prior anon console handler so each test sees a clean setup."""
    root = logging.getLogger("anon")
    for h in list(root.handlers):
        if getattr(h, "_anon_console", False):
            root.removeHandler(h)


def test_console_logging_formats_warning_with_timestamp_and_short_name(capsys):
    _fresh_logger_tree()
    logutil.setup_console_logging("info")
    logging.getLogger("anon.cmake_api").warning("configure failed (rc=1)")
    err = capsys.readouterr().err
    assert re.search(rf"^\[anon {TS_RE}\] WARNING cmake_api: configure failed \(rc=1\)$",
                     err, re.M), err


def test_console_logging_shows_info_without_level_tag(capsys):
    """INFO lines (e.g. the exact cmake command) must reach the console — hiding them
    is what made degraded runs unreadable — but without a noisy level tag."""
    _fresh_logger_tree()
    logutil.setup_console_logging("info")
    logging.getLogger("anon.cmake_api").info("configuring: cmake --preset x")
    err = capsys.readouterr().err
    assert re.search(rf"^\[anon {TS_RE}\] cmake_api: configuring: cmake --preset x$",
                     err, re.M), err
    assert "INFO" not in err


def test_console_logging_is_idempotent_and_relevels(capsys):
    _fresh_logger_tree()
    logutil.setup_console_logging("info")
    logutil.setup_console_logging("warning")  # second call re-levels, never duplicates
    root = logging.getLogger("anon")
    marked = [h for h in root.handlers if getattr(h, "_anon_console", False)]
    assert len(marked) == 1
    logging.getLogger("anon.x").info("hidden at warning level")
    logging.getLogger("anon.x").warning("shown")
    err = capsys.readouterr().err
    assert "hidden at warning level" not in err
    assert "shown" in err


def test_warning_counter_feeds_the_run_summary(capsys):
    """The `arch run` summary appends "N warning(s)" so a degraded run can't end on a
    healthy-looking line; the counter tallies WARNING+ through the console handler."""
    _fresh_logger_tree()
    logutil.setup_console_logging("info")
    logutil.reset_warnings()
    logging.getLogger("anon.cmake_api").info("not counted")
    logging.getLogger("anon.cmake_api").warning("counted")
    logging.getLogger("anon.extract_cpp_facts").warning("counted too")
    assert logutil.warnings_seen() == 2
    logutil.reset_warnings()
    assert logutil.warnings_seen() == 0
    capsys.readouterr()


# --------------------------------------------------------------- RunReport.substep

def test_substep_records_timing_and_emits_events():
    events = []
    report = RunReport(sink=events.append)
    with report.stage("extract"):
        with report.substep("extract", "cmake-graph"):
            pass
    assert "cmake-graph" in report.substeps["extract"]
    assert report.to_dict()["substeps"]["extract"]["cmake-graph"] >= 0
    kinds = [(e["event"], e.get("substep")) for e in events]
    assert ("substep_start", "cmake-graph") in kinds
    assert ("substep_finish", "cmake-graph") in kinds
    finish = next(e for e in events if e["event"] == "substep_finish")
    assert finish["stage"] == "extract" and "seconds" in finish


def test_substep_honors_cancel_at_its_boundary():
    """A GUI cancel lands at the next EXTRACTOR boundary, not minutes later at the
    stage boundary (§7.5 cooperative cancellation, carried one level down)."""
    cancel = threading.Event()
    report = RunReport(cancel_event=cancel)
    with pytest.raises(JobCancelled):
        with report.stage("extract"):
            with report.substep("extract", "first"):
                cancel.set()
            with report.substep("extract", "second"):
                raise AssertionError("substep after cancel must never run")
    assert "first" in report.substeps["extract"], "completed substep still recorded"
    assert "second" not in report.substeps["extract"]


# ------------------------------------------------------- CLI narration + cmd_extract

def test_console_progress_narrates_stages_and_substeps(capsys):
    cli._console_progress({"event": "stage_start", "stage": "extract"})
    cli._console_progress({"event": "substep_start", "stage": "extract",
                           "substep": "cmake-graph"})
    cli._console_progress({"event": "substep_finish", "stage": "extract",
                           "substep": "cmake-graph", "seconds": 2.5})
    cli._console_progress({"event": "substep_finish", "stage": "extract",
                           "substep": "runtime", "seconds": 0.01})
    cli._console_progress({"event": "stage_finish", "stage": "extract", "seconds": 3.0})
    err = capsys.readouterr().err
    assert re.search(rf"^\[anon {TS_RE}\] ▶ extract$", err, re.M)
    assert re.search(rf"^\[anon {TS_RE}\]   · cmake-graph$", err, re.M)
    assert re.search(rf"^\[anon {TS_RE}\]   ✓ cmake-graph \(2\.5s\)$", err, re.M)
    assert "runtime" not in err, "sub-second substep finishes stay quiet"
    assert re.search(rf"^\[anon {TS_RE}\] ✓ extract \(3\.0s\)$", err, re.M)


def test_cmd_extract_times_every_extractor_as_a_substep(tmp_path):
    """The 5-minute extract stage is attributable: one substep per registry extractor,
    named after its fragment, lands in run-report substeps AND on the event sink."""
    src = pristine_toy(tmp_path / "toy-src")
    ws = resolve_workspace(src, tmp_path / "arch", src / "architecture" / "rules")
    events = []
    report = RunReport(sink=events.append)
    cli.cmd_extract(ws, report, "toy-repo", "test-commit")
    expected = {frag.removesuffix(".json")
                for _fn, frag, _v, _g in cli._extractor_registry()}
    assert set(report.substeps["extract"]) == expected
    started = {e["substep"] for e in events if e["event"] == "substep_start"}
    assert started == expected
