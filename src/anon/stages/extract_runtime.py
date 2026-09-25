"""Stage — runtime-topology extractor (the §5 ``runtime`` evidence kind).

Produces the §5 ``runtime`` evidence kind, which is in the schema enum but was
never emitted by any extractor. Runtime topology is a **deterministic, static**
signal mined from .NET Aspire AppHost wiring and DI registrations.

**Design invariant (§5 / §6.3): runtime edges MUST NOT feed the build-edge weight.**
The build graph is the source of structural truth; a runtime ``.WithReference(x)``
is not a build/link dependency and must never raise the §7.4 significance of a
build edge. We therefore emit runtime facts into the **deployment facet**
(``deployment.nodes`` / ``deployment.edges``, schema ``deploymentNode`` /
``deploymentEdge``) — NEVER into build ``relationships[]``. Curation never
weight-drops the deployment facet, so this sidesteps the threshold entirely and is
plan-faithful (§6.3, §9.3/§9.4). The fragment shape mirrors
``extract_deployment.py`` exactly so ``normalize_facts._merge_deployment`` unions
the two fragments order-independently (the §18.4 byte-identical guarantee).

Two runtime sources (both high-signal for the C# pilot, e.g. dotnet/eShop):

1. **.NET Aspire AppHost** — the strongest deterministic runtime signal. The
   AppHost ``Program.cs`` declares the runtime workloads, the infra resources
   (Postgres/Redis/RabbitMQ/...), and their ``.WithReference(...)`` wiring.
2. **Hosted services / DI** — ``AddHostedService<TWorker>()`` marks a background
   runtime node. Conservative (a node, plus a from-host ``calls`` edge if the host
   image is resolvable).

Graceful degradation (§18.4): no AppHost / no Aspire / no hosted services →
``run`` writes NOTHING and returns ``None`` (additive; preserves byte-identity of
build-only repos). Malformed/odd input is never fatal — files are read with
``utf-8-sig`` and exceptions are logged and skipped.

A second concern (§9.4 "test-derived (near-future)" input): :func:`mine_test_scenarios`
scans test files for ordered call sequences and proposes **scenario candidates over
EXISTING curated container edges only** (a step naming a non-existent edge is dropped,
never invented — principle #3). :func:`write_scenario_candidates` writes them to
``<generated>/scenario-candidates.yaml`` as an advisory artifact a human promotes
into ``rules/scenarios.yaml``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..jsonio import dump_json
from ..paths import Workspace

log = logging.getLogger("anon.extract_runtime")

EXTRACTOR: dict[str, str] = {"name": "runtime-topology", "version": "0.1.0"}

# ---------------------------------------------------------------------------
# Aspire resource-builder -> (infra subkind, technology). The builder name maps a
# resource to an `infra:<subkind>:<name>` node (§4.4 classification mirrors the
# deployment extractor's _INFRA_IMAGES table). Matched case-insensitively against
# `Add<Builder>("name")`.
# ---------------------------------------------------------------------------
_RESOURCE_BUILDERS: dict[str, tuple[str, str]] = {
    "addpostgres": ("database", "PostgreSQL"),
    "addsqlserver": ("database", "SQL Server"),
    "addmysql": ("database", "MySQL"),
    "addmongodb": ("database", "MongoDB"),
    "addcosmosdb": ("database", "Azure Cosmos DB"),
    "addazurecosmosdb": ("database", "Azure Cosmos DB"),
    "addredis": ("cache", "Redis"),
    "addgarnet": ("cache", "Garnet"),
    "addvalkey": ("cache", "Valkey"),
    "addrabbitmq": ("queue", "RabbitMQ"),
    "addkafka": ("queue", "Kafka"),
    "addazureservicebus": ("queue", "Azure Service Bus"),
    "addnats": ("queue", "NATS"),
    "addazureblob": ("storage", "Azure Blob Storage"),
    "addazureblobstorage": ("storage", "Azure Blob Storage"),
    "addazurequeue": ("queue", "Azure Queue Storage"),
    "addazuretable": ("storage", "Azure Table Storage"),
    "addazurestorage": ("storage", "Azure Storage"),
    "addelasticsearch": ("storage", "Elasticsearch"),
    "addqdrant": ("storage", "Qdrant"),
    "addmilvus": ("storage", "Milvus"),
    "addkeycloak": ("ingress", "Keycloak"),
}

# A C# identifier (for resource var names, project names, reference targets).
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# var = builder.Add<Builder>("name")   (var and "name" both optional captures)
_RES_ASSIGN_RE = re.compile(
    r"(?:var\s+(?P<var>" + _IDENT + r")\s*=\s*)?"
    r"\bbuilder\s*\.\s*(?P<builder>Add" + _IDENT + r")\s*"
    r"(?:<[^>]*>\s*)?"
    r"\(\s*(?P<args>[^)]*)\)",
)

# builder.AddProject<Projects.Foo>("name")  — the workload registration. We then
# scan the same logical statement for chained .WithReference(...) / .WithEnvironment(...).
_ADD_PROJECT_RE = re.compile(
    r"\bbuilder\s*\.\s*AddProject\s*<\s*Projects\s*\.\s*(?P<proj>" + _IDENT + r")\s*>\s*"
    r"\(\s*(?P<args>[^)]*)\)",
)

# A chained .WithReference(x) — captures the referenced variable identifier.
_WITH_REFERENCE_RE = re.compile(
    r"\.\s*WithReference\s*\(\s*(?P<ref>" + _IDENT + r")",
)

# A chained .WithEnvironment("KEY", ...) — we keep only the KEY, never the value (§8.3).
_WITH_ENVIRONMENT_RE = re.compile(
    r"\.\s*WithEnvironment\s*\(\s*\"(?P<key>[^\"]*)\"",
)

# AddHostedService<TWorker>() anywhere in a *.cs file (DI background workers).
_HOSTED_SERVICE_RE = re.compile(
    r"\bAddHostedService\s*<\s*(?:[A-Za-z_][A-Za-z0-9_.]*\.)?(?P<worker>" + _IDENT + r")\s*>",
)


# ---------------------------------------------------------------------------
# Fragment node/edge builders — mirror extract_deployment.py shapes EXACTLY so the
# normalizer unions them (deploy:image:* / infra:<kind>:<name> / env:* ids;
# deploymentEdge {source,target,kind,evidence,env?}).
# ---------------------------------------------------------------------------

def _infra_node(subkind: str, name: str, technology: str) -> dict[str, Any]:
    return {
        "id": f"infra:{subkind}:{name}",
        "name": name,
        "kind": "infra",
        "subkind": subkind,
        "technology": technology,
        "external": True,
    }


def _runtime_edge(source: str, target: str, kind: str, detail: str, *,
                  env: str | None = None) -> dict[str, Any]:
    """A deployment-facet edge carrying §5 ``runtime`` evidence (never a build edge)."""
    e: dict[str, Any] = {
        "source": source,
        "target": target,
        "kind": kind,
        "evidence": [{"type": "runtime", "detail": detail}],
    }
    if env:
        e["env"] = env
    return e


def _merge_node(nodes: dict[str, dict], node: dict[str, Any]) -> None:
    nid = node["id"]
    if nid not in nodes:
        nodes[nid] = node


def _merge_edge(edges: dict[tuple, dict], edge: dict[str, Any]) -> None:
    key = (edge["source"], edge["target"], edge["kind"])
    if key not in edges:
        edges[key] = edge


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str | None:
    """Read *path* with BOM-tolerant utf-8; return None and log on any failure."""
    try:
        # utf-8-sig strips a Windows BOM; errors="replace" never raises on odd bytes.
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("skipping unreadable file %s: %s", path, exc)
        return None


def _rel_id(repo: Path, csproj: Path) -> str:
    """Stable csharp build-target id for a csproj path (mirrors extract_build_graph ids)."""
    try:
        rel = csproj.relative_to(repo)
    except ValueError:
        rel = csproj
    return f"csharp:csproj:{str(rel).replace(chr(92), '/')}"


# ---------------------------------------------------------------------------
# AppHost discovery + ProjectReference resolution
# ---------------------------------------------------------------------------

# <ProjectReference Include="..\Api\Api.csproj" />  — capture the include path.
_PROJECT_REF_RE = re.compile(
    r"<ProjectReference\b[^>]*\bInclude\s*=\s*\"(?P<path>[^\"]+)\"",
    re.IGNORECASE,
)


def _find_apphosts(repo: Path) -> list[tuple[Path, Path]]:
    """Return sorted ``(program_cs, csproj)`` pairs for every Aspire AppHost project.

    An AppHost is a project whose ``.csproj`` references ``Aspire.Hosting.AppHost``
    OR whose ``Program.cs`` contains ``DistributedApplication.CreateBuilder``.
    """
    found: dict[Path, Path] = {}  # program_cs -> csproj
    for csproj in repo.rglob("*.csproj"):
        text = _read_text(csproj)
        if text is None:
            continue
        program = csproj.parent / "Program.cs"
        is_apphost = "Aspire.Hosting.AppHost" in text
        if not is_apphost and program.exists():
            ptext = _read_text(program)
            if ptext and "DistributedApplication.CreateBuilder" in ptext:
                is_apphost = True
        if is_apphost and program.exists():
            found[program.resolve()] = csproj
    # return sorted by program path for determinism
    return [(p, found[p]) for p in sorted(found)]


# Sentinel cid for a normalized alias that two genuinely-different stems both claim. Stored
# in the alias map so `_resolve_proj`'s normalized fallback declines it (returns None) rather
# than binding a fall-through token to whichever stem happened to be processed first.
_AMBIGUOUS = "\x00ambiguous"


def _normalize_proj_token(token: str) -> str:
    """Separator-insensitive, case-insensitive key for matching a ``Projects.<Ident>``
    token against a ProjectReference stem (dynamic-view plan §5 / R4).

    ``Projects.<Ident>`` tokens are C# identifiers, so a dotted/hyphenated csproj stem
    like ``Basket.API`` appears in code as ``Projects.Basket_API`` (the dots become
    underscores). The original matcher keyed strictly on the exact stem, so
    ``project_refs.get("Basket_API")`` missed the key ``"Basket.API"`` and the genuine
    service-to-service ``.WithReference`` edge fell back to a synthetic ``deploy:image:*``
    node. Stripping ``_`` / ``.`` / ``-`` and lowercasing collapses all three forms to one
    key (``"Basket.API"`` / ``"Basket_API"`` / ``"basketapi"`` -> ``"basketapi"``)."""
    return token.replace("_", "").replace(".", "").replace("-", "").lower()


def _resolve_project_refs(repo: Path, csproj: Path) -> dict[str, str]:
    """Map ``<project-name> -> csharp:csproj:<id>`` for every ProjectReference of *csproj*.

    The project name is the referenced csproj's stem (``../Api/Api.csproj`` -> ``Api``),
    which is what ``AddProject<Projects.Api>`` resolves against. In addition to the exact
    stem key, a separator-normalized key (``_normalize_proj_token``) is inserted so that a
    ``Projects.Basket_API`` token (underscores) resolves to the ``Basket.API`` stem (dots)
    — the §5/R4 name-normalization fix. Use :func:`_resolve_proj` to look up, never a bare
    ``.get`` (it tries the exact key first, then the normalized one).

    Ambiguity guard: two genuinely-different ProjectReferences whose stems normalize to the
    SAME key (e.g. ``Basket.API`` and ``BasketApi`` both -> ``basketapi``) would otherwise
    let the alias bind to whichever was processed first, silently resolving a fall-through
    token to the WRONG csproj. When a second, DIFFERENT container id collides on a normalized
    key we mark that key AMBIGUOUS (``_AMBIGUOUS``); :func:`_resolve_proj` then declines the
    normalized fallback for it, so the edge becomes an honest unresolved/out-of-model
    observation instead. EXACT-stem resolution is unaffected — only the normalized fallback is
    suppressed for the colliding key."""
    out: dict[str, str] = {}
    # normalized key -> the single cid it unambiguously resolves to (tracked separately so a
    # collision marks ONLY the normalized alias ambiguous, never the exact-stem keys in `out`).
    norm_map: dict[str, str] = {}
    text = _read_text(csproj)
    if text is None:
        return out
    base = csproj.parent
    for m in _PROJECT_REF_RE.finditer(text):
        raw = m.group("path").replace("\\", "/")
        try:
            ref_path = (base / raw).resolve()
        except Exception:  # pragma: no cover - defensive
            continue
        name = Path(raw).stem  # "Api" from "../Api/Api.csproj"
        if not name:
            continue
        cid = _rel_id(repo, ref_path)
        if name not in out:
            out[name] = cid
        # Separator-normalized alias so Projects.Basket_API matches the Basket.API stem.
        norm = _normalize_proj_token(name)
        if not norm:
            continue
        existing = norm_map.get(norm)
        if existing is None:
            norm_map[norm] = cid
        elif existing != cid and existing != _AMBIGUOUS:
            # a SECOND, different stem normalizes to the same key — poison the alias so the
            # normalized fallback declines it (honest unresolved, not silently-wrong binding).
            norm_map[norm] = _AMBIGUOUS
    # Fold the unambiguous normalized aliases into `out` (exact stems already present win).
    for norm, cid in norm_map.items():
        out[norm] = cid
    return out


def _resolve_proj(project_refs: dict[str, str], proj: str | None) -> str | None:
    """Resolve a ``Projects.<Ident>`` token to a csproj id: exact stem first, then the
    separator-normalized alias (§5/R4). ``None`` when neither matches."""
    if not proj:
        return None
    hit = project_refs.get(proj)
    if hit is not None:
        return hit
    norm_hit = project_refs.get(_normalize_proj_token(proj))
    # An AMBIGUOUS normalized alias (two distinct stems collided) is declined — the edge
    # becomes an honest unresolved/out-of-model observation, not a silently-wrong binding.
    if norm_hit == _AMBIGUOUS:
        return None
    return norm_hit


# ---------------------------------------------------------------------------
# AppHost Program.cs parsing
# ---------------------------------------------------------------------------

def _split_statements(text: str) -> list[str]:
    """Split AppHost code into logical statements on ``;`` (chained builder calls
    span multiple physical lines but live in one statement). Comments stripped
    line-wise first so a ``//`` does not swallow a chained call."""
    lines: list[str] = []
    for raw in text.splitlines():
        # strip a line comment (best-effort; not string-aware, adequate for AppHost DSL)
        idx = raw.find("//")
        lines.append(raw if idx < 0 else raw[:idx])
    joined = "\n".join(lines)
    return [s for s in joined.split(";")]


