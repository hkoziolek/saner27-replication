"""``arch fetch`` — the manifest + overlay test-fixture harness (plan §19.4).

Anon is *one* installable tool pointed at *pristine, pinned, external* checkouts
(§19.4 — neither submodules nor copy-the-pipeline-in). This module realizes that:

  - ``manifest.yaml`` lists each fixture repo (``id`` / ``url`` / ``lang`` / ``build`` /
    ``license`` / pinned ``commit`` / ``configure`` command).
  - :func:`run` shallow-clones each repo at its pinned SHA into a **gitignored**
    ``fixtures/<id>/`` (the clone stays pristine — never committed, never modified).
  - The per-repo **overlay** (``overlays/<id>/architecture/``: ``rules/mapping-rules.yaml``,
    ``layering-rules.yaml`` and committed golden outputs) supplies config; outputs go to
    ``out/<id>/``. The :class:`Workspace` for a fixture run therefore has
    ``repo = fixtures/<id>``, ``arch_dir = out/<id>/architecture``,
    ``rules_dir = overlays/<id>/architecture/rules`` (plan §19.4, mirrored by paths.py's
    fixture note).

A fixture whose ``commit`` is still the ``TODO-pin-sha`` placeholder is **skipped with a
clear message** (reproducibility requires a real SHA, never a moving branch — §19.4).
Tests never hit the network: placeholders skip, and the fetch-logic test points at a fake
manifest in ``tmp_path``.

:func:`validate_manifest` checks every repo carries the required fields and flags
GPL/restrictive licenses (§19.6 — the harness commits *derived* golden outputs, so license
matters even though clones stay gitignored).

CLI wiring: ``cli.py`` currently stubs ``arch fetch`` (task #5). The orchestrator should
point that subcommand at :func:`run` / :func:`validate_manifest`; until then this module is
runnable as ``python -m anon.fetch`` (see the ``__main__`` shim).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .paths import Workspace

# A repo id is joined into filesystem paths (fixtures/<id>, out/<id>), so it must be a
# single safe path segment — no separators, no '..' traversal, no absolute/drive prefix.
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


def _is_safe_id(repo_id: str | None) -> bool:
    return bool(repo_id) and _SAFE_ID_RE.fullmatch(repo_id) is not None and repo_id not in (".", "..")

# A fixture whose commit is one of these is unpinned -> skip (never clone a moving branch).
PLACEHOLDER_COMMITS = {"TODO-pin-sha", "TODO", "", None}

# Required per-repo manifest fields (§19.4 schema).
REQUIRED_FIELDS = ("id", "url", "lang", "build", "license")

# SPDX ids / license names that are GPL or otherwise restrictive for *committed derived
# artifacts* (§19.6). The harness commits golden outputs, so a copyleft/restrictive fixture
# is flagged — confine it to non-redistributed local runs or prefer a permissive alternative.
RESTRICTIVE_LICENSES = {
    "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later",
    "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
    "LGPL-2.1", "LGPL-3.0",
    "nopCommerce Public License",   # the §19.6 named outlier
}

# Permissive SPDX ids we recognize as clearly fine (informational; anything else that is not
# in RESTRICTIVE_LICENSES is reported as "unknown — review" rather than silently trusted).
PERMISSIVE_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Unlicense"}


@dataclass(frozen=True)
class Fixture:
    """One resolved manifest entry (plan §19.4)."""

    id: str
    url: str
    lang: str
    build: str
    license: str
    commit: str | None = None
    configure: str | None = None
    role: str | None = None
    image: str | None = None

    @property
    def is_pinned(self) -> bool:
        return self.commit not in PLACEHOLDER_COMMITS


# --- manifest loading ----------------------------------------------------------

def load_manifest(manifest_path: str | Path) -> list[Fixture]:
    """Parse ``manifest.yaml`` into :class:`Fixture` records (no validation, no I/O)."""
    p = Path(manifest_path)
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    fixtures: list[Fixture] = []
    for repo in data.get("repos", []) or []:
        fixtures.append(Fixture(
            id=repo.get("id"),
            url=repo.get("url"),
            lang=repo.get("lang"),
            build=repo.get("build"),
            license=repo.get("license"),
            commit=repo.get("commit"),
            configure=repo.get("configure"),
            role=repo.get("role"),
            image=repo.get("image"),
        ))
    return fixtures


def validate_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Validate the manifest (§19.4 fields + §19.6 licensing). Returns a report dict.

    The report has ``ok`` (bool — no missing-field/duplicate errors), ``errors`` (hard
    problems that make a fixture unusable) and ``warnings`` (license flags, unpinned SHAs).
    A restrictive/unknown license is a *warning*, not an error: §19.6 allows it for
    non-redistributed local runs, so we surface it rather than fail the harness.
    """
    fixtures = load_manifest(manifest_path)
    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for fx in fixtures:
        ident = fx.id or "<missing-id>"
        missing = [f for f in REQUIRED_FIELDS if not getattr(fx, f, None)]
        if missing:
            errors.append(f"{ident}: missing required field(s): {', '.join(missing)}")
        if fx.id:
            if fx.id in seen_ids:
                errors.append(f"{fx.id}: duplicate repo id")
            seen_ids.add(fx.id)
            if not _is_safe_id(fx.id):
                errors.append(f"{fx.id!r}: invalid repo id — must be a single safe path "
                              "segment ([A-Za-z0-9._-], no separators or '..') (§19.4)")
        if not fx.is_pinned:
            warnings.append(f"{ident}: commit is a placeholder ({fx.commit!r}) — fetch will skip it (§19.4)")
        lic = fx.license
        if lic in RESTRICTIVE_LICENSES:
            warnings.append(f"{ident}: restrictive/copyleft license {lic!r} — confine to "
                            "non-redistributed local runs or prefer a permissive alternative (§19.6)")
        elif lic and lic not in PERMISSIVE_LICENSES:
            warnings.append(f"{ident}: unrecognized license {lic!r} — review before committing derived goldens (§19.6)")

    return {
        "ok": not errors,
        "count": len(fixtures),
        "errors": errors,
        "warnings": warnings,
    }


