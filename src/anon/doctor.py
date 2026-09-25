"""``arch doctor`` — tooling health-check gate (plan §22.2).

Reads ``scripts/tooling.lock`` (a YAML manifest of pinned tool requirements) and checks
each tool against what is installed on the current host.  Never installs anything — it only
*reports*.  The ``arch bootstrap`` command reads the doctor output and surfaces the
per-platform install commands; the §22.2 CI gate exits non-zero when any *required* tool is
missing or stale.

Public API
----------
``load_lock(path)``      — parse tooling.lock into a dict
``detect_platform()``    — "windows" | "linux" | "darwin"
``parse_version(s)``     — lenient numeric tuple from a version string
``check_tool(name, spec)``  — locate + version-probe one tool; returns a status dict
``run(lock_path, platform)``  — check all tools; returns a summary report dict
``render(report)``       — human-readable table + PASS/FAIL footer
``bootstrap_advice(report)`` — which script to run + per-gap install commands
``main(argv)``           — CLI entry point; wired to ``arch doctor`` by the orchestrator

The ``__main__`` shim makes ``python -m anon.doctor`` work during bootstrapping before
the ``arch`` CLI is installed.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Repo-root finder (mirrors the pattern from llm_provider._anon_repo_root,
# but implemented independently per the house rules — do NOT import private helpers)
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Walk up from this module until we find ``pyproject.toml`` (the repo root)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


_DEFAULT_LOCK_PATH = _repo_root() / "scripts" / "tooling.lock"


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def load_lock(path: str | Path) -> dict[str, Any]:
    """Parse *path* (a ``tooling.lock`` YAML file) and return the raw dict.

    Raises ``FileNotFoundError`` if the file does not exist.
    """
    p = Path(path)
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def detect_platform() -> str:
    """Return ``"windows"``, ``"linux"``, or ``"darwin"`` based on ``sys.platform``."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("darwin"):
        return "darwin"
    return "linux"


def parse_version(s: str) -> tuple[int, ...]:
    """Extract a numeric version tuple from *s* (lenient — ignores non-numeric parts).

    Examples::

        parse_version("3.12.1")   -> (3, 12, 1)
        parse_version("v4.2.0+build")  -> (4, 2, 0)
        parse_version("17")       -> (17,)
        parse_version("")         -> ()
    """
    parts = re.findall(r"[0-9]+", s)
    return tuple(int(p) for p in parts) if parts else ()


def _remediation(spec: dict[str, Any], platform: str) -> str | None:
    """Return the platform-appropriate install hint from the spec, or None."""
    if platform == "windows":
        wid = spec.get("winget")
        if wid:
            return f"winget install --id {wid} --exact --silent"
    elif platform == "linux":
        apt = spec.get("apt")
        if apt:
            return f"sudo apt-get install -y {apt}"
    elif platform == "darwin":
        # bootstrap.sh uses brew where available; fall back to apt hint
        apt = spec.get("apt")
        if apt:
            return f"brew install {apt}"
    return None


