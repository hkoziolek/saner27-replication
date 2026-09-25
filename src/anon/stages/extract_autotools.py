"""Stage 1 — autotools (Makefile.am / Makefile.in) C/C++ L0 extraction (eval plan §5.2 / §20#3).

The third C/C++ build front-end (after CMake and MSBuild): a **pure static parse** of the
build files an autotools project *declares* — no ``configure`` run, no compiler, no Bear —
in the same spirit as :mod:`extract_msbuild_cpp`'s build-free ``.vcxproj`` parse. This is
what unlocks the GT-pub SAR fixtures (bash 4.2, libxml2 2.4.22) whose published ground
truths were unusable while autotools yielded 0 build targets (the documented limitation
case in ``plans/notes/2026-06-13-opencv-mod-nopcommerce-bash.md``).

Two declaration idioms are parsed, per Makefile (``Makefile.am`` preferred over the
generated ``Makefile.in`` when both exist):

  * **automake-declarative** — ``bin_PROGRAMS`` / ``lib_LTLIBRARIES`` / ``noinst_LIBRARIES``
    primaries, members from ``<canon>_SOURCES``, link deps from ``<canon>_LDADD`` /
    ``<canon>_LIBADD`` (libxml2-style).
  * **hand-written pre-automake** — ``Program = bash$(EXEEXT)`` + ``CSOURCES``/``HSOURCES``,
    ``LIBRARY_NAME = libglob.a``, explicit ``libX.a: $(OBJECTS)`` archive rules (``.o`` deps
    mapped back to sources via the Makefile's own compile rules), and per-program link rules
    (``mksignames$(EXEEXT): mksignames.o buildsignames.o``) — the bash-4.2 style.

Attribution precedence (deterministic): an explicitly declared member beats the
directory-subtree fallback; among explicit claims the target declared in the file's own
directory wins (``lib/tilde/tilde.c`` belongs to ``libtilde`` even though ``libreadline``'s
object list also names ``tilde.o``), ties broken by sorted target name. Files never named
by any Makefile fall back to the *primary* target of the nearest ancestor Makefile
(headers, mostly) — the same longest-prefix convention :class:`extract_cpp_facts._TargetResolver`
uses for CMake.

Stable ids (plan §6.3): ``cpp:target:<name>`` — same namespace as the CMake front-end, so
the Doxygen L2 layer (:mod:`extract_cpp_facts`, which falls back to this module's model
when no CMake model exists) merges onto the same targets.

Alongside the fact fragment the extractor writes a ``file-deps/1`` sidecar carrying ONLY
the file→owning-target map (no edges — includes are L2, Doxygen's job), which is how
``arch export --graph --granularity file`` learns file ownership for a non-CMake C repo.

Graceful degradation (plan §18.4): fires only when the repo root has ``configure``/
``configure.ac``/``configure.in`` AND a root ``Makefile.am``/``Makefile.in`` AND no root
``CMakeLists.txt`` (CMake stays authoritative when both exist); anything else returns
``None`` and writes nothing, byte-identical to a run without this stage.
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..cmake_api import CMakeEdge, CMakeModel, CMakeTarget
from ..filedeps import make_sidecar, write_sidecar
from ..jsonio import dump_json
from ..paths import Workspace

log = logging.getLogger("anon.extract_autotools")

EXTRACTOR = {"name": "autotools-make", "version": "0.1.0"}

_SRC_EXTS = {".c", ".cc", ".cpp", ".cxx", ".y", ".l", ".def"}
_HDR_EXTS = {".h", ".hh", ".hpp", ".hxx"}
_MEMBER_EXTS = _SRC_EXTS | _HDR_EXTS
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".cache"}

# Conventional phony/lifecycle rule names — never program targets (case-insensitive check).
_PHONY = {
    "all", "clean", "mostlyclean", "distclean", "maintainer-clean", "realclean",
    "install", "install-strip", "installdirs", "uninstall", "reinstall", "check",
    "test", "tests", "tags", "info", "dvi", "html", "pdf", "ps", "dist", "depend",
    "depends", "force", "everything", "others", "basic", "subdirs", "documentation",
    "targets", "profiling-tests", "static", "dynamic", "world",
    # automake/gettext service rules whose deps are $(SOURCES) — never programs
    "id", "ctags", "gtags", "cscope", "cscopelist", "distdir", "installcheck",
    "installcheck-am", "install-am", "check-am", "all-am", "distcheck", "stamp-po",
    "remove-potcdate", "update-po", "install-exec", "install-data",
}

# automake primary variables that declare buildable targets -> fact-model type.
_AM_PRIMARY_RE = re.compile(
    r"^(?:bin|sbin|libexec|pkglib|pkglibexec|lib|noinst|check|EXTRA)_"
    r"(PROGRAMS|LIBRARIES|LTLIBRARIES)$")
_AM_TYPE = {"PROGRAMS": "executable", "LIBRARIES": "static_lib", "LTLIBRARIES": "shared_lib"}

_VAR_RE = re.compile(r"\$[({](?P<name>[A-Za-z_][A-Za-z0-9_]*)[)}]")
_AT_RE = re.compile(r"@(?P<name>[A-Za-z_][A-Za-z0-9_]*)@")
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*)\s*([:+?]?=)\s*(.*)$")
_LIB_BASENAME_RE = re.compile(r"^lib[A-Za-z0-9_.+\-]*\.(a|la)$")
_DASH_L_RE = re.compile(r"^-l([A-Za-z0-9_.+\-]+)$")


# ------------------------------------------------------------------ one Makefile parse

@dataclass
class _Makefile:
    dir: str                                   # repo-relative posix dir ("" = root)
    path: Path                                 # the Makefile.am/.in on disk
    vars: dict[str, str] = field(default_factory=dict)
    rules: list[tuple[list[str], list[str]]] = field(default_factory=list)  # (targets, deps)


@dataclass
class _Target:
    name: str
    type: str                                  # fact-model enum: executable | static_lib | shared_lib
    makefile: _Makefile
    members: set[str] = field(default_factory=set)   # explicit repo-relative sources
    lib_refs: set[str] = field(default_factory=set)  # lib basenames referenced ("libglob")
    primary: bool = False                      # the Makefile's main target (subtree fallback)


def _logical_lines(text: str) -> list[str]:
    """Physical lines joined over trailing-backslash continuations, comments dropped."""
    out: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = pending + raw
        if line.rstrip().endswith("\\"):
            pending = line.rstrip()[:-1] + " "
            continue
        pending = ""
        # strip comments (naive — '#' inside a make string is vanishingly rare here)
        if "#" in line:
            line = line.split("#", 1)[0]
        if line.strip():
            out.append(line)
    if pending.strip():
        out.append(pending)
    return out


def _parse_makefile(mf: Path, repo: Path) -> _Makefile:
    try:
        rel_dir = mf.parent.resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        rel_dir = ""
    if rel_dir == ".":
        rel_dir = ""
    parsed = _Makefile(dir=rel_dir, path=mf)
    try:
        text = mf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return parsed
    for line in _logical_lines(text):
        if line.startswith("\t"):
            continue  # recipe line — never parsed (no shell semantics here)
        m = _ASSIGN_RE.match(line)
        if m:
            name, op, value = m.group(1), m.group(2), m.group(3).strip()
            if op == "+=" and name in parsed.vars:
                parsed.vars[name] = parsed.vars[name] + " " + value
            elif op == "?=" and name in parsed.vars:
                pass
            else:
                parsed.vars[name] = value
            continue
        # a rule: "targets : deps" (ignore target-specific vars and pattern/suffix rules)
        if ":" in line:
            head, _, tail = line.partition(":")
            if tail.startswith("="):     # ":=" assignment already excluded above; be safe
                continue
            if _ASSIGN_RE.match(tail.lstrip()):
                continue                  # target-specific variable — not a dependency line
            tgt_tokens = head.split()
            if not tgt_tokens or any(t.startswith(".") or "%" in t for t in tgt_tokens):
                continue                  # .SUFFIXES/.c.o/pattern rules
            parsed.rules.append((tgt_tokens, tail.lstrip(":").split()))
    return parsed


# ------------------------------------------------------------------ expansion + paths

def _expand(value: str, mf: _Makefile, root_rel: str, depth: int = 0) -> str:
    """Expand ``$(VAR)``/``${VAR}`` from the file's own assignments and neutralize
    configure ``@substitutions@``. Unknown references become empty strings — exactly what
    an unset make variable does — so optional configure-conditional pieces drop out."""
    if depth > 12 or ("$" not in value and "@" not in value):
        return _AT_RE.sub(lambda m: _at_subst(m.group("name"), root_rel), value) \
            if "@" in value else value

    def _var(m: re.Match[str]) -> str:
        name = m.group("name")
        if name in ("srcdir",):
            return "."
        if name in ("top_srcdir", "topdir", "top_builddir", "BUILD_DIR", "dot"):
            return _at_subst(name, root_rel)
        raw = mf.vars.get(name)
        if raw is None:
            return ""
        return _expand(raw, mf, root_rel, depth + 1)

    value = _VAR_RE.sub(_var, value)
    value = re.sub(r"\$[@<^*?]", "", value)          # automatic variables
    value = _AT_RE.sub(lambda m: _at_subst(m.group("name"), root_rel), value)
    return value


def _at_subst(name: str, root_rel: str) -> str:
    """Configure-substitution defaults for a srcdir==builddir layout; all else empty."""
    if name == "srcdir":
        return "."
    if name in ("top_srcdir", "topdir", "top_builddir", "BUILD_DIR", "dot"):
        return root_rel or "."
    if name == "EXEEXT":
        return ""
    return ""


def _resolve(token: str, mf: _Makefile, repo: Path) -> str | None:
    """Resolve one expanded path token to an existing repo-relative posix file, else None."""
    token = token.strip().replace("\\", "/")
    if not token or token.startswith("-") or "$" in token or "@" in token:
        return None
    joined = posixpath.normpath(posixpath.join(mf.dir, token)) if not token.startswith("/") \
        else token.lstrip("/")
    if joined.startswith(".."):
        return None
    if (repo / joined).is_file():
        return joined
    return None


def _root_rel(mf_dir: str) -> str:
    """Relative path from *mf_dir* back to the repo root ('' for the root itself)."""
    if not mf_dir:
        return "."
    return "/".join([".."] * len(mf_dir.split("/")))


# ------------------------------------------------------------------ target discovery

def _lib_name(token: str) -> str | None:
    base = posixpath.basename(token)
    if _LIB_BASENAME_RE.match(base):
        return base.rsplit(".", 1)[0]
    return None


def _am_canon(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _compile_rule_map(mf: _Makefile, repo: Path, root_rel: str) -> dict[str, str]:
    """``<stem>`` -> source path for explicit ``foo.o: path/foo.c`` compile rules."""
    out: dict[str, str] = {}
    for tgts, deps in mf.rules:
        if len(tgts) != 1 or not tgts[0].endswith(".o"):
            continue
        stem = posixpath.basename(_expand(tgts[0], mf, root_rel))[:-2]
        if not stem:
            continue
        for d in deps:
            for tok in _expand(d, mf, root_rel).split():
                if posixpath.splitext(tok)[1].lower() in (".c", ".cc", ".cpp", ".cxx", ".y", ".l"):
                    resolved = _resolve(tok, mf, repo)
                    if resolved:
                        out.setdefault(stem, resolved)
    return out


def _obj_to_source(stem: str, mf: _Makefile, repo: Path,
                   compile_rules: dict[str, str]) -> str | None:
    if stem in compile_rules:
        return compile_rules[stem]
    for ext in (".c", ".cc", ".cpp", ".cxx", ".y", ".l"):
        cand = _resolve(stem + ext, mf, repo)
        if cand:
            return cand
    return None


def _members_from_tokens(tokens: list[str], mf: _Makefile, repo: Path,
                         compile_rules: dict[str, str], root_rel: str) -> set[str]:
    """Sources named by dependency/variable tokens: direct source paths + ``.o``→source."""
    out: set[str] = set()
    for tok in tokens:
        for t in _expand(tok, mf, root_rel).split():
            ext = re.search(r"(\.[A-Za-z0-9_]+)$", posixpath.basename(t))
            suffix = ext.group(1).lower() if ext else ""
            if suffix in _MEMBER_EXTS:
                resolved = _resolve(t, mf, repo)
                if resolved:
                    out.add(resolved)
            elif suffix == ".o":
                src = _obj_to_source(posixpath.basename(t)[:-2], mf, repo, compile_rules)
                if src:
                    out.add(src)
    return out


def _discover_targets(mf: _Makefile, repo: Path) -> list[_Target]:
    """All build targets one Makefile declares, with their explicit members + lib refs."""
    root_rel = _root_rel(mf.dir)
    compile_rules = _compile_rule_map(mf, repo, root_rel)
    targets: dict[str, _Target] = {}

    def _add(name: str, ttype: str) -> _Target:
        if name not in targets:
            targets[name] = _Target(name=name, type=ttype, makefile=mf)
        return targets[name]

    # --- idiom A: automake primaries ------------------------------------------------
    am_declared = False
    for var, raw in sorted(mf.vars.items()):
        pm = _AM_PRIMARY_RE.match(var)
        if not pm or var.startswith("EXTRA_"):
            continue
        for tok in _expand(raw, mf, root_rel).split():
            base = posixpath.basename(tok)
            lib = _lib_name(base)
            name = lib if lib else base
            if not name or "$" in name:
                continue
            t = _add(name, _AM_TYPE[pm.group(1)] if not lib
                     else ("static_lib" if base.endswith(".a") else "shared_lib"))
            canon = _am_canon(base)
            t.members |= _members_from_tokens(
                mf.vars.get(f"{canon}_SOURCES", "").split(), mf, repo, compile_rules, root_rel)
            for dep_var in (f"{canon}_LDADD", f"{canon}_LIBADD"):
                for dtok in _expand(mf.vars.get(dep_var, ""), mf, root_rel).split():
                    ref = _lib_name(dtok)
                    if not ref:
                        dm = _DASH_L_RE.match(dtok)
                        ref = f"lib{dm.group(1)}" if dm else None
                    if ref:
                        t.lib_refs.add(ref)
            am_declared = True
    if am_declared:
        # primary = the installed LIBRARY when one exists (the automake dir is FOR its
        # lib_LTLIBRARIES artifact; programs beside it are tools/tests), else first program
        ordered = sorted(targets.values(), key=lambda t: (t.type == "executable", t.name))
        if ordered:
            ordered[0].primary = True
        return list(targets.values())

    # --- idiom B: hand-written Makefile.in ------------------------------------------
    # B1. LIBRARY_NAME = libX.a
    lib_from_var = _lib_name(_expand(mf.vars.get("LIBRARY_NAME", ""), mf, root_rel).strip())
    if lib_from_var:
        _add(lib_from_var, "static_lib")
    # B2. archive rules  libX.a: $(OBJECTS)…
    for tgts, deps in mf.rules:
        for tok in tgts:
            expanded = _expand(tok, mf, root_rel).strip()
            lib = _lib_name(expanded)
            if not lib:
                continue
            t = _add(lib, "static_lib")
            t.members |= _members_from_tokens(deps, mf, repo, compile_rules, root_rel)
    # B3. Program = name$(EXEEXT)
    prog_name = _expand(mf.vars.get("Program", ""), mf, root_rel).strip()
    prog: _Target | None = None
    if prog_name and "/" not in prog_name and "." not in prog_name:
        prog = _add(prog_name, "executable")
    # B4. per-program link rules  name$(EXEEXT): foo.o bar.c …
    for tgts, deps in mf.rules:
        for tok in tgts:
            name = _expand(tok, mf, root_rel).strip()
            if (not name or "/" in name or "." in name or "$" in name
                    or name.lower() in _PHONY or name.lower().startswith("stamp-")):
                continue
            members = _members_from_tokens(deps, mf, repo, compile_rules, root_rel)
            if not members and name not in targets:
                continue  # a phony-ish rule with no resolvable sources is not a program
            t = _add(name, "executable")
            t.members |= members
    # B5. the primary target's source-list variables (CSOURCES/HSOURCES/DEFSRC/…).
    # Precedence: the Program › the LIBRARY_NAME-designated library (bash's readline dir
    # builds libreadline.a AND libhistory.a — LIBRARY_NAME says which one the dir is FOR)
    # › the single remaining library.
    primary = prog or (targets.get(lib_from_var) if lib_from_var else None) or next(
        iter(sorted((t for t in targets.values() if t.type != "executable"),
                    key=lambda t: t.name)), None)
    if primary is None and len(targets) == 1:
        primary = next(iter(targets.values()))
    if primary is not None:
        primary.primary = True
        for var, raw in sorted(mf.vars.items()):
            if re.search(r"(^|_)([CH]?SOURCES|SRC)$|^DEFSRC$", var):
                primary.members |= _members_from_tokens(
                    raw.split(), mf, repo, compile_rules, root_rel)
        # B6. primary executable link refs: every lib basename the file's declarations name
        if primary.type == "executable":
            for raw in mf.vars.values():
                for tok in _expand(raw, mf, root_rel).split():
                    ref = _lib_name(tok)
                    if ref:
                        primary.lib_refs.add(ref)
            for _tgts, deps in mf.rules:
                for d in deps:
                    for tok in _expand(d, mf, root_rel).split():
                        ref = _lib_name(tok)
                        if ref:
                            primary.lib_refs.add(ref)
    # non-primary executables: link refs from their own rule deps only
    for tgts, deps in mf.rules:
        for tok in tgts:
            name = _expand(tok, mf, root_rel).strip()
            t = targets.get(name)
            if t is None or t is primary or t.type != "executable":
                continue
            for d in deps:
                for dtok in _expand(d, mf, root_rel).split():
                    ref = _lib_name(dtok)
                    if ref:
                        t.lib_refs.add(ref)
    return list(targets.values())


# ------------------------------------------------------------------ repo-level model

def is_autotools_repo(repo: Path) -> bool:
    """Root configure(.ac/.in) + root Makefile.am/.in, and NOT a CMake repo (unless
    ``ANON_SKIP_CMAKE=1`` stood the CMake extractor down — then autotools is L0)."""
    if (repo / "CMakeLists.txt").exists() and not os.environ.get("ANON_SKIP_CMAKE"):
        return False
    has_configure = any((repo / n).is_file()
                        for n in ("configure", "configure.ac", "configure.in"))
    has_makefile = any((repo / n).is_file() for n in ("Makefile.am", "Makefile.in"))
    return has_configure and has_makefile


def _iter_makefiles(repo: Path) -> list[Path]:
    """One Makefile per directory (``Makefile.am`` preferred), deterministic order."""
    by_dir: dict[Path, Path] = {}
    for pattern in ("Makefile.am", "Makefile.in"):
        for p in repo.rglob(pattern):
            if set(part.lower() for part in p.parts) & _SKIP_DIRS:
                continue
            by_dir.setdefault(p.parent.resolve(), p.resolve())
    return [by_dir[d] for d in sorted(by_dir, key=lambda d: d.as_posix())]


def build_model(repo: Path) -> CMakeModel | None:
    """Parse the repo's Makefiles into a :class:`CMakeModel`, or ``None`` if not autotools.

    The CMake model dataclasses are the neutral in-memory build-graph shape every C/C++
    front-end feeds (targets + sources + link edges); reusing them means the Doxygen L2
    layer's :class:`_TargetResolver` and the fragment emission work unchanged."""
    repo = repo.resolve()
    if not is_autotools_repo(repo):
        return None

    makefiles = [_parse_makefile(p, repo) for p in _iter_makefiles(repo)]
    all_targets: list[_Target] = []
    for mf in makefiles:
        all_targets.extend(_discover_targets(mf, repo))
    if not all_targets:
        return None

    # De-dup by name. The top-level Makefile routinely REFERENCES a sub-library through
    # a delegation rule (``$(GLOB_LIBRARY): … && cd lib/glob && $(MAKE)``) while the
    # library's own Makefile DECLARES it — merge the member/link evidence and make the
    # deeper (own-directory) declaration canonical for dir/type. A genuine same-depth
    # clash is logged, never silent.
    by_name: dict[str, _Target] = {}
    for t in all_targets:
        existing = by_name.get(t.name)
        if existing is None:
            by_name[t.name] = t
            continue
        canonical, other = ((t, existing)
                            if len(t.makefile.dir) > len(existing.makefile.dir)
                            else (existing, t))
        if (existing.makefile.dir != t.makefile.dir
                and len(existing.makefile.dir) == len(t.makefile.dir)):
            log.warning("duplicate autotools target name %r (%s vs %s) — keeping %s",
                        t.name, existing.makefile.dir or ".", t.makefile.dir or ".",
                        canonical.makefile.dir or ".")
        canonical.members |= other.members
        canonical.lib_refs |= other.lib_refs
        canonical.primary = canonical.primary or other.primary
        by_name[t.name] = canonical

    # --- explicit-claim conflict resolution (deterministic) -------------------------
    claims: dict[str, list[_Target]] = {}
    for t in by_name.values():
        for f in t.members:
            claims.setdefault(f, []).append(t)

    def _winner(f: str, cands: list[_Target]) -> _Target:
        fdir = posixpath.dirname(f)

        def _key(t: _Target):
            mdir = t.makefile.dir
            in_own_tree = fdir == mdir or fdir.startswith(mdir + "/") if mdir else fdir == ""
            # own-directory declaration › deepest Makefile › the Makefile's PRIMARY
            # artifact (an auxiliary rule like builtins' helpdoc must not steal the
            # library's .def sources) › sorted name.
            return (not in_own_tree, -len(mdir), not t.primary, t.name)
        return sorted(cands, key=_key)[0]

    owner: dict[str, str] = {}
    for f in sorted(claims):
        owner[f] = _winner(f, claims[f]).name

    # --- subtree fallback: unclaimed sources under each Makefile's own directory ----
    mf_dirs = {mf.dir for mf in makefiles}
    primaries = {t.makefile.dir: t for t in by_name.values() if t.primary}
    for mdir in sorted(primaries):
        base = repo / mdir if mdir else repo
        prim = primaries[mdir]
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _MEMBER_EXTS:
                continue
            try:
                rel = p.resolve().relative_to(repo).as_posix()
            except (ValueError, OSError):
                continue
            if rel in owner:
                continue
            rdir = posixpath.dirname(rel)
            if set(part.lower() for part in Path(rel).parts) & _SKIP_DIRS:
                continue
            # a deeper Makefile's territory is not ours (its own primary claims it)
            probe, deeper = rdir, False
            while probe != mdir and probe:
                if probe in mf_dirs:
                    deeper = True
                    break
                probe = posixpath.dirname(probe)
            if deeper and rdir != mdir:
                continue
            owner[rel] = prim.name

    # --- assemble the model ----------------------------------------------------------
    members_by_target: dict[str, list[str]] = {}
    for f, tname in owner.items():
        members_by_target.setdefault(tname, []).append(f)
    lib_names = {n for n, t in by_name.items() if t.type in ("static_lib", "shared_lib")}
    edges: list[CMakeEdge] = []
    seen: set[tuple[str, str]] = set()
    for name in sorted(by_name):
        t = by_name[name]
        for ref in sorted(t.lib_refs):
            if ref in lib_names and ref != name and (name, ref) not in seen:
                edges.append(CMakeEdge(source=name, target=ref, visibility="public"))
                seen.add((name, ref))
    # A rule that LOOKED like a target but lost every claimed source to the conflict
    # resolution (stamp rules: parser-built, stamp-h, hashtest, helpdoc…) and sits on
    # no link edge is build ceremony, not architecture — drop it.
    endpoints = {e.source for e in edges} | {e.target for e in edges}
    model = CMakeModel()
    for name in sorted(by_name):
        t = by_name[name]
        if not members_by_target.get(name) and name not in endpoints:
            continue
        model.targets[name] = CMakeTarget(
            name=name, type=t.type, source_dir=t.makefile.dir,
            sources=sorted(members_by_target.get(name, [])))
    model.edges = edges
    return model