def _arg_name(args: str) -> str | None:
    """Extract the first string-literal argument (the resource/workload name)."""
    m = re.search(r"\"([^\"]*)\"", args)
    return m.group(1) if m else None


def _parse_apphost(
    program_cs: Path,
    project_refs: dict[str, str],
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
) -> None:
    """Parse one AppHost ``Program.cs`` into runtime nodes/edges (§5)."""
    text = _read_text(program_cs)
    if text is None:
        return

    # First pass: map resource *variable name* -> node id, for every
    # `var x = builder.Add<Builder>("name")` resource declaration (infra + projects).
    var_to_node: dict[str, str] = {}
    var_to_name: dict[str, str] = {}

    for stmt in _split_statements(text):
        for m in _RES_ASSIGN_RE.finditer(stmt):
            var = m.group("var")
            builder = (m.group("builder") or "")
            args = m.group("args") or ""
            name = _arg_name(args)
            bkey = builder.lower()

            if bkey == "addproject":
                # workload — resolve <Projects.Foo> to a csproj id
                pm = re.search(
                    r"AddProject\s*<\s*Projects\s*\.\s*(" + _IDENT + r")\s*>", stmt
                )
                proj = pm.group(1) if pm else None
                node_id = _resolve_proj(project_refs, proj)
                if node_id is None:
                    # Unresolvable workload (no matching ProjectReference): fall back to a
                    # synthetic deploy:image node keyed on the declared name so its
                    # references still attach to *something* — never invent a csproj id.
                    if name:
                        node_id = f"deploy:image:{name}"
                        _merge_node(nodes, {"id": node_id, "name": name, "kind": "image"})
                if var and node_id:
                    var_to_node[var] = node_id
                    if name:
                        var_to_name[var] = name
                continue

            infra = _RESOURCE_BUILDERS.get(bkey)
            if infra and name:
                subkind, technology = infra
                node = _infra_node(subkind, name, technology)
                _merge_node(nodes, node)
                if var:
                    var_to_node[var] = node["id"]
                    var_to_name[var] = name

    # Second pass: for each AddProject<Projects.Foo>("name") statement, attach the
    # chained .WithReference(x) (calls) and .WithEnvironment("KEY") (runtime_config).
    for stmt in _split_statements(text):
        for pm in _ADD_PROJECT_RE.finditer(stmt):
            proj = pm.group("proj")
            args = pm.group("args") or ""
            pname = _arg_name(args)
            source_id = _resolve_proj(project_refs, proj)
            if source_id is None and pname:
                source_id = f"deploy:image:{pname}"
            if source_id is None:
                continue

            for rm in _WITH_REFERENCE_RE.finditer(stmt):
                ref_var = rm.group("ref")
                target_id = var_to_node.get(ref_var)
                if target_id is None:
                    # references something we never declared — drop (never invent a node)
                    continue
                if target_id == source_id:
                    continue
                detail = f"AddProject<Projects.{proj}>().WithReference({ref_var})"
                _merge_edge(edges, _runtime_edge(source_id, target_id, "calls", detail))

            for em in _WITH_ENVIRONMENT_RE.finditer(stmt):
                key = em.group("key")
                # runtime_config is a self-referential edge carrying only the KEY (§8.3).
                detail = f"AddProject<Projects.{proj}>().WithEnvironment(\"{key}\")"
                _merge_edge(edges, _runtime_edge(
                    source_id, source_id, "runtime_config", detail))


