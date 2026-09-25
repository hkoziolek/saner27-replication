"""Stage 1 — C# L2 semantic facts via the Roslyn helper (plan §4.2, node 1).

Drives the out-of-process .NET Roslyn helper (``csharp-helper/``) which loads the
solution with ``MSBuildWorkspace`` (after ``MSBuildLocator.RegisterDefaults()``) and
emits namespace ownership + cross-project ``symbol_use`` evidence as a fact fragment
into ``ws.fragments/"roslyn-csharp.json"``. Its ids reuse the build-graph scheme
(``csharp:csproj:<repo-relative-path>``) so ``normalize_facts`` merges the two
fragments (§6.4).

This layer is SDK-dependent and *additive*: a matching .NET SDK must be installed and
``dotnet restore`` already run, else ``OpenSolutionAsync`` returns partial compilations
with dropped references (plan §4.2/§19.5). If the helper or SDK is unavailable — or any
step fails — this stage **degrades gracefully** (plan §18.4): it logs a coverage warning
and returns ``None`` so the L0 backbone still produces a model. It NEVER hard-fails the
run because Roslyn could not load.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .. import apiskeleton, filedeps
from ..jsonio import dump_json, load_json
from ..paths import Workspace

log = logging.getLogger("anon.extract_csharp_facts")

# Bump in lockstep with csharp-helper/Fragment.cs ExtractorVersion. 0.3.0 added the
# `apiSkeleton` block (informative-llm-narratives §2.1 "D"); the version rides the §8.3
# cache key so the richer narrative input recomputes rather than serving a stale cache.
EXTRACTOR = {"name": "roslyn-csharp", "version": "0.3.0"}

# Repo-root-relative location of the helper project. Resolved against the anon repo
# (where csharp-helper/ lives), not against the *target* repo being analyzed.
HELPER_PROJECT_DIR = "csharp-helper"
HELPER_CSPROJ = "RoslynExtractor.csproj"
HELPER_DLL = "RoslynExtractor.dll"
HELPER_BUILD_CONFIG = "Release"

# Build-output / VCS dirs to skip when discovering a solution (mirrors extract_build_graph)
# so a .sln/.slnx copied into bin/obj or vendored under node_modules is not picked up.
_SKIP_DIRS = {"bin", "obj", ".git", "node_modules"}

# How long to allow the helper build / run before giving up (and degrading). Generous for
# a cold build; the warm path is fast.
_BUILD_TIMEOUT_S = 600
_RUN_TIMEOUT_S = 1800  # cold L2 symbol extract budget (plan §1.1 #6: <=30 min)


def _anon_repo_root() -> Path:
    """Locate the anon repo root (the dir that contains ``csharp-helper/``).

    Walks up from this module; falls back to cwd-relative if not found above.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / HELPER_PROJECT_DIR / HELPER_CSPROJ).exists():
            return parent
    return Path.cwd()


def helper_project_dir() -> Path:
    return _anon_repo_root() / HELPER_PROJECT_DIR


def dotnet_available() -> bool:
    return shutil.which("dotnet") is not None


def helper_available() -> bool:
    """True if ``dotnet`` is on PATH and the helper project exists (best-effort gate)."""
    return dotnet_available() and (helper_project_dir() / HELPER_CSPROJ).exists()


def _built_dll(proj_dir: Path) -> Path | None:
    """Return the most plausible built helper DLL path, or ``None`` if not built yet."""
    # dotnet build emits to bin/<config>/<tfm>/<dll>; the tfm folder name can vary, so glob.
    candidates = sorted(proj_dir.glob(f"bin/{HELPER_BUILD_CONFIG}/*/{HELPER_DLL}"))
    return candidates[-1] if candidates else None


