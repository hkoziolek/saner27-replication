"""Stage: cross-language interop detection — C# P/Invoke + C++/CLI + COM scan (plan §16#15).

Three pure-Python, SDK-independent regex scans over source text, each emitting an
``interop`` evidence edge from the owning build target to an external boundary target:

1. **C# P/Invoke** — ``[DllImport("lib")]`` / ``[LibraryImport("lib")]`` in ``.cs`` →
   an edge from the owning ``.csproj`` to a ``native:lib:<name>`` boundary.
2. **C++/CLI bridges** (plan §16#15 tail) — a ``/clr`` managed↔native seam, detected from
   ``#using <X.dll>`` / ``gcnew`` / ``ref class`` / ``value class`` / ``#include <vcclr.h>``
   in ``.cpp``/``.h``/``.hpp`` (and ``<CLRSupport>true</CLRSupport>`` / ``<CompileAsManaged>``
   in the owning ``.vcxproj``) → an edge from the owning ``cpp:vcxproj:<path>`` target to a
   ``native:clr:<assembly>`` boundary (the owning target is also tagged ``interop:cppcli``).
3. **COM** (plan §16#15 tail) — ``[ComImport]`` / ``[Guid("…")]`` / ``Marshal.GetActiveObject`` /
   ``Activator.CreateInstance(Type.GetTypeFromProgID/CLSID(…))`` in ``.cs``; ``#import "X.tlb"`` /
   ``CoCreateInstance`` / ``CoGetClassObject`` / ``IID_*`` / ``CLSID_*`` in C++ → an edge from
   the owning target to a ``com:<name>`` boundary.

All boundary targets are marked ``external=True`` because the C++ side that would resolve a
raw name to a real ``cpp`` target is resolved post-merge by ``interop_resolve`` — all three
seam kinds now bind when unambiguous (P/Invoke by name, C++/CLI by managed-assembly name, COM
by the ``com_provides`` identity captured in ``extract_msbuild_cpp``). A boundary with no
unambiguous native match stays a dangling external boundary (honest, not a failure). Multiple
hits for the same boundary from the same owner collapse to one edge (first-detail wins).

Precision (§16#15 / 5c): each source is run through :func:`strip_comments_and_strings` before
the regex scans, so a ``[DllImport]`` / ``[ComImport]`` / ``#using`` token inside a comment or
a marker-bearing string literal cannot mint a phantom seam.

Graceful degradation: if NO P/Invoke, C++/CLI or COM signal is found anywhere → returns
``None`` and writes nothing, keeping repos without native interop (e.g. the toy fixture
or the Serilog pilot) byte-identical to a run that did not call this stage at all.

Stable ids (plan §6.3):
  csproj target : ``csharp:csproj:<repo-relative-posix-path>``  (same scheme as extract_build_graph)
  vcxproj target: ``cpp:vcxproj:<repo-relative-posix-path>``    (same scheme as extract_msbuild_cpp)
  native lib    : ``native:lib:<normalized-name>``              (P/Invoke boundary)
  CLR assembly  : ``native:clr:<normalized-name>``              (C++/CLI bridge boundary)
  COM target    : ``com:<progid-or-clsid-or-typelib>``          (COM boundary)

Boundary ``language`` is always ``"native"`` (the only schema-valid enum value for a
non-cpp/non-csharp boundary, §16#15); the ``native:clr:`` / ``com:`` id prefix + tags carry
the CLR-vs-COM distinction.

Name normalisation: trailing ``.dll`` / ``.so`` / ``.dylib`` is stripped case-insensitively;
the remainder is kept verbatim.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..jsonio import dump_json
from ..model import make_relationship
from ..paths import Workspace

EXTRACTOR = {"name": "csharp-interop", "version": "0.2.0"}

# Matches [DllImport("lib"...)] or [LibraryImport("lib"...)] with:
#   - single or double quotes (the attribute value is always a string literal in C#,
#     but defensive matching avoids edge cases in commented/generated code)
#   - optional verbatim string prefix @"..."
#   - optional EntryPoint= / CallingConvention= / ... tail before the closing ]
#   - leading whitespace-tolerant
_PINVOKE_RE = re.compile(
    r'\[\s*(?P<attr>DllImport|LibraryImport)\s*\(\s*@?["\'](?P<lib>[^"\']+)["\']',
    re.IGNORECASE,
)

# Extensions that are stripped from native library names (case-insensitive).
_STRIP_EXT = re.compile(r'\.(?:dll|so|dylib)$', re.IGNORECASE)

# --- C++/CLI bridge signals (plan §16#15 tail) -----------------------------------
# A managed↔native seam in /clr C++. `#using <Foo.dll>` names the bridged managed
# assembly (the boundary); the other markers only prove a TU is C++/CLI (managed code).
_USING_RE = re.compile(r'#using\s*[<"](?P<asm>[^>"]+)[>"]', re.IGNORECASE)
# Markers that merely identify a translation unit as managed C++/CLI (no assembly name).
_CPPCLI_MARKER_RE = re.compile(
    r'(?:\bgcnew\b|\bref\s+class\b|\bref\s+struct\b|\bvalue\s+class\b|\bvalue\s+struct\b'
    r'|#include\s*[<"]vcclr\.h[>"])'
)
# .vcxproj managed switches: <CLRSupport>true|pure|safe</CLRSupport> or <CompileAsManaged>.
_CLR_TAGS = ("clrsupport", "compileasmanaged")

# Extensions scanned for C++ (C++/CLI bridges + C++-side COM).
_CPP_EXTS = (".cpp", ".cxx", ".cc", ".h", ".hpp", ".hh", ".hxx", ".inl")

# --- COM signals (plan §16#15 tail) ----------------------------------------------
# C# side: a [ComImport] interop type and the runtime activation surfaces.
_COM_IMPORT_RE = re.compile(r'\[\s*ComImport\b', re.IGNORECASE)
_GUID_ATTR_RE = re.compile(r'\[\s*Guid\s*\(\s*@?["\'](?P<guid>[^"\']+)["\']', re.IGNORECASE)
_GET_ACTIVE_OBJECT_RE = re.compile(r'Marshal\s*\.\s*GetActiveObject\s*\(\s*@?["\'](?P<id>[^"\']+)["\']')
# Activator.CreateInstance(Type.GetTypeFromProgID("…")) / GetTypeFromCLSID(new Guid("…")).
_PROGID_RE = re.compile(r'GetTypeFromProgID\s*\(\s*@?["\'](?P<id>[^"\']+)["\']', re.IGNORECASE)
_CLSID_GUID_RE = re.compile(r'GetTypeFromCLSID\s*\(\s*(?:new\s+Guid\s*\(\s*)?@?["\'](?P<id>[^"\']+)["\']', re.IGNORECASE)

# C++ side: type-library import and the COM activation/identity surfaces.
_IMPORT_TLB_RE = re.compile(r'#import\s*[<"](?P<tlb>[^>"]+)[>"]', re.IGNORECASE)
_CO_CREATE_RE = re.compile(r'\b(?:CoCreateInstance(?:Ex)?|CoGetClassObject)\b')
# IID_Foo / CLSID_Foo identifier usage — the canonical COM identity token.
_IID_CLSID_RE = re.compile(r'\b(?:IID|CLSID)_(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b')

# Directories to skip when walking the repo — mirrors extract_build_graph._all_csproj.
_SKIP_DIRS = {"bin", "obj", ".git", "node_modules"}


# Interop marker tokens that, if they appear INSIDE a string literal, evidence that the
# string is doc/sample text rather than a real seam — so such a string is blanked (§16#15 / 5c).
# A normal argument string (e.g. "nativecalc", "Excel.Application") contains none of these and
# is left intact so the argument-capturing regexes still see it.
_MARKER_IN_STRING_RE = re.compile(
    # Word-boundary the identifier markers so a lib/ProgID NAME that merely CONTAINS one as a
    # substring (e.g. "DllImportHelper") is not blanked — only a whole-token marker is. Include
    # the C++/CLI markers `value class`/`value struct`/`vcclr.h` that _CPPCLI_MARKER_RE detects,
    # so a string literal carrying them can't survive stripping and mint a phantom cppcli seam (5c).
    r'\b(?:DllImport|LibraryImport|ComImport|gcnew|CoCreateInstance|CoGetClassObject)\b'
    r'|\bref\s+class\b|\bref\s+struct\b|\bvalue\s+class\b|\bvalue\s+struct\b'
    r'|#using|#import|vcclr\.h',
    re.IGNORECASE,
)


def strip_comments_and_strings(text: str) -> str:
    """Blank C#/C++ comments (and marker-bearing string literals) so neither mints a *phantom* seam.

    A token like ``[DllImport]`` / ``[ComImport]`` / ``gcnew`` inside a comment
    (``// we used to [DllImport] this``) is not real interop — scanning it would invent a
    boundary the build never reaches. The same goes for a *string literal that itself contains*
    such marker syntax (sample/doc text, e.g. ``var s = "[ComImport] interface";``). This
    deterministic single-pass scanner blanks them by replacing the offending span with spaces
    (length-preserving, so it never splices two real tokens together), handling:

      * line comments   ``// …``  (to end of line)
      * block comments  ``/* … */`` (across lines)
      * string literals ``"…"`` / char ``'…'`` (``\\`` escapes), C# verbatim ``@"…""…"``,
        C# interpolated ``$"…"`` / ``$@"…"``, and C++ raw ``R"delim( … )delim"``

    **Comments are always blanked. A string literal is blanked ONLY when its body contains an
    interop marker** (``DllImport``/``ComImport``/``gcnew``/``CoCreateInstance``/``#using``/…).
    A plain argument string (``"nativecalc"``, ``"Excel.Application"``) contains no marker, so it
    survives verbatim — that is essential, because the seam-detecting regexes read the native
    library / ProgID *out of* that argument string. Self-contained: an unterminated construct
    consumes to end-of-input (never raises); replacing with spaces (not removal) keeps offsets
    stable, which keeps the scan deterministic."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        # line comment — always blanked.
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue

        # block comment — always blanked (newlines preserved so line structure survives).
        if c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            end = n if j == -1 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:end]))
            i = end
            continue

        # C# verbatim / interpolated-verbatim string: @"…" or $@"…" or @$"…"
        if (c in "@$") and _starts_verbatim(text, i):
            i = _consume_verbatim(text, i, out)
            continue

        # C++ raw string: R"delim( … )delim" (optionally prefixed by encoding L/u/U/u8)
        if _raw_string_start(text, i) is not None:
            i = _consume_raw_string(text, i, _raw_string_start(text, i), out)
            continue

        # regular string / char literal with backslash escapes
        if c == '"' or c == "'":
            i = _consume_quoted(text, i, c, out)
            continue

        out.append(c)
        i += 1
    return "".join(out)


