"""Autotools (Makefile.am / Makefile.in) L0 extractor tests (``extract_autotools``).

The static-parse front-end that unlocks the GT-pub autotools fixtures (bash 4.2,
libxml2 2.4.22) — eval plan §5.2 / §20#3. Synthetic repos under ``tmp_path`` exercise
both declaration idioms (hand-written pre-automake and automake-declarative), the
attribution precedence rules, and the graceful no-op gates.

    .venv/Scripts/python.exe -m pytest tests/test_extract_autotools.py -q
"""
from __future__ import annotations

from pathlib import Path

from anon.paths import resolve_workspace
from anon.stages import extract_autotools as at


def _write(repo: Path, rel: str, text: str = "") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _handwritten_repo(repo: Path) -> None:
    """A miniature bash-4.2-shaped repo: hand-written Makefile.in idiom."""
    _write(repo, "configure", "#!/bin/sh\n")
    _write(repo, "shell.c")
    _write(repo, "eval.c")
    _write(repo, "shell.h")
    _write(repo, "include/stdc.h")
    _write(repo, "support/mktool.c")
    _write(repo, "lib/glob/glob.c")
    _write(repo, "lib/glob/glob.h")
    _write(repo, "lib/glob/smatch.c")
    _write(
        repo, "Makefile.in",
        "Program = shell$(EXEEXT)\n"
        "CSOURCES = shell.c eval.c\n"
        "HSOURCES = shell.h $(BASHINCDIR)/stdc.h\n"
        "BASHINCDIR = ${srcdir}/include\n"
        "GLOB_LIBRARY = $(dot)/lib/glob/libglob.a\n"
        "LIBDEP = $(GLOB_LIBRARY)\n"
        "SUPPORT_SRC = $(srcdir)/support/\n"
        "$(Program): $(OBJECTS) $(LIBDEP)\n"
        "\t$(CC) -o $@\n"
        "mktool.o: $(SUPPORT_SRC)mktool.c\n"
        "\t$(CC) -c $(SUPPORT_SRC)mktool.c\n"
        "mktool$(EXEEXT): mktool.o\n"
        "\t$(CC) -o $@ mktool.o\n"
        "clean:\n"
        "\trm -f *.o\n",
    )
    _write(
        repo, "lib/glob/Makefile.in",
        "LIBRARY_NAME = libglob.a\n"
        "CSOURCES = $(srcdir)/glob.c $(srcdir)/smatch.c\n"
        "libglob.a: $(OBJECTS)\n"
        "\t$(AR) cr $@\n",
    )


def test_handwritten_idiom_targets_members_and_links(tmp_path: Path):
    _handwritten_repo(tmp_path)
    model = at.build_model(tmp_path)
    assert model is not None
    assert set(model.targets) == {"shell", "libglob", "mktool"}
    assert model.targets["shell"].type == "executable"
    assert model.targets["libglob"].type == "static_lib"
    # explicit source vars + resolved variable indirections ($(BASHINCDIR)/stdc.h)
    assert "shell.c" in model.targets["shell"].sources
    assert "include/stdc.h" in model.targets["shell"].sources
    # .o deps map back to their compile rule's source, cross-directory
    assert model.targets["mktool"].sources == ["support/mktool.c"]
    # subtree fallback: the lib dir's un-declared header belongs to the dir's primary
    assert "lib/glob/glob.h" in model.targets["libglob"].sources
    # link edge from the lib basename the program's declarations reference
    assert [(e.source, e.target) for e in model.edges] == [("shell", "libglob")]


