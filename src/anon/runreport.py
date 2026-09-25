"""Pipeline observability — ``run-report.json`` (plan §10.2).

Captures per-stage wall-clock and the model-trust dashboard (§10.2, added v0.5) that
makes the §11.6 stopping rules checkable (a gate reads ``trust.coverage_pct``).

Stage-2 GUI additions (GUI plan §6/§7.5): an optional *sink* receives live
``stage_start`` / ``stage_finish`` events — plus ``substep_start`` / ``substep_finish``
for units inside a stage, e.g. one extractor — (the `GET /jobs/{id}/events` SSE feed), and an
optional *cancel_event* makes cancellation cooperative — checked at every stage
boundary, so a cancelled run never leaves a stage half-written (stages write their
artifacts atomically).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .jsonio import dump_json
from .paths import Workspace


class JobCancelled(Exception):
    """Raised at a stage boundary when the run's ``cancel_event`` is set (GUI §7.5)."""


@dataclass
class RunReport:
    stages: dict[str, float] = field(default_factory=dict)   # stage -> seconds
    substeps: dict[str, dict[str, float]] = field(default_factory=dict)  # stage -> {substep -> s}
    trust: dict[str, Any] = field(default_factory=dict)
    cache: dict[str, int] = field(default_factory=dict)      # hits/misses
    notes: list[str] = field(default_factory=list)
    # GUI §7.5 hooks — None for CLI runs, injected by the serve job manager:
    # sink(event_dict) is best-effort (an observer crash never breaks the pipeline);
    # cancel_event (threading.Event-like) is honored at the NEXT stage boundary.
    sink: Any = None
    cancel_event: Any = None
    _monotonic: Any = time.monotonic

    def _emit(self, event: dict[str, Any]) -> None:
        if self.sink is not None:
            try:
                self.sink(event)
            except Exception:  # noqa: BLE001 - observability must never break the run
                pass

    @contextmanager
    def stage(self, name: str):
        """Time a stage: ``with report.stage("extract"): ...``."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobCancelled(f"cancelled before stage '{name}'")
        self._emit({"event": "stage_start", "stage": name})
        start = self._monotonic()
        try:
            yield
        finally:
            seconds = round(self._monotonic() - start, 3)
            self.stages[name] = seconds
            self._emit({"event": "stage_finish", "stage": name, "seconds": seconds})

    @contextmanager
    def substep(self, stage: str, name: str):
        """Time one unit of work *inside* a stage (e.g. one extractor of ``extract``).

        Emits ``substep_start`` / ``substep_finish`` events to the sink — the SSE feed
        and the CLI progress narration — so a 5-minute extract phase is attributable to
        the extractor that spent the time, and records the seconds under
        ``substeps[stage][name]`` in run-report.json (§10.2). Cancellation is honored
        here too, so a GUI cancel lands at the next extractor boundary, not minutes
        later at the stage boundary.
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobCancelled(f"cancelled before substep '{stage}:{name}'")
        self._emit({"event": "substep_start", "stage": stage, "substep": name})
        start = self._monotonic()
        try:
            yield
        finally:
            seconds = round(self._monotonic() - start, 3)
            self.substeps.setdefault(stage, {})[name] = seconds
            self._emit({"event": "substep_finish", "stage": stage, "substep": name,
                        "seconds": seconds})

    def set_trust(self, **kwargs: Any) -> None:
        self.trust.update(kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stages": self.stages,
            "substeps": self.substeps,
            "cache": self.cache,
            "trust": self.trust,
            "notes": self.notes,
        }

    def write(self, ws: Workspace) -> None:
        dump_json(self.to_dict(), ws.run_report)

    def summary_line(self) -> str:
        """One-line CI summary, e.g. plan §10.2's example."""
        parts = []
        total = round(sum(self.stages.values()), 1)
        parts.append(f"total {total}s")
        cov = self.trust.get("coverage_pct", {})
        if cov:
            # Round (not truncate): coverage is a rounded float fraction, and float
            # imprecision makes e.g. round(0.29, 2) * 100 == 28.9999…, which int() would
            # wrongly floor to 28%. round() reports 29% while leaving 1.0 -> 100%.
            parts.append("coverage " + ", ".join(f"{k} {round(v * 100)}%" for k, v in cov.items()))
        if self.cache:
            parts.append(f"cache {self.cache.get('hits', 0)}/{sum(self.cache.values())}")
        over = self.trust.get("over_budget_views") or []
        if over:
            parts.append(f"{len(over)} views over budget")
        dropped = self.trust.get("edges_dropped")
        if dropped:
            parts.append(f"{dropped} edges dropped")
        return " | ".join(parts)