def _emit_string(out: list[str], opener: str, body: str, closer: str) -> None:
    """Emit a consumed string literal, blanking the BODY only if it carries an interop marker.

    The opener/closer (quote + any C#/C++ prefix) are kept VERBATIM so the seam-detecting
    regexes still anchor on them (a ``[DllImport("nativecalc")]`` argument regex needs the
    quote chars). The body is kept verbatim unless it contains marker syntax, in which case it
    is blanked to a same-length run (newlines preserved) so a sample/doc string can't mint a
    phantom seam while keeping byte offsets stable (deterministic scan)."""
    out.append(opener)
    if _MARKER_IN_STRING_RE.search(body):
        out.append("".join("\n" if ch == "\n" else " " for ch in body))
    else:
        out.append(body)
    out.append(closer)


def _starts_verbatim(text: str, i: int) -> bool:
    """True iff position *i* begins a C# verbatim string: @"… , $@"… or @$"… ."""
    c = text[i]
    j = i + 1
    n = len(text)
    if c == "@":
        if j < n and text[j] == "$":
            j += 1
        return j < n and text[j] == '"'
    if c == "$":
        if j < n and text[j] == "@":
            return j + 1 < n and text[j + 1] == '"'
        # $"…" is an ordinary (escaped) string; handled by _consume_quoted, not here.
        return False
    return False


