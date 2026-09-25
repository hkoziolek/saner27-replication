"""Stage 2 — fact-model validation (plan §6.5, §10).

Gates:
  1. **JSON Schema** — the fact model must conform to ``schema/fact-model.schema.json``.
     Always run by :func:`validate`.
  2. **Referential integrity** — every relationship ``source``/``target`` must resolve
     to an existing element id (incl. component/code children, §6.3), and
     ``No relationship without evidence`` (plan §5). Always run by :func:`validate`.
  3. **Schema-changelog gate (§6.5d)** — :func:`check_schema_changelog` verifies that
     ``schema/CHANGELOG.md`` carries an entry for the current ``schema_version`` (and,
     for a MAJOR bump, that a ``migrate_facts.py`` exists).

Gates 1–2 run on every :func:`validate` call. Gate 3 is **opt-in**: it runs only when a
caller passes ``check_changelog=True`` to :func:`validate`. The CLI ``validate``/``run``/
``extract`` paths request it (via :func:`run`, which calls ``validate(..., check_changelog=
True)``), so it is active for every regeneration driven through the ``arch`` CLI; the bare
``validate(facts)`` helper used by unit tests deliberately leaves it off.
:func:`check_schema_changelog` itself *returns* problems rather than raising, so a
standalone CI step can fold its result into other findings.

A malformed fact model fails the build before anything reaches Structurizr.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jsonschema

from ..jsonio import load_json
from ..model import all_element_ids
from ..paths import Workspace


class ValidationError(Exception):
    """Raised when the fact model is schema-invalid or referentially broken."""


def find_schema(name: str = "fact-model.schema.json") -> Path:
    """Locate a bundled schema file by walking up from this package to the repo root."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "schema" / name
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"schema/{name} not found above {here}")