# ---------------------------------------------------------------------------
# Hosted services / DI (conservative)
# ---------------------------------------------------------------------------

def _parse_hosted_services(
    repo: Path,
    apphost_dirs: set[Path],
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
) -> None:
    """Mine ``AddHostedService<TWorker>()`` registrations into background runtime nodes.

    Conservative: each distinct worker type becomes a ``deploy:image:<Worker>`` node
    tagged ``runtime:hosted-service``. We do NOT try to resolve the *host* process
    (which csproj it runs in) beyond the file's own project, so we emit only the node
    plus, when the host csproj is resolvable, a ``calls`` edge host->worker.
    """
    workers: dict[str, str] = {}  # worker name -> host csproj id (or "")
    for cs in sorted(repo.rglob("*.cs")):
        # skip the AppHost project dir itself (already covered by the Aspire pass)
        if cs.parent.resolve() in apphost_dirs:
            continue
        text = _read_text(cs)
        if text is None or "AddHostedService" not in text:
            continue
        # resolve the owning csproj (nearest *.csproj up the tree, within repo)
        host_id = ""
        search = cs.parent
        while True:
            cands = sorted(search.glob("*.csproj"))
            if cands:
                host_id = _rel_id(repo, cands[0].resolve())
                break
            if search == repo or search.parent == search:
                break
            search = search.parent
        for m in _HOSTED_SERVICE_RE.finditer(text):
            worker = m.group("worker")
            if worker and worker not in workers:
                workers[worker] = host_id

    for worker in sorted(workers):
        node_id = f"deploy:image:{worker}"
        _merge_node(nodes, {
            "id": node_id, "name": worker, "kind": "image",
            "tags": ["runtime:hosted-service"],
        })
        host_id = workers[worker]
        if host_id:
            _merge_edge(edges, _runtime_edge(
                host_id, node_id, "calls",
                f"AddHostedService<{worker}>()"))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(ws: Workspace) -> Path | None:
    """Scan *ws.repo* for runtime topology and write a ``deployment``-facet fragment.

    Returns the fragment path on success, or ``None`` when no runtime signal is
    found (graceful degrade — preserves byte-identity of build-only repos, §18.4).
    """
    repo = ws.repo
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    apphosts = _find_apphosts(repo)
    apphost_dirs = {p.parent.resolve() for p, _ in apphosts}

    for program_cs, csproj in apphosts:
        project_refs = _resolve_project_refs(repo, csproj)
        _parse_apphost(program_cs, project_refs, nodes, edges)

    _parse_hosted_services(repo, apphost_dirs, nodes, edges)

    if not nodes and not edges:
        log.info("no runtime topology (Aspire AppHost / hosted services) under %s "
                 "— skipping fragment (§18.4)", repo)
        return None

    sorted_nodes = sorted(nodes.values(), key=lambda n: n["id"])
    sorted_edges = sorted(
        edges.values(), key=lambda e: (e["source"], e["target"], e["kind"]))

    fragment: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provenance": {"extractors": [EXTRACTOR]},
        "targets": [],
        "relationships": [],
        "deployment": {
            "nodes": sorted_nodes,
            "edges": sorted_edges,
        },
    }

    out = ws.fragments / "runtime.json"
    dump_json(fragment, out)
    log.info("runtime fragment: %d nodes, %d edges -> %s",
             len(sorted_nodes), len(sorted_edges), out)
    return out