def test_handwritten_fragment_and_sidecar(tmp_path: Path):
    _handwritten_repo(tmp_path)
    fragment, sidecar = at.build_fragment(tmp_path)
    ids = {t["id"] for t in fragment["targets"]}
    assert "cpp:target:shell" in ids and "cpp:target:libglob" in ids
    for t in fragment["targets"]:
        assert t["tags"] == ["build:autotools"]
    rels = [(r["source"], r["target"]) for r in fragment["relationships"]]
    assert rels == [("cpp:target:shell", "cpp:target:libglob")]
    assert fragment["relationships"][0]["evidence"][0]["type"] == "link"
    # sidecar owner map (file-deps/1): every attributed file, no edges (includes are L2)
    assert sidecar["files"]["lib/glob/glob.c"] == "cpp:target:libglob"
    assert sidecar["files"]["shell.c"] == "cpp:target:shell"
    assert sidecar["edges"] == []


def test_automake_idiom(tmp_path: Path):
    _write(tmp_path, "configure.ac", "AC_INIT\n")
    _write(tmp_path, "parser.c")
    _write(tmp_path, "tree.c")
    _write(tmp_path, "tool.c")
    _write(
        tmp_path, "Makefile.am",
        "lib_LTLIBRARIES = libxml2.la\n"
        "libxml2_la_SOURCES = parser.c tree.c\n"
        "bin_PROGRAMS = xmllint\n"
        "xmllint_SOURCES = tool.c\n"
        "xmllint_LDADD = libxml2.la\n",
    )
    model = at.build_model(tmp_path)
    assert set(model.targets) == {"libxml2", "xmllint"}
    assert model.targets["libxml2"].type == "shared_lib"
    assert sorted(model.targets["libxml2"].sources) == ["parser.c", "tree.c"]
    assert model.targets["xmllint"].sources == ["tool.c"]
    assert [(e.source, e.target) for e in model.edges] == [("xmllint", "libxml2")]


def test_same_dir_tie_prefers_primary(tmp_path: Path):
    """An auxiliary rule (builtins' helpdoc) must not steal the primary lib's sources."""
    _write(tmp_path, "configure", "")
    _write(tmp_path, "Makefile.in", "Program = app$(EXEEXT)\nCSOURCES = app.c\n")
    _write(tmp_path, "app.c")
    _write(tmp_path, "builtins/alias.def")
    _write(
        tmp_path, "builtins/Makefile.in",
        "DEFSRC = $(srcdir)/alias.def\n"
        "libbuiltins.a: $(OFILES)\n"
        "\t$(AR) cr $@\n"
        "helpdoc: $(DEFSRC)\n"
        "\t$(BUILD_DOC)\n",
    )
    model = at.build_model(tmp_path)
    assert "builtins/alias.def" in model.targets["libbuiltins"].sources
    assert "helpdoc" not in model.targets  # lost its only claim -> dropped as ceremony


def test_noop_on_cmake_and_plain_repos(tmp_path: Path):
    # CMake repo with a stray vendored Makefile.in must NOT fire (CMake is authoritative)
    _write(tmp_path, "CMakeLists.txt", "project(x)\n")
    _write(tmp_path, "configure", "")
    _write(tmp_path, "Makefile.in", "Program = x\n")
    assert at.build_model(tmp_path) is None
    # plain repo without configure
    other = tmp_path / "plain"
    _write(other, "Makefile.in", "Program = x\n")
    assert at.build_model(other) is None


def test_run_degrades_to_none_and_writes_nothing(tmp_path: Path):
    _write(tmp_path, "readme.md", "not autotools\n")
    ws = resolve_workspace(str(tmp_path), str(tmp_path / "arch"))
    assert at.run(ws) is None
    assert not (ws.fragments / "autotools-graph.json").exists()


def test_run_is_deterministic(tmp_path: Path):
    _handwritten_repo(tmp_path)
    ws = resolve_workspace(str(tmp_path), str(tmp_path / "arch"))
    out = at.run(ws)
    assert out is not None
    first = out.read_bytes()
    sidecar_first = (ws.fragments / "file-deps-autotools-make.json").read_bytes()
    at.run(ws)
    assert out.read_bytes() == first
    assert (ws.fragments / "file-deps-autotools-make.json").read_bytes() == sidecar_first
