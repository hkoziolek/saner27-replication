"""Opt-in, append-only edit trace — the RQ9 measurement instrument (eval §10a.2).

The blind-curation experiment requires "log the full edit trace + wall-clock" for the
curator's session. Set ``ANON_EDIT_TRACE`` to a file path before starting
``arch serve``; every reviewed rules write and every applied curate-edit op batch then
appends one JSON line (UTC timestamp + event payload). With the variable unset (the
default, and the only mode CI ever sees) every call is a no-op.

Two deliberate properties:

* **The trace lives OUTSIDE ``generated/``** (the RQ9 protocol points it at
  ``out/<system>/blind-curation/edit-trace.jsonl``). It is a raw measurement log, not a
  model artifact, so the wall-clock timestamps it must carry do not violate the
  "no ``generated/`` artifact may carry a timestamp" determinism gate.
* **Recording can never break an endpoint**: any I/O or serialization failure is logged
  as a warning on the ``anon.edit_trace`` logger and swallowed — losing a trace
  line is acceptable, failing a curator's write is not.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENV_VAR = "ANON_EDIT_TRACE"

log = logging.getLogger("anon.edit_trace")


def trace_path() -> Path | None:
    """The configured trace file, or None when tracing is off (the default)."""
    raw = os.environ.get(ENV_VAR, "").strip()
    return Path(raw) if raw else None


def enabled() -> bool:
    return trace_path() is not None


def record(event: str, **fields: Any) -> None:
    """Append one ``{"ts": <UTC ISO>, "event": <event>, **fields}`` JSON line.

    No-op when ``ANON_EDIT_TRACE`` is unset; never raises (see module docstring).
    """
    path = trace_path()
    if path is None:
        return
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "event": event, **fields}
    try:
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="") as fh:  # LF always
            fh.write(line + "\n")
    except OSError as exc:
        log.warning("edit-trace write to %s failed (%s) — trace line dropped", path, exc)


def read_trace(path: Path) -> list[dict[str, Any]]:
    """Parse a trace file back into its entries (skips blank/corrupt lines, visibly)."""
    entries: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("edit-trace %s line %d is not valid JSON — skipped", path, i)
    return entries
