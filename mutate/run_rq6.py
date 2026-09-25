"""Eval RQ6 entry point — seeded mutations (§8.1) + degradation correctness (§8.2).

Usage (from the repo root, venv active)::

    python -m mutate.run_rq6                       # both modes, all configured systems
    python -m mutate.run_rq6 eshop libxml2         # a subset
    python -m mutate.run_rq6 --mode mutation --ops remove_dep,add_target --instances 2
    python -m mutate.run_rq6 --mode degradation    # fact-level only (all 8 corpus systems)

Mutation systems need the gitignored ``fixtures/<system>`` clone; degradation runs off
the committed ``out/<system>/architecture/generated/curated-facts.json`` only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mutate import degradation, harness  # noqa: E402
from mutate.harness import OP_ORDER      # noqa: E402

MUTATION_DEFAULT = ["eshop", "nopcommerce", "orchardcore", "libxml2", "bash"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("systems", nargs="*",
                    help="systems (default: mutation subset / degradation corpus)")
    ap.add_argument("--mode", default="all", choices=["all", "mutation", "degradation"])
    ap.add_argument("--ops", default=",".join(OP_ORDER),
                    help="comma list of mutation operators")
    ap.add_argument("--instances", type=int, default=4,
                    help="mutation instances per operator per system (default 4)")
    ap.add_argument("--work", help="work-area root (default out/<sys>/mutation/_work)")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work copies after the run")
    ap.add_argument("--reuse-work", action="store_true",
                    help="reuse an existing work copy instead of a fresh one")
    args = ap.parse_args(argv)
    ops = [o.strip() for o in args.ops.split(",") if o.strip()]

    rc = 0
    if args.mode in ("all", "mutation"):
        systems = args.systems or MUTATION_DEFAULT
        for s in systems:
            if s not in harness.SYSTEMS:
                print(f"[mutate] {s}: not a configured mutation system", file=sys.stderr)
                continue
            try:
                harness.run_system(
                    s, ops=ops, instances=args.instances,
                    work_root=Path(args.work) if args.work else None,
                    fresh=not args.reuse_work, keep_work=args.keep_work)
            except Exception as exc:  # noqa: BLE001 — keep going, report at the end
                print(f"[mutate] {s}: FAILED — {exc}", file=sys.stderr)
                rc = 1
    if args.mode in ("all", "degradation"):
        systems = args.systems or sorted(degradation.CORPUS)
        for s in systems:
            if s not in degradation.CORPUS:
                print(f"[degrade] {s}: not in the fidelity corpus", file=sys.stderr)
                continue
            try:
                degradation.run_system(s)
            except Exception as exc:  # noqa: BLE001
                print(f"[degrade] {s}: FAILED — {exc}", file=sys.stderr)
                rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
