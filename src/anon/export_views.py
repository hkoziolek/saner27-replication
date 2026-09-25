"""``arch export`` — static text export of curated views (plan §10.3).

Every other consumption path routes through an interactive Structurizr Local browser
session. This adds the automated static-export path so passive stakeholders can embed
diagrams in a wiki/report/slide: shell out to the **Structurizr CLI**
``export -workspace <workspace.dsl> -format plantuml|mermaid|dot`` into the machine-owned
``generated/views/`` folder (§12), scoped to the curated default views.

**Two-step honesty (§10.3).** The Structurizr CLI exports to PlantUML/Mermaid/DOT *text*
(step 1, :func:`run`); raster/SVG comes from rendering that text **on-prem** (step 2,
:func:`render`) with a local PlantUML jar / mermaid-cli / self-hosted Kroki / Graphviz
``dot``. Both steps **degrade gracefully** (§18.4): absent render tooling logs+skips, it
never fails the run. The text export is the always-available deliverable; the SVG render
is opt-in (``run(..., render=True)``) and best-effort on top of it.

**SVGs are CI artifacts, never golden.** SVG bytes emitted by external renderers
(PlantUML/Graphviz/mermaid-cli versions, embedded fonts/timestamps) are **not**
byte-stable, so the rendered ``views/*.svg`` live in the machine-owned, gitignored
``generated/views/`` artifact area (§12) and are **never** golden-compared. Only the
deterministic fact model and DSL fragments carry the byte-identical guarantee.

**Graceful degradation (§18.4).** If the Structurizr CLI or Java is not on the toolchain
(the common case on a fresh checkout / a CI agent without the optional render tooling),
this **logs and skips — it never fails the run**. The export path is opt-in and additive,
exactly like the §4.4 deployment extractors: absent tooling yields no views, the rest of
the pipeline is unaffected.

CLI wiring: ``cli.py`` currently stubs ``arch export`` (task #5). The orchestrator should
point that subcommand at :func:`run`; until then this is runnable as
``python -m anon.export_views`` (see the ``__main__`` shim).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .paths import Workspace, resolve_workspace

# Text formats the Structurizr CLI can export (§10.3). plantuml is the default — the §6.1
# PlantUML/Mermaid generators are the toolchain-independent fallback route.
SUPPORTED_FORMATS = ("plantuml", "mermaid", "dot")

# The text-export file extensions the Structurizr CLI writes per format (§10.3). The render
# step (:func:`render`) globs the matching extension to find what step 1 produced.
_FORMAT_EXT = {"plantuml": ".puml", "mermaid": ".mmd", "dot": ".dot"}

# Self-hosted Kroki endpoint (§10.3/§16 no-cloud): a render fallback usable for ANY format.
# It is read from this env var and only used when explicitly set — we NEVER hit the public
# kroki.io by default, to honor the on-prem-only decision.
_KROKI_ENV = "ANON_KROKI_URL"

# Kroki's diagram-type slug per export format (POST <base>/<type>/svg with the diagram text).
_KROKI_TYPE = {"plantuml": "plantuml", "mermaid": "mermaid", "dot": "graphviz"}

# The CLI launcher differs by OS (§22.3): structurizr.sh on Unix, structurizr.bat on
# Windows. We try, in order, whatever is on PATH; the orchestrator may also route through
# the structurizr/cli Docker image (which removes the host-Java/launcher difference).
_LAUNCHER_CANDIDATES = ("structurizr.sh", "structurizr.bat", "structurizr")

# Step-2 render note (§10.3). Rendering now EXISTS (:func:`render`) but is still honest
# about prerequisites: it needs an on-prem renderer (PlantUML jar+Java+Graphviz / mermaid-cli
# / self-hosted Kroki / Graphviz dot). When none is present it logs+skips — the text export
# (step 1) remains the always-available deliverable.
RASTER_TODO = (
    "Raster/SVG rendering (§10.3 step 2) is available via `render()` / `run(render=True)`: "
    "the Structurizr CLI exports PlantUML/Mermaid/DOT *text*, then an on-prem renderer "
    "(local PlantUML jar+Java+Graphviz / mermaid-cli / self-hosted Kroki / Graphviz dot) "
    "rasterizes it to SVG. It degrades gracefully — absent render tooling logs+skips and the "
    "text export still ships."
)


def _find_launcher() -> str | None:
    """Locate a Structurizr CLI launcher on PATH (§22.3), or None if absent."""
    for name in _LAUNCHER_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _java_present() -> bool:
    """True iff a Java runtime is on PATH (the CLI needs Java 17+, §22.3)."""
    return shutil.which("java") is not None


def _find_plantuml() -> list[str] | None:
    """Resolve a PlantUML invocation prefix (§10.3 step 2), or None if unavailable.

    Tries, in order: a ``plantuml`` launcher on PATH (returns ``["plantuml"]``); else a
    ``plantuml.jar`` (env ``PLANTUML_JAR`` or CWD) driven through ``java -jar`` — which also
    needs Java on PATH. Returns the argv prefix to which ``-tsvg <file>`` is appended, or None.
    PlantUML additionally needs Graphviz at *render* time for non-sequence diagrams; that is a
    runtime prerequisite we surface in the skip reason, not something we can fully precheck.
    """
    launcher = shutil.which("plantuml")
    if launcher:
        return [launcher]
    jar = os.environ.get("PLANTUML_JAR")
    candidate = Path(jar) if jar else (Path.cwd() / "plantuml.jar")
    if candidate.is_file() and _java_present():
        return [shutil.which("java") or "java", "-jar", str(candidate)]
    return None


def _find_mmdc() -> str | None:
    """Locate the mermaid-cli ``mmdc`` launcher on PATH (§10.3 step 2), or None if absent."""
    return shutil.which("mmdc")


def _find_dot() -> str | None:
    """Locate the Graphviz ``dot`` launcher on PATH (§10.3 step 2), or None if absent."""
    return shutil.which("dot")


def _kroki_url() -> str | None:
    """The self-hosted Kroki base URL from the env (§10.3), or None when unset/blank.

    On-prem only: we use Kroki ONLY when the operator explicitly points this at their own
    instance — there is no public-endpoint default (§16 no-cloud).
    """
    url = (os.environ.get(_KROKI_ENV) or "").strip()
    return url.rstrip("/") if url else None


def validate_workspace(ws: Workspace, *, log=None) -> dict:
    """Run the Structurizr CLI ``validate`` on ``workspace.dsl`` (plan §10 gate 4, §14 P3).

    Returns ``{"validated": bool, "skipped": bool, "reason": str|None}``. Like
    :func:`run`, this **degrades gracefully** (§18.4): a missing CLI/Java/workspace is a
    skip (``validated`` False, ``skipped`` True), not a failure — so a host without the
    Structurizr CLI still completes the run. A genuine validation FAILURE (the CLI runs and
    rejects the DSL) returns ``validated=False, skipped=False`` so a caller/CI can gate on it.
    """
    if log is None:
        def log(msg: str) -> None:
            print(msg, file=sys.stderr)
    report = {"validated": False, "skipped": True, "reason": None}
    if not ws.workspace_dsl.exists():
        report["reason"] = f"no workspace.dsl at {ws.workspace_dsl} (run `arch generate` first)"
        log(f"[validate] SKIP — {report['reason']}.")
        return report
    launcher = _find_launcher()
    if launcher is None or not _java_present():
        report["reason"] = ("Structurizr CLI / Java not on PATH — DSL validation skipped "
                            "(facts were still schema+integrity validated; §22.3)")
        log(f"[validate] SKIP — {report['reason']}.")
        return report
    cmd = [launcher, "validate", "-workspace", str(ws.workspace_dsl)]
    log(f"[validate] {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        report["reason"] = f"Structurizr CLI invocation failed: {exc}"
        log(f"[validate] SKIP — {report['reason']}.")
        return report
    report["skipped"] = False
    if res.returncode == 0:
        report["validated"] = True
        log("[validate] workspace.dsl OK")
    else:
        report["reason"] = f"Structurizr validate failed ({res.returncode}): {res.stderr.strip()[:300]}"
        log(f"[validate] FAIL — {report['reason']}")
    return report


def _run_renderer(cmd: list[str], *, log) -> tuple[bool, str | None]:
    """Invoke a renderer subprocess; return ``(ok, reason)`` (§18.4 graceful).

    Never raises: a missing binary / non-zero exit / timeout becomes ``(False, reason)`` so
    one failed view never breaks the run.
    """
    log(f"[render]   {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"renderer invocation failed: {exc}"
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()[:200]
        return False, f"renderer exited {res.returncode}: {detail}"
    return True, None


def _kroki_render(base: str, fmt: str, src: Path, dst: Path, *, log) -> tuple[bool, str | None]:
    """POST ``src`` text to a self-hosted Kroki (``base``) and write the SVG to ``dst`` (§10.3).

    On-prem only and stdlib-only (``urllib``). Never raises — any HTTP/URL error degrades to
    ``(False, reason)``.
    """
    diagram_type = _KROKI_TYPE.get(fmt, fmt)
    url = f"{base}/{diagram_type}/svg"
    log(f"[render]   POST {url} <- {src.name}")
    try:
        payload = src.read_text(encoding="utf-8").encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "text/plain"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 (operator-supplied on-prem URL)
            svg = resp.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"Kroki render failed: {exc}"
    dst.write_bytes(svg)
    return True, None


def render(out_dir: Path, fmt: str, *, log=None) -> dict:
    """Render the text views produced by :func:`run` into ``out_dir/*.svg`` on-prem (§10.3 step 2).

    This is the **second** of the two-step export (§10.3): step 1 (:func:`run`) shells out to
    the Structurizr CLI to write PlantUML/Mermaid/DOT *text*; this step rasterizes that text to
    SVG using whichever on-prem renderer is present — tried in order, first available wins:

    * ``fmt == "plantuml"`` → a ``plantuml`` launcher / ``plantuml.jar`` (``java -jar``),
      ``plantuml -tsvg <file>.puml`` (needs Java + Graphviz at render time);
    * ``fmt == "mermaid"`` → ``mmdc -i <file>.mmd -o <file>.svg`` (mermaid-cli);
    * a self-hosted **Kroki** (env ``ANON_KROKI_URL``) — any format, on-prem only,
      stdlib ``urllib``, never a public endpoint by default (§16);
    * ``fmt == "dot"`` → Graphviz ``dot -Tsvg <file>.dot -o <file>.svg``.

    **Graceful degradation (§18.4):** when no suitable renderer is present this **logs and
    skips** — it never raises and never fails the run. Per-view render failures are likewise
    non-fatal (logged, the others still proceed).

    **Determinism caveat (§10.3/§12):** external renderers do NOT produce byte-stable SVG, so
    the outputs live under the gitignored/machine-owned ``generated/views/`` artifact area and
    are treated as **CI artifacts — never golden-compared**.

    Returns ``{"rendered": bool, "format": fmt, "svgs": [str, ...], "reason": str|None}``.
    ``rendered`` is True iff at least one SVG was written.
    """
    if log is None:
        def log(msg: str) -> None:
            print(msg, file=sys.stderr)

    report: dict = {"rendered": False, "format": fmt, "svgs": [], "reason": None}

    if fmt not in SUPPORTED_FORMATS:
        report["reason"] = f"unsupported format {fmt!r}; choose one of {SUPPORTED_FORMATS}"
        log(f"[render] SKIP — {report['reason']}.")
        return report

    out_dir = Path(out_dir)
    ext = _FORMAT_EXT[fmt]
    # Deterministic discovery order so logs/behavior are stable across runs (§5 determinism).
    sources = sorted(p for p in out_dir.glob(f"*{ext}") if p.is_file())
    if not sources:
        report["reason"] = (f"no {ext} text views in {out_dir} — run the text export "
                            "(`run(...)`) first")
        log(f"[render] SKIP — {report['reason']}.")
        return report

    # Pick a renderer (first available wins). PlantUML/mermaid/dot are format-specific;
    # Kroki is the format-agnostic on-prem fallback.
    plantuml = _find_plantuml() if fmt == "plantuml" else None
    mmdc = _find_mmdc() if fmt == "mermaid" else None
    dot = _find_dot() if fmt == "dot" else None
    kroki = _kroki_url()

    if not (plantuml or mmdc or dot or kroki):
        report["reason"] = (
            f"no on-prem renderer for {fmt!r}: install PlantUML (jar+Java+Graphviz) / "
            f"mermaid-cli (`mmdc`) / Graphviz (`dot`), or set {_KROKI_ENV} to a self-hosted "
            "Kroki — rendering skipped (text export is unaffected, §18.4)"
        )
        log(f"[render] SKIP — {report['reason']}.")
        return report

    if plantuml:
        renderer = f"plantuml ({' '.join(plantuml)})"
    elif mmdc:
        renderer = f"mermaid-cli ({mmdc})"
    elif dot:
        renderer = f"dot ({dot})"
    else:
        renderer = f"kroki ({kroki})"
    log(f"[render] {len(sources)} {fmt} view(s) -> SVG via {renderer}")

    svgs: list[str] = []
    failures: list[str] = []
    for src in sources:
        dst = src.with_suffix(".svg")
        if plantuml:
            # PlantUML writes <stem>.svg alongside the source for -tsvg.
            ok, reason = _run_renderer([*plantuml, "-tsvg", str(src)], log=log)
        elif mmdc:
            ok, reason = _run_renderer([mmdc, "-i", str(src), "-o", str(dst)], log=log)
        elif dot:
            ok, reason = _run_renderer([dot, "-Tsvg", str(src), "-o", str(dst)], log=log)
        else:
            ok, reason = _kroki_render(kroki, fmt, src, dst, log=log)
        if ok and dst.exists():
            svgs.append(str(dst))
        else:
            failures.append(f"{src.name}: {reason or 'no SVG produced'}")
            log(f"[render]   FAIL — {src.name}: {reason or 'no SVG produced'}")

    report["svgs"] = svgs
    report["rendered"] = bool(svgs)
    if failures and not svgs:
        report["reason"] = "all renders failed: " + "; ".join(failures[:3])
    elif failures:
        report["reason"] = f"{len(failures)} of {len(sources)} view(s) failed to render"
    if svgs:
        log(f"[render] wrote {len(svgs)} SVG(s) -> {out_dir}")
    return report


# Module-level handle to :func:`render` so :func:`run`'s ``render=`` keyword (which shadows the
# name inside the function body) can still invoke the renderer (§10.3 step 2).
_render = render


def run(ws: Workspace, fmt: str = "plantuml", *, render: bool = False, log=None) -> dict:
    """Export curated views from ``workspace.dsl`` into ``generated/views/`` (§10.3).

    Returns a report dict ``{"exported": bool, "format": fmt, "out_dir": str,
    "reason": str|None}``; when ``render`` is True it also carries a ``"render"`` sub-report
    (the :func:`render` result, §10.3 step 2). When the CLI/Java/workspace is missing,
    ``exported`` is False and ``reason`` explains why — **the function never raises for missing
    tooling** (graceful degrade, §18.4). A genuine non-zero CLI exit is also captured as a skip
    reason rather than propagated, so an export problem never breaks ``arch run``.

    ``render`` (default ``False``, opt-in) rasterizes the exported text to ``views/*.svg`` via
    :func:`render` **after** a successful text export — likewise best-effort and never fatal.
    The default keeps existing callers and any golden output byte-identical.
    """
    if log is None:
        def log(msg: str) -> None:
            print(msg, file=sys.stderr)

    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported export format {fmt!r}; choose one of {SUPPORTED_FORMATS}")

    out_dir = ws.views_export
    report = {"exported": False, "format": fmt, "out_dir": str(out_dir), "reason": None}

    if not ws.workspace_dsl.exists():
        report["reason"] = f"no workspace.dsl at {ws.workspace_dsl} (run `arch run`/`arch generate` first)"
        log(f"[export] SKIP — {report['reason']}.")
        return report

    launcher = _find_launcher()
    if launcher is None:
        report["reason"] = ("Structurizr CLI not found on PATH (structurizr.sh/.bat) — "
                            "install it or use the structurizr/cli Docker image (§22.3)")
        log(f"[export] SKIP — {report['reason']}. {RASTER_TODO}")
        return report
    if not _java_present():
        report["reason"] = "Java runtime not found on PATH (the Structurizr CLI needs Java 17+, §22.3)"
        log(f"[export] SKIP — {report['reason']}.")
        return report

    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [launcher, "export",
           "-workspace", str(ws.workspace_dsl),
           "-format", fmt,
           "-output", str(out_dir)]
    log(f"[export] {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        report["reason"] = f"Structurizr CLI invocation failed: {exc}"
        log(f"[export] SKIP — {report['reason']}.")
        return report

    if res.returncode != 0:
        report["reason"] = f"Structurizr CLI exited {res.returncode}: {res.stderr.strip()[:200]}"
        log(f"[export] SKIP — {report['reason']}.")
        return report

    report["exported"] = True
    log(f"[export] wrote {fmt} views -> {out_dir}. {RASTER_TODO}")

    if render:
        # §10.3 step 2 (opt-in): rasterize the just-exported text to SVG, on-prem and
        # best-effort. Never fatal — a missing renderer degrades to a logged skip (§18.4).
        report["render"] = _render(out_dir, fmt, log=log)

    return report


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="anon.export_views", description=__doc__)
    ap.add_argument("--repo", required=True, help="target working tree")
    ap.add_argument("--arch-dir", help="artifact dir (default <repo>/architecture)")
    ap.add_argument("--format", default="plantuml", choices=SUPPORTED_FORMATS)
    ap.add_argument("--render", action="store_true",
                    help="also rasterize the exported text to views/*.svg on-prem (§10.3 step 2)")
    args = ap.parse_args(argv)
    ws = resolve_workspace(args.repo, args.arch_dir)
    report = run(ws, args.format, render=args.render)
    # exit 0 even on a degraded skip — export is additive and never fails the pipeline (§18.4).
    rendered = report.get("render", {}).get("rendered") if "render" in report else None
    print(f"[export] exported={report['exported']} format={report['format']} "
          f"out={report['out_dir']}"
          + (f" rendered={rendered}" if rendered is not None else "")
          + (f" reason={report['reason']}" if report["reason"] else ""))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
