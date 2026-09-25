"""Stage: shared-contract / codegen-contract detection (plan §16#14, §4.1, §21.3).

Models a **shared cross-component interface definition** — a ``.proto`` / Thrift /
FlatBuffers / WSDL / Avro / ``.idl`` file — as a **first-class architectural element**
(a ``type:"contract"``, ``language:"idl"`` target) and links every build target that
*references* that contract to it with a medium-confidence ``package_ref`` edge
(plan §21.3: "shared contract == ``package_ref`` evidence, medium confidence").

This is the §16#14 (iii) policy made concrete: generated *code* (``*.pb.cc`` /
``*.pb.go`` / ``*_pb2.py`` / generated ``*.cs``) stays excluded as a renderable element,
but the **contract the generator produces** is kept and rendered. The scoped-exclusion
spirit of §4.1 is honoured by *keeping the definition file* while *excluding* the
generated outputs (we never walk into generated dirs, and we never treat a generated
file as a contract).

Why a directory heuristic (and not real target ids)?
  Extractors are independent and do **not** see other extractors' fragments, so we cannot
  resolve the authoritative ``csharp:csproj:<path>`` / ``cpp:target:<name>`` id of a
  referencing project without the build graph. We therefore:
    * derive the **same stable id the build-graph extractors use** when a sibling/ancestor
      ``.csproj`` / ``.vcxproj`` is found above the referencing source file (so the edge
      joins the real graph after normalize merges by id; CMake dirs are NOT guessed —
      target names differ from dir names, see :func:`_derive_endpoint_id`), and
    * otherwise emit a ``dir:<repo-relative-path>`` **placeholder** endpoint and leave it
      for a post-merge rebinding pass. That pass lives in
      :func:`anon.stages.normalize_facts.rebind_contract_dirs` (run post-merge, right
      after the fragments are merged and BEFORE the extract-stage referential-integrity gate),
      which resolves each ``dir:`` endpoint against the merged targets' ``path`` fields and
      prunes any edge whose placeholder can't be resolved — mirroring how ``interop_resolve``
      rebinds ``native:lib:*`` to real ``cpp:target:*`` after merge. (Curate's
      ``apply_mapping_rules._rebind`` uses the same rule but, post-normalize, finds no ``dir:``
      endpoints left and is effectively a no-op.)

Graceful degradation: NO contract definition files anywhere → ``run`` returns ``None`` and
writes nothing, keeping repos without shared contracts (the toy fixture, most pilots)
byte-identical to a run that did not call this stage at all.

Stable ids (plan §6.3):
  contract target : ``contract:<kind>:<file-stem>``  (kind ∈ proto|thrift|flatbuffers|wsdl|avro|idl)
  referencing dir : ``csharp:csproj:<path>`` | ``cpp:vcxproj:<path>`` | ``dir:<path>`` (placeholder)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..jsonio import dump_json
from ..model import make_relationship
from ..paths import Workspace

EXTRACTOR = {"name": "shared-contracts", "version": "0.2.0"}  # 0.2.0: no cpp:target guess

# Contract-definition file extension -> contract kind (the <kind> in the stable id).
# ``.idl`` (CORBA/COM/Web IDL) is the generic fallback kind.
_CONTRACT_KINDS = {
    ".proto": "proto",
    ".thrift": "thrift",
    ".fbs": "flatbuffers",
    ".wsdl": "wsdl",
    ".avsc": "avro",
    ".idl": "idl",
}

# Source files that may *reference* a contract (consumers/producers of the generated code).
_SOURCE_EXTS = {".cs", ".cpp", ".cc", ".h", ".hpp", ".go", ".py"}

# Directories never walked: build output, VCS, deps, and generated-code sinks. Keeping the
# DEFINITION file but excluding GENERATED outputs is the §4.1 scoped-exclusion rule.
_SKIP_DIRS = {
    "bin", "obj", ".git", "node_modules", ".cache", "generated", "gen",
    "build", "out", "target", "__pycache__", "dist",
}

# Generated-code filename signatures — these are codegen *outputs*, never contract sources,
# and are skipped even if they live outside a _SKIP_DIRS directory (§4.1).
_GENERATED_RE = re.compile(
    r"(?:\.pb\.(?:cc|h|go)|_pb2(?:_grpc)?\.py|\.pb\.cs|grpc\.pb\.|\.generated\.)",
    re.IGNORECASE,
)


def _norm(path: Path, repo: Path) -> str:
    """Repo-relative POSIX path (stable across OSes)."""
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _is_skipped(path: Path, repo: Path) -> bool:
    """True if any *repo-relative* path segment is a skipped dir.

    Only segments BELOW the repo root are considered, so a repo whose own root dir happens
    to be named like a skip dir (e.g. a temp dir ``gen/``) is not skipped wholesale.
    """
    try:
        rel = path.resolve().relative_to(repo.resolve())
    except ValueError:
        return True
    return any(part.lower() in _SKIP_DIRS for part in rel.parts)


def _find_contract_files(repo: Path) -> list[tuple[Path, str]]:
    """Return sorted ``(path, kind)`` for every contract-definition file under *repo*."""
    repo_resolved = repo.resolve()
    out: list[tuple[Path, str]] = []
    for ext, kind in _CONTRACT_KINDS.items():
        for p in repo.rglob(f"*{ext}"):
            if _is_skipped(p, repo):
                continue
            rp = p.resolve()
            # rglob follows junctions/symlinks (Windows too); drop any whose target
            # escapes the repo so _norm's relative_to() can't raise.
            if not rp.is_relative_to(repo_resolved):
                continue
            if _GENERATED_RE.search(rp.name):
                continue
            out.append((rp, kind))
    return sorted(out, key=lambda pk: (pk[0].as_posix(), pk[1]))


def _find_source_files(repo: Path) -> list[Path]:
    """Walk *repo* for referencing source files, skipping build/VCS/generated dirs."""
    repo_resolved = repo.resolve()
    out: list[Path] = []
    for p in repo.rglob("*"):
        if p.suffix.lower() not in _SOURCE_EXTS:
            continue
        if _is_skipped(p, repo):
            continue
        rp = p.resolve()
        if not rp.is_relative_to(repo_resolved):
            continue
        if _GENERATED_RE.search(rp.name):
            continue
        out.append(rp)
    return sorted(out)


def _reference_patterns(stem: str, kind: str) -> list[re.Pattern[str]]:
    """Patterns that indicate a source file consumes/produces the ``<stem>`` contract.

    For ``.proto`` we look for ``import "x.proto"``, a generated include/import
    (``x.pb.h`` / ``import x_pb2`` / ``Grpc.X``), or a bare reference to the message
    namespace ``X``. For other IDLs we match the stem and its generated artefacts.
    """
    s = re.escape(stem)
    pats = [
        # import "x.proto" / include "x.thrift" / <wsdl ... location="x.wsdl">
        re.compile(rf'''["'<]{s}\.(?:proto|thrift|fbs|wsdl|avsc|idl)\b''', re.IGNORECASE),
        # generated C/C++ include:  #include "x.pb.h"  /  "x_generated.h" (flatbuffers)
        re.compile(rf'''{s}(?:\.pb|_generated|\.grpc\.pb)\.h\b''', re.IGNORECASE),
        # generated Python import:  import x_pb2  /  from x_pb2 import ...
        re.compile(rf'''\b{s}_pb2(?:_grpc)?\b''', re.IGNORECASE),
        # generated C#/Go grpc namespace:  Grpc.X  /  using X;  reference to message stem
        re.compile(rf'''\bGrpc\.{s}\b''', re.IGNORECASE),
        # bare capitalised reference to the contract/message stem (e.g. `Orders.`),
        # word-bounded and case-sensitive so it isn't matched by an unrelated lowercase word.
        re.compile(rf'''\b{re.escape(stem[:1].upper() + stem[1:])}\b'''),
    ]
    return pats


def _derive_endpoint_id(src_file: Path, repo: Path) -> str:
    """Best-effort stable id of the build target that owns *src_file* (§6.3).

    Walks up from *src_file* toward the repo root and returns the id the build-graph
    extractors would assign to the first enclosing project, so the edge JOINS the real
    graph after normalize merges by id:
      * a ``*.csproj``      -> ``csharp:csproj:<repo-relative-path>`` (extract_build_graph)
      * a ``*.vcxproj``     -> ``cpp:vcxproj:<repo-relative-path>``   (extract_msbuild_cpp)
    Everything else — including CMake-owned sources — falls back to a
    ``dir:<repo-relative-dir>`` **placeholder**, resolved (or pruned) post-merge by
    :func:`anon.stages.normalize_facts.rebind_contract_dirs`. A CMake dir name is
    deliberately NOT guessed into a ``cpp:target:<dir-name>`` id: CMake target names
    routinely differ from their directory (opencv's ``modules/video`` builds
    ``opencv_video``), and a guessed id that no build fragment declares fails the
    referential-integrity gate; the ``dir:`` placeholder resolves by *path* against the
    merged targets instead, which is the correct join key.
    """
    repo_resolved = repo.resolve()
    candidate = src_file.parent.resolve()
    while True:
        csproj = sorted(candidate.glob("*.csproj"))
        if csproj:
            return f"csharp:csproj:{_norm(csproj[0], repo)}"
        vcxproj = sorted(candidate.glob("*.vcxproj"))
        if vcxproj:
            return f"cpp:vcxproj:{_norm(vcxproj[0], repo)}"
        if candidate == repo_resolved:
            break
        parent = candidate.parent
        if parent == candidate:  # filesystem root
            break
        candidate = parent
    return f"dir:{_norm(src_file.parent, repo)}"


def build_fragment(repo: Path) -> dict[str, Any] | None:
    """Scan *repo* for shared contracts + references; return a fragment dict or None."""
    repo = repo.resolve()

    contract_files = _find_contract_files(repo)
    if not contract_files:
        return None

    source_files = _find_source_files(repo)

    # contract id -> target dict
    contract_targets: dict[str, dict[str, Any]] = {}
    # (endpoint_id, contract_id) -> stem (for the evidence detail; first wins)
    edges: dict[tuple[str, str], str] = {}

    for cfile, kind in contract_files:
        stem = cfile.stem
        contract_id = f"contract:{kind}:{stem}"
        if contract_id not in contract_targets:
            contract_targets[contract_id] = {
                "id": contract_id,
                "name": stem,
                "type": "contract",
                "language": "idl",
                "external": True,
                "path": _norm(cfile, repo),
                "tags": ["contract", "external"],
            }

        patterns = _reference_patterns(stem, kind)
        for src in source_files:
            try:
                text = src.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            if not any(p.search(text) for p in patterns):
                continue
            endpoint = _derive_endpoint_id(src, repo)
            key = (endpoint, contract_id)
            if key not in edges:
                edges[key] = stem

    targets: list[dict[str, Any]] = sorted(contract_targets.values(), key=lambda t: t["id"])

    relationships: list[dict[str, Any]] = []
    for (endpoint, contract_id), stem in sorted(edges.items()):
        rel = make_relationship(
            source=endpoint,
            target=contract_id,
            evidence=[{"type": "package_ref", "detail": f"references {stem}"}],
            kind="uses contract",
        )
        rel["tags"] = ["contract", "cross-language"]
        # §21.3: a shared contract consumed by both sides is a real, machine-checkable
        # cross-language signal — medium confidence. The generic compute_confidence rule
        # scores a lone package_ref as `low` (it isn't in the corroborating set), so we
        # pin the §21.3 medium value here rather than weakening the shared model rule.
        rel["confidence"] = "medium"
        relationships.append(rel)

    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR]},
        "targets": targets,
        "relationships": relationships,
    }


def run(ws: Workspace) -> Path | None:
    """Scan the repo for shared contracts and write the fragment.

    Returns the fragment path if at least one contract-definition file was found, or
    ``None`` if the repo has no shared contracts (graceful degrade — keeps repos without
    ``.proto``/IDL/WSDL byte-identical to runs that did not call this stage).
    """
    fragment = build_fragment(ws.repo)
    if fragment is None:
        return None
    out = ws.fragments / "contracts.json"
    dump_json(fragment, out)
    return out
