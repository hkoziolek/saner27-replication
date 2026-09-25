"""Eval §10 (RQ7) — performance & scalability: instrumented cold/warm pipeline runs.

For each system, runs the REAL end-to-end pipeline (``arch run --no-llm --incremental``)
twice into a scratch arch dir under ``out/<system>/perf/``:

  * **cold** — fresh arch dir, empty fragment cache (the §10.1 first-run cost, incl. the
    expensive extractors: CMake File-API configure, Doxygen, the Roslyn helper);
  * **warm** — the identical command again (per-extractor FragmentCache hits, §10.1).

Measured per run: total wall-clock, per-stage/per-extractor wall-clock (from the run's own
``run-report.json``), and **peak RSS of the whole process tree** (python + cmake + doxygen
+ dotnet children, sampled via psutil at 200 ms — the P§4.2 Roslyn-memory measure). Size
axes for the §10 scaling curves come from the committed corpus artifacts: KLOC (the
``metrics/1`` per-target counts in curated facts), #targets, #files/#edges (the canonical
graph export's ``graph-meta-*.json``).

Budgets checked (P§1.1 #6): cold ≤ 30 min (L2/L3 profile), warm ≤ 3 min.

Output: ``out/<system>/perf/rq7-perf.json`` (read by ``analysis/aggregate_results.py``).
The artifact records the env knobs and host shape, and carries **no timestamp** — record
run dates in your log, not the artifact.

Usage::

    python baselines/rq7_perf.py                 # all configured systems (itk excluded)
    python baselines/rq7_perf.py toy eshop bash  # a subset
    python baselines/rq7_perf.py itk             # the big one — run it deliberately
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = Path(sys.executable)

# system -> config. `env` = the documented per-system extraction knobs (recorded in the
# artifact). Systems are ordered small -> large; `itk` is opt-in (hours-scale Doxygen).
SYSTEMS: dict[str, dict] = {
    "toy": {"language": "cs", "fixture": "tests/fixtures/toy-repo",
            "rules": "tests/fixtures/toy-repo/architecture/rules"},
    "serilog": {"language": "cs", "fixture": "fixtures/serilog",
                "rules": "overlays/serilog/architecture/rules"},
    "eshop": {"language": "cs", "fixture": "fixtures/eshop",
              "rules": "overlays/eshop/architecture/rules"},
    "libxml2": {"language": "cpp", "fixture": "fixtures/libxml2",
                "rules": "overlays/libxml2/architecture/rules"},
    "bash": {"language": "cpp", "fixture": "fixtures/bash",
             "rules": "overlays/bash/architecture/rules"},
    "fmt": {"language": "cpp", "fixture": "fixtures/fmt",
            "rules": "overlays/fmt/architecture/rules"},
    "nopcommerce": {"language": "cs", "fixture": "fixtures/nopcommerce",
                    "rules": "overlays/nopcommerce/architecture/rules"},
    "orchardcore": {"language": "cs", "fixture": "fixtures/orchardcore",
                    "rules": "overlays/orchardcore/architecture/rules"},
    "terminal": {"language": "cpp", "fixture": "fixtures/terminal",
                 "rules": "overlays/terminal/architecture/rules"},
    "abseil": {"language": "cpp", "fixture": "fixtures/abseil",
               "rules": "overlays/abseil/architecture/rules"},
    "opencv": {"language": "cpp", "fixture": "fixtures/opencv",
               "rules": "overlays/opencv/architecture/rules",
               "env": {"ANON_DOXYGEN_INPUT": "modules"}},
    # ITK is opt-in: the fidelity-run configuration's scoped Doxygen pass is hours-scale.
    # env = the documented onboarding recipe (memory/itk-fidelity-anchor): short external
    # cmake build dir (ITK FATAL_ERRORs on >50-char build paths), Modules-scoped Doxygen
    # with the exclude override (defaults eat Modules/ThirdParty on Windows) and an 8 h
    # timeout, plus the policy floor newer CMakes need for ITK v4.5.2's CMakeLists.
    "itk": {"language": "cpp", "fixture": "fixtures/itk",
            "rules": "overlays/itk/architecture/rules", "optin": True,
            "env": {"ANON_CMAKE_BUILD_DIR": "out/itk/cbp",
                    "ANON_CMAKE_ARGS": "-DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF "
                                            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
                    "ANON_DOXYGEN_INPUT": "Modules",
                    "ANON_DOXYGEN_EXCLUDE": "*/build/* */.cache/* */out/* */.git/*",
                    "ANON_DOXYGEN_TIMEOUT": "28800",
                    "PYTHONUTF8": "1"}},
}

COLD_BUDGET_S = 30 * 60      # P§1.1 #6 cold L2/L3
WARM_BUDGET_S = 3 * 60       # P§1.1 #6 warm


def _measure(cmd: list[str], env: dict[str, str], log_path: Path,
             timeout_s: int) -> dict:
    """Run *cmd*, sampling the process tree's RSS; return wall/peak/rc."""
    try:
        import psutil
    except ImportError:
        psutil = None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    peak = 0
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8", newline="") as log:
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                cwd=str(REPO))
        ps = psutil.Process(proc.pid) if psutil else None
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if time.monotonic() - t0 > timeout_s:
                proc.kill()
                return {"wall_s": round(time.monotonic() - t0, 1), "rc": -1,
                        "peak_rss_mb": round(peak / 2**20, 1) if peak else None,
                        "timed_out": True}
            if ps is not None:
                try:
                    rss = ps.memory_info().rss
                    for child in ps.children(recursive=True):
                        try:
                            rss += child.memory_info().rss
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    peak = max(peak, rss)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            time.sleep(0.2)
    return {"wall_s": round(time.monotonic() - t0, 1), "rc": rc,
            "peak_rss_mb": round(peak / 2**20, 1) if peak else None,
            "timed_out": False}