# --- fetch / clone -------------------------------------------------------------

def fixture_workspace(harness_root: str | Path, fx: Fixture) -> Workspace:
    """The §19.4 out-of-tree :class:`Workspace` for *fx*: pristine clone in, outputs/rules out.

    ``repo = fixtures/<id>``, ``arch_dir = out/<id>/architecture``,
    ``rules_dir = overlays/<id>/architecture/rules``. Building it does no I/O.
    """
    root = Path(harness_root).resolve()
    return Workspace(
        repo=root / "fixtures" / fx.id,
        arch_dir=root / "out" / fx.id / "architecture",
        rules_dir=root / "overlays" / fx.id / "architecture" / "rules",
    )


def _git_clone(url: str, commit: str, dest: Path, log) -> bool:
    """Shallow-clone *url* at the pinned *commit* into *dest*. Returns True on success.

    Uses ``git init`` + ``fetch --depth 1 <sha>`` so a *specific commit* is fetched
    shallowly (``clone --depth 1`` only works for a branch/tag tip, not an arbitrary SHA).
    Network/tooling failure is logged and returns False (the run records it; it does not
    raise — one bad fixture must not abort the harness)."""
    dest.mkdir(parents=True, exist_ok=True)
    steps = [
        ["git", "init", "-q", str(dest)],
        ["git", "-C", str(dest), "remote", "add", "origin", url],
        # The `--` terminates option parsing so a leading-dash commit value (e.g.
        # "--upload-pack=…") is treated as a refspec, never a git option.
        ["git", "-C", str(dest), "fetch", "--depth", "1", "origin", "--", commit],
        ["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"],
    ]
    for step in steps:
        try:
            res = subprocess.run(step, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"  ! git step failed ({' '.join(step[:3])}…): {exc}")
            # Remove the half-initialized tree so the next run retries instead of treating
            # this corpse as a pristine clone (the run() idempotency guard skips non-empty dirs).
            shutil.rmtree(dest, ignore_errors=True)
            return False
        if res.returncode != 0:
            log(f"  ! git step failed ({' '.join(step[:3])}…): {res.stderr.strip()[:200]}")
            shutil.rmtree(dest, ignore_errors=True)
            return False
    return True


