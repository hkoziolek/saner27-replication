"""Console-output helpers — one place that decides what a Anon log line looks like.

Two audiences share the console (plan §10.2 observability):

- the ``[anon …]`` narration lines the CLI prints (progress, summaries, pointers),
- the :mod:`logging` records the extractors emit (``anon.cmake_api`` warnings etc.).

Before this module existed the second stream fell through to Python's *last-resort*
handler: WARNING+ printed bare (no timestamp, no logger name, INFO silently dropped) —
so a failing ``cmake configure`` surfaced as two context-free error blobs and the
``configuring: <exact command>`` INFO line that would have explained them never showed.
:func:`setup_console_logging` gives both streams the same timestamped shape so a run
transcript reads as one coherent, attributable timeline.

Console only — file artifacts are never touched here, so the determinism guarantee
(byte-identical ``generated/``) is unaffected by wall-clock timestamps.
"""
from __future__ import annotations

import logging
import os
import sys
import time

#: Wall-clock stamp used by every console line, e.g. ``14:03:22``. Local time on
#: purpose: these lines are read live by the person watching the run, not archived.
TS_FMT = "%H:%M:%S"


def ts() -> str:
    """Current local wall-clock as ``HH:MM:SS`` — the timestamp inside ``[anon …]``."""
    return time.strftime(TS_FMT)


class _ConsoleFormatter(logging.Formatter):
    """``[anon HH:MM:SS] LEVEL name: message`` — LEVEL shown for WARNING+ only.

    The logger-name prefix ``anon.`` is stripped (every logger here lives under
    it), so an extractor warning reads e.g.
    ``[anon 14:03:22] WARNING cmake_api: cmake configure failed (rc=1) …``.
    """

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        if name.startswith("anon."):
            name = name[len("anon."):]
        level = f"{record.levelname} " if record.levelno >= logging.WARNING else ""
        stamp = time.strftime(TS_FMT, self.converter(record.created))
        return f"[anon {stamp}] {level}{name}: {record.getMessage()}"


class _StderrHandler(logging.StreamHandler):
    """A StreamHandler that resolves ``sys.stderr`` at EMIT time (like the stdlib's
    last-resort handler), so stream redirection after setup — pytest's capsys, a
    host re-wiring stderr — is always honored instead of writing to a stale object.

    Also counts WARNING+ records (:func:`warnings_seen`): a degraded run's summary
    line can then say "N warning(s)" instead of looking healthy while the warnings
    scrolled past minutes earlier."""

    def __init__(self):
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):  # StreamHandler.setStream assigns; late binding wins
        pass

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            global _warning_count
            _warning_count += 1
        super().emit(record)


_warning_count = 0


def warnings_seen() -> int:
    """WARNING+ records emitted through the anon console handler since the last
    :func:`reset_warnings` — console narration only, never part of any artifact."""
    return _warning_count


def reset_warnings() -> None:
    global _warning_count
    _warning_count = 0


def setup_console_logging(level: int | str | None = None) -> None:
    """Attach the timestamped console handler to the ``anon`` logger (idempotent).

    *level* defaults to the ``ANON_LOG`` environment variable (``debug`` /
    ``info`` / ``warning`` / ``error``), else INFO — INFO is deliberate: extractor
    INFO lines are sparse and diagnostic (e.g. the exact ``cmake`` command about to
    run), and hiding them is what made degraded runs unreadable.

    Configures only the ``anon`` logger; propagation to the root logger stays ON
    (records must keep reaching root-level observers such as pytest's caplog), which
    is harmless for the console: once this handler exists, Python's bare last-resort
    printing no longer kicks in.
    """
    if level is None:
        level = os.environ.get("ANON_LOG", "info")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger("anon")
    root.setLevel(level)
    for h in root.handlers:
        if getattr(h, "_anon_console", False):
            h.setLevel(level)
            return
    handler = _StderrHandler()
    handler.setFormatter(_ConsoleFormatter())
    handler.setLevel(level)
    handler._anon_console = True  # type: ignore[attr-defined] - idempotency marker
    root.addHandler(handler)