# ---------------------------------------------------------------------------
# §9.4 test-derived scenario candidates (advisory)
# ---------------------------------------------------------------------------

# An ordered method-call / awaited-call: `await x.Foo(` | `x.Foo(` | `client.Get(`.
_CALL_RE = re.compile(
    r"(?:await\s+)?(?P<recv>" + _IDENT + r")\s*\.\s*(?P<method>" + _IDENT + r")\s*\(",
)

_TEST_GLOBS = ("**/*Tests*.cs", "**/*Test*.cs", "**/*_test.cpp")


def _container_lookup(facts: dict[str, Any]) -> dict[str, str]:
    """label -> container_id, accepting container_id / container_name / build-target id.

    Mirrors ``scenarios._build_lookup`` so candidates speak the same name vocabulary as
    the hand-authored ``scenarios.yaml``.
    """
    lookup: dict[str, str] = {}
    for t in facts.get("targets", []):
        cid = t.get("container_id", t.get("id"))
        tid = t.get("id")
        if tid and tid not in lookup:
            lookup[tid] = cid
        if cid and cid not in lookup:
            lookup[cid] = cid
        cname = t.get("container_name", "")
        if cname and cname not in lookup:
            lookup[cname] = cid
    return lookup


def _container_edges(facts: dict[str, Any]) -> set[tuple[str, str]]:
    """Set of (source_cid, target_cid) inter-container edges (mirrors scenarios.py)."""
    t2c = {t.get("id"): t.get("container_id", t.get("id"))
           for t in facts.get("targets", [])}
    edges: set[tuple[str, str]] = set()
    for r in facts.get("relationships", []):
        cs = t2c.get(r.get("source"))
        ct = t2c.get(r.get("target"))
        if cs and ct and cs != ct:
            edges.add((cs, ct))
    return edges