def run(manifest_path: str | Path, ids: list[str] | None = None, *,
        harness_root: str | Path | None = None, log=None) -> dict[str, Any]:
    """Fetch fixtures listed in *manifest_path* (plan §19.4).

    *ids* — optional subset of repo ids to fetch (default: all). *harness_root* — where
    ``fixtures/`` / ``out/`` / ``overlays/`` live (default: the manifest's parent dir).
    *log* — sink for human-readable progress (default: print to stderr).

    For each selected fixture: skip if the commit is a ``TODO-pin-sha`` placeholder (clear
    message), skip if the clone already exists (idempotent — pristine, don't re-clone),
    otherwise shallow-clone at the pinned SHA into ``fixtures/<id>/``. **Never modifies an
    existing clone.** Returns a report dict with per-fixture status: ``fetched`` /
    ``skipped-placeholder`` / ``skipped-existing`` / ``failed``.

    Tests must not hit the network: a manifest of only-placeholder entries exercises the
    skip path with zero clones.
    """
    if log is None:
        def log(msg: str) -> None:
            print(msg, file=sys.stderr)

    root = Path(harness_root).resolve() if harness_root else Path(manifest_path).resolve().parent
    fixtures = load_manifest(manifest_path)
    want = set(ids) if ids else None

    status: dict[str, str] = {}
    for fx in fixtures:
        if want is not None and fx.id not in want:
            continue
        if not _is_safe_id(fx.id):
            log(f"[fetch] {fx.id!r}: SKIP — unsafe repo id (path-traversal guard, §19.4).")
            status[str(fx.id)] = "failed"
            continue
        dest = root / "fixtures" / fx.id
        if not fx.is_pinned:
            log(f"[fetch] {fx.id}: SKIP — commit is a placeholder ({fx.commit!r}); "
                "pin a real SHA in manifest.yaml before fetching (§19.4).")
            status[fx.id] = "skipped-placeholder"
            continue
        if dest.exists() and any(dest.iterdir()):
            log(f"[fetch] {fx.id}: SKIP — already cloned at {dest} (pristine; not re-cloning).")
            status[fx.id] = "skipped-existing"
            continue
        log(f"[fetch] {fx.id}: cloning {fx.url} @ {fx.commit[:12]} -> {dest}")
        if _git_clone(fx.url, fx.commit, dest, log):
            status[fx.id] = "fetched"
        else:
            log(f"[fetch] {fx.id}: FAILED to clone (see git error above).")
            status[fx.id] = "failed"

    fetched = sum(1 for s in status.values() if s == "fetched")
    skipped = sum(1 for s in status.values() if s.startswith("skipped"))
    failed = sum(1 for s in status.values() if s == "failed")
    log(f"[fetch] done: {fetched} fetched, {skipped} skipped, {failed} failed "
        f"(of {len(status)} selected).")
    return {"root": str(root), "status": status,
            "fetched": fetched, "skipped": skipped, "failed": failed}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="anon.fetch", description=__doc__)
    ap.add_argument("--manifest", default="manifest.yaml", help="manifest path (default ./manifest.yaml)")
    ap.add_argument("--id", action="append", dest="ids", help="fetch only this repo id (repeatable)")
    ap.add_argument("--harness-root", help="where fixtures/ out/ overlays/ live (default: manifest dir)")
    ap.add_argument("--validate-only", action="store_true", help="validate the manifest and exit (§19.6)")
    args = ap.parse_args(argv)

    report = validate_manifest(args.manifest)
    for w in report["warnings"]:
        print(f"[manifest] WARN {w}", file=sys.stderr)
    for e in report["errors"]:
        print(f"[manifest] ERROR {e}", file=sys.stderr)
    if not report["ok"]:
        print("[manifest] invalid — fix errors above before fetching.", file=sys.stderr)
        return 1
    if args.validate_only:
        print(f"[manifest] OK — {report['count']} fixtures, {len(report['warnings'])} warning(s).")
        return 0

    run(args.manifest, ids=args.ids, harness_root=args.harness_root)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
