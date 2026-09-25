"""Stage 1 — build/target-graph extraction (plan §4, node 1).

C# L0 (plan §4.2): parse ``.sln`` + ``.csproj`` for the project graph. This is the
pilot's primary L0 source (§16#19) and, crucially, needs **no .NET SDK** — it is a
pure XML/text parse, so the deterministic backbone runs anywhere. Richer L2 symbol
facts come from the Roslyn helper in ``extract_csharp_facts.py`` (SDK-dependent,
additive).

Emits one fact *fragment* (plan §4.3) — a fact-model-shaped document with provenance
stamped by the caller — into ``<arch>/.cache/fragments/``. ``normalize_facts`` merges
all fragments (§6.4).

Stable ids (plan §6.3): ``csharp:csproj:<path-relative-to-repo-root>`` for projects
(the assembly name is NOT unique); ``csharp:package:<name>`` for NuGet packages
(external boundary).

Hardening (task #1) over the foundation version:
  * **Solution-folder grouping** — ``.sln`` solution folders (the special GUID
    ``{2150E333-...}``) and their ``NestedProjects`` mapping are parsed into a
    ``slnfolder:<name>`` hint tag on each contained target (§4.2 "Solution folders are
    a useful grouping hint for curation").
  * **MSBuild property expansion** — ``ProjectReference``/``PackageReference`` ``Include``
    paths that use ``$(MSBuildThisFileDirectory)`` / ``$(SolutionDir)`` / Directory.Build.props
    style relative refs are expanded as far as is statically possible so the edge still
    resolves to the right stable id.
  * **Missing referenced csproj** — if a ``<ProjectReference>`` points at a file that
    does not exist on disk, the edge is *still* emitted to the computed
    ``csharp:csproj:<rel>`` id (the dangling target is materialized as an ``unknown``
    placeholder so referential integrity holds, §5/§10), rather than silently dropped.
  * **De-dup** — targets are unique by id; relationships are de-duped by
    ``(source, target, evidence-detail)`` so a project listed in both the ``.sln`` and a
    direct scan, or a ref declared twice, never doubles an edge.

Pure XML/text only — no ``dotnet`` invocation (that is the additive L2 helper).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..jsonio import dump_json
from ..model import make_relationship
from ..paths import Workspace

EXTRACTOR = {"name": "csharp-build-graph", "version": "0.3.0"}

# Visual Studio .sln project line:
#   Project("{TYPE-GUID}") = "Name", "rel\path.csproj", "{PROJECT-GUID}"
_SLN_PROJECT = re.compile(
    r'Project\("\{(?P<type>[^}]+)\}"\)\s*=\s*"(?P<name>[^"]+)",\s*"(?P<path>[^"]+)",\s*"\{(?P<guid>[^}]+)\}"'
)

# A NestedProjects entry maps a child project/folder GUID to its parent folder GUID:
#   {CHILD-GUID} = {PARENT-GUID}
_SLN_NESTED = re.compile(
    r"\{(?P<child>[0-9A-Fa-f-]+)\}\s*=\s*\{(?P<parent>[0-9A-Fa-f-]+)\}"
)

# The canonical project-type GUID for a Visual Studio *solution folder* (not a buildable
# project). Case-insensitive compare.
_SLN_FOLDER_TYPE_GUID = "2150E333-8FDC-42A3-9474-1A3956D46DE8"

# MSBuild reserved/path properties we can resolve statically. Anything else is left as a
# literal segment (best-effort; the path still de-references to *some* stable key).
_MSBUILD_PROP = re.compile(r"\$\((?P<name>[A-Za-z_][A-Za-z0-9_]*)\)")


def _norm(path: Path, repo: Path) -> str:
    """Repo-relative POSIX path (stable across OSes)."""
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _safe_rel(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        # Outside the repo root (e.g. a sibling-repo ref) — keep a stable POSIX form.
        return path.as_posix()


def _csproj_id(csproj: Path, repo: Path) -> str:
    return f"csharp:csproj:{_norm(csproj, repo)}"


def _csproj_id_safe(csproj: Path, repo: Path) -> str:
    return f"csharp:csproj:{_safe_rel(csproj, repo)}"


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]  # drop any XML namespace


_SKIP_DIRS = {"bin", "obj", ".git", "node_modules"}


def _find_solutions(repo: Path) -> list[Path]:
    # Skip build-output / VCS dirs (mirrors _all_csproj): a .sln copied into bin/obj or
    # vendored under node_modules/.git would otherwise pull in stale/duplicate projects.
    return sorted(p for p in repo.rglob("*.sln")
                  if not ({part.lower() for part in p.parts} & _SKIP_DIRS))


def _find_slnx(repo: Path) -> list[Path]:
    """All ``.slnx`` (the newer XML solution format, e.g. OrchardCore/eShop), skip-filtered.

    The classic ``.sln`` (text + GUIDs) and the XML ``.slnx`` are parsed by separate
    functions because their solution-folder encodings are unrelated; both yield the same
    ``slnfolder:<name>`` grouping hint (§4.2) so a ``.slnx``-only repo is no longer silent
    on solution-folder grouping (the cold-start grouping signal for T1.4)."""
    return sorted(p for p in repo.rglob("*.slnx")
                  if not ({part.lower() for part in p.parts} & _SKIP_DIRS))


def _expand_msbuild(include: str, csproj: Path, sln_dir: Path | None) -> str:
    """Expand the few static MSBuild path properties we can resolve in an Include path.

    Directory.Build.props-style refs commonly use ``$(MSBuildThisFileDirectory)`` or
    ``$(SolutionDir)``; resolving them lets the edge land on the correct stable id
    instead of a mangled path. Unknown properties expand to empty (best-effort) so the
    remaining literal path is still usable.
    """
    props = {
        "msbuildthisfiledirectory": str(csproj.parent) + "/",
        "msbuildprojectdirectory": str(csproj.parent),
        "solutiondir": (str(sln_dir) + "/") if sln_dir else "",
        "msbuildstartupdirectory": str(sln_dir) if sln_dir else "",
    }

    def _sub(m: re.Match[str]) -> str:
        return props.get(m.group("name").lower(), "")

    return _MSBUILD_PROP.sub(_sub, include)


def _resolve_include(include: str, csproj: Path, sln_dir: Path | None) -> Path:
    """Resolve a (possibly property-bearing, backslash) Include path to an absolute Path."""
    expanded = _expand_msbuild(include, csproj, sln_dir).replace("\\", "/")
    p = Path(expanded)
    if not p.is_absolute():
        p = csproj.parent / p
    # ``resolve()`` normalizes ``..`` segments even if the target file is missing.
    return p.resolve()


def _parse_sln(sln: Path) -> dict[str, Any]:
    """Parse a ``.sln`` into ``{projects: {guid: {name, path}}, folders: {guid: name},
    nested: {child_guid: parent_guid}}``. Pure text — no MSBuild."""
    text = sln.read_text(encoding="utf-8-sig", errors="replace")
    projects: dict[str, dict[str, str]] = {}
    folders: dict[str, str] = {}
    for m in _SLN_PROJECT.finditer(text):
        guid = m.group("guid").upper()
        rel = m.group("path").replace("\\", "/")
        if m.group("type").upper() == _SLN_FOLDER_TYPE_GUID:
            folders[guid] = m.group("name")
        elif rel.lower().endswith(".csproj"):
            projects[guid] = {"name": m.group("name"), "path": rel}
        # non-.csproj buildable projects (vbproj/fsproj/etc.) are out of scope for v1.

    nested: dict[str, str] = {}
    # NestedProjects entries live inside GlobalSection(NestedProjects); the regex is
    # tolerant — any "{guid} = {guid}" line where the parent is a known folder counts.
    nested_section = re.search(
        r"GlobalSection\(NestedProjects\).*?EndGlobalSection", text, re.DOTALL
    )
    if nested_section:
        for m in _SLN_NESTED.finditer(nested_section.group(0)):
            nested[m.group("child").upper()] = m.group("parent").upper()
    return {"projects": projects, "folders": folders, "nested": nested}


def _folder_chain(guid: str, folders: dict[str, str], nested: dict[str, str]) -> list[str]:
    """Return the solution-folder name chain (outermost-first) for a project guid.

    Walks the ``NestedProjects`` parent links; guards against cycles defensively.
    """
    chain: list[str] = []
    seen: set[str] = set()
    cur = nested.get(guid)
    while cur and cur in folders and cur not in seen:
        chain.append(folders[cur])
        seen.add(cur)
        cur = nested.get(cur)
    chain.reverse()
    return chain


def _parse_slnx(slnx: Path) -> dict[Path, set[str]]:
    """Parse a ``.slnx`` for solution-folder grouping -> ``{abs csproj path: {slnfolder tags}}``.

    The XML ``.slnx`` format encodes folders directly as nested ``<Folder Name="/a/b/">``
    elements, each containing ``<Project Path="rel.csproj"/>`` rows — no GUID indirection
    like ``.sln``. We tag each contained ``.csproj`` with ``slnfolder:<segment>`` for **every
    leaf segment** of its enclosing folder's name (``/src/OrchardCore.Modules.Cms/`` ->
    ``slnfolder:OrchardCore.Modules.Cms``), so curation/auto-propose can group by any level
    (mirrors the per-level tagging the ``.sln`` ``_folder_chain`` does). Pure XML parse — no
    MSBuild/SDK. Malformed XML degrades to an empty mapping (graceful, §11)."""
    out: dict[Path, set[str]] = {}
    try:
        root = ET.parse(slnx).getroot()
    except (ET.ParseError, OSError):
        return out

    def _folder_segments(name: str) -> list[str]:
        return [seg for seg in (name or "").replace("\\", "/").split("/") if seg]

    def _walk(node: ET.Element, chain: list[str]) -> None:
        for child in node:
            tag = _strip_ns(child.tag)
            if tag == "Folder":
                seg = _folder_segments(child.get("Name", ""))
                # the folder's leaf name is its own level; nest deeper folders under it.
                new_chain = chain + (seg[-1:] if seg else [])
                _walk(child, new_chain)
            elif tag == "Project":
                rel = (child.get("Path") or "").replace("\\", "/")
                if rel.lower().endswith(".csproj") and chain:
                    abs_path = (slnx.parent / rel).resolve()
                    tags = out.setdefault(abs_path, set())
                    for level in chain:
                        tags.add(f"slnfolder:{level}")

    _walk(root, [])
    return out


def _all_csproj(repo: Path) -> list[Path]:
    """All .csproj under the repo, excluding obvious build-output dirs."""
    out = []
    for p in repo.rglob("*.csproj"):
        if not ({part.lower() for part in p.parts} & _SKIP_DIRS):
            out.append(p.resolve())
    return sorted(out)


def _l1_transitive_packages(csproj: Path) -> list[str] | None:
    """L1 NuGet graph (plan §4.2): transitive package deps from ``obj/project.assets.json``.

    Returns the sorted list of *all* (direct + transitive) package names resolved by a
    prior ``dotnet restore``, or ``None`` if the assets file is absent (no restore yet) —
    in which case L1 coverage stays false (honest §11 coverage). Pure JSON read, no SDK
    call. Transitive packages become external boundary targets (usually dropped by
    ``drop_external``, but available for boundary promotion, §4.2/§7.4).
    """
    assets = csproj.parent / "obj" / "project.assets.json"
    if not assets.exists():
        return None
    try:
        data = json.loads(assets.read_text(encoding="utf-8-sig", errors="replace"))
    except (ValueError, OSError):
        return None
    # Guard the structure: a well-formed-JSON-but-wrong-shape assets file (targets as a
    # list/string, etc.) must degrade to None, not raise AttributeError out of the loop.
    if not isinstance(data, dict):
        return None
    targets = data.get("targets")
    if not isinstance(targets, dict):
        return None
    names: set[str] = set()
    for framework in targets.values():
        if not isinstance(framework, dict):
            continue
        for key, spec in framework.items():
            # keys are "PackageName/Version"; type "package" excludes "project" deps.
            if isinstance(spec, dict) and spec.get("type") == "package" and "/" in key:
                names.add(key.split("/", 1)[0])
    return sorted(names)


def _parse_csproj(csproj: Path, sln_dir: Path | None) -> dict[str, Any]:
    """Return {'project_refs': [(abs_path, raw_include)], 'package_refs': [names]}."""
    project_refs: list[tuple[Path, str]] = []
    package_refs: list[str] = []
    try:
        root = ET.parse(csproj).getroot()
    except (ET.ParseError, OSError):
        return {"project_refs": [], "package_refs": []}
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag == "ProjectReference":
            inc = elem.get("Include")
            if inc:
                project_refs.append((_resolve_include(inc, csproj, sln_dir), inc))
        elif tag == "PackageReference":
            name = elem.get("Include") or elem.get("Update")
            if name:
                package_refs.append(name)
    return {"project_refs": project_refs, "package_refs": package_refs}


def build_fragment(repo: Path) -> dict[str, Any]:
    """Build the C# L0 fact fragment for *repo* (provenance added by the runner)."""
    repo = repo.resolve()
    solutions = _find_solutions(repo)

    # --- collect projects + their solution-folder hint tags ---------------------
    # csproj abs-path -> set of slnfolder tags (a project may appear in >1 .sln).
    folder_tags: dict[Path, set[str]] = {}
    sln_dir_for: dict[Path, Path] = {}  # csproj -> the .sln dir (for $(SolutionDir))
    csprojs_from_sln: list[Path] = []

    for sln in solutions:
        parsed = _parse_sln(sln)
        for guid, info in parsed["projects"].items():
            abs_path = (sln.parent / info["path"]).resolve()
            csprojs_from_sln.append(abs_path)
            sln_dir_for.setdefault(abs_path, sln.parent)
            chain = _folder_chain(guid, parsed["folders"], parsed["nested"])
            if chain:
                tags = folder_tags.setdefault(abs_path, set())
                # Tag each level of the folder hierarchy so curation can group by any.
                for name in chain:
                    tags.add(f"slnfolder:{name}")

    # The newer XML .slnx encodes folders inline (no GUIDs); parse it for the same
    # slnfolder hints so a .slnx-only repo (OrchardCore/eShop) still emits grouping signal.
    for slnx in _find_slnx(repo):
        for abs_path, tags in _parse_slnx(slnx).items():
            csprojs_from_sln.append(abs_path)
            sln_dir_for.setdefault(abs_path, slnx.parent)
            folder_tags.setdefault(abs_path, set()).update(tags)

    # Union with a direct scan so projects not listed in any .sln are still captured.
    csprojs = sorted({*csprojs_from_sln, *_all_csproj(repo)})

    targets: dict[str, dict[str, Any]] = {}
    rels_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    packages: dict[str, dict[str, Any]] = {}
    coverage: dict[str, Any] = {}
    csproj_ids = {c: _csproj_id_safe(c, repo) for c in csprojs}

    def _add_target(t: dict[str, Any]) -> None:
        """De-dup by id; merge tags if the same target appears twice."""
        existing = targets.get(t["id"])
        if existing is None:
            targets[t["id"]] = t
        else:
            existing["tags"] = sorted({*existing.get("tags", []), *t.get("tags", [])})

    def _add_rel(source: str, target_id: str, evidence: list[dict[str, Any]]) -> None:
        """De-dup edges by (source, target, evidence-detail)."""
        detail = evidence[0].get("detail", "") if evidence else ""
        key = (source, target_id, detail)
        if key not in rels_by_key:
            rels_by_key[key] = make_relationship(source=source, target=target_id, evidence=evidence)

    for csproj in csprojs:
        tid = csproj_ids[csproj]
        tags = ["build:msbuild", *sorted(folder_tags.get(csproj, set()))]
        _add_target({
            "id": tid,
            "name": csproj.stem,
            "type": "csproj",
            "language": "csharp",
            "path": _safe_rel(csproj.parent, repo),
            "external": False,
            "tags": tags,
        })
        coverage[tid] = {"L0": True, "L1": False, "L2": False, "L3": False}

        refs = _parse_csproj(csproj, sln_dir_for.get(csproj))
        # Stamp DIRECT package refs onto the project target itself (datasheet plan §6.1):
        # curation drops external package targets (`drop_external`), so the datasheet's
        # technology fingerprint must survive on the project, not on the package edge.
        if refs["package_refs"]:
            targets[tid]["package_refs"] = sorted(set(refs["package_refs"]))
        for ref_path, raw_include in refs["project_refs"]:
            # Use the on-disk identity if we know it; else compute the id from the path
            # so a *missing* referenced csproj still produces the edge (and a placeholder
            # target below) rather than vanishing.
            target_id = csproj_ids.get(ref_path) or _csproj_id_safe(ref_path, repo)
            rel_detail = _safe_rel(ref_path, repo)
            _add_rel(
                source=tid, target_id=target_id,
                evidence=[{"type": "project_ref",
                           "detail": f'<ProjectReference Include="{rel_detail}">',
                           "visibility": "public"}],
            )
            # Materialize a placeholder for a dangling reference so referential
            # integrity (§5/§10) holds; a real scan/sln entry overrides it via _add_target.
            if target_id not in csproj_ids.values() and target_id not in targets:
                _add_target({
                    "id": target_id,
                    "name": ref_path.stem,
                    "type": "unknown",
                    "language": "csharp",
                    "path": rel_detail.rsplit("/", 1)[0] if "/" in rel_detail else "",
                    "external": False,
                    "tags": ["build:msbuild", "missing-ref"],
                })

        direct_pkgs = set(refs["package_refs"])
        # Iterate in sorted order so the emitted targets/relationships lists are byte-stable
        # across runs (set iteration order varies with PYTHONHASHSEED); the set itself is
        # still used for the transitive-dedup membership test below.
        for pkg in sorted(direct_pkgs):
            pkg_id = f"csharp:package:{pkg}"
            packages.setdefault(pkg_id, {
                "id": pkg_id, "name": pkg, "type": "package", "language": "csharp",
                "external": True, "tags": ["external", "nuget"],
            })
            _add_rel(
                source=tid, target_id=pkg_id,
                evidence=[{"type": "package_ref", "detail": f'<PackageReference Include="{pkg}">'}],
            )

        # L1: transitive NuGet graph from a prior restore (plan §4.2). Presence-gated:
        # absent assets file => L1 stays false (honest coverage, §11).
        transitive = _l1_transitive_packages(csproj)
        if transitive is not None:
            coverage[tid]["L1"] = True
            for pkg in transitive:
                if pkg in direct_pkgs:
                    continue  # already a direct dep above
                pkg_id = f"csharp:package:{pkg}"
                packages.setdefault(pkg_id, {
                    "id": pkg_id, "name": pkg, "type": "package", "language": "csharp",
                    "external": True, "tags": ["external", "nuget", "transitive"],
                })
                _add_rel(
                    source=tid, target_id=pkg_id,
                    evidence=[{"type": "package_ref",
                               "detail": f"transitive package {pkg} (project.assets.json)"}],
                )

    for pkg in packages.values():
        _add_target(pkg)

    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR], "coverage": coverage},
        "targets": list(targets.values()),
        "relationships": list(rels_by_key.values()),
    }


def run(ws: Workspace) -> Path:
    """Extract the C# build graph and write the fragment. Returns the fragment path."""
    fragment = build_fragment(ws.repo)
    out = ws.fragments / "csharp-build-graph.json"
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
