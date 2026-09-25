"""Existing-architecture-artifact discovery — read-only (plan §4.5.1).

Real repos are rarely greenfield for architecture (§4.5): they often already carry a
hand-drawn ``workspace.dsl`` / ``*.dsl`` / ``*.c4``, a Structurizr ``workspace.json``,
C4-PlantUML ``.puml`` diagrams, and a folder of ADRs. The pipeline's prime directive is
**never clobber human work**, so before extraction a discovery pass runs that only
*reports* what it finds — **it mutates nothing**.

This module implements §4.5.1 only: detection + lightweight classification (sniff format,
note parse success), never a full model parse and never relationship/structure extraction
from ADR prose (that is forbidden by §4.5.4). Downstream adoption (seed/reconcile) lives in
§7.4/§7.5/§11.5 and is out of scope here.

What is detected
----------------
**Hand-crafted C4 / Structurizr models** (``models[]``):
  - ``**/*.dsl`` — Structurizr DSL (sniffed for a ``workspace``/``model`` block).
  - ``**/*.c4`` — C4 DSL.
  - Structurizr ``workspace.json`` — distinguishing a *layout-only* JSON the pipeline
    itself writes (§9.1: element ``id``/``x``/``y`` positions only) from a *full model*
    JSON that carries model elements (``softwareSystem`` / ``container`` / ...).
  - C4-PlantUML ``.puml`` / ``.plantuml`` — heuristic: an ``!include`` of
    ``C4_Context`` / ``C4_Container`` / ``C4_Component``.

**ADRs** (``adrs[]``) in the common locations (``docs/adr/``, ``doc/adr/``, ``adr/``,
``architecture/decisions/``, ``doc/architecture/decisions/``): Nygard (``adr-NNN*.md``,
*Context / Decision / Status / Consequences* sections) and MADR
(``NNNN-title-with-dashes.md``, YAML-ish ``status:`` front-matter + *Decision Outcome*).
For each: ``path``, ``id`` (from the filename), ``title`` (first ``# `` heading), and
``status`` when detectable.

The pipeline's own ``architecture/`` output dir (the workspace ``arch_dir``) and any
``generated/`` subtree are **excluded** so discovery never re-ingests generated output as
if it were hand-crafted (§4.5.1).

Public API
----------
:func:`discover`  Pure, deterministic report dict from a repo root (mutates nothing).
:func:`run`       Workspace-aware wrapper that writes ``generated/discovered-artifacts.json``.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Iterable

from .jsonio import dump_json, load_json
from .paths import Workspace, resolve_workspace

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

# ADR directory conventions (§4.5.1). Repo-relative, forward-slash.
ADR_DIRS: tuple[str, ...] = (
    "docs/adr",
    "doc/adr",
    "adr",
    "architecture/decisions",
    "doc/architecture/decisions",
)

# Bytes to sniff from a candidate file — enough to classify, never the whole file.
_SNIFF_BYTES = 8192

# Model file extensions we treat as hand-crafted DSL (§4.5.1).
_DSL_EXTS = (".dsl", ".c4")
_PUML_EXTS = (".puml", ".plantuml")

# C4-PlantUML include heuristic (§4.5.1): an !include of a C4 macro library.
_C4_PUML_RE = re.compile(
    r"!include(?:url)?\b.*C4_(?:Context|Container|Component|Dynamic|Deployment)",
    re.IGNORECASE,
)

# Structurizr DSL "looks like a model" sniff — a top-level workspace/model/!include token.
_DSL_MODEL_RE = re.compile(r"\b(workspace|model)\b", re.IGNORECASE)

# Markdown H1 heading -> title.
_H1_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.MULTILINE)

# MADR / Nygard status sniffs.
# YAML-ish front-matter `status: accepted` (MADR) or a `## Status\n<value>` block (Nygard).
_FRONTMATTER_STATUS_RE = re.compile(r"^status\s*:\s*(?P<v>.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_HEADING_STATUS_RE = re.compile(
    r"^#{1,6}\s*status\s*$\s*(?P<v>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Filename id prefix: leading digits (MADR `0007-...`) or `adr-0007-...` (Nygard).
_ADR_ID_RE = re.compile(r"^(?:adr[-_]?)?(?P<id>\d{1,5})\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Low-level helpers                                                            #
# --------------------------------------------------------------------------- #

def _read_text(path: Path, limit: int | None = None) -> str:
    """Read text defensively; return ``""`` on any error (unreadable/binary/missing).

    Uses ``utf-8-sig`` so a BOM (common on Windows-authored files) is stripped, and
    ``errors="replace"`` so an odd byte never raises. *limit* caps the bytes read for
    cheap sniffing.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            return fh.read() if limit is None else fh.read(limit)
    except (OSError, ValueError):
        return ""


