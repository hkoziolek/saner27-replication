"""Eval §14.1 — portable baseline bundle for the air-gapped Tier-P ([anonymized]) workspaces.

Packages everything the SAR baselines need into ONE self-contained zip so they run fully
OFFLINE on a restricted machine (the only network access in the whole baseline stack is the
one-time ARCADE jar download, which this script performs on the connected side)::

    python baselines/make_bundle.py [--out out/tier-p/anon-baseline-kit.zip]
        [--fetch] [--without-jar]

Bundle layout (mirrors the repo layout so run_arcade.py works UNCHANGED — its REPO_ROOT
resolves to the bundle root)::

    anon-baseline-kit/
      README.md                        # generated quickstart
      BUNDLE-MANIFEST.json             # kit/jar versions, git commit, per-file sha256
      sha256sums.txt                   # verify after transfer
      baselines/run_arcade.py
      baselines/arc/{ArcRunner,StructuralRunner}.java (+ .class when compilable here)
      tools/arcade/ARCADE_Core.jar     # pinned v1.2.0 (omit via --without-jar)
      analysis/anonymize_results.py    # the §14.3 export channel (also runs [anonymized]-side)
      docs/tier-p-baseline-kit.md      # the guide
      smoke/architecture/generated/graph/graph-file.rsf   # offline self-test graph
      references/smoke/reference.rsf                      # exercises MoJoFM/a2a end-to-end

[anonymized]-side prerequisites: Python 3.10+, Java 17 (JDK if the .class files must be rebuilt).
Deterministic: fixed zip timestamps + sorted entries — same inputs => byte-identical zip.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT_NAME = "anon-baseline-kit"
KIT_VERSION = "1.1"
ARCADE_VERSION = "v1.2.0"
ARCADE_JAR = REPO_ROOT / "tools" / "arcade" / "ARCADE_Core.jar"
ARCADE_URL = ("https://github.com/usc-softarch/ARCADE_Core/releases/download/"
              "v1.2.0/ARCADE_Core.jar")   # pinned — must match baselines/run_arcade.py
ZIP_DATE = (2026, 1, 1, 0, 0, 0)          # fixed timestamp => deterministic archive

REPO_FILES = [
    "baselines/run_arcade.py",
    "baselines/make_comparison.py",       # RQ1/RQ2 all-common comparison-<gran>.json writer
    "baselines/determinism_check.py",     # RQ5 double-run check -> determinism.json
    "baselines/no_curation_floor.py",     # RQ1 floor (stdout companion to the a2 rung `none`)
    "baselines/a2_ablation.py",           # a2-ablation.json writer (floor + curation rungs)
    "baselines/stability_churn.py",       # optional A8 churn -> stability-churn.json
    "baselines/rq7_perf.py",              # RQ7 §10 cold/warm perf -> perf/rq7-perf.json
    "baselines/arc/ArcRunner.java",
    "baselines/arc/StructuralRunner.java",
    "analysis/anonymize_results.py",
    "docs/tier-p-baseline-kit.md",
    "docs/tier-p-org-runbook.md",
]
OPTIONAL_CLASSES = ["baselines/arc/ArcRunner.class", "baselines/arc/StructuralRunner.class"]

# Tiny deterministic self-test fixture: two directory-aligned clusters + one cross edge.
# `dir` recovers the reference exactly; ACDC and the java metric path (mojo.MoJo/SystemEvo)
# get exercised end-to-end with no network and no real repo.
SMOKE_GRAPH = """\
depend core/alpha.c core/beta.c
depend core/beta.c core/gamma.c
depend core/gamma.c core/alpha.c
depend ui/window.c ui/widget.c
depend ui/widget.c ui/render.c
depend ui/render.c core/alpha.c
"""
SMOKE_REFERENCE = """\
contain core core/alpha.c
contain core core/beta.c
contain core core/gamma.c
contain ui ui/render.c
contain ui ui/widget.c
contain ui ui/window.c
"""

README = f"""\
# {KIT_NAME} v{KIT_VERSION}

Offline SAR-baseline kit for Tier-P systems (eval plan §5.5/§14.1). Everything here is
LOCAL JVM/Python computation — zero network egress at run time.

## Verify the transfer

    python -c "import hashlib,sys;\\
    [print(l.split()[0]==hashlib.sha256(open(l.split(None,1)[1].strip(),'rb').read()).hexdigest(), l.split(None,1)[1].strip()) for l in open('sha256sums.txt')]"

(or `sha256sum -c sha256sums.txt` where coreutils exist). All lines must be True/OK.

## Smoke test (no repo needed)

    python baselines/run_arcade.py smoke --granularity file --techniques acdc,dir,cc \\
        --arch-dir smoke/architecture

Expected: three `[<tech>-file] ... MoJoFM=... a2a=... vs reference` lines, exit 0; `dir`
scores MoJoFM=100.0 (the smoke graph is directory-aligned by construction). Outputs land in
`out/smoke/baseline/`.

## Real runs (per system)

1. Export the canonical graph from the Anon workspace already on this machine:
       arch export --graph --granularity <file|target> --repo <repo> --arch-dir <arch-dir>
2. Copy/point the export at `out/<system>/architecture/generated/graph/` (or pass
   `--arch-dir`), put the GT-diag reference at `references/<system>/reference.rsf`, then:
       python baselines/run_arcade.py <system> --granularity <g> --techniques acdc,arc,wca,limbo,dir,cc
   Notes: `arc` additionally needs the source clone (`--src-root`) and reruns >=5x (unseeded
   LDA — report mean +/- range); `arc/wca/limbo` need k (defaults to the reference cluster
   count); rebuilding the .class files needs a JDK (`javac`), a JRE is enough otherwise.
