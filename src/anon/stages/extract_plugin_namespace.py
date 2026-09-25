"""Stage 1 — plugin-namespace TYPE facts (plan §4.2; a build-graph *refinement*, sibling of
``extract_csproj_sdk`` / ``extract_orchardcore_manifest``).

Many .NET plugin ecosystems name each plugin ``<Vendor>.Plugin.<Type>.<Name>`` — the segment after
the ``Plugin`` marker is the plugin's functional TYPE (nopCommerce ``Nop.Plugin.Tax.Avalara`` -> Tax,
``Nop.Plugin.Payments.Stripe`` -> Payments; the same shape recurs in Orchard 1, Umbraco, and other
CMS/e-commerce plugin systems). The A2 ablation (``plans/notes/2026-06-14-a2-curation-ablation.md``)
found this type is exactly the family nopCommerce's reference groups plugins by, and the only thing a
naming/dir/dependency heuristic missed. This extractor generalizes that convention — what was a
repo-specific ``members_glob`` overlay becomes a hands-off tag, the same way ``sdk-role:*`` did.

Emits a ``plugin-type:<Type>`` tag on each ``csharp:csproj:<rel>`` target whose dotted project name
carries a ``Plugin``/``Plugins`` marker with a following type token (same id scheme as the build graph
-> ``normalize_facts`` unions the tag, §6.4c). Adds NO relationships and NO structure (§4: refine,
never replace); the tag is inert in the rendered DSL (``generate_structurizr._element_tags`` allowlist).
Pure name parse — no .NET SDK. A repo with no such plugins (the toy fixture, eShop, Orleans, Serilog,
all C++ systems) yields an empty fragment: a graceful no-op (§18.4), never a hard failure.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..jsonio import dump_json
from ..paths import Workspace

EXTRACTOR = {"name": "plugin-namespace", "version": "0.1.0"}

_SKIP_DIRS = {"bin", "obj", ".git", "node_modules"}
# The plugin-namespace markers (PascalCase, as the convention is written). A type token must follow.
_MARKERS = ("Plugin", "Plugins")
_TYPE_OK = re.compile(r"^[A-Za-z0-9]+$")  # a single clean PascalCase/alnum type token


def _norm(path: Path, repo: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _all_csproj(repo: Path) -> list[Path]:
    return sorted(p.resolve() for p in repo.rglob("*.csproj")
                  if not ({part.lower() for part in p.parts} & _SKIP_DIRS))


def plugin_type(project_basename: str) -> str | None:
    """The plugin TYPE from a ``<Vendor>.Plugin.<Type>.<Name>`` dotted basename, or ``None``.

    Finds the first ``Plugin``/``Plugins`` token in the dotted name and returns the next token
    (the type), e.g. ``Nop.Plugin.Tax.Avalara`` -> ``Tax``. Requires a clean alnum type token, so a
    project merely *named* ``Foo.Plugin`` (no type) or ``MyPluginThing`` (no dot boundary) yields
    no tag — an honest absence rather than a junk family."""
    tokens = project_basename.split(".")
    for i, tok in enumerate(tokens[:-1]):          # need a token AFTER the marker
        if tok in _MARKERS:
            nxt = tokens[i + 1]
            if _TYPE_OK.match(nxt):
                return nxt
    return None


def build_fragment(repo: Path) -> dict[str, Any]:
    """Build the plugin-type fact fragment for *repo* (empty ``targets`` when no plugins match)."""
    repo = repo.resolve()
    targets: dict[str, dict[str, Any]] = {}
    for csproj in _all_csproj(repo):
        ptype = plugin_type(csproj.stem)
        if not ptype:
            continue
        tid = f"csharp:csproj:{_norm(csproj, repo)}"
        targets[tid] = {
            "id": tid,
            "name": csproj.stem,
            "type": "csproj",
            "language": "csharp",
            "path": _norm(csproj.parent, repo),
            "external": False,
            "tags": [f"plugin-type:{ptype}"],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR], "coverage": {}},
        "targets": [targets[k] for k in sorted(targets)],
        "relationships": [],
    }


def run(ws: Workspace) -> Path | None:
    """Extract plugin-namespace types and write the fragment, or ``None`` if the repo has no
    matching plugins (graceful no-op, §18.4). Never hard-fails."""
    fragment = build_fragment(ws.repo)
    if not fragment["targets"]:
        return None
    out = ws.fragments / "plugin-namespace.json"
    dump_json(fragment, out)
    return out


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    import argparse

    from ..paths import resolve_workspace

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir)
    print(run(w))