def _consume_verbatim(text: str, i: int, out: list[str]) -> int:
    """Consume a C# verbatim string starting at *i*; emit it (marker-aware); return new index.

    Inside ``@"…"`` there are no backslash escapes; a doubled ``""`` is a literal quote (stays
    inside the string body)."""
    n = len(text)
    # the @/$ prefix up to and including the opening quote
    j = i
    while j < n and text[j] != '"':
        j += 1
    opener = text[i:j + 1] if j < n else text[i:j]  # prefix + opening quote (if present)
    if j >= n:
        out.append(" " * (j - i))
        return n
    j += 1  # past opening quote
    body_start = j
    while j < n:
        if text[j] == '"':
            if j + 1 < n and text[j + 1] == '"':  # "" -> embedded quote, stays in body
                j += 2
                continue
            _emit_string(out, opener, text[body_start:j], '"')
            return j + 1
        j += 1
    _emit_string(out, opener, text[body_start:n], "")  # unterminated -> to EOF
    return n


def _raw_string_start(text: str, i: int) -> str | None:
    """If *i* begins a C++ raw string ``R"delim(``, return the closing token ``)delim"``.

    Accepts an optional encoding prefix (``L`` / ``u`` / ``U`` / ``u8``) before ``R``."""
    n = len(text)
    j = i
    # optional encoding prefix
    for pref in ("u8", "L", "u", "U"):
        if text.startswith(pref, j):
            j += len(pref)
            break
    if not text.startswith('R"', j):
        return None
    j += 2
    k = text.find("(", j)
    if k == -1:
        return None
    delim = text[j:k]
    return ")" + delim + '"'


