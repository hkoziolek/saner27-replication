"""Eval §13 / §7-A2 — the no-curation floor: build-graph-only fidelity, zero curation.

Separates the deterministic pipeline's structural contribution from the (LLM- or
human-) authored curation grouping (the curation-authorship confound,
``plans/notes/2026-06-14-llm-in-curation-and-no-curation-floor.md``). The floor clusters
with the build graph's nodes but NO container grouping:

  * C++ file granularity: each file -> its owning build target (the file-deps sidecar
    ``files`` map). Pure attribution, no judgement.
  * C# target granularity: each project -> its own singleton cluster. At project
    granularity this is identical to the ``dir`` baseline, so just read that.

Scored with the same ARCADE metrics + common-entity projection as the curated runs::

    python baselines/no_curation_floor.py itk abseil opencv
        [--reference references/<sys>/reference.rsf]   # default per system

Prints, per system, the floor (MoJoFM/a2a, #targets) so it can sit beside the curated
number in the RQ1 fidelity table. Compare to ``out/<sys>/baseline/comparison-*.json``.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from run_arcade import a2a, mojofm, read_contains, write_contains  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def cpp_floor(system: str, reference: Path) -> dict | None:
    sidecar = (REPO / "out" / system / "architecture" / ".cache" / "fragments"
               / "file-deps-doxygen-xml.json")
    if not sidecar.exists():
        print(f"  {system}: no file-deps sidecar — run the C++ file-granularity export first",
              file=sys.stderr)
        return None
    files = json.loads(sidecar.read_text(encoding="utf-8"))["files"]
    by_target = {f: t.split(":")[-1] for f, t in files.items() if t}  # drop unattributed
    ref = read_contains(reference)
    shared = set(ref) & set(by_target)
    if not shared:
        return None
    tmp = REPO / "out" / system / "_floor"
    tmp.mkdir(parents=True, exist_ok=True)
    write_contains({m: c for m, c in ref.items() if m in shared}, tmp / "ref.rsf")
    write_contains({m: c for m, c in by_target.items() if m in shared}, tmp / "floor.rsf")
    out = {
        "system": system, "granularity": "file",
        "floor_kind": "each file -> owning build target (no curation)",
        "mojofm": mojofm(tmp / "floor.rsf", tmp / "ref.rsf"),
        "a2a": round(a2a(tmp / "floor.rsf", tmp / "ref.rsf"), 2),
        "targets": len(set(c for m, c in by_target.items() if m in shared)),
        "shared_entities": len(shared),
    }
    shutil.rmtree(tmp)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("systems", nargs="+", help="C++ file-granularity systems (itk abseil opencv)")
    ap.add_argument("--reference", help="override reference RSF (default references/<sys>/reference.rsf)")
    args = ap.parse_args(argv)
    for s in args.systems:
        ref = Path(args.reference) if args.reference else REPO / "references" / s / "reference.rsf"
        r = cpp_floor(s, ref)
        if r:
            print(f"{s:8} no-curation floor: MoJoFM={r['mojofm']} a2a={r['a2a']} "
                  f"targets={r['targets']} shared={r['shared_entities']}")
            (REPO / "out" / s / "baseline" / "no-curation-floor.json").write_text(
                json.dumps(r, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