def _rel_posix(path: Path, root: Path) -> str:
    """Repo-relative, forward-slash path string (determinism + cross-OS stability)."""
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        rel = path
    return rel.as_posix()


def _is_excluded(path: Path, excluded: list[Path]) -> bool:
    """True iff *path* is inside any excluded directory, or any ``generated/`` subtree.

    The workspace ``arch_dir`` is excluded so the pipeline never re-ingests its own
    generated output as hand-crafted (§4.5.1). ``generated/`` anywhere is belt-and-braces
    in case the arch-dir is redirected out-of-tree.
    """
    rp = path.resolve()
    for ex in excluded:
        try:
            rp.relative_to(ex)
            return True
        except ValueError:
            continue
    return "generated" in rp.parts


# --------------------------------------------------------------------------- #
# Model classification (§4.5.1)                                                #
# --------------------------------------------------------------------------- #

def _classify_workspace_json(path: Path) -> tuple[bool, str]:
    """Classify a ``workspace.json`` as a layout-only vs full-model Structurizr JSON.

    Returns ``(parse_ok, kind)`` where *kind* is one of
    ``"structurizr-json"`` (full hand-authored model — has model elements) or
    ``"structurizr-layout"`` (the layout-only positions JSON the pipeline writes, §9.1).

    A *full model* has a ``model`` object carrying ``softwareSystems`` / ``people`` /
    ``containers``. A *layout-only* file (§9.1) has only element ``id``/``x``/``y``
    positions (typically under ``views`` / ``configuration``), no ``model`` elements.
    A file that doesn't parse is reported with ``parse_ok=False`` and the conservative
    ``structurizr-json`` kind so a human still sees it.
    """
    try:
        data = load_json(path)
    except (OSError, ValueError):
        return False, "structurizr-json"

    if not isinstance(data, dict):
        return True, "structurizr-json"

    model = data.get("model")
    has_model_elements = isinstance(model, dict) and any(
        bool(model.get(k))
        for k in ("softwareSystems", "people", "containers", "deploymentNodes", "elements")
    )
    if has_model_elements:
        return True, "structurizr-json"
    return True, "structurizr-layout"


def _classify_model_file(path: Path, root: Path) -> dict[str, Any] | None:
    """Classify one candidate model file, or ``None`` if it isn't one.

    Returns ``{path, format, parse_ok, kind}`` (all repo-relative / deterministic).
    Detection is by extension + a cheap content sniff; this never fully parses the model.
    """
    suffix = path.suffix.lower()
    rel = _rel_posix(path, root)

    # Structurizr / C4 DSL.
    if suffix in _DSL_EXTS:
        text = _read_text(path, _SNIFF_BYTES)
        fmt = "structurizr-dsl" if suffix == ".dsl" else "c4-dsl"
        # parse_ok here = "readable and looks like a model" (we do not run the real DSL
        # parser, §4.5.1 — that is a downstream adoption concern). Empty/garbled => False.
        parse_ok = bool(text.strip()) and bool(_DSL_MODEL_RE.search(text))
        return {"path": rel, "format": fmt, "parse_ok": parse_ok, "kind": "model"}

    # C4-PlantUML — only counts if it !includes a C4 macro library.
    if suffix in _PUML_EXTS:
        text = _read_text(path, _SNIFF_BYTES)
        if not _C4_PUML_RE.search(text):
            return None
        return {
            "path": rel,
            "format": "c4-plantuml",
            "parse_ok": bool(text.strip()),
            "kind": "diagram",
        }

    # Structurizr workspace.json (full model vs layout-only).
    if suffix == ".json" and path.name.lower() == "workspace.json":
        parse_ok, kind = _classify_workspace_json(path)
        return {
            "path": rel,
            "format": "structurizr-json",
            "parse_ok": parse_ok,
            # kind distinguishes a layout-only sidecar from a real model.
            "kind": "layout" if kind == "structurizr-layout" else "model",
        }

    return None


