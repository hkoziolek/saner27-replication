"""One-shot fact-model MAJOR-version migration (plan §6.5b, §16#12).

A MAJOR ``schema_version`` bump (1.x -> 2.x) ships a new schema file *and* this
migration so committed artifacts/goldens are regenerated, not hand-patched. MINOR bumps
are additive-only and need no migration.

There is no 2.x line yet, so this is a documented stub: it asserts the input is on the
1.x line and passes it through. When a 2.x schema lands, implement the field-level
transform here and update ``schema/CHANGELOG.md`` (CI-enforced, §6.5d).
"""
from __future__ import annotations

from typing import Any


def migrate(facts: dict[str, Any], to_version: str = "1.0") -> dict[str, Any]:
    sv = str(facts.get("schema_version", ""))
    if not sv.startswith("1."):
        raise NotImplementedError(f"no migration path from schema_version {sv!r}")
    if not to_version.startswith("1."):
        raise NotImplementedError(
            "migration to a 2.x MAJOR line is not implemented; add the transform here when 2.x lands.")
    return facts  # 1.x -> 1.x is the identity (MINOR bumps are additive-only)


if __name__ == "__main__":  # pragma: no cover
    import argparse

    from ..jsonio import dump_json, load_json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--to", default="1.0")
    args = ap.parse_args()
    dump_json(migrate(load_json(args.input), args.to), args.output)