def _ensure_built(proj_dir: Path) -> Path | None:
    """Return the built helper DLL, building it once (cached) if needed.

    Returns ``None`` on build failure (caller degrades).
    """
    dll = _built_dll(proj_dir)
    if dll is not None:
        return dll
    log.info("building Roslyn helper (one-time): dotnet build %s -c %s",
             proj_dir, HELPER_BUILD_CONFIG)
    try:
        proc = subprocess.run(
            ["dotnet", "build", str(proj_dir / HELPER_CSPROJ), "-c", HELPER_BUILD_CONFIG,
             "--nologo", "-v", "quiet"],
            capture_output=True, text=True, timeout=_BUILD_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Roslyn helper build could not start: %s — degrading (§18.4)", exc)
        return None
    if proc.returncode != 0:
        log.warning("Roslyn helper build failed (rc=%s) — degrading (§18.4):\n%s",
                    proc.returncode, (proc.stderr or proc.stdout or "").strip()[-2000:])
        return None
    return _built_dll(proj_dir)


def _find_solution(repo: Path) -> Path | None:
    """Find the repo's *top-level* solution, or ``None`` if there is none.

    Accepts both classic ``.sln`` and the newer XML ``.slnx`` format (eShop, OrchardCore).
    Selection is **shallowest-first across both extensions**, with ``.sln`` preferred over
    ``.slnx`` only at *equal* depth (it is the more universally supported format) and a
    lexical final tie-break. The shallowest-overall rule is load-bearing: a repo can ship a
    root aggregate solution *and* nested per-project ones (eShop has a root ``eShop.slnx``
    plus ``src/ClientApp/ClientApp.sln`` + ``tests/ClientApp.UnitTests/…sln``). The old
    ``.sln``-beats-any-``.slnx`` rule then picked a deep single-project ``.sln``, so the
    helper only ever saw that one project — the root ``.slnx`` is the real solution and must
    win on depth (plan §4.2 / §16#19). Roslyn 5.x ``OpenSolutionAsync`` opens ``.slnx``
    directly given a .NET 10 SDK, and the helper's under-enumeration recovery (RoslynWorker
    ``DeclaredProjectPaths``) backstops the format's partial-load behavior."""
    def _depth_key(p: Path) -> tuple[int, int, str]:
        # shallower wins; at equal depth prefer .sln (0) over .slnx (1); then lexical.
        return (len(p.relative_to(repo).parts),
                0 if p.suffix.lower() == ".sln" else 1,
                str(p))
    def _ok(p: Path) -> bool:
        return not ({part.lower() for part in p.parts} & _SKIP_DIRS)
    sols = [p for p in repo.rglob("*.sln") if _ok(p)]
    sols += [p for p in repo.rglob("*.slnx") if _ok(p)]
    sols.sort(key=_depth_key)
    return sols[0] if sols else None


def run(ws: Workspace) -> Path | None:
    """Run the Roslyn helper and write a fragment, or return ``None`` if unavailable.

    Returns the fragment path on success, ``None`` on graceful degradation (§18.4).
    """
    # Escape hatch (§18.3 cheap-first / §18.4), the C# twin of ANON_SKIP_DOXYGEN:
    # the Roslyn L2 layer is the expensive one on .NET repos (helper build + per-project
    # semantic models). Experiment harnesses that only exercise the L0 build graph (e.g.
    # the eval §8.1 seeded-mutation runs) set ANON_SKIP_ROSLYN=1 to keep the cheap
    # L0/L1 backbone and honestly leave L2 unextracted.
    if os.environ.get("ANON_SKIP_ROSLYN"):
        log.info("ANON_SKIP_ROSLYN set — skipping C# L2 Roslyn layer (§18.3)")
        return None
    if not helper_available():
        log.info("Roslyn helper unavailable (dotnet on PATH: %s; helper present: %s) — "
                 "skipping L2 facts, L0 build graph carries the run (§18.4)",
                 dotnet_available(), (helper_project_dir() / HELPER_CSPROJ).exists())
        return None

    solution = _find_solution(ws.repo)
    if solution is None:
        log.info("no .sln under %s — no Roslyn L2 facts (§18.4)", ws.repo)
        return None

    dll = _ensure_built(helper_project_dir())
    if dll is None:
        return None  # build failure already logged; degrade

    out = ws.fragments / "roslyn-csharp.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["dotnet", str(dll), str(solution), str(ws.repo), str(out)],
            capture_output=True, text=True, timeout=_RUN_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Roslyn helper run failed to start: %s — degrading (§18.4)", exc)
        return None

    # The helper degrades internally (writes a fragment + warnings, exits 0) rather than
    # hard-failing. A non-zero rc here means an unexpected crash; degrade rather than abort.
    if proc.returncode != 0:
        log.warning("Roslyn helper exited rc=%s — degrading (§18.4):\n%s",
                    proc.returncode, (proc.stderr or "").strip()[-2000:])
        return None
    if not out.exists():
        log.warning("Roslyn helper produced no output at %s — degrading (§18.4)", out)
        return None

    if proc.stderr:
        # Helper logs a one-line summary (and any warnings) to stderr; surface it.
        log.info("Roslyn helper: %s", proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "")
    _lift_filedeps_sidecar(ws, out)
    _lift_apiskeleton_sidecar(ws, out)
    return out


def _lift_filedeps_sidecar(ws: Workspace, fragment_path: Path) -> None:
    """Lift the helper's ``fileDeps`` block into a ``file-deps/1`` sidecar (eval §4.1) and strip
    it from the fact fragment (it is not a fact-model field). This is what gives C# a
    file-granularity dependency graph for ``arch export --graph --granularity file`` — the gap
    that previously made C# project-granularity only. Best-effort: a parse hiccup or a helper
    build that predates ``fileDeps`` leaves the fragment untouched and simply yields no C# file
    graph (graceful, §18.4)."""
    try:
        data = load_json(fragment_path)
    except (OSError, ValueError):
        return
    fd = data.pop("fileDeps", None) if isinstance(data, dict) else None
    if not isinstance(fd, dict):
        return  # old helper build with no fileDeps — nothing to lift, fragment unchanged
    files = {k: v for k, v in (fd.get("files") or {}).items() if isinstance(k, str)}
    edges = {(a, b) for a, b in (fd.get("edges") or [])
             if isinstance(a, str) and isinstance(b, str)}
    if files or edges:
        filedeps.write_sidecar(ws, filedeps.make_sidecar(
            extractor={"name": EXTRACTOR["name"], "version": EXTRACTOR["version"]},
            language="cs", files=files, edges=edges))
    # Rewrite the fragment without the fileDeps block so the fact fragment stays clean.
    dump_json(data, fragment_path)


def _lift_apiskeleton_sidecar(ws: Workspace, fragment_path: Path) -> None:
    """Lift the helper's ``apiSkeleton`` block into an ``api-skeleton/1`` sidecar
    (informative-llm-narratives §2.1 "D") and strip it from the fact fragment — it is an
    LLM-narrative *input*, never a fact-model field, so it stays OUTSIDE the determinism
    hash like ``file-deps`` (§1). Best-effort: a parse hiccup or a helper build that
    predates ``apiSkeleton`` leaves the fragment untouched and simply yields a graph-only
    narrative payload (graceful, §18.4 / plan §11)."""
    try:
        data = load_json(fragment_path)
    except (OSError, ValueError):
        return
    sk = data.pop("apiSkeleton", None) if isinstance(data, dict) else None
    if not isinstance(sk, dict):
        return  # old helper build with no apiSkeleton — nothing to lift, fragment unchanged
    skeletons = {tid: types for tid, types in sk.items()
                 if isinstance(tid, str) and isinstance(types, list)}
    if skeletons:
        apiskeleton.write_sidecar(ws, apiskeleton.make_sidecar(
            extractor={"name": EXTRACTOR["name"], "version": EXTRACTOR["version"]},
            language="cs", skeletons=skeletons))
    # Rewrite the fragment without the apiSkeleton block so the fact fragment stays clean.
    dump_json(data, fragment_path)


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    import argparse

    from ..paths import resolve_workspace

    from ..logutil import setup_console_logging
    setup_console_logging()  # same timestamped console format as the `arch` CLI
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir)
    result = run(w)
    print(result if result else "(degraded: no Roslyn L2 facts)")
