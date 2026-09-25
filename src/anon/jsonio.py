"""Deterministic JSON I/O.

Byte-identical output across runs/machines/OSes is a v1 gate (plan §1.1 metric 2,
§18.4). Every artifact this pipeline commits is written through :func:`dump_json` so
the determinism guarantee has exactly one implementation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def dumps_json(obj: Any) -> str:
    """Canonical JSON string: sorted keys, compact-but-readable, trailing newline, LF.

    - ``sort_keys=True`` makes key order independent of construction order.
    - ``ensure_ascii=False`` keeps unicode readable (and stable).
    - A trailing newline keeps POSIX tools / git diffs happy.
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def dump_json(obj: Any, path: str | os.PathLike[str]) -> None:
    """Write *obj* as canonical JSON to *path* with LF newlines (cross-platform stable)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # newline="" prevents the platform from translating \n -> \r\n on Windows, so the
    # file is byte-identical on Windows and Linux (plan §22.3).
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(dumps_json(obj))


def load_json(path: str | os.PathLike[str]) -> Any:
    # utf-8-sig transparently strips a leading UTF-8 BOM (a no-op for BOM-free files):
    # externally-supplied snapshots/baselines are frequently saved with a BOM on Windows,
    # which plain "utf-8" would choke on. Our own dump_json output stays BOM-free.
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)