def _consume_raw_string(text: str, i: int, close: str, out: list[str]) -> int:
    """Consume a C++ raw string from *i*; emit it (marker-aware); return new index."""
    n = len(text)
    body_start = text.find("(", i)
    opener = text[i:body_start + 1]  # prefix + R"delim(
    end = text.find(close, body_start + 1)
    if end == -1:
        _emit_string(out, opener, text[body_start + 1:n], "")  # unterminated -> to EOF
        return n
    _emit_string(out, opener, text[body_start + 1:end], close)
    return end + len(close)


def _consume_quoted(text: str, i: int, quote: str, out: list[str]) -> int:
    """Consume a regular ``"…"`` / ``'…'`` literal (backslash escapes) from *i*; return new index.

    The body (with backslash escapes collapsed to spaces for marker-scanning) is kept verbatim
    unless it carries an interop marker; markers never contain ``\\`` so collapsing escapes is
    safe for the marker check while the kept body stays byte-for-byte identical to the source."""
    n = len(text)
    j = i + 1
    body_chars: list[str] = []
    while j < n:
        ch = text[j]
        if ch == "\\" and j + 1 < n:  # escaped char — keep both chars in the body
            body_chars.append(text[j:j + 2])
            j += 2
            continue
        if ch == quote:
            _emit_string(out, quote, "".join(body_chars), quote)
            return j + 1
        if ch == "\n":  # newline ends a non-verbatim literal (defensive)
            _emit_string(out, quote, "".join(body_chars), "")
            out.append("\n")
            return j + 1
        body_chars.append(ch)
        j += 1
    _emit_string(out, quote, "".join(body_chars), "")  # unterminated -> to EOF
    return n


def _norm(path: Path, repo: Path) -> str:
    """Repo-relative POSIX path (stable across OSes)."""
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _csproj_id(csproj: Path, repo: Path) -> str:
    return f"csharp:csproj:{_norm(csproj, repo)}"


def _vcxproj_id(vcxproj: Path, repo: Path) -> str:
    # Same scheme as extract_msbuild_cpp._vcxproj_id (the C++ L0 owner id).
    return f"cpp:vcxproj:{_norm(vcxproj, repo)}"


