"""Informative-narratives plan §2.1 — the per-project **API skeleton** sidecar.

The §4 narrative layer's highest-leverage new input (informative-llm-narratives §2.1, "D")
is *what a unit exposes and how it is shaped*: the public/protected type + member
signatures, their XML-doc summaries, base types + interfaces, and notable attributes — the
direct evidence for **responsibilities** and **design patterns** (a ``: IRequestHandler<>``
⇒ mediator/CQRS; ``: DbContext`` ⇒ EF repository). No method bodies — signatures are
~5-15 % of full-source tokens.

Like the ``file-deps/1`` sidecar, this travels next to the fact fragments as a machine-local
sidecar **OUTSIDE** the determinism-hashed core and the fact-model schema (adding it bumps
nothing, perturbs no golden) — it is an *enrichment input*, never structure::

    <arch-dir>/.cache/fragments/api-skeleton-<extractor>.json

    {"schema": "api-skeleton/1", "extractor": {...}, "language": "cs",
     "skeletons": {"<target-id>": [ {type-skeleton}, ... ], ...}}

The top-level key is ``skeletons`` (not ``targets``) deliberately: ``normalize_facts`` globs
every ``*.json`` under the fragments dir and iterates each one's ``targets`` field, so a
sidecar must NOT carry a ``targets`` key — exactly as ``file-deps/1`` carries ``files``.

A *type-skeleton* is ``{name, kind, base?, interfaces?, attributes?, doc?, members?}`` where
each *member* is ``{sig, doc?, attrs?}``. The Roslyn helper emits this as an ``apiSkeleton``
block (camelCase) on its fragment; :func:`extract_csharp_facts._lift_apiskeleton_sidecar`
lifts it here and strips it from the fact fragment. The C++/Doxygen analogue (public
declarations + brief docs) can write the same schema later (plan §11) — the payload
assembler is language-neutral.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .jsonio import dump_json
from .paths import Workspace

APISKELETON_SCHEMA = "api-skeleton/1"


def make_sidecar(extractor: dict[str, str], language: str,
                 skeletons: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Assemble a canonical ``api-skeleton/1`` sidecar (target ids sorted)."""
    return {
        "schema": APISKELETON_SCHEMA,
        "extractor": extractor,
        "language": language,
        "skeletons": dict(sorted(skeletons.items())),
    }


def write_sidecar(ws: Workspace, sidecar: dict[str, Any]) -> Path:
    """Write the sidecar as ``api-skeleton-<extractor-name>.json`` under the fragments dir."""
    out = ws.fragments / f"api-skeleton-{sidecar['extractor']['name']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_json(sidecar, out)
    return out


def load_sidecars(ws: Workspace) -> list[dict[str, Any]]:
    """All api-skeleton sidecars, in sorted-filename order (deterministic merge precedence)."""
    from .jsonio import load_json
    out = []
    for p in sorted(ws.fragments.glob("api-skeleton-*.json")):
        try:
            data = load_json(p)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("schema") == APISKELETON_SCHEMA:
            out.append(data)
    return out


def merge(sidecars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Union the sidecars into one ``{target_id: [type-skeleton, ...]}`` map.

    The first sidecar (sorted-filename order) to claim a target id wins; later sidecars do
    not overwrite a populated entry. Best-effort — a malformed entry is skipped, never
    fatal (this is an enrichment input, not a gate)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for sc in sidecars:
        for tid, types in (sc.get("skeletons") or {}).items():
            if not isinstance(tid, str) or tid in out:
                continue
            if isinstance(types, list):
                out[tid] = [t for t in types if isinstance(t, dict)]
    return out


def load(ws: Workspace) -> dict[str, list[dict[str, Any]]]:
    """Convenience: ``merge(load_sidecars(ws))`` — the per-target API skeleton map, or
    ``{}`` when no helper emitted one (honest absence → graph-only narrative payload)."""
    return merge(load_sidecars(ws))