def _sizes(system: str) -> dict:
    """Size axes from the committed corpus artifacts (fallback: absent -> None)."""
    gen = REPO / "out" / system / "architecture" / "generated"
    out: dict = {"kloc": None, "targets": None, "files": None,
                 "edges_file": None, "edges_target": None}
    facts_p = gen / "curated-facts.json"
    if facts_p.is_file():
        facts = json.loads(facts_p.read_text(encoding="utf-8"))
        fp = [t for t in facts.get("targets", []) if not t.get("external")]
        out["targets"] = len(fp)
        code = sum((t.get("metrics") or {}).get("code", 0) for t in fp)
        out["kloc"] = round(code / 1000, 1) if code else None
    for gran, key in (("file", "edges_file"), ("target", "edges_target")):
        meta_p = gen / "graph" / f"graph-meta-{gran}.json"
        if meta_p.is_file():
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            out[key] = meta.get("graph", {}).get("edges")
            if gran == "file":
                out["files"] = meta.get("graph", {}).get("nodes")
    return out


def run_system(system: str, timeout_s: int, keep_arch: bool = False) -> dict | None:
    cfg = SYSTEMS.get(system)
    if cfg is None:
        print(f"[rq7] unknown system {system!r}", file=sys.stderr)
        return None
    fixture = REPO / cfg["fixture"]
    rules = REPO / cfg["rules"]
    if not fixture.exists():
        print(f"[rq7] {system}: no fixture at {fixture} — skipping", file=sys.stderr)
        return None
    perf_dir = REPO / "out" / system / "perf"
    arch = perf_dir / "_arch"
    if arch.exists():
        shutil.rmtree(arch)

    env = dict(os.environ)
    env.update(cfg.get("env", {}))
    cmd = [str(PY), "-m", "anon.cli", "run", "--repo", str(fixture),
           "--arch-dir", str(arch), "--rules-dir", str(rules), "--no-llm",
           "--incremental"]

    phases: dict[str, dict] = {}
    for phase in ("cold", "warm"):
        print(f"[rq7] {system}: {phase} run ...", file=sys.stderr)
        m = _measure(cmd, env, perf_dir / f"{phase}.log", timeout_s)
        report_p = arch / "generated" / "run-report.json"
        report = json.loads(report_p.read_text(encoding="utf-8")) \
            if report_p.is_file() else {}
        phases[phase] = {**m, "stages": report.get("stages", {}),
                         "substeps": report.get("substeps", {}),
                         "cache": report.get("cache", {})}
        print(f"[rq7] {system}: {phase} {m['wall_s']}s rc={m['rc']} "
              f"peak={m['peak_rss_mb']}MB", file=sys.stderr)
        if m["rc"] != 0:
            break

    cold, warm = phases.get("cold", {}), phases.get("warm", {})
    ok = cold.get("rc") == 0 and warm.get("rc") == 0
    result = {
        "schema": "rq7-perf/1", "system": system, "language": cfg["language"],
        "profile": "full", "success": ok,
        "env": cfg.get("env", {}),
        "host": _host(),
        "size": _sizes(system),
        "cold": cold, "warm": warm,
        "speedup": (round(cold["wall_s"] / warm["wall_s"], 2)
                    if ok and warm.get("wall_s") else None),
        "budget": {"cold_budget_s": COLD_BUDGET_S, "warm_budget_s": WARM_BUDGET_S,
                   "cold_within": ok and cold["wall_s"] <= COLD_BUDGET_S,
                   "warm_within": ok and warm["wall_s"] <= WARM_BUDGET_S},
    }
    perf_dir.mkdir(parents=True, exist_ok=True)
    out_path = perf_dir / "rq7-perf.json"
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="")
    print(f"[rq7] {system}: cold {cold.get('wall_s')}s / warm {warm.get('wall_s')}s "
          f"(speedup {result['speedup']}) -> {out_path}", file=sys.stderr)
    if not keep_arch:
        shutil.rmtree(arch, ignore_errors=True)
    return result


def _host() -> dict:
    try:
        import psutil
        return {"cpus": psutil.cpu_count(logical=True),
                "ram_gb": round(psutil.virtual_memory().total / 2**30)}
    except ImportError:
        return {"cpus": os.cpu_count(), "ram_gb": None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("systems", nargs="*",
                    help="systems to measure (default: all non-opt-in)")
    ap.add_argument("--timeout", type=int, default=4 * 3600,
                    help="per-run timeout in seconds (default 4h)")
    ap.add_argument("--keep-arch", action="store_true")
    args = ap.parse_args(argv)
    systems = args.systems or [s for s, c in SYSTEMS.items() if not c.get("optin")]
    rc = 0
    for s in systems:
        try:
            r = run_system(s, args.timeout, args.keep_arch)
            if r is not None and not r["success"]:
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"[rq7] {s}: FAILED — {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