def _normalize_lib(raw: str) -> str:
    """Strip trailing .dll/.so/.dylib (case-insensitive) from a native library name."""
    return _STRIP_EXT.sub("", raw)


def _normalize_assembly(raw: str) -> str:
    """Normalize a #using <Foo.dll> assembly name → bare assembly (extension stripped)."""
    # Keep only the file stem (drop any path) then strip a .dll extension.
    return _STRIP_EXT.sub("", Path(raw.replace("\\", "/")).name)


def _normalize_com(raw: str) -> str:
    """Normalize a COM ProgID / CLSID / type-library reference to a stable id token.

    Keeps the verbatim identity for ProgIDs/GUIDs; for a ``#import "X.tlb"`` reference,
    strips the path and the ``.tlb`` extension so ``..\\lib\\Office.tlb`` → ``Office``.
    """
    s = raw.strip()
    base = Path(s.replace("\\", "/")).name
    return re.sub(r'\.(?:tlb|olb|dll|exe)$', "", base, flags=re.IGNORECASE) or s


def _find_cs_files(repo: Path) -> list[Path]:
    """Walk repo for *.cs, skipping build-output and VCS dirs."""
    repo_resolved = repo.resolve()
    out: list[Path] = []
    for p in repo.rglob("*.cs"):
        if any(part.lower() in _SKIP_DIRS for part in p.parts):
            continue
        rp = p.resolve()
        # Drop symlinks/junctions whose target escapes the repo: rglob follows them (on
        # Windows too), and _norm's relative_to() would then raise ValueError.
        if not rp.is_relative_to(repo_resolved):
            continue
        out.append(rp)
    return sorted(out)


def _owning_csproj(cs_file: Path, repo: Path) -> Path | None:
    """Walk up from *cs_file* toward *repo* root; return first dir that contains a *.csproj.

    Returns ``None`` if no .csproj ancestor is found within the repo boundary.
    """
    repo_resolved = repo.resolve()
    candidate = cs_file.parent.resolve()
    while True:
        csproj_files = sorted(candidate.glob("*.csproj"))
        if csproj_files:
            return csproj_files[0]
        if candidate == repo_resolved:
            return None
        parent = candidate.parent
        if parent == candidate:
            # Filesystem root — give up.
            return None
        candidate = parent


def _find_files(repo: Path, exts: tuple[str, ...]) -> list[Path]:
    """Walk repo for files with any of *exts* (lowercase), skipping build-output/VCS dirs.

    Generalisation of ``_find_cs_files`` so the C++ and ``.vcxproj`` scans share the same
    skip-list and symlink-escape guard. Result is sorted for deterministic iteration.
    """
    repo_resolved = repo.resolve()
    out: list[Path] = []
    for p in repo.rglob("*"):
        if p.suffix.lower() not in exts:
            continue
        if any(part.lower() in _SKIP_DIRS for part in p.parts):
            continue
        rp = p.resolve()
        if not rp.is_relative_to(repo_resolved):
            continue
        if rp.is_file():
            out.append(rp)
    return sorted(out)


def _owning_vcxproj(src_file: Path, repo: Path) -> Path | None:
    """Walk up from *src_file* toward *repo* root; first dir containing a *.vcxproj wins.

    Mirrors ``_owning_csproj`` for the C++ side (same upward-walk, same boundary guard).
    """
    repo_resolved = repo.resolve()
    candidate = src_file.parent.resolve()
    while True:
        vcxproj_files = sorted(candidate.glob("*.vcxproj"))
        if vcxproj_files:
            return vcxproj_files[0]
        if candidate == repo_resolved:
            return None
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def _vcxproj_is_managed(vcxproj: Path) -> bool:
    """True iff a .vcxproj declares /clr (``<CLRSupport>`` != false, or ``<CompileAsManaged>``)."""
    try:
        root = ET.parse(vcxproj).getroot()
    except (ET.ParseError, OSError):
        return False
    for el in root.iter():
        tag = el.tag.split("}", 1)[-1].lower()
        if tag in _CLR_TAGS:
            val = (el.text or "").strip().lower()
            # <CLRSupport>false</CLRSupport> is the native default — not a bridge.
            if val and val != "false":
                return True
    return False