def _container_name(facts: dict[str, Any]) -> dict[str, str]:
    """container_id -> a human display name (for nicer candidate ``from``/``to`` labels)."""
    out: dict[str, str] = {}
    for t in facts.get("targets", []):
        cid = t.get("container_id", t.get("id"))
        cname = t.get("container_name") or t.get("name") or cid
        if cid and cid not in out:
            out[cid] = cname
    return out


def mine_test_scenarios(ws: Workspace, curated_facts: dict[str, Any]) -> list[dict]:
    """Mine ordered call-sequences from test files into §9.4 scenario candidates.

    Each candidate is a scenario over **EXISTING curated container edges only**: the
    ordered ``(receiver-container -> method-container)`` steps are kept only when they
    correspond to a real inter-container edge in *curated_facts*. A step naming a
    non-existent edge is DROPPED — never invented (principle #3). Returns a list in the
    same shape as ``rules/scenarios.yaml`` (``key`` / ``name`` / ``scope`` / ``steps``),
    sorted deterministically by key.

    Receivers/methods are matched against the container vocabulary (container name /
    container_id / build-target id), so a test that calls ``orders.PlaceOrder()`` only
    contributes a step if ``orders`` resolves to a container with an outgoing edge.
    """
    lookup = _container_lookup(curated_facts)
    valid_edges = _container_edges(curated_facts)
    names = _container_name(curated_facts)

    candidates: list[dict] = []
    test_files: list[Path] = []
    for pat in _TEST_GLOBS:
        test_files.extend(ws.repo.glob(pat))

    for path in sorted(set(test_files)):
        text = _read_text(path)
        if text is None:
            continue
        # Resolve the ordered sequence of (receiver_cid) tokens this test touches, in
        # source order, then pair consecutive distinct containers into edges and keep
        # only the pairs that are real curated container edges.
        seq: list[str] = []
        for m in _CALL_RE.finditer(text):
            recv = m.group("recv")
            cid = lookup.get(recv)
            if cid is None:
                continue
            if not seq or seq[-1] != cid:
                seq.append(cid)

        steps: list[dict] = []
        for a, b in zip(seq, seq[1:]):
            if a == b:
                continue
            if (a, b) not in valid_edges:
                # step names a non-existent container edge — drop it (principle #3)
                continue
            steps.append({
                "from": names.get(a, a),
                "to": names.get(b, b),
                "description": "Observed in test " + path.name,
            })

        if not steps:
            continue
        key = "test-" + re.sub(r"[^A-Za-z0-9]+", "-", path.stem).strip("-").lower()
        candidates.append({
            "key": key,
            "name": f"Test-derived: {path.stem}",
            "scope": "system",
            "steps": steps,
        })

    candidates.sort(key=lambda c: c["key"])
    return candidates