# --------------------------------------------------------------------------- #
# ADR classification (§4.5.1 / §4.5.4)                                         #
# --------------------------------------------------------------------------- #

def _adr_id_from_name(stem: str) -> str:
    """Extract an ADR id from a filename stem (``0007-foo`` -> ``0007``, ``adr-12`` -> ``12``).

    Falls back to the whole stem when no leading numeric id is present, so an oddly-named
    ADR is still reported (never dropped).
    """
    m = _ADR_ID_RE.match(stem)
    if m:
        return m.group("id")
    return stem


def _adr_title(text: str, fallback_stem: str) -> str:
    """First Markdown ``# `` heading, or a humanized filename stem as fallback."""
    m = _H1_RE.search(text)
    if m:
        return m.group(1).strip()
    # Humanize `0007-use-mqtt-for-telemetry` -> `use mqtt for telemetry`.
    cleaned = re.sub(r"^(?:adr[-_]?)?\d{1,5}[-_]?", "", fallback_stem, flags=re.IGNORECASE)
    return cleaned.replace("-", " ").replace("_", " ").strip() or fallback_stem


def _adr_status(text: str) -> str | None:
    """Detect ADR status: MADR front-matter ``status:`` or a Nygard ``## Status`` block."""
    m = _FRONTMATTER_STATUS_RE.search(text)
    if m:
        val = m.group("v").strip().strip("\"'")
        if val:
            return val
    m = _HEADING_STATUS_RE.search(text)
    if m:
        val = m.group("v").strip()
        # Guard against the status line itself being another heading.
        if val and not val.startswith("#"):
            return val
    return None


def _looks_like_adr(stem: str) -> bool:
    """Filename heuristic: MADR ``NNNN-...`` or Nygard ``adr-NNN...`` (or a bare ``NNN``)."""
    return bool(re.match(r"^(?:adr[-_]?)?\d{1,5}\b", stem, flags=re.IGNORECASE))