def _add_boundary(targets: dict[str, dict[str, Any]], tid: str, name: str, extra_tags: list[str]) -> None:
    """Ensure an external native boundary target *tid* exists (idempotent)."""
    if tid not in targets:
        targets[tid] = {
            "id": tid,
            "name": name,
            "type": "shared_lib",
            "language": "native",          # schema enum: native | cpp | csharp | idl (§16#15)
            "external": True,
            "tags": sorted(set(["external"] + extra_tags)),
        }


def _scan_csharp_com(text: str) -> list[tuple[str, str]]:
    """Detect COM usage in one C# source. Returns sorted ``(com_name, detail)`` tuples.

    Signals (plan §16#15): a ``[ComImport]``-decorated type (its ``[Guid("…")]`` names the
    boundary), ``Marshal.GetActiveObject("ProgID")``, and ``Activator.CreateInstance`` over
    ``Type.GetTypeFromProgID``/``GetTypeFromCLSID``. A bare ``[ComImport]`` with no Guid maps
    to a single ``com:comimport`` boundary so the seam is still represented.
    """
    found: dict[str, str] = {}   # com_name -> first detail seen (deterministic via sort below)

    if _COM_IMPORT_RE.search(text):
        # Pair each [ComImport] with the [Guid("…")] decorating the SAME type — search a small
        # window around each [ComImport] occurrence rather than collecting EVERY [Guid] in the
        # file (unrelated [Guid]-decorated non-COM types must not mint phantom com: boundaries).
        paired = False
        for cm in _COM_IMPORT_RE.finditer(text):
            window = text[max(0, cm.start() - 160):cm.end() + 160]
            gm = _GUID_ATTR_RE.search(window)
            if gm:
                raw = gm.group("guid")
                found.setdefault(_normalize_com(raw), f'[ComImport] [Guid("{raw}")]')
                paired = True
        if not paired:
            found.setdefault("comimport", "[ComImport] interface")

    for m in _GET_ACTIVE_OBJECT_RE.finditer(text):
        name = _normalize_com(m.group("id"))
        found.setdefault(name, f'Marshal.GetActiveObject("{m.group("id")}")')

    for m in _PROGID_RE.finditer(text):
        name = _normalize_com(m.group("id"))
        found.setdefault(name, f'GetTypeFromProgID("{m.group("id")}")')

    for m in _CLSID_GUID_RE.finditer(text):
        name = _normalize_com(m.group("id"))
        found.setdefault(name, f'GetTypeFromCLSID("{m.group("id")}")')

    return sorted(found.items())


def _scan_cpp_com(text: str) -> list[tuple[str, str]]:
    """Detect COM usage in one C/C++ source. Returns sorted ``(com_name, detail)`` tuples.

    Signals (plan §16#15): ``#import "X.tlb"`` (names a type library boundary),
    ``CoCreateInstance``/``CoGetClassObject`` activation, and ``IID_*``/``CLSID_*`` identity
    tokens. Activation/identity calls without a named type library collapse to a single
    ``com:cocreate`` boundary keyed by the strongest available identity token.
    """
    found: dict[str, str] = {}

    for m in _IMPORT_TLB_RE.finditer(text):
        name = _normalize_com(m.group("tlb"))
        found.setdefault(name, f'#import "{m.group("tlb")}"')

    # Prefer a concrete CLSID_/IID_ identity to name the boundary; fall back to a generic
    # cocreate marker if a CoCreateInstance/CoGetClassObject call has no nearby identity token.
    iid_names = [m.group("name") for m in _IID_CLSID_RE.finditer(text)]
    if _CO_CREATE_RE.search(text):
        if iid_names:
            for nm in iid_names:
                found.setdefault(nm, f"CoCreateInstance (CLSID_{nm}/IID_{nm})")
        else:
            found.setdefault("cocreate", "CoCreateInstance")
    elif iid_names:
        # Identity tokens present without an activation call still evidence a COM contract.
        for nm in iid_names:
            found.setdefault(nm, f"IID_/CLSID_{nm}")

    return sorted(found.items())


