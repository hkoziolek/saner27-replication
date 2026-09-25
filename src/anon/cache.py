"""Incremental-extraction cache primitive (§10.1).

§10.1 ("incremental extraction & runtime budget") makes "caching everywhere"
(§18.4) concrete: a re-run should reuse the fact *fragments* of extractors whose
inputs did not change, re-doing only the work whose inputs did. This module is the
**pure, file-based, deterministic** caching primitive that path is built on. It is
an OPT-IN accelerator — nothing here changes default (non-incremental) behavior or
any committed/golden output; the orchestrator (``cli.py``) wires it into extractors
behind ``--incremental``.

The cache lives in the gitignored ``architecture/.cache/`` (§12). CI restores the
same logical store from Azure DevOps ``Cache@2``; the CLI restores it from
``.cache/`` — the **cache-key derivation is identical on both paths** (§10.1).

Determinism rules (a hard CI gate, §18.4):
  * Keys hash repo-*relative* POSIX paths + file content — never mtime, never an
    absolute path — so a key is identical across machines and OSes.
  * Everything is sorted before hashing.
  * Cache files are written through :func:`jsonio.dump_json` (sorted keys, LF).
  * No timestamps land in any cache content.

The store is robust to being absent or corrupt: a missing/garbage entry is treated
as a clean MISS and never raises (graceful degradation, §18.4) — the worst case is
a full (correct) re-extract, exactly the non-incremental behavior.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

from .jsonio import dump_json, load_json
from .paths import Workspace

# Mirrors §8.3's enumerated-key spirit: a stable separator so concatenating parts
# is unambiguous (``"a" + "bc"`` must not collide with ``"ab" + "c"``).
_SEP = "\x00"


def _rel_posix(path: Path, root: Path) -> str:
    """Repo-relative POSIX path string (falls back to the bare name if outside root).

    Hashing the *relative* POSIX path (not an absolute, OS-flavored one) is what makes
    a key identical on Windows and Linux (§10.1 / §18.4 determinism).
    """
    p = Path(path)
    try:
        rel = p.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        rel = Path(p.name)
    return rel.as_posix()


def content_hash_files(paths: Iterable[Path], root: Path | None = None) -> str:
    """Stable sha256 over the SORTED ``(rel-path, bytes)`` of the *existing* files.

    Missing files are ignored (an extractor's input set may legitimately not all
    exist yet). When *root* is given, paths are recorded repo-relative so the hash is
    location-independent; otherwise the bare file name is used. Order-independent:
    the inputs are sorted before hashing, so glob/iteration order cannot perturb it.
    """
    root = Path(root).resolve() if root is not None else None
    entries: list[tuple[str, bytes]] = []
    for p in paths:
        fp = Path(p)
        if not fp.is_file():
            continue  # ignore missing (§10.1: hash only the inputs that exist)
        key = _rel_posix(fp, root) if root is not None else fp.name
        try:
            entries.append((key, fp.read_bytes()))
        except OSError:
            continue  # unreadable -> treat as absent rather than crash (§18.4)
    h = hashlib.sha256()
    for rel, data in sorted(entries, key=lambda e: e[0]):
        h.update(rel.encode("utf-8"))
        h.update(_SEP.encode("utf-8"))
        h.update(hashlib.sha256(data).hexdigest().encode("utf-8"))
        h.update(_SEP.encode("utf-8"))
    return h.hexdigest()


def cache_key(*parts: str) -> str:
    """sha256 of the joined *parts* (e.g. extractor name + version + input hash).

    Mirrors the fully-enumerated-key spirit of §8.3: the key is a deterministic
    function of every input that should bust it. Parts are joined with a NUL
    separator so they cannot run together ambiguously.
    """
    joined = _SEP.join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def should_reextract(prev_key: str | None, new_key: str) -> bool:
    """True iff the inputs changed (or there is no previous key) — trivial helper."""
    return prev_key != new_key


def _glob_files(repo: Path, input_globs: list[str]) -> list[Path]:
    """Resolve *input_globs* (relative to *repo*) to a sorted list of existing files."""
    found: set[Path] = set()
    for pattern in input_globs:
        for match in repo.glob(pattern):
            if match.is_file():
                found.add(match)
    return sorted(found, key=lambda p: _rel_posix(p, repo))


class FragmentCache:
    """A small content-addressed store of extractor fact-fragments (§10.1).

    Bound to a cache directory (default ``ws.cache / "extract"``). Fragments are
    stored as ``<key>.json`` (written deterministically via ``jsonio.dump_json``); a
    sidecar ``manifest.json`` maps ``extractor_id -> {key, fragment_file}`` so a run
    can report hit/miss per extractor. Robust to a missing/corrupt store: any read
    error is a clean MISS, never an exception.
    """

    MANIFEST_NAME = "manifest.json"

    def __init__(self, cache_dir: Path) -> None:
        self.dir = Path(cache_dir)

    @classmethod
    def for_workspace(cls, ws: Workspace) -> "FragmentCache":
        """Default placement: ``<arch>/.cache/extract`` (gitignored, §12)."""
        return cls(ws.cache / "extract")

    # --- fragment storage -------------------------------------------------------

    def _fragment_path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict | None:
        """Return the cached fragment for *key*, or ``None`` on miss/corruption.

        A missing file, malformed JSON, or any I/O error is treated as a MISS and
        never raises (§18.4 graceful degradation) — the caller just re-extracts.
        """
        path = self._fragment_path(key)
        if not path.is_file():
            return None
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return None  # corrupt/garbage cache file -> clean miss
        return data if isinstance(data, dict) else None

    def put(self, key: str, fragment: dict) -> None:
        """Store *fragment* under *key* as ``<key>.json`` (deterministic write)."""
        dump_json(fragment, self._fragment_path(key))

    # --- manifest (hit/miss reporting) ------------------------------------------

    @property
    def _manifest_path(self) -> Path:
        return self.dir / self.MANIFEST_NAME

    def load_manifest(self) -> dict:
        """Load ``extractor_id -> {key, fragment_file}``; ``{}`` if absent/corrupt."""
        path = self._manifest_path
        if not path.is_file():
            return {}
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def record(self, extractor_id: str, key: str) -> None:
        """Note in the manifest that *extractor_id*'s output now lives under *key*.

        The manifest carries nothing volatile (no timestamps) so the gitignored file
        stays stable run-to-run (§18.4 determinism, even off the committed path).
        """
        manifest = self.load_manifest()
        manifest[extractor_id] = {"key": key, "fragment_file": f"{key}.json"}
        # Sort keys handled by dump_json; the dict ordering does not affect output.
        dump_json(manifest, self._manifest_path)

    def last_key(self, extractor_id: str) -> str | None:
        """The key *extractor_id* was last stored under, or ``None`` if never seen."""
        entry = self.load_manifest().get(extractor_id)
        if isinstance(entry, dict):
            key = entry.get("key")
            if isinstance(key, str):
                return key
        return None


def decide(
    repo: Path,
    extractor_id: str,
    version: str,
    input_globs: list[str],
    cache_dir: Path | None = None,
) -> tuple[str, bool]:
    """Compute *(key, changed-vs-last)* for *extractor_id* over its glob'd inputs (§10.1).

    The input file set is the union of *input_globs* resolved under *repo*; the key is
    a deterministic function of ``extractor_id``, ``version`` and that set's content
    hash (the §10.1 per-target keying: build-file / source-set content hashes). The
    last key is persisted in a per-extractor file under the cache dir so a later run
    can answer "did anything change?" without the previous fragment.

    Returns ``(key, changed)`` where ``changed`` is ``True`` on the first ever run for
    this extractor or whenever an input's content changed since the last ``decide``.
    Never raises: an unreadable last-key file is treated as "no previous key".
    """
    repo_p = Path(repo)
    cdir = Path(cache_dir) if cache_dir is not None else (repo_p / "architecture" / ".cache" / "extract")
    files = _glob_files(repo_p, input_globs)
    inputs_hash = content_hash_files(files, root=repo_p)
    key = cache_key(extractor_id, version, inputs_hash)

    last_path = cdir / f"{_safe_name(extractor_id)}.last-key"
    prev_key: str | None = None
    if last_path.is_file():
        try:
            prev_key = last_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            prev_key = None  # unreadable -> treat as no previous key (§18.4)

    changed = should_reextract(prev_key, key)

    # Persist the new key so the *next* run can compare against it. This is a state
    # write into the gitignored cache only; it never touches a committed artifact.
    try:
        last_path.parent.mkdir(parents=True, exist_ok=True)
        with open(last_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(key + "\n")
    except OSError:
        pass  # best-effort; a write failure just means the next run re-extracts

    return key, changed


def _safe_name(extractor_id: str) -> str:
    """Filesystem-safe slug for an extractor id (only used for the sidecar filename)."""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in extractor_id)