def write_scenario_candidates(ws: Workspace, candidates: list[dict]) -> Path | None:
    """Write *candidates* to ``<generated>/scenario-candidates.yaml`` (advisory, §9.4).

    Returns the path written, or ``None`` if there are no candidates (no file written —
    keeps build-only/test-free repos byte-identical). The file is a human-promoted input
    to ``rules/scenarios.yaml``; it is NOT consumed by any stage.
    """
    if not candidates:
        return None

    out = ws.generated / "scenario-candidates.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "# ADVISORY — test-derived scenario candidates (plan §9.4).",
        "# Generated by the runtime-topology extractor; NOT consumed by any stage.",
        "# Each step sequences an EXISTING curated container edge (a step naming a",
        "# non-existent edge is dropped, never invented — principle #3). Review and",
        "# promote useful entries into rules/scenarios.yaml by hand.",
        "version: 1",
        "",
        "scenarios:",
    ]
    for c in candidates:
        lines.append(f"  - key: {_yaml_scalar(c['key'])}")
        lines.append(f"    name: {_yaml_scalar(c['name'])}")
        lines.append(f"    scope: {_yaml_scalar(c.get('scope', 'system'))}")
        lines.append("    steps:")
        for step in c["steps"]:
            lines.append(f"      - from: {_yaml_scalar(step['from'])}")
            lines.append(f"        to: {_yaml_scalar(step['to'])}")
            if step.get("description"):
                lines.append(f"        description: {_yaml_scalar(step['description'])}")

    text = "\n".join(lines) + "\n"
    # explicit newline="" so we write LF on Windows (byte-stable, §22.3)
    out.write_text(text, encoding="utf-8", newline="")
    return out


