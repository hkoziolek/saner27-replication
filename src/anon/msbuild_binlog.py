"""Reconstruct a ``compile_commands.json`` from an MSBuild **binary log** (plan §4.1 / §16#1).

MSBuild/VS C++ emits no compile DB; the plan's L1 route is ``msbuild /bl`` → parse the
``cl.exe`` invocations. Rather than reimplement the versioned binlog *binary* format, this
**replays** the binlog through MSBuild itself (``MSBuild.exe <file>.binlog`` re-emits the
recorded events to a logger — it does NOT re-run the build, so it's fast and side-effect-free)
at diagnostic verbosity, then scrapes the recorded ``cl.exe`` command lines for their ``/I``
include dirs, ``/D`` defines, and source files. The result is a standard compile DB the clang
L2 scan (:mod:`extract_clang_deps`) can consume.

:func:`parse_cl_invocations` is split out and pure so it is unit-testable against a captured
log snippet without MSBuild; :func:`compile_db_from_binlog` is the gated end-to-end entry that
needs ``MSBuild.exe`` (located via PATH or ``vswhere``). Everything degrades to ``None`` rather
than raising when MSBuild is absent or replay fails (plan §18.4).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("anon.msbuild_binlog")

_SRC_EXTS = (".cpp", ".cxx", ".cc", ".c")
_REPLAY_TIMEOUT_S = 300

# Matches the `cl.exe` COMMAND token at a boundary (start-of-line, whitespace, quote, or a
# path separator), so `clang-cl.exe` (preceded by '-') and prose mentioning cl.exe mid-token
# are NOT treated as a compiler invocation. Case-insensitive.
_CL_EXE = re.compile(r'(?:^|[\s"\\/])cl\.exe\b', re.IGNORECASE)


def find_msbuild() -> str | None:
    """Locate ``MSBuild.exe`` — PATH first, then a VS install via ``vswhere`` (Windows)."""
    for name in ("MSBuild.exe", "msbuild", "msbuild.exe"):
        found = shutil.which(name)
        if found:
            return found
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.exists():
        try:
            out = subprocess.run(
                [str(vswhere), "-latest", "-requires",
                 "Microsoft.Component.MSBuild", "-property", "installationPath"],
                capture_output=True, text=True, timeout=30)
            inst = out.stdout.strip()
            if inst:
                cand = Path(inst) / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
                if cand.exists():
                    return str(cand)
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _split_args(cmd: str) -> list[str]:
    """Split a command-line tail into args, respecting double quotes (cl.exe style)."""
    args: list[str] = []
    cur: list[str] = []
    in_q = False
    for ch in cmd:
        if ch == '"':
            in_q = not in_q
        elif ch.isspace() and not in_q:
            if cur:
                args.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        args.append("".join(cur))
    return args


def _is_source(tok: str) -> bool:
    # A source token has a C/C++ extension, is not a flag, and is not a `NAME=value` define
    # fragment (e.g. `/D VER=ver.c` tokenizes to `VER=ver.c`, which must not count as a TU).
    return (tok.lower().endswith(_SRC_EXTS)
            and not tok.startswith(("/", "-"))
            and "=" not in tok)


def _flag_value(args: list[str], i: int) -> tuple[str, int]:
    """Value of a /I or /D flag at *args[i]*, and the new index. For a bare flag, take the next
    token ONLY if it isn't itself a flag or a source file (so `/I /c foo.cpp` doesn't record
    `/c` as an include and desync parsing)."""
    inline = args[i][2:]
    if inline:
        return inline.strip('"'), i
    nxt = args[i + 1] if i + 1 < len(args) else ""
    if nxt and not nxt.startswith(("/", "-")) and not _is_source(nxt):
        return nxt.strip('"'), i + 1
    return "", i


def parse_cl_invocations(text: str, *, base_dir: str | None = None) -> list[dict[str, Any]]:
    """Extract compile-DB entries from ``cl.exe`` command lines found in a (replayed) log.

    Pure/testable: scans *text* for lines invoking ``cl.exe`` on a C/C++ source and turns each
    into a ``{directory, file, command}`` entry carrying the ``/I`` and ``/D`` flags. A line is
    only treated as a compile command if it names at least one source file (filters mentions of
    cl.exe that aren't invocations). De-duped by resolved source path (last wins).
    """
    entries: dict[str, dict[str, Any]] = {}
    for raw in text.splitlines():
        m = _CL_EXE.search(raw)
        if m is None:   # no `cl.exe` command token on this line (boundary-matched, §8)
            continue
        tail = raw[m.end():]
        args = _split_args(tail)
        sources = [a for a in args if _is_source(a)]
        if not sources:
            continue
        includes: list[str] = []
        defines: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            # Match the include (/I) and define (/D) flags. Case-sensitive on the letter so a
            # lowercase cl flag like ``/diagnostics:column`` is not mistaken for a ``/d`` define;
            # and a define value carrying a bare ``:`` (no ``=``) is a flag, not a macro, so skip it.
            if a[:2] in ("/I", "-I"):
                val, i = _flag_value(args, i)
                if val:
                    includes.append(val)
            elif a[:2] in ("/D", "-D"):
                val, i = _flag_value(args, i)
                if val and not (":" in val and "=" not in val):
                    defines.append(val)
            i += 1
        flags = [f"/I{inc}" for inc in includes] + [f"/D{d}" for d in defines]
        for src in sources:
            src_path = src
            directory = base_dir or str(Path(src).parent)
            try:
                if base_dir and not Path(src).is_absolute():
                    src_path = str((Path(base_dir) / src).resolve())
            except (OSError, ValueError):
                pass
            entries[src_path] = {
                "directory": directory,
                "file": src_path,
                "command": "cl.exe " + " ".join(flags + ["/c", src]),
            }
    return list(entries.values())


def replay_binlog(binlog: Path, msbuild: str | None = None) -> str | None:
    """Replay *binlog* through MSBuild at diagnostic verbosity; return the log text or None."""
    msbuild = msbuild or find_msbuild()
    if msbuild is None:
        log.info("MSBuild.exe not found (PATH/vswhere) — cannot replay binlog (§18.4)")
        return None
    cmd = [msbuild, str(binlog), "-noAutoResponse", "-noConsoleLogger",
           "-verbosity:diagnostic",
           f"-fileLogger", f"-fileLoggerParameters:LogFile={binlog}.replay.log;Verbosity=diagnostic"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=_REPLAY_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("binlog replay failed: %s — degrading (§18.4)", exc)
        return None
    replay = Path(f"{binlog}.replay.log")
    if replay.exists():
        try:
            return replay.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return None


def compile_db_from_binlog(binlog: Path, msbuild: str | None = None) -> list[dict[str, Any]] | None:
    """Replay *binlog* and parse its ``cl.exe`` invocations into a compile DB, or ``None``."""
    binlog = Path(binlog)
    if not binlog.exists():
        return None
    text = replay_binlog(binlog, msbuild)
    if not text:
        return None
    db = parse_cl_invocations(text)
    return db or None