def _repo_root_from_pkg() -> Path | None:
    """Best-effort repo root: the first ancestor of this package containing ``schema/``."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "schema").is_dir():
            return parent
    return None


# A CHANGELOG section header for a version, e.g. ``## 1.0 — 2026-06-04 — initial draft``.
_CHANGELOG_HEADER = re.compile(r"^#{1,6}\s+(?:v)?(?P<ver>\d+\.\d+(?:\.\d+)?)\b", re.MULTILINE)


def check_schema_changelog(repo_root: str | Path | None = None,
                           schema_version: str | None = None) -> list[str]:
    """Verify the §6.5d CI gate: ``schema/CHANGELOG.md`` has an entry for *schema_version*.

    Returns a list of problems (empty == OK) — by contract this never raises, so a caller
    can fold the result into other validation findings. The §6.5d rule:

      - ``schema_version`` must not change without a ``schema/CHANGELOG.md`` entry, and
      - a MAJOR bump (``2.x`` etc.) additionally requires a ``migrate_facts.py`` script.

    The intended use is ``check_schema_changelog(repo_root, facts["schema_version"])``.
    When *schema_version* is omitted the helper falls back to the package's emitted
    ``SCHEMA_VERSION`` constant (the single source of truth for the version stamped into
    every fact model), so a standalone CI step can call it with just a repo root.
    """
    problems: list[str] = []
    root = Path(repo_root).resolve() if repo_root else _repo_root_from_pkg()
    if root is None:
        return ["schema-changelog: could not locate repo root (no schema/ dir found)"]

    if schema_version is None:
        # Fall back to the package's emitted SCHEMA_VERSION — the same value stamped into
        # every fact model. (The schema file itself carries no ``version`` key, so reading
        # one would always yield None and make this gate fail spuriously.)
        from .. import SCHEMA_VERSION
        schema_version = SCHEMA_VERSION

    changelog = root / "schema" / "CHANGELOG.md"
    if not changelog.exists():
        return [f"schema-changelog: {changelog} is missing (§6.5d requires a changelog)"]

    text = changelog.read_text(encoding="utf-8", errors="replace")
    versions = {m.group("ver") for m in _CHANGELOG_HEADER.finditer(text)}
    if schema_version not in versions:
        problems.append(
            f"schema-changelog: no entry for schema_version {schema_version!r} in "
            f"{changelog} (§6.5d — every schema_version change needs a CHANGELOG.md entry)")

    # MAJOR bump (not on the 1.x line) additionally needs a migration script (§6.5b/d).
    major = schema_version.split(".", 1)[0]
    if major.isdigit() and int(major) >= 2:
        migrate = root / "src" / "anon" / "stages" / "migrate_facts.py"
        if not migrate.exists():
            problems.append(
                f"schema-changelog: MAJOR schema_version {schema_version!r} requires a "
                f"migrate_facts.py migration script (§6.5b/d); {migrate} is missing")
    return problems


def check_referential_integrity(facts: dict[str, Any]) -> list[str]:
    """Return a list of integrity errors (empty == OK)."""
    errors: list[str] = []
    ids = all_element_ids(facts)
    for r in facts.get("relationships", []):
        if r.get("source") not in ids:
            errors.append(f"relationship {r.get('id')}: source '{r.get('source')}' is not a known element")
        if r.get("target") not in ids:
            errors.append(f"relationship {r.get('id')}: target '{r.get('target')}' is not a known element")
        if not r.get("evidence"):
            errors.append(f"relationship {r.get('id')}: no evidence (plan §5: no relationship without evidence)")

    # Deployment edges (schema 1.1 §4.4): each source/target must resolve to a known
    # deployment node id OR a known build-target/element id (a 'packages' edge points
    # image -> build-target). The JSON-Schema gate only validates edge SHAPE, not that
    # the endpoints exist — so a dangling deployment edge would otherwise pass silently.
    deployment = facts.get("deployment") or {}
    node_ids = {n.get("id") for n in deployment.get("nodes", [])}
    known = ids | node_ids
    for e in deployment.get("edges", []):
        kind = e.get("kind")
        if e.get("source") not in known:
            errors.append(f"deployment edge ({kind}): source '{e.get('source')}' is not a known node or element")
        if e.get("target") not in known:
            errors.append(f"deployment edge ({kind}): target '{e.get('target')}' is not a known node or element")
    return errors


def validate(facts: dict[str, Any], schema_path: Path | None = None, *,
             check_changelog: bool = False, repo_root: str | Path | None = None) -> None:
    """Validate *facts*; raise :class:`ValidationError` on any problem.

    By default only the JSON-Schema and referential-integrity gates run, so the many
    unit-test callers of ``validate(facts)`` are unaffected. Pass ``check_changelog=True``
    (the CLI ``validate``/``run``/``extract`` paths do) to additionally run the §6.5d
    schema-changelog gate after the first two pass: :func:`check_schema_changelog` must find
    a ``schema/CHANGELOG.md`` entry for ``facts["schema_version"]`` (and a migration script
    for a MAJOR bump), else this raises. *repo_root* is forwarded to that gate and defaults
    to the package repo root (where the tool-owned ``schema/`` lives).
    """
    schema = load_json(schema_path or find_schema())
    try:
        jsonschema.validate(instance=facts, schema=schema)
    except jsonschema.ValidationError as exc:  # pragma: no cover - message passthrough
        raise ValidationError(f"schema validation failed: {exc.message} (at {list(exc.absolute_path)})") from exc
    errors = check_referential_integrity(facts)
    if errors:
        raise ValidationError("referential integrity failed:\n  - " + "\n  - ".join(errors))
    if check_changelog:
        problems = check_schema_changelog(repo_root, facts.get("schema_version"))
        if problems:
            raise ValidationError("schema-changelog gate failed:\n  - " + "\n  - ".join(problems))


def run(ws: Workspace, which: str = "extracted") -> None:
    """Validate a written facts artifact (``extracted`` | ``curated``).

    This is the artifact-validating entry point behind ``arch validate``/``run``/``extract``,
    so it enables the §6.5d schema-changelog gate (``check_changelog=True``).
    """
    path = ws.extracted_facts if which == "extracted" else ws.curated_facts
    validate(load_json(path), check_changelog=True)


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import sys

    from ..paths import resolve_workspace

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    ap.add_argument("--which", default="extracted", choices=["extracted", "curated"])
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir)
    try:
        run(w, args.which)
    except ValidationError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        sys.exit(1)
    print("OK")
