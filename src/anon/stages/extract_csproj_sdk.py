"""Stage 1 — C# project deployable-ROLE facts from the csproj SDK (plan §4.2; a build-graph
*refinement*, sibling of ``extract_orchardcore_manifest``).

A .NET project's `<Project Sdk="…">` (plus its target framework) declares its *deployable role* —
a web service, a background worker, a MAUI client app, a Razor/UI library, an Aspire orchestration
host, or a plain class library. The A2 ablation (``plans/notes/2026-06-14-a2-curation-ablation.md``)
found this role is the signal eShop's reference groups its non-service projects by: the four
`BuildingBlocks` (EventBus / EventBusRabbitMQ / IntegrationEventLogEF / ServiceDefaults) share no
name or directory — only the *library* role — so no naming/dir/dependency heuristic recovers that
cluster, and it was the one genuinely non-circular C# judgment call. This extractor surfaces the
role as a governed ``sdk-role:<role>`` grouping-hint tag so a curator can group on it via the
``members_tag`` selector (the eShop overlay uses ``sdk-role:library`` as the residual-after-naming
catch-all = BuildingBlocks).

Emits a fact *fragment* refining the C#/.csproj build graph: a ``sdk-role:<role>`` tag on each
existing ``csharp:csproj:<rel>`` target (same id scheme as extract_build_graph → normalize_facts
unions the tag, §6.4c). Adds NO relationships and NO structure (§4: refine, never replace). Pure
XML/text parse — no .NET SDK.

NOTE (registry / determinism): wired into ``cli._extractor_registry`` (so ``arch run`` emits the
tags), but NOT into the ``conftest.run_toy_pipeline`` golden subset — which deliberately covers only
``extract_build_graph`` + the Roslyn L2 helper, so wiring this in churns no golden. Its determinism is
covered by this module's own unit tests (``tests/test_extract_csproj_sdk.py``), as for the other
registry extractors outside that subset. The tag is also inert in the rendered model:
``generate_structurizr._element_tags`` carries only an allowlist of view-key tags
(``needs-curation``/``layer:``/…); ``sdk-role:`` — like ``slnfolder:``/``build:msbuild`` — is not on
it, so it stays a curation grouping signal and never reaches the DSL.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..jsonio import dump_json
from ..paths import Workspace

EXTRACTOR = {"name": "csproj-sdk-role", "version": "0.1.0"}

_SKIP_DIRS = {"bin", "obj", ".git", "node_modules"}
_SDK = re.compile(r'<Project\s+Sdk="([^"]+)"')
_TFM = re.compile(r"<TargetFrameworks?>([^<]+)</")


def _norm(path: Path, repo: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _all_csproj(repo: Path) -> list[Path]:
    return sorted(p.resolve() for p in repo.rglob("*.csproj")
                  if not ({part.lower() for part in p.parts} & _SKIP_DIRS))


def sdk_role(csproj_text: str) -> str | None:
    """Classify a csproj's deployable role from its SDK attribute + target framework, or ``None``
    when there is no recognizable ``<Project Sdk="…">`` (so the tag is honestly absent)."""
    m = _SDK.search(csproj_text)
    if not m:
        return None
    sdk = m.group(1).lower()
    t = _TFM.search(csproj_text)
    tfm = t.group(1) if t else ""
    if sdk.endswith(".web"):
        return "service-web"
    if sdk.endswith(".worker"):
        return "service-worker"
    if "aspire" in sdk:                       # Aspire app host = orchestration role
        return "orchestration"
    if "maui" in sdk or any(x in tfm for x in ("android", "ios", "maccatalyst")):
        return "client-app"
    if "razor" in sdk:
        return "ui-library"
    if sdk == "microsoft.net.sdk":
        return "library"
    return None                               # an unrecognized custom SDK → no role claim


def build_fragment(repo: Path) -> dict[str, Any]:
    """Build the csproj-SDK-role fact fragment for *repo* (empty ``targets`` on a non-C# repo)."""
    repo = repo.resolve()
    targets: dict[str, dict[str, Any]] = {}
    for csproj in _all_csproj(repo):
        try:
            role = sdk_role(csproj.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            role = None
        if not role:
            continue
        tid = f"csharp:csproj:{_norm(csproj, repo)}"
        targets[tid] = {
            "id": tid,
            "name": csproj.stem,
            "type": "csproj",
            "language": "csharp",
            "path": _norm(csproj.parent, repo),
            "external": False,
            "tags": [f"sdk-role:{role}"],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR], "coverage": {}},
        "targets": [targets[k] for k in sorted(targets)],
        "relationships": [],
    }


def run(ws: Workspace) -> Path | None:
    """Extract csproj SDK roles and write the fragment, or ``None`` if the repo has no C# projects
    with a recognizable SDK (graceful no-op, §18.4). Never hard-fails."""
    fragment = build_fragment(ws.repo)
    if not fragment["targets"]:
        return None
    out = ws.fragments / "csproj-sdk-role.json"
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
