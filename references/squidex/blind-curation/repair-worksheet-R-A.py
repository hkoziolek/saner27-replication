#!/usr/bin/env python3
"""Repair rater R-A's Squidex worksheet from its Excel-damaged export (RQ9, eval §10a).

`worksheet-R-A.raw.csv` is the file exactly as received (2026-08-14 session, downloaded
2026-08-30). It went through an Excel round-trip that (a) quoted each original line into the
first column ("entity,cluster,notes" became one cell), (b) put the rater's answer in the
second column prefixed with "[Parent container: <X>] ", (c) double-encoded the UTF-8 header,
and (d) physically spliced two rows into each other (Squidex.Infrastructure and
Squidex.Domain.Users), losing one of their cluster cells.

This script rewrites it into the pre-registered worksheet format DETERMINISTICALLY:
  * entity   = first cell with the stray ",," stripped;
  * cluster  = the rater's label with the "[Parent container: X]" prefix removed;
  * notes    = "parent: X" (the rater's hierarchy signal is kept as data, not discarded);
  * the two spliced rows are filled from the rater's own answer to the facilitator's question
    (R-A, 2026-08-30): Squidex.Domain.Users -> Domain, Squidex.Infrastructure -> Infrastructure;
  * the wall-clock line becomes the standard `# wall-clock:` comment.
The entity set is asserted equal to `worksheet-target.csv` (the list the rater received).
No cluster label is changed. Re-run: `python repair-worksheet-R-A.py` (from this directory).
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "worksheet-R-A.raw.csv"
OUT = HERE / "worksheet-R-A.csv"
TARGET = HERE / "worksheet-target.csv"

PREFIX = re.compile(r"^\s*\[Parent container:\s*(?P<parent>[^\]]+)\]\s*(?P<cluster>.+?)\s*$")
CONFIRMED = {  # the spliced rows — R-A's answer to the facilitator, 2026-08-30
    "csharp:csproj:backend/src/Squidex.Domain.Users/Squidex.Domain.Users.csproj":
        ("Domain", "parent: backend; row spliced in the raw export — cluster confirmed by "
                   "R-A on 2026-08-30 (the orphaned '[Parent container: backend] Domain' "
                   "cell in the raw file is this row's)"),
    "csharp:csproj:backend/src/Squidex.Infrastructure/Squidex.Infrastructure.csproj":
        ("Infrastructure", "row spliced in the raw export (cluster cell lost) — cluster "
                           "confirmed by R-A on 2026-08-30; parent not recoverable"),
}
HEADER = [
    "# RQ9 rater worksheet (eval §10a) — assign every entity a cluster name; use the",
    "# project's own documented component names where they exist. Write EXCLUDE to mark an",
    "# entity out-of-scope (tests, samples, dead code) — record why in the notes column.",
    "# Blinding: do NOT consult the tool's output, any reference.rsf, or another rater.",
    "# REPAIRED 2026-08-30 by the facilitator from worksheet-R-A.raw.csv (Excel-damaged export)",
    "# with repair-worksheet-R-A.py — labels unchanged; '[Parent container: X]' moved to notes;",
    "# two spliced rows filled from R-A's own confirmation (see notes). Frozen thereafter.",
]


def main() -> int:
    targets = [r["entity"] for r in csv.DictReader(
        ln for ln in TARGET.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#"))]
    text = RAW.read_bytes().decode("utf-8", errors="replace")
    rows: dict[str, tuple[str, str]] = {}
    wall = None
    for rec in csv.reader(io.StringIO(text)):
        if not rec or not any(c.strip() for c in rec):
            continue
        first = rec[0].strip()
        if first.startswith("#") or first.startswith("entity,"):
            continue
        if first.lower().startswith("wall-clock"):
            wall = first
            continue
        if not first.startswith("csharp:csproj:"):
            continue                      # the orphaned cell of a spliced row
        entity = first.rstrip(",").strip()
        if entity in CONFIRMED or entity not in targets:
            continue                      # spliced rows handled below; garbage skipped
        label = rec[1] if len(rec) > 1 else ""
        m = PREFIX.match(label)
        if m:
            rows[entity] = (m.group("cluster"), f"parent: {m.group('parent').strip()}")
        else:
            rows[entity] = (label.strip(), "")
    for entity, (cluster, note) in CONFIRMED.items():
        rows[entity] = (cluster, note)
    missing = [t for t in targets if t not in rows]
    extra = [e for e in rows if e not in targets]
    assert not missing and not extra, (missing, extra)
    assert wall, "wall-clock line not found in the raw file"
    wall = wall.replace("–", "-").replace("—", "-")
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    for ln in HEADER:
        out.write(ln + "\n")
    w.writerow(["entity", "cluster", "notes"])
    for entity in targets:                # the rater's original row order
        cluster, note = rows[entity]
        w.writerow([entity, cluster, note])
    out.write(f"# {wall}\n")
    OUT.write_text(out.getvalue(), encoding="utf-8", newline="\n")
    print(f"repaired {len(targets)} entities -> {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