def _classify_adr(path: Path, root: Path) -> dict[str, Any]:
    """Classify one ADR markdown file -> ``{path, id, title, status}`` (deterministic)."""
    text = _read_text(path, _SNIFF_BYTES)
    stem = path.stem
    return {
        "path": _rel_posix(path, root),
        "id": _adr_id_from_name(stem),
        "title": _adr_title(text, stem),
        "status": _adr_status(text),
    }


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def discover(repo_root: Path, exclude_dirs: Iterable[Path] = ()) -> dict[str, Any]:
    """Discover hand-crafted architecture artifacts under *repo_root* (read-only, §4.5.1).

    Pure and deterministic: walks the tree, classifies candidate model files and ADRs,
    and returns a report dict. **It mutates nothing.** Unreadable/garbled files are
    sniffed defensively and never raise; a repo with nothing yields empty lists.

    Parameters
    ----------
    repo_root:
        The target working tree root to scan.
    exclude_dirs:
        Directories to skip entirely (typically the workspace ``arch_dir`` so the
        pipeline's own generated output is never re-ingested). Any ``generated/`` subtree
        is excluded unconditionally as well.

    Returns
    -------
    dict
        ``{"models": [{path, format, parse_ok, kind}],
           "adrs": [{path, id, title, status}],
           "counts": {...}}`` — all paths repo-relative, forward-slash, lists sorted.
    """
    root = Path(repo_root).resolve()
    excluded = [Path(d).resolve() for d in exclude_dirs]

    models: list[dict[str, Any]] = []
    adrs: list[dict[str, Any]] = []
    seen_adr_paths: set[str] = set()

    if not root.is_dir():
        # Graceful: a non-existent / non-directory repo yields a valid empty report.
        return _empty_report()

    # --- pass 1: model files (any extension we care about, anywhere in the tree) ---
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _is_excluded(path, excluded):
            continue
        entry = _classify_model_file(path, root)
        if entry is not None:
            models.append(entry)

    # --- pass 2: ADRs (only under the conventional directories, §4.5.1) ---
    for adr_dir in ADR_DIRS:
        base = root / adr_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if not path.is_file():
                continue
            if _is_excluded(path, excluded):
                continue
            if not _looks_like_adr(path.stem):
                continue
            rel = _rel_posix(path, root)
            if rel in seen_adr_paths:  # the same dir reachable via two conventions
                continue
            seen_adr_paths.add(rel)
            adrs.append(_classify_adr(path, root))

    # Deterministic ordering — sort by the stable repo-relative path.
    models.sort(key=lambda m: m["path"])
    adrs.sort(key=lambda a: a["path"])

    counts = {
        "models": len(models),
        "adrs": len(adrs),
        "models_by_format": _count_by(models, "format"),
        "adrs_with_status": sum(1 for a in adrs if a.get("status")),
    }

    return {"models": models, "adrs": adrs, "counts": counts}


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    """Deterministic ``{value: count}`` histogram over *items[key]* (sorted keys)."""
    out: dict[str, int] = {}
    for it in items:
        v = str(it.get(key, ""))
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def _empty_report() -> dict[str, Any]:
    """A valid, empty discovery report (graceful-degradation default)."""
    return {
        "models": [],
        "adrs": [],
        "counts": {"models": 0, "adrs": 0, "models_by_format": {}, "adrs_with_status": 0},
    }


def run(ws: Workspace) -> dict[str, Any]:
    """Run discovery for a resolved :class:`Workspace` and write the report (§4.5.1).

    Calls :func:`discover` on ``ws.repo`` excluding ``ws.arch_dir`` (so the pipeline's own
    generated output is never re-ingested), writes the deterministic report to
    ``<generated>/discovered-artifacts.json`` via :func:`dump_json`, and returns it.

    Graceful: never raises on an empty/odd repo — it yields a valid (possibly empty) report.
    """
    report = discover(ws.repo, exclude_dirs=[ws.arch_dir])
    out_path = ws.generated / "discovered-artifacts.json"
    dump_json(report, out_path)
    return report


# --------------------------------------------------------------------------- #
# CLI shim (mirrors the house pattern, e.g. export_views.py / combine.py)      #
# --------------------------------------------------------------------------- #

def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="anon.discover", description=__doc__)
    ap.add_argument("--repo", required=True, help="target working tree to scan")
    ap.add_argument("--arch-dir", help="artifact dir (default <repo>/architecture)")
    args = ap.parse_args(argv)
    ws = resolve_workspace(args.repo, args.arch_dir)
    report = run(ws)
    c = report["counts"]
    # Discovery is advisory and never fails the run (§4.5.1) — always exit 0.
    # ASCII '->' (not the §-style arrow) so the standalone shim prints cleanly on a
    # Windows cp1252 console; the UTF-8 stdio reconfig lives in anon.force_utf8_stdio
    # and is not in effect when this module is run directly via `python -m`.
    print(f"[discover] models={c['models']} adrs={c['adrs']} "
          f"-> {(ws.generated / 'discovered-artifacts.json')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