def check_tool(name: str, spec: dict[str, Any], *, platform: str | None = None) -> dict[str, Any]:
    """Probe a single tool described by *name* and *spec*.

    Steps:
    1. Locate via ``shutil.which(spec.get("which", name))``.
    2. If found, run ``spec["version_cmd"]`` (short timeout, capture, never raises).
    3. Apply ``spec["version_re"]`` to combined stdout+stderr to extract the version.
    4. Compare to ``spec["min_version"]``; produce a status of ``"ok"``, ``"stale"``,
       ``"unknown-version"`` (present but unparseable), or ``"missing"``.

    Returns a dict with keys: ``name``, ``required``, ``status``,
    ``installed`` (str or None), ``min`` (str), ``path`` (str or None),
    ``remediation`` (str or None).
    """
    if platform is None:
        platform = detect_platform()

    required: bool = bool(spec.get("required", False))
    min_ver_str: str = str(spec.get("min_version", "0"))
    which_name: str = spec.get("which", name)
    remedy = _remediation(spec, platform)

    record: dict[str, Any] = {
        "name": name,
        "required": required,
        "status": "missing",
        "installed": None,
        "min": min_ver_str,
        "path": None,
        "remediation": remedy,
    }

    # --- 1. locate ---
    found_path = shutil.which(which_name)
    if found_path is None:
        return record

    record["path"] = found_path

    # --- 2. run version_cmd ---
    version_cmd: list[str] | None = spec.get("version_cmd")
    if not version_cmd:
        record["status"] = "unknown-version"
        return record

    try:
        res = subprocess.run(
            version_cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        combined = (res.stdout or "") + (res.stderr or "")
    except (OSError, subprocess.SubprocessError, FileNotFoundError):
        record["status"] = "unknown-version"
        return record

    # --- 3. extract version ---
    version_re: str | None = spec.get("version_re")
    if not version_re:
        record["status"] = "unknown-version"
        return record

    m = re.search(version_re, combined)
    if not m:
        record["status"] = "unknown-version"
        return record

    installed_str = m.group(1)
    record["installed"] = installed_str

    # --- 4. compare ---
    installed_tuple = parse_version(installed_str)
    min_tuple = parse_version(min_ver_str)

    if not installed_tuple or not min_tuple:
        record["status"] = "unknown-version"
        return record

    # Pad to equal length for comparison
    length = max(len(installed_tuple), len(min_tuple))
    inst_padded = installed_tuple + (0,) * (length - len(installed_tuple))
    min_padded = min_tuple + (0,) * (length - len(min_tuple))

    if inst_padded >= min_padded:
        record["status"] = "ok"
    else:
        record["status"] = "stale"

    return record


# ---------------------------------------------------------------------------
# llm_preflight() — provider/auth AND redaction policy, together (T1.2)
# ---------------------------------------------------------------------------

def llm_preflight(ws: Any | None = None) -> dict[str, Any]:
    """Validate the LLM provider/auth AND the egress redaction policy *together* (T1.2).

    A misconfigured provider paired with an inert redaction policy is the silent
    half-enrichment failure mode this preflight catches BEFORE a live run: either gap on its
    own is a finding, and a clean run needs BOTH a usable provider and a non-inert effective
    policy.

    *ws* is an optional :class:`anon.paths.Workspace`; when supplied, the effective
    redaction policy (user's ``rules/payload-redaction.yaml`` if present, else the bundled
    default) is resolved through it. When ``None``, only the provider/auth half is checked
    and the policy half reports ``"unknown"`` (no workspace to resolve against).

    Returns a finding dict::

        {
            "check": "llm-preflight",
            "provider": {"status": "ok"|"missing-config"|"unusable", "detail": str},
            "redaction": {"status": "ok"|"inert"|"missing"|"unknown",
                          "source": "user"|"default"|None, "detail": str},
            "ok": bool,        # True iff provider usable AND redaction non-inert
            "status": "ok"|"warn",
        }

    Never raises and never makes a network call — pure config resolution (graceful
    degradation: missing SDKs/config => a reported finding, not a crash).
    """
    # --- provider/auth half (reuse the live-provider config resolver) ---
    provider: dict[str, Any] = {"status": "missing-config", "detail": ""}
    try:
        from . import llm_provider
        cfg = llm_provider.load_llm_config()
        if cfg is None:
            provider["detail"] = ("no LLM config — set ANON_LLM_* env vars or create "
                                  "llm.local.yaml (see llm.example.yaml)")
        else:
            usable, why = cfg.usable()
            if usable:
                provider = {"status": "ok",
                            "detail": f"{cfg.client_kind}:{cfg.model_id} @ {cfg.endpoint}"}
            else:
                provider = {"status": "unusable", "detail": why or "config incomplete"}
    except Exception as exc:  # noqa: BLE001 - config probing must never crash doctor
        provider = {"status": "missing-config", "detail": f"config error: {exc}"}

    # --- redaction-policy half (effective policy: user's, else bundled default) ---
    redaction: dict[str, Any] = {"status": "unknown", "source": None, "detail": ""}
    if ws is not None:
        try:
            from .stages import enrich_llm
            cfg2, source = enrich_llm.effective_redaction(ws)
            if enrich_llm.redaction_is_inert(cfg2):
                redaction = {"status": "inert", "source": source,
                             "detail": (f"effective policy ({source}) scrubs nothing — author "
                                        "a non-empty rules/payload-redaction.yaml")}
            else:
                n = len(cfg2.get("secret_patterns") or []) + len(cfg2.get("deny_list") or [])
                redaction = {"status": "ok", "source": source,
                             "detail": f"{n} redaction rule(s) active ({source} policy)"}
        except FileNotFoundError as exc:
            redaction = {"status": "missing", "source": None,
                         "detail": f"no redaction policy available: {exc}"}
        except Exception as exc:  # noqa: BLE001 - policy probing must never crash doctor
            redaction = {"status": "missing", "source": None,
                         "detail": f"redaction policy error: {exc}"}

    ok = provider["status"] == "ok" and redaction["status"] == "ok"
    return {
        "check": "llm-preflight",
        "provider": provider,
        "redaction": redaction,
        "ok": ok,
        "status": "ok" if ok else "warn",
    }


def render_llm_preflight(finding: dict[str, Any]) -> str:
    """Human-readable rendering of an :func:`llm_preflight` finding (advisory; never gates
    the no-llm path)."""
    lines = ["LLM egress preflight (T1.2):"]
    p = finding.get("provider", {})
    r = finding.get("redaction", {})
    lines.append(f"  provider : {p.get('status', '?'):<14} {p.get('detail', '')}")
    lines.append(f"  redaction: {r.get('status', '?'):<14} {r.get('detail', '')}")
    if finding.get("ok"):
        lines.append("  -> OK: a live LLM run is provider-configured AND egress-protected.")
    else:
        lines.append("  -> WARN: fix the gap(s) above before a live --llm-propose / --llm-accepted run.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# run() — check all tools
# ---------------------------------------------------------------------------

def run(
    lock_path: str | Path | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Check every tool in the tooling.lock manifest.

    *lock_path* defaults to ``scripts/tooling.lock`` relative to the repo root.
    *platform* defaults to :func:`detect_platform`.

    Returns::

        {
            "platform": str,
            "tools": [<check_tool result>, ...],
            "missing_required": [name, ...],   # required tools that are missing or stale
            "ok": bool,                         # False iff any required tool is missing/stale
        }
    """
    if lock_path is None:
        lock_path = _DEFAULT_LOCK_PATH
    if platform is None:
        platform = detect_platform()

    lock = load_lock(lock_path)
    results: list[dict[str, Any]] = []
    missing_required: list[str] = []

    for tool_name, spec in lock.items():
        if not isinstance(spec, dict):
            continue
        result = check_tool(tool_name, spec, platform=platform)
        results.append(result)
        if result["required"] and result["status"] in ("missing", "stale"):
            missing_required.append(tool_name)

    return {
        "platform": platform,
        "tools": results,
        "missing_required": missing_required,
        "ok": len(missing_required) == 0,
    }


# ---------------------------------------------------------------------------
# render() — human-readable table
# ---------------------------------------------------------------------------

_STATUS_LABEL = {
    "ok":              "ok            ",
    "missing":         "MISSING       ",
    "stale":           "STALE         ",
    "unknown-version": "unknown-ver   ",
}

_COL_WIDTHS = {
    "name":        16,
    "required":     8,
    "status":      14,
    "installed":   12,
    "min":         10,
    "remediation": 50,
}


def render(report: dict[str, Any]) -> str:
    """Render *report* (from :func:`run`) as a human-readable table with PASS/FAIL footer."""
    lines: list[str] = []

    header = (
        f"{'tool':<{_COL_WIDTHS['name']}}"
        f"{'req':<{_COL_WIDTHS['required']}}"
        f"{'status':<{_COL_WIDTHS['status']}}"
        f"{'installed':<{_COL_WIDTHS['installed']}}"
        f"{'min':<{_COL_WIDTHS['min']}}"
        f"remediation"
    )
    separator = "-" * len(header)
    lines.append(f"arch doctor — platform: {report.get('platform', '?')}")
    lines.append(separator)
    lines.append(header)
    lines.append(separator)

    for t in report.get("tools", []):
        name = t["name"][:_COL_WIDTHS["name"] - 1]
        req = "yes" if t["required"] else "no"
        raw_status = t.get("status", "missing")
        status = _STATUS_LABEL.get(raw_status, raw_status)[:_COL_WIDTHS["status"] - 1]
        installed = (t.get("installed") or "-")[:_COL_WIDTHS["installed"] - 1]
        min_ver = (t.get("min") or "-")[:_COL_WIDTHS["min"] - 1]
        remedy = t.get("remediation") or "-"

        lines.append(
            f"{name:<{_COL_WIDTHS['name']}}"
            f"{req:<{_COL_WIDTHS['required']}}"
            f"{status:<{_COL_WIDTHS['status']}}"
            f"{installed:<{_COL_WIDTHS['installed']}}"
            f"{min_ver:<{_COL_WIDTHS['min']}}"
            f"{remedy}"
        )

    lines.append(separator)
    missing = report.get("missing_required", [])
    if report.get("ok"):
        lines.append("PASS — all required tools present and at or above minimum version.")
    else:
        lines.append(
            f"FAIL — {len(missing)} required tool(s) missing or stale: "
            + ", ".join(missing)
        )
        lines.append(
            "Run `arch bootstrap` (or `python -m anon.doctor --bootstrap`) "
            "for platform-specific install commands."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# bootstrap_advice() — surfaces the platform bootstrap script + per-gap commands
# ---------------------------------------------------------------------------

def bootstrap_advice(report: dict[str, Any]) -> str:
    """Return a human-readable string: which bootstrap script to run + per-gap install steps.

    Called by ``arch bootstrap``; doctor itself never installs anything (§22.2 boundary).
    """
    platform = report.get("platform", detect_platform())
    lines: list[str] = []

    if platform == "windows":
        script = "pwsh -File scripts/bootstrap.ps1"
    else:
        script = "bash scripts/bootstrap.sh"

    lines.append(f"Bootstrap script for this platform ({platform}):")
    lines.append(f"  {script}")
    lines.append("")

    gaps = [t for t in report.get("tools", []) if t.get("status") in ("missing", "stale")]
    if not gaps:
        lines.append("No gaps detected — all tools are present and up-to-date.")
        return "\n".join(lines)

    lines.append("Per-tool install commands for gaps found:")
    for t in gaps:
        remedy = t.get("remediation")
        status = t.get("status", "missing")
        installed = t.get("installed")
        min_ver = t.get("min")
        note = ""
        if status == "stale" and installed:
            note = f"  (installed {installed}, need >= {min_ver})"
        req_flag = " [required]" if t["required"] else " [optional]"
        lines.append(f"  {t['name']}{req_flag}{note}:")
        if remedy:
            lines.append(f"    {remedy}")
        else:
            lines.append(f"    (no automated install command — see project docs)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``arch doctor`` / ``python -m anon.doctor``.

    Exits non-zero when any required tool is missing or stale (§22.2 CI gate).
    """
    ap = argparse.ArgumentParser(
        prog="anon.doctor",
        description=__doc__,
    )
    ap.add_argument(
        "--lock",
        default=None,
        help="path to tooling.lock (default: scripts/tooling.lock relative to repo root)",
    )
    ap.add_argument(
        "--platform",
        choices=("windows", "linux", "darwin"),
        default=None,
        help="override platform detection (for testing / cross-platform dry-runs)",
    )
    ap.add_argument(
        "--bootstrap",
        action="store_true",
        help="print bootstrap advice (platform script + per-gap install commands) and exit",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit the raw report dict as JSON (for machine consumption / CI)",
    )
    args = ap.parse_args(argv)

    report = run(lock_path=args.lock, platform=args.platform)

    if args.json:
        import json
        print(json.dumps(report, indent=2))
    elif args.bootstrap:
        print(render(report))
        print()
        print(bootstrap_advice(report))
    else:
        print(render(report))

    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