def _yaml_scalar(s: str) -> str:
    """Quote a scalar as a double-quoted YAML string (deterministic, always-safe)."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Source B — runtime topology (Aspire .WithReference) — the phase-2 spine (§5)
# ---------------------------------------------------------------------------

# A scenario-key slug: lowercase, non-alphanumeric runs -> '-' (mirrors scenarios._scenario_slug).
_KEY_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _key_slug(s: str) -> str:
    return _KEY_SLUG_RE.sub("-", (s or "").lower()).strip("-") or "scenario"


def _runtime_workload_edges(
        facts: dict[str, Any],
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    """Lift runtime ``calls`` edges to container→container, split workload→workload from
    out-of-model (workload→infra / unresolved) hops (dynamic-view plan §0.3/§5).

    Returns ``(workload_edges, out_of_model)`` where:
      - ``workload_edges`` is keyed ``(source_cid, target_cid)`` (both endpoints resolve to a
        container, no self-loops) → ``{detail, target_label}``: ``detail`` is the first
        ``.WithReference(var)`` evidence string (the §1a#2 label, never a bare "Uses");
        ``target_label`` is the referenced deployment node name/technology when the runtime
        edge target also names a deployment node, else the container display name.
      - ``out_of_model`` is the list of ``{source, target, kind}`` hops a container→infra (or
        unresolved) edge produced — recorded, never silently dropped (plan §0.3)."""
    t2c = {t.get("id"): t.get("container_id", t.get("id")) for t in facts.get("targets", [])}
    deployment = facts.get("deployment") or {}
    node_label: dict[str, str] = {}
    for n in deployment.get("nodes", []):
        nid = n.get("id")
        if not nid:
            continue
        label = n.get("name") or nid
        tech = n.get("technology")
        node_label[nid] = f"{label} ({tech})" if tech else label

    workload: dict[tuple[str, str], dict[str, Any]] = {}
    out_of_model: list[dict[str, Any]] = []
    for e in sorted(deployment.get("edges", []),
                    key=lambda e: (e.get("source", ""), e.get("target", ""))):
        if e.get("kind") != "calls":
            continue
        src, tgt = e.get("source"), e.get("target")
        cs, ct = t2c.get(src), t2c.get(tgt)
        if cs and ct and cs != ct:
            detail = next((ev.get("detail", "") for ev in e.get("evidence", [])
                           if ev.get("detail")), "")
            key = (cs, ct)
            if key not in workload:
                workload[key] = {
                    "detail": detail,
                    "target_label": node_label.get(tgt, ""),
                }
        else:
            # workload→infra (or unresolved): NOT a container→container step (plan §0.3).
            out_of_model.append({
                "source": src or "", "target": tgt or "", "kind": e.get("kind", "calls"),
            })
    # de-dup + deterministic order for the note list
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for h in sorted(out_of_model, key=lambda h: (h["source"], h["target"], h["kind"])):
        sig = (h["source"], h["target"], h["kind"])
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(h)
    return workload, deduped


def derive_runtime_scenarios(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Source B — runtime-topology scenario candidates from Aspire ``.WithReference`` wiring
    (dynamic-view plan §5). One candidate per weakly-connected runtime component, seeded at
    each entry workload (a workload with no inbound runtime ``calls`` edge), walked **fan-out
    first**: an entry's N outgoing edges become ONE parallel group (shared ``order``); DFS
    sequences successive depth levels, expanding in deterministic target-container-id order.

    Returns raw proposal dicts (the common shape consumed by
    ``scenario_candidates.build_candidate``): ``key`` / ``name`` / ``scope`` / ``source`` /
    ``confidence`` / ``ordering_provenance`` / ``note`` / ``steps[]`` (each with stable
    ``from`` / ``to`` container ids, ``from_name`` / ``to_name`` display labels, a non-unique
    ``order``, and a ``description``) plus ``notes.out_of_model`` (container→infra hops). The
    ``from`` / ``to`` are STABLE container ids (display names live only in ``from_name`` /
    ``to_name``, plan §0.4#6). All output is sorted/keyed deterministically.

    The confidence split (plan §5): ``high`` for edge EXISTENCE (declared wiring is strong
    runtime evidence) but ordering is only ``medium`` — sibling fan-out order is sort-order,
    not declared — stated in the note. ``ordering_provenance: declared-in-aspire``."""
    workload, out_of_model = _runtime_workload_edges(facts)
    if not workload:
        return []

    cname = _container_name(facts)
    t2c = {t.get("id"): t.get("container_id", t.get("id")) for t in facts.get("targets", [])}

    # adjacency (sorted target order) + in-degree over the workload→workload runtime graph
    adj: dict[str, list[str]] = {}
    indeg: dict[str, int] = {}
    nodes: set[str] = set()
    for (cs, ct) in workload:
        adj.setdefault(cs, []).append(ct)
        nodes.add(cs)
        nodes.add(ct)
        indeg[ct] = indeg.get(ct, 0) + 1
        indeg.setdefault(cs, indeg.get(cs, 0))
    for cs in adj:
        adj[cs] = sorted(adj[cs])

    entries = sorted(n for n in nodes if not indeg.get(n))
    # A component with no zero-in-degree node (a pure cycle) still deserves a candidate:
    # seed it at the smallest container id so output stays deterministic and non-empty.
    if not entries:
        entries = [min(nodes)]

    candidates: list[dict[str, Any]] = []
    for entry in entries:
        steps: list[dict[str, Any]] = []
        visited_edges: set[tuple[str, str]] = set()
        visited_nodes: set[str] = {entry}
        # BFS by depth level: each level shares one `order` band so a fan-out renders as a
        # parallel group rather than a linearized chain (plan §2/§5). Within a level, expand
        # in deterministic target-container-id order.
        frontier = [entry]
        order = 1
        while frontier:
            next_frontier: list[str] = []
            level_edges: list[tuple[str, str]] = []
            for src in sorted(frontier):
                for tgt in adj.get(src, []):
                    if (src, tgt) in visited_edges:
                        continue
                    visited_edges.add((src, tgt))
                    level_edges.append((src, tgt))
            if not level_edges:
                break
            for (cs, ct) in sorted(level_edges):
                info = workload[(cs, ct)]
                target_label = info.get("target_label") or cname.get(ct, ct)
                detail = info.get("detail") or ""
                # Label policy §1a#2: the referenced node name/technology + the
                # .WithReference(var) detail — never a bare "Uses".
                description = (f"references {target_label} — {detail}"
                              if detail else f"references {target_label}")
                steps.append({
                    "from": cs, "to": ct,
                    "from_name": cname.get(cs, cs), "to_name": cname.get(ct, ct),
                    "order": order, "description": description,
                })
                if ct not in visited_nodes:
                    visited_nodes.add(ct)
                    next_frontier.append(ct)
            order += 1
            frontier = next_frontier

        if not steps:
            continue

        # out-of-model hops whose source is reachable from this entry (the chain "appears to
        # stop" at an infra hop) — attached so the GUI can explain the boundary (plan §0.3).
        oom = [h for h in out_of_model if t2c.get(h["source"]) in visited_nodes]

        entry_name = cname.get(entry, entry)
        candidates.append({
            "key": "runtime-" + _key_slug(entry_name),
            "name": f"Runtime: {entry_name} request flow",
            "scope": "system",
            "source": "runtime",
            "confidence": "high",
            "ordering_provenance": "declared-in-aspire",
            "note": ("edge existence is declared in Aspire AppHost wiring (high "
                     "confidence); the ORDERING confidence is only medium — sibling "
                     "fan-out order is sort-order, not declared by Aspire"),
            "steps": steps,
            "notes": {"out_of_model": oom},
        })

    candidates.sort(key=lambda c: c["key"])
    return candidates
