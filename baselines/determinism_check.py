"""Eval RQ5 — double-run determinism check over two independent pipeline runs.

Compares two arch workspaces produced by two ``arch run --no-llm`` invocations of the same
repo (second run into a FRESH ``--arch-dir``) and writes the verdict to
``out/<system>/baseline/determinism.json`` — the whitelisted source of the Tier-P
``identical_rate`` row (``analysis/anonymize_results.py``)::

    python baselines/determinism_check.py <arch-dir-1> <arch-dir-2> --system <id>
    python baselines/determinism_check.py <arch-dir-1> <arch-dir-2> --out <path>.json

Two comparisons, both must pass:

  * **fact models** — ``generated/{extracted,curated}-facts.json`` compared after stripping
    the volatile provenance fields (``generated_at``/``commit``/``interop_resolved`` — the
    same set ``anon.model._VOLATILE_PROVENANCE`` excludes from ``content_hash``; the
    authoritative implementation is used when anon is importable, with a stdlib
    sorted-JSON fallback that is equality-equivalent for on-disk canonical facts);
  * **DSL fragments** — every machine-owned ``generated/*.dsl`` byte-identical, same file set.

Exit 0 when identical, 1 when not (the JSON is written either way). Output is deterministic:
sorted keys, LF, no timestamps — record the run date in your local log, never in the artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VOLATILE_PROVENANCE = {"generated_at", "commit", "interop_resolved"}
FACT_FILES = ["extracted-facts.json", "curated-facts.json"]


def _facts_hash(path: Path) -> str | None:
    """Provenance-stripped content hash; ``anon.model.content_hash`` when available."""
    try:
        facts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        from anon.model import content_hash  # authoritative (canonicalized) path
        return content_hash(facts)
    except ImportError:
        prov = dict(facts.get("provenance", {}))
        for k in VOLATILE_PROVENANCE:
            prov.pop(k, None)
        stripped = {**facts, "provenance": prov}
        blob = json.dumps(stripped, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compare(arch_a: Path, arch_b: Path) -> dict:
    facts: dict[str, dict] = {}
    for name in FACT_FILES:
        ha = _facts_hash(arch_a / "generated" / name)
        hb = _facts_hash(arch_b / "generated" / name)
        facts[name] = {"present": ha is not None and hb is not None,
                       "equal": ha is not None and ha == hb,
                       "hash_a": ha, "hash_b": hb}

    dsl_a = {p.name: p for p in sorted((arch_a / "generated").glob("*.dsl"))}
    dsl_b = {p.name: p for p in sorted((arch_b / "generated").glob("*.dsl"))}
    mismatched = sorted(set(dsl_a) ^ set(dsl_b))
    mismatched += sorted(n for n in set(dsl_a) & set(dsl_b)
                         if dsl_a[n].read_bytes() != dsl_b[n].read_bytes())
    dsl = {"files_compared": len(set(dsl_a) & set(dsl_b)),
           "equal": not mismatched and bool(dsl_a),
           "mismatched": mismatched}

    identical = all(f["equal"] for f in facts.values()) and dsl["equal"]
    return {"runs": 2, "identical": identical,
            "identical_rate": 100.0 if identical else 0.0,
            "facts": facts, "dsl": dsl}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arch_dirs", nargs=2, metavar="arch-dir",
                    help="the two independently produced arch workspaces")
    ap.add_argument("--system", help="fixture id: write to out/<system>/baseline/determinism.json")
    ap.add_argument("--out", help="explicit output path (overrides --system)")
    args = ap.parse_args(argv)
    if not args.out and not args.system:
        ap.error("one of --system or --out is required")
    out_path = Path(args.out) if args.out else \
        REPO_ROOT / "out" / args.system / "baseline" / "determinism.json"

    report = compare(Path(args.arch_dirs[0]), Path(args.arch_dirs[1]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(report, indent=2, sort_keys=True) + "\n")

    verdict = "IDENTICAL" if report["identical"] else "DIFFER"
    detail = "; ".join([f"{n} {'==' if f['equal'] else '!='}" for n, f in report["facts"].items()]
                       + [f"dsl {report['dsl']['files_compared']} files"
                          + ("" if report["dsl"]["equal"] else
                             f", mismatched: {', '.join(report['dsl']['mismatched'])}")])
    print(f"[determinism] {verdict} — {detail}\n  -> {out_path}")
    return 0 if report["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