def _link_detail(src: str, dst: str, visibility: str) -> str:
    return f"Makefile: {src} links {dst}"


def build_fragment(repo: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """``(fact_fragment, file_deps_sidecar)`` for an autotools repo, or ``None``."""
    model = build_model(repo)
    if model is None or not model.targets:
        return None
    from .extract_cmake import fragment_from_model
    fragment = fragment_from_model(model, extractor=EXTRACTOR, build_tag="build:autotools",
                                   link_detail=_link_detail)
    sidecar = make_sidecar(
        EXTRACTOR, "cpp",
        files={src: f"cpp:target:{t.name}"
               for t in model.targets.values() for src in t.sources},
        edges=set())   # includes are L2 — Doxygen's job (extract_cpp_facts)
    return fragment, sidecar


def run(ws: Workspace) -> Path | None:
    """Extract the autotools build graph, or ``None`` on graceful degrade (§18.4)."""
    result = build_fragment(ws.repo)
    if result is None:
        log.info("not an autotools repo (or nothing declared) under %s — skipping (§18.4)",
                 ws.repo)
        return None
    fragment, sidecar = result
    out = ws.fragments / "autotools-graph.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_json(fragment, out)
    if sidecar["files"]:
        write_sidecar(ws, sidecar)
    return out


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    import argparse

    from ..logutil import setup_console_logging
    from ..paths import resolve_workspace

    setup_console_logging()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch-dir")
    args = ap.parse_args()
    w = resolve_workspace(args.repo, args.arch_dir)
    print(run(w) or "(degraded: not an autotools repo)")