3. Consolidate the all-common RQ1/RQ2 comparison (reads the pipeline + baseline clusters):
       python baselines/make_comparison.py <system> --granularity <g>
   For the RQ1 floor + curation rungs (a2-ablation.json) and the RQ5 double-run check, use
   the venv where `arch` is installed (they import the anon package):
       python baselines/a2_ablation.py <system>        # register the system in SYSTEMS first
       python baselines/determinism_check.py <arch-dir-1> <arch-dir-2> --system <system>
4. Anonymize for export (§14.3) — the ONLY artifacts that may leave this machine:
       python analysis/anonymize_results.py --map tier-p-map.json --export-dir export-anon
   then run the sign-off checklist in docs/tier-p-baseline-kit.md; the full per-system
   ordering (GT-diag protocol, waves, deadlines) is docs/tier-p-org-runbook.md.

Jar provenance: ARCADE_Core.jar {ARCADE_VERSION}, {ARCADE_URL}
(sha256 in BUNDLE-MANIFEST.json — the jar is redistributed unmodified).
"""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                              check=True, capture_output=True, text=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def _try_compile_arc() -> None:
    """Best-effort .class refresh so the [anonymized] side needs only a JRE for arc/wca/limbo."""
    arc = REPO_ROOT / "baselines" / "arc"
    srcs = [arc / "ArcRunner.java", arc / "StructuralRunner.java"]
    classes = [arc / "ArcRunner.class", arc / "StructuralRunner.class"]
    if not ARCADE_JAR.exists():
        return
    if all(c.exists() for c in classes) and \
            min(c.stat().st_mtime for c in classes) >= max(s.stat().st_mtime for s in srcs):
        return
    try:
        subprocess.run(["javac", "-cp", str(ARCADE_JAR), "-d", str(arc), *map(str, srcs)],
                       check=True, capture_output=True, text=True)
        print("[bundle] compiled arc drivers (.class included)", file=sys.stderr)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[bundle] WARNING: javac unavailable/failed ({exc}) — .java sources ship, "
              f"[anonymized] side needs a JDK for arc/wca/limbo.", file=sys.stderr)


def collect_entries(without_jar: bool) -> dict[str, bytes]:
    """arcname -> content. Everything the zip will contain, generated files included."""
    entries: dict[str, bytes] = {}
    for rel in REPO_FILES:
        p = REPO_ROOT / rel
        if not p.exists():
            raise FileNotFoundError(f"required bundle input missing: {p}")
        entries[rel] = p.read_bytes()
    for rel in OPTIONAL_CLASSES:
        p = REPO_ROOT / rel
        if p.exists():
            entries[rel] = p.read_bytes()
    if not without_jar:
        entries["tools/arcade/ARCADE_Core.jar"] = ARCADE_JAR.read_bytes()
    entries["smoke/architecture/generated/graph/graph-file.rsf"] = SMOKE_GRAPH.encode()
    entries["references/smoke/reference.rsf"] = SMOKE_REFERENCE.encode()
    readme = README if not without_jar else README + (
        "\nNOTE: built with --without-jar — download the pinned jar to tools/arcade/ "
        "on a connected machine first:\n    curl -sL -o tools/arcade/ARCADE_Core.jar "
        + ARCADE_URL + "\n")
    entries["README.md"] = readme.encode()
    return entries


def build(out_zip: Path, *, without_jar: bool = False) -> Path:
    entries = collect_entries(without_jar)
    manifest = {
        "kit": KIT_NAME,
        "kit_version": KIT_VERSION,
        "arcade_version": ARCADE_VERSION,
        "arcade_url": ARCADE_URL,
        "git_commit": _git_commit(),
        "with_jar": not without_jar,
        "files": {name: {"sha256": sha256(data), "bytes": len(data)}
                  for name, data in sorted(entries.items())},
    }
    entries["BUNDLE-MANIFEST.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    entries["sha256sums.txt"] = "".join(
        f"{sha256(data)}  {name}\n" for name, data in sorted(entries.items())
        if name != "sha256sums.txt").encode()

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            info = zipfile.ZipInfo(f"{KIT_NAME}/{name}", date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, entries[name])
    return out_zip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path,
                    default=REPO_ROOT / "out" / "tier-p" / f"{KIT_NAME}.zip")
    ap.add_argument("--fetch", action="store_true",
                    help="download the pinned ARCADE jar if missing (connected side only)")
    ap.add_argument("--without-jar", action="store_true",
                    help="package without the jar (CI/testing, or the target already has it)")
    args = ap.parse_args(argv)

    if not args.without_jar and not ARCADE_JAR.exists():
        if args.fetch:
            print(f"[bundle] fetching {ARCADE_URL}", file=sys.stderr)
            ARCADE_JAR.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(ARCADE_URL, ARCADE_JAR)  # noqa: S310 — pinned https URL
        else:
            print(f"ERROR: {ARCADE_JAR} missing. Re-run with --fetch, or download:\n"
                  f"  curl -sL -o \"{ARCADE_JAR}\" {ARCADE_URL}\n"
                  f"(or use --without-jar to package without it)", file=sys.stderr)
            return 2

    _try_compile_arc()
    out = build(args.out, without_jar=args.without_jar)
    size_mb = out.stat().st_size / 1e6
    print(f"[bundle] wrote {out} ({size_mb:.1f} MB, jar={'no' if args.without_jar else 'yes'})")
    print("[bundle] transfer the zip + verify sha256sums.txt on the target "
          "(see docs/tier-p-baseline-kit.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