def build_fragment(repo: Path) -> dict[str, Any] | None:
    """Scan *repo* for P/Invoke + C++/CLI + COM interop; return a fragment or None if nothing.

    Three additive scans share one accumulator so a repo with only one signal still
    produces a minimal, valid fragment, and a repo with none stays a byte-identical no-op.
    """
    repo = repo.resolve()

    # (owner_id, boundary_id) -> first evidence detail seen (first-detail-wins de-dup).
    hits: dict[tuple[str, str], str] = {}
    # id -> owning build target (csproj / vcxproj). normalize merges the richer build-graph
    # target by id, so a minimal stub is enough for referential integrity here.
    owner_targets: dict[str, dict[str, Any]] = {}
    # id -> external boundary target (native:lib / native:clr / com).
    boundary_targets: dict[str, dict[str, Any]] = {}
    # owner_id -> extra tags to merge onto the owning target (e.g. interop:cppcli).
    owner_tags: dict[str, set[str]] = {}

    def _ensure_csproj(proj_id: str, csproj: Path) -> None:
        if proj_id not in owner_targets:
            owner_targets[proj_id] = {
                "id": proj_id,
                "name": csproj.stem,
                "type": "csproj",
                "language": "csharp",
            }

    def _ensure_vcxproj(proj_id: str, vcxproj: Path) -> None:
        if proj_id not in owner_targets:
            owner_targets[proj_id] = {
                "id": proj_id,
                "name": vcxproj.stem,
                "type": "shared_lib",
                "language": "cpp",
            }

    # --- Phase 1: C# P/Invoke (existing behaviour, intact) -----------------------
    for cs_file in _find_cs_files(repo):
        try:
            raw = cs_file.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        # Precision (§16#15 / 5c): blank comments + string literals so a commented-out or
        # quoted [DllImport]/[ComImport]/GetTypeFromProgID token can't mint a phantom seam.
        text = strip_comments_and_strings(raw)

        pinvoke = list(_PINVOKE_RE.finditer(text))
        com_cs = _scan_csharp_com(text)
        if not pinvoke and not com_cs:
            continue

        csproj = _owning_csproj(cs_file, repo)
        if csproj is None:
            continue

        proj_id = _csproj_id(csproj, repo)
        file_rel = _norm(cs_file, repo)
        _ensure_csproj(proj_id, csproj)

        for m in pinvoke:
            raw_lib = m.group("lib")
            normalized = _normalize_lib(raw_lib)
            lib_id = f"native:lib:{normalized}"
            _add_boundary(boundary_targets, lib_id, normalized, ["native"])
            edge_key = (proj_id, lib_id)
            if edge_key not in hits:
                # The matched attribute travels with this hit (no fragile whole-file
                # re-search), so a lib imported via BOTH DllImport and LibraryImport
                # reports the attribute that actually produced this (first) edge.
                attr_name = "LibraryImport" if m.group("attr").lower() == "libraryimport" else "DllImport"
                hits[edge_key] = f'[{attr_name}("{raw_lib}")] in {file_rel}'

        # --- Phase 2a: C# COM (same owning csproj) ------------------------------
        for com_name, detail in com_cs:
            com_id = f"com:{com_name}"
            _add_boundary(boundary_targets, com_id, com_name, ["com"])
            edge_key = (proj_id, com_id)
            if edge_key not in hits:
                hits[edge_key] = f"{detail} in {file_rel}"

    # --- Phase 2b/3: C++ COM + C++/CLI bridges -----------------------------------
    # Two sources of /clr evidence per .vcxproj: the project switch (CLRSupport /
    # CompileAsManaged) and managed markers in its translation units. Both tag the
    # owning target interop:cppcli; a `#using <Foo.dll>` additionally names a CLR boundary.
    for vcxproj in _find_files(repo, (".vcxproj",)):
        if _vcxproj_is_managed(vcxproj):
            proj_id = _vcxproj_id(vcxproj, repo)
            _ensure_vcxproj(proj_id, vcxproj)
            owner_tags.setdefault(proj_id, set()).add("interop:cppcli")

    for src in _find_files(repo, _CPP_EXTS):
        try:
            raw = src.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        # Precision (§16#15 / 5c): blank comments + marker-bearing string literals so a
        # commented-out #using / CoCreateInstance / ref class token can't mint a phantom seam.
        text = strip_comments_and_strings(raw)

        usings = list(_USING_RE.finditer(text))
        is_cppcli = bool(usings) or bool(_CPPCLI_MARKER_RE.search(text))
        com_cpp = _scan_cpp_com(text)
        if not is_cppcli and not com_cpp:
            continue

        vcxproj = _owning_vcxproj(src, repo)
        if vcxproj is None:
            continue
        proj_id = _vcxproj_id(vcxproj, repo)
        _ensure_vcxproj(proj_id, vcxproj)
        file_rel = _norm(src, repo)

        # Phase 3: C++/CLI bridge. Mark the owning target as a managed↔native seam, and
        # for each named assembly (`#using <Foo.dll>`) emit an edge to a native:clr boundary.
        if is_cppcli:
            owner_tags.setdefault(proj_id, set()).add("interop:cppcli")
            for u in usings:
                asm = _normalize_assembly(u.group("asm"))
                if not asm:
                    continue
                clr_id = f"native:clr:{asm}"
                _add_boundary(boundary_targets, clr_id, asm, ["clr"])
                edge_key = (proj_id, clr_id)
                if edge_key not in hits:
                    hits[edge_key] = f'#using <{u.group("asm")}> in {file_rel}'

        # Phase 2b: C++-side COM.
        for com_name, detail in com_cpp:
            com_id = f"com:{com_name}"
            _add_boundary(boundary_targets, com_id, com_name, ["com"])
            edge_key = (proj_id, com_id)
            if edge_key not in hits:
                hits[edge_key] = f"{detail} in {file_rel}"

    if not hits and not owner_tags:
        return None

    # Apply accumulated owner tags (sorted, de-duped) onto the owning targets.
    for proj_id, tags in owner_tags.items():
        # A managed .vcxproj/.cpp may have no boundary edge (e.g. only `gcnew`); ensure the
        # owning target still appears so the interop:cppcli tag is not orphaned.
        if proj_id not in owner_targets:
            continue
        existing = owner_targets[proj_id].get("tags", [])
        owner_targets[proj_id]["tags"] = sorted(set(existing) | tags)

    if not owner_targets and not boundary_targets:
        return None

    # Build targets list (owners first, then boundaries; each sorted by id).
    targets: list[dict[str, Any]] = sorted(owner_targets.values(), key=lambda t: t["id"])
    targets += sorted(boundary_targets.values(), key=lambda t: t["id"])

    # Build relationships (sorted by (source, target)).
    relationships: list[dict[str, Any]] = []
    for (owner_id, boundary_id), detail in sorted(hits.items()):
        relationships.append(
            make_relationship(
                source=owner_id,
                target=boundary_id,
                evidence=[{"type": "interop", "detail": detail}],
            )
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR]},
        "targets": targets,
        "relationships": relationships,
    }


def run(ws: Workspace) -> Path | None:
    """Scan the repo for P/Invoke + C++/CLI + COM interop and write the fragment.

    Returns the fragment path if any interop signal was found, or ``None`` if the repo has
    no native interop (graceful degrade — keeps repos without P/Invoke / C++/CLI / COM
    byte-identical to runs that did not call this stage).
    """
    fragment = build_fragment(ws.repo)
    if fragment is None:
        return None
    out = ws.fragments / "csharp-interop.json"
    dump_json(fragment, out)
    return out
