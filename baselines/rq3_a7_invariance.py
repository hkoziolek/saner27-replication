"""Eval §7 RQ3 — axis **A7**: LLM naming enrichment is *structurally inert* (Δ=0).

A7 asks whether the §8 LLM enrichment — the only place a model touches a Anon run —
can move *structure* (the target→container partition or the relationship set). The plan's
answer is a hard invariant, shared with RQ4: **it cannot**. Enrichment has a names-and-
descriptions channel only; the referential-integrity guard (`enrich_llm`, §8.1) rejects any
relationship an LLM tries to invent, and the provider protocol exposes no grouping field at
all. So the structural Δ of enrichment is not "small" — it is *identically zero by
construction*, and this runner MEASURES that on the real curated facts.

WHY A FAKE PROVIDER (and why that is the STRONGER test)
-------------------------------------------------------
We drive enrichment with an **adversarial** in-process provider that renames *every* element
to a fresh, distinct string (`RenamingProvider`). No network, no key, deterministic — and a
harder test than a real model: if even a provider that changes every name it is handed cannot
perturb the partition or the edge set, a real model (which changes fewer) trivially cannot
either. The live path is exercised separately by `tests/test_enrich.py`
(`test_no_llm_passthrough_byte_identical`, `test_degrade_on_relationship_invention`); this
runner turns that invariant into a per-system, quotable **A7 = 0** number for §15.

WHAT IS "STRUCTURE" HERE
------------------------
* the **partition**: the `id -> container_id` map over targets (which targets share a box);
* the **relationship set**: every `(source, target, kind, weight, is_declared)` edge;
* the **target set**: no element added or dropped.
Names (`name`/`llm_name`/`container_name`), descriptions and responsibilities are the
*semantic* channel and are EXPECTED to move — the runner also confirms they actually did
(`names_changed > 0`), so a green Δ=0 can never be the vacuous "the provider did nothing."

Usage::

    python baselines/rq3_a7_invariance.py                 # all configured systems
    python baselines/rq3_a7_invariance.py eshop abseil    # a subset

Output: a table to stdout + ``out/<sys>/baseline/rq3-a7.json`` (aggregated by the §15
pipeline as axis A7: metric ``structural_delta`` = 0, plus ``names_changed``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from anon.jsonio import dump_json, load_json  # noqa: E402  (editable install)
from anon.paths import resolve_workspace       # noqa: E402
from anon.stages import enrich_llm              # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# The six §5 fidelity systems (same set the other RQ3 runners drive). opencv's representative
# curation is the module-granularity overlay, but A7 reads whatever curated-facts the system's
# own arch dir carries — the partition altitude is irrelevant to the invariant.
SYSTEMS = ["itk", "abseil", "opencv", "eshop", "orchardcore", "nopcommerce", "bash", "libxml2"]


class RenamingProvider:
    """Adversarial naming provider: renames EVERY element to a fresh, distinct string and emits
    a schema-valid record (so enrichment takes the happy path, not the degrade path). Makes NO
    network call. If enrichment still leaves the partition + edges identical, structure is inert
    to naming by construction."""

    model_id = "a7-renaming-adversary-1"

    def complete(self, system_prompt, prompt, element_facts):  # Provider protocol (§8.3)
        eid = element_facts["element_id"]
        tag = hashlib.sha1(eid.encode("utf-8")).hexdigest()[:8]
        return {
            "element_id": eid,
            "element_name": f"Renamed-{tag}",                       # deliberately != build name
            "description": f"Adversarially renamed element {tag}.",
            "responsibilities": [f"responsibility-{tag}"],
            "naming_confidence": "high",
            "evidence": [element_facts.get("name", eid)],
        }


def _partition(facts: dict) -> dict[str, str | None]:
    """id -> container_id (the structural grouping; names deliberately excluded)."""
    return {t["id"]: t.get("container_id") for t in facts.get("targets", [])}


def _edges(facts: dict) -> list[tuple]:
    """Canonical, sorted relationship tuples — the structural edge set."""
    return sorted(
        (r.get("source"), r.get("target"), r.get("kind"),
         r.get("weight"), r.get("is_declared_dependency"))
        for r in facts.get("relationships", []))


def _hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _names(facts: dict) -> dict[str, str]:
    """id -> the display name enrichment is allowed to move (llm_name if set, else name)."""
    return {t["id"]: (t.get("llm_name") or t.get("name") or "") for t in facts.get("targets", [])}


def run_system(system: str) -> dict | None:
    arch = REPO / "out" / system / "architecture"
    curated_path = arch / "generated" / "curated-facts.json"
    if not curated_path.exists():
        print(f"  {system}: no curated-facts.json — run `arch run` first; skipping", file=sys.stderr)
        return None
    curated = load_json(curated_path)

    # Enrich in an ISOLATED temp workspace so no committed artifact is touched. Point rules at the
    # system's real overlay (so redaction/config is faithful); enrichment writes only under arch_dir.
    rules_dir = REPO / "overlays" / system / "architecture" / "rules"
    if not rules_dir.exists():
        rules_dir = arch / "rules"
    tmp = Path(tempfile.mkdtemp(prefix=f"a7-{system}-"))
    try:
        ws = resolve_workspace(REPO / "out" / system, arch_dir=tmp / "arch", rules_dir=rules_dir)
        ws.curated_facts.parent.mkdir(parents=True, exist_ok=True)
        dump_json(curated, ws.curated_facts)
        enriched = enrich_llm.run(ws, "llm-propose", provider=RenamingProvider())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    part_before, part_after = _partition(curated), _partition(enriched)
    edges_before, edges_after = _edges(curated), _edges(enriched)

    # structural_delta: partition cells that moved + edge-set symmetric difference. MUST be 0.
    moved = sum(1 for k in set(part_before) | set(part_after)
                if part_before.get(k) != part_after.get(k))
    edge_delta = len(set(edges_before) ^ set(edges_after))
    targets_delta = len(set(part_before) ^ set(part_after))
    structural_delta = moved + edge_delta + targets_delta

    names_before, names_after = _names(curated), _names(enriched)
    names_changed = sum(1 for k in names_before if names_before.get(k) != names_after.get(k))

    res = {
        "system": system,
        "axis": "A7",
        "structural_delta": structural_delta,          # the headline: 0 == invariant holds
        "partition_moved": moved,
        "edge_delta": edge_delta,
        "targets_delta": targets_delta,
        "names_changed": names_changed,                 # > 0 proves the provider actually renamed
        "n_targets": len(part_before),
        "n_edges": len(edges_before),
        "partition_hash_before": _hash(sorted(part_before.items())),
        "partition_hash_after": _hash(sorted(part_after.items())),
        "edges_hash_before": _hash(edges_before),
        "edges_hash_after": _hash(edges_after),
        "invariant_holds": structural_delta == 0 and names_changed > 0,
    }
    (REPO / "out" / system / "baseline" / "rq3-a7.json").write_text(
        json.dumps(res, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("systems", nargs="*", default=list(SYSTEMS),
                    help="systems to check (default: all configured)")
    args = ap.parse_args(argv)

    results = []
    for s in (args.systems or list(SYSTEMS)):
        if s not in SYSTEMS:
            print(f"  {s}: not a configured fidelity system — skipping", file=sys.stderr)
            continue
        r = run_system(s)
        if r:
            results.append(r)
    if not results:
        print("no systems produced results — see notes above.", file=sys.stderr)
        return 1

    print("\n## A7 — LLM naming enrichment is structurally inert (Δ=0 by construction)")
    print(f"{'system':13} {'struct_delta':>12} {'names_changed':>14} {'targets':>8} "
          f"{'edges':>6}  {'invariant':>9}")
    ok = True
    for r in results:
        ok = ok and r["invariant_holds"]
        print(f"{r['system']:13} {r['structural_delta']:>12} {r['names_changed']:>14} "
              f"{r['n_targets']:>8} {r['n_edges']:>6}  "
              f"{'HOLDS' if r['invariant_holds'] else 'VIOLATED':>9}")
    print("\nstruct_delta = partition cells moved + edge-set symmetric diff + targets added/removed.")
    print("0 with names_changed>0 == enrichment moved every name but no structure (the A7/RQ4 invariant).")
    print("Per-system detail: out/<sys>/baseline/rq3-a7.json")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
