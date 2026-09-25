"""Stage P7 — deployment/runtime topology extractor (plan §4.4).

Scans ``ws.repo`` for deployment artifacts and emits ONE fact fragment containing
a top-level ``deployment`` object (schema 1.1 §4.4):

  ``ws.fragments / "deployment.json"``

Supported sources (best-effort, robust to malformed files):
  - **docker-compose** (``docker-compose*.yml/.yaml``, ``compose*.yml/.yaml``)
  - **Dockerfile** (``**/Dockerfile``, ``**/*.Dockerfile``)
  - **Kubernetes** (``*.yaml`` / ``*.yml`` with ``apiVersion`` + ``kind``)
  - **Helm** (directory containing ``Chart.yaml``) — static ``partial:unrendered`` parse
    by default; OPT-IN render via ``helm template`` (see below).
  - **Infrastructure-as-Code** (§4.4 / §16#16; coarse, low-confidence, opt-in/additive):
    **Terraform** (``*.tf`` HCL), **Bicep** (``*.bicep``), **ARM** (``azuredeploy.json``
    / ``*.arm.json`` deploymentTemplates) → external ``infra:<kind>:<name>`` nodes.
  - **Kustomize** overlays — static k8s by default; OPT-IN render via ``kustomize build`` /
    ``kubectl kustomize`` scoped to the overlay's ``env:<name>`` (see below).

The static IaC parsers (Terraform/Bicep/ARM) are PURE — they always run and need no
toolchain.

Determinism — rendering is OPT-IN, never automatic (T1.1):
  The static (``partial:unrendered``) representation is ALWAYS the hashed core, so the
  fragment is **PATH-independent by construction** — having ``helm``/``kustomize``/
  ``kubectl`` on PATH does NOT change the output, preserving byte-identical determinism
  (and identical ``content_hash``) between a bare CI box and a tooled dev box. The render
  steps (``helm template`` / ``kustomize build``) are an environment-conditional overlay
  the caller must EXPLICITLY request via ``run(ws, resolve=True)`` or the
  ``ANON_RESOLVE_DEPLOYMENT`` env var (``1``/``true``/``yes``/``on``); they are off by
  default and therefore off in CI. Even when requested they are still gated on the tool
  being present (``shutil.which``) and degrade gracefully to the static path on any failure
  — they never raise (§18.4). An opt-in render records a toolchain fingerprint under
  ``provenance.deployment_render`` so the conditional detail is attestable.

Graceful degradation (plan §18.4):
  - If NO deployment artifacts are found, writes NOTHING and returns ``None``.
    Build-only repos stay byte-identical.
  - A bad/malformed file is skipped with a debug log — never raises.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .. import SCHEMA_VERSION
from ..jsonio import dump_json
from ..paths import Workspace

log = logging.getLogger("anon.extract_deployment")

EXTRACTOR: dict[str, str] = {"name": "deployment", "version": "0.1.0"}

# helm template / kustomize build are the only shell-outs in this module; everything
# else is pure static parsing. A render that hangs or errors falls back to the static
# path (§18.4) — it is never load-bearing.
_RENDER_TIMEOUT_S = 120

# ---------------------------------------------------------------------------
# Well-known infrastructure image prefixes -> (kind, subkind) for the infra
# node classification (deploy:image vs infra:<kind>:<name>).
# ---------------------------------------------------------------------------
_INFRA_IMAGES: dict[str, tuple[str, str]] = {
    "postgres": ("database", "PostgreSQL"),
    "mysql": ("database", "MySQL"),
    "mariadb": ("database", "MariaDB"),
    "mcr.microsoft.com/mssql": ("database", "SQL Server"),
    "mssql": ("database", "SQL Server"),
    "sqlserver": ("database", "SQL Server"),
    "redis": ("cache", "Redis"),
    "rabbitmq": ("queue", "RabbitMQ"),
    "mongo": ("database", "MongoDB"),
    "confluentinc/cp-kafka": ("queue", "Kafka"),
    "bitnami/kafka": ("queue", "Kafka"),
    "kafka": ("queue", "Kafka"),
    "nginx": ("ingress", "nginx"),
    "traefik": ("ingress", "Traefik"),
    "elasticsearch": ("storage", "Elasticsearch"),
    "kibana": ("storage", "Kibana"),
    "grafana": ("storage", "Grafana"),
    "prometheus": ("storage", "Prometheus"),
}


def _infra_kind(image_ref: str) -> tuple[str, str] | None:
    """Return ``(subkind, technology)`` if *image_ref* matches a known infra image.

    The image_ref may include a tag (``postgres:15``) which we strip for the match.
    Returns ``None`` if not recognised as infra.
    """
    ref_lower = image_ref.lower().split(":")[0].split("@")[0]
    # exact prefix match (longest key first so specificity wins)
    for prefix in sorted(_INFRA_IMAGES, key=len, reverse=True):
        if ref_lower == prefix or ref_lower.startswith(prefix + "/") or ref_lower.endswith("/" + prefix):
            sk, _ = _INFRA_IMAGES[prefix]
            return sk, image_ref
    return None


# ---------------------------------------------------------------------------
# Helpers: node / edge construction
# ---------------------------------------------------------------------------

def _sort_ports(ports: Any) -> list[str]:
    """Sort port strings numerically by their leading digits (so '80' precedes '100'),
    falling back to lexicographic for tokens like ``80/tcp``. Non-numeric tokens sort last."""
    def _key(p: str) -> tuple[int, str]:
        m = re.match(r"\d+", p)
        return (int(m.group()) if m else 1 << 30, p)
    return sorted(ports, key=_key)


def _image_node(name: str, *, technology: str = "", ports: list[str] | None = None,
                env: str | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"id": f"deploy:image:{name}", "name": name, "kind": "image"}
    if technology:
        node["technology"] = technology
    if ports:
        node["ports"] = _sort_ports(ports)
    if env:
        node["env"] = env
    if tags:
        node["tags"] = sorted(tags)
    return node


def _infra_node(subkind: str, name: str, technology: str, *,
                env: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": f"infra:{subkind}:{name}",
        "name": name,
        "kind": "infra",
        "subkind": subkind,
        "technology": technology,
        "external": True,
    }
    if env:
        node["env"] = env
    return node


def _env_node(name: str) -> dict[str, Any]:
    return {"id": f"env:{name}", "name": name, "kind": "env"}


def _edge(source: str, target: str, kind: str, *, env: str | None = None,
          evidence_type: str = "deploy", detail: str = "") -> dict[str, Any]:
    ev: dict[str, Any] = {"type": evidence_type}
    if detail:
        ev["detail"] = detail
    e: dict[str, Any] = {"source": source, "target": target, "kind": kind,
                          "evidence": [ev]}
    if env:
        e["env"] = env
    return e


# ---------------------------------------------------------------------------
# De-duplication helpers (node by id; edge by source+target+kind)
# ---------------------------------------------------------------------------

def _merge_node(nodes: dict[str, dict], node: dict[str, Any]) -> None:
    nid = node["id"]
    if nid not in nodes:
        nodes[nid] = node
        return
    existing = nodes[nid]
    # merge ports
    new_ports = set(existing.get("ports") or []) | set(node.get("ports") or [])
    if new_ports:
        existing["ports"] = _sort_ports(new_ports)
    # merge tags
    new_tags = set(existing.get("tags") or []) | set(node.get("tags") or [])
    if new_tags:
        existing["tags"] = sorted(new_tags)
    # fill in missing scalar fields
    for field in ("technology", "env", "subkind", "replicas", "external"):
        if field not in existing and field in node:
            existing[field] = node[field]


def _merge_edge(edges: dict[tuple, dict], edge: dict[str, Any]) -> None:
    key = (edge["source"], edge["target"], edge["kind"])
    if key not in edges:
        edges[key] = edge


# ---------------------------------------------------------------------------
# docker-compose parsing
# ---------------------------------------------------------------------------

def _compose_env_name(filepath: Path) -> str:
    """Derive env name from filename, e.g. ``docker-compose.prod.yml`` -> ``prod``."""
    stem = filepath.stem  # e.g. "docker-compose.prod" or "docker-compose"
    # Strip leading "docker-compose" / "compose"
    for prefix in ("docker-compose", "compose"):
        if stem.startswith(prefix):
            rest = stem[len(prefix):]
            if rest.startswith("."):
                return rest[1:] or "default"
            return "default"
    return stem or "default"


def _parse_compose_ports(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            published = entry.get("published") or entry.get("target")
            if published:
                out.append(str(published))
    return out


def _build_dockerfile(svc: dict, compose_dir: Path) -> Path | None:
    """Resolve the Dockerfile a compose service's ``build:`` section points at.

    ``build: <ctx>`` (string) means ``<ctx>/Dockerfile``; the mapping form honors
    ``context`` (default ``.``) + ``dockerfile`` (default ``Dockerfile``), both
    relative to the compose file's directory per the compose spec. Returns ``None``
    for malformed values or ``dockerfile_inline`` (no file to claim)."""
    build = svc.get("build")
    if isinstance(build, str):
        ctx, dockerfile = build, "Dockerfile"
    elif isinstance(build, dict):
        if "dockerfile_inline" in build:
            return None
        ctx = build.get("context") or "."
        dockerfile = build.get("dockerfile") or "Dockerfile"
        if not isinstance(ctx, str) or not isinstance(dockerfile, str):
            return None
    else:
        return None
    try:
        return (compose_dir / ctx / dockerfile).resolve()
    except OSError:  # pragma: no cover - defensive (e.g. null bytes in path)
        return None


def _parse_compose(
    path: Path,
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
    claimed: dict[Path, set[str]] | None = None,
) -> None:
    """Parse a docker-compose file and populate *nodes* / *edges* in place.

    *claimed* (Dockerfile path → claiming ``deploy:image:*`` ids) records which
    Dockerfile each ``build:`` service builds, so the later Dockerfile scan merges
    its evidence (base image, EXPOSE, packages edges) into the SERVICE's node
    instead of minting a second path-named image for the same deployable."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except Exception as exc:
        log.debug("skipping malformed compose file %s: %s", path, exc)
        return
    if not isinstance(doc, dict):
        return

    env_name = _compose_env_name(path)
    env_id = f"env:{env_name}"
    _merge_node(nodes, _env_node(env_name))

    services = doc.get("services") or {}
    if not isinstance(services, dict):
        return

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        svc_name = str(svc_name)
        # YAML happily parses `image: 12345` as an int; coerce to a string so the
        # downstream `.lower()`/`.replace()` calls (and the fact schema) never see a
        # non-string — honoring the module's "never raises on malformed input" contract.
        raw_image = svc.get("image")
        image_ref: str = raw_image if isinstance(raw_image, str) else ""
        has_build = "build" in svc
        ports = _parse_compose_ports(svc.get("ports"))

        if has_build:
            # it's a service with a build context -> deploy:image
            node = _image_node(svc_name, technology=image_ref, ports=ports, env=env_name)
            _merge_node(nodes, node)
            if claimed is not None:
                dockerfile = _build_dockerfile(svc, path.parent)
                if dockerfile is not None:
                    claimed.setdefault(dockerfile, set()).add(node["id"])
        else:
            # image-only: check if it's a known infra image
            infra = _infra_kind(image_ref) if image_ref else None
            if infra:
                subkind, technology = infra
                node = _infra_node(subkind, svc_name, technology, env=env_name)
                _merge_node(nodes, node)
            else:
                # unknown/app image
                node = _image_node(svc_name, technology=image_ref, ports=ports, env=env_name)
                _merge_node(nodes, node)

        # depends_on -> calls edges
        depends_raw = svc.get("depends_on")
        depends: list[str] = []
        if isinstance(depends_raw, list):
            depends = [str(d) for d in depends_raw]
        elif isinstance(depends_raw, dict):
            depends = list(depends_raw.keys())

        svc_node_id = nodes.get(f"deploy:image:{svc_name}", {}).get("id") or \
                      nodes.get(f"infra:{nodes.get(svc_name, {}).get('subkind', 'database')}:{svc_name}", {}).get("id")
        # Just derive source id directly from the node we put in:
        source_id = (f"deploy:image:{svc_name}" if has_build or not _infra_kind(image_ref)
                     else f"infra:{_infra_kind(image_ref)[0]}:{svc_name}")  # type: ignore[index]

        for dep in depends:
            # Only emit an edge to a dep that is an actual service in this compose; a
            # depends_on naming an undefined service (typo / externally-provided) would
            # otherwise point at a node that is never created (dangling edge).
            if dep not in services:
                log.debug("compose %s: service %r depends_on undefined service %r — skipping edge",
                          path.name, svc_name, dep)
                continue
            # target id: look up the dep node id
            dep_image_id = f"deploy:image:{dep}"
            dep_infra_id = None
            dep_svc = services.get(dep)
            if isinstance(dep_svc, dict):
                dep_image_raw = dep_svc.get("image")
                dep_image = dep_image_raw if isinstance(dep_image_raw, str) else ""
                dep_has_build = "build" in dep_svc
                if not dep_has_build and dep_image:
                    dep_infra = _infra_kind(dep_image)
                    if dep_infra:
                        dep_infra_id = f"infra:{dep_infra[0]}:{dep}"
            target_id = dep_infra_id if dep_infra_id else dep_image_id
            _merge_edge(edges, _edge(source_id, target_id, "calls",
                                     env=env_name, detail="compose depends_on"))


# ---------------------------------------------------------------------------
# Dockerfile parsing
# ---------------------------------------------------------------------------

# An image reference that is wholly or partly an unresolved build ARG / variable
# (``$X``, ``${X}``, ``build_${TARGETOS}``) cannot be statically pinned — tag it
# partial:unrendered (§4.4) rather than emitting the literal as a junk technology.
_ARG_IN_REF = re.compile(r"\$\{?\w+\}?")


def _parse_from(stripped: str) -> tuple[str, str | None] | None:
    """Parse a ``FROM`` line into ``(base_image, stage_alias|None)``.

    Skips leading flags (``--platform=...``) and reads the optional ``AS <alias>`` tail, so
    ``FROM --platform=$BUILDPLATFORM mcr/x:1 AS build`` -> ``("mcr/x:1", "build")``.
    """
    toks = stripped.split()[1:]  # drop the FROM keyword
    while toks and toks[0].startswith("--"):
        toks.pop(0)
    if not toks:
        return None
    base = toks[0]
    alias = None
    if len(toks) >= 3 and toks[1].lower() == "as":
        alias = toks[2]
    return base, alias


def _resolve_final_base(stages: list[tuple[str, str | None]]) -> str:
    """Resolve the runtime stage's base image, following multi-stage alias chains.

    The runtime image is the LAST ``FROM`` (Docker semantics). If its base names an earlier
    stage's alias, follow the chain to the real base image (guarding cycles). Returns the
    resolved base ref — which may still contain an unresolved ``${...}`` if the chain ends on
    an ARG-templated reference (caller tags it partial:unrendered)."""
    alias_to_base = {alias: base for base, alias in stages if alias}
    base = stages[-1][0]
    seen: set[str] = set()
    while base in alias_to_base and base not in seen:
        seen.add(base)
        base = alias_to_base[base]
    return base


def _parse_dockerfile(
    path: Path,
    repo: Path,
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
    claimed: dict[Path, set[str]] | None = None,
) -> None:
    """Parse a Dockerfile and populate *nodes* / *edges* in place.

    A Dockerfile that a compose service's ``build:`` section points at (*claimed*)
    contributes its evidence to THAT service's node — one deployable, one node —
    instead of minting a second image named after the Dockerfile's directory."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.debug("skipping unreadable Dockerfile %s: %s", path, exc)
        return

    # Name the image after the parent directory (relative to repo root). Lowercase it:
    # container image names are lowercase by convention, and this makes a Dockerfile under
    # ``Api/`` dedupe with a compose service ``api`` (same id ``deploy:image:api``) instead
    # of producing two near-duplicate nodes. (A compose ``build:`` claim below overrides
    # the directory heuristic entirely.)
    try:
        rel_dir = path.parent.relative_to(repo)
        name = str(rel_dir).replace("\\", "/").replace("/", "-") or repo.name
    except ValueError:
        name = path.parent.name or "image"
    if not name or name == ".":
        # a Dockerfile at the repo root has rel_dir "." — name the image after the repo.
        name = repo.name or "image"
    name = name.lower()

    # Resolve multi-stage: collect (base, alias) per FROM; the final stage is the runtime
    # image, with its base resolved back through any stage-alias chain (§4.4).
    stages: list[tuple[str, str | None]] = []
    expose_ports: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        up = stripped.upper()
        if up.startswith("FROM "):
            parsed = _parse_from(stripped)
            if parsed:
                stages.append(parsed)
        elif up.startswith("EXPOSE "):
            for tok in stripped.split()[1:]:
                expose_ports.append(tok)

    if not stages:
        return  # not a valid Dockerfile

    final_base = _resolve_final_base(stages)
    tags: list[str] = []
    if _ARG_IN_REF.search(final_base):
        # the runtime base could not be statically pinned (ARG/variable indirection, e.g.
        # `FROM build_${TARGETOS}`) — record it honestly rather than as a junk technology.
        tags.append("partial:unrendered")
        technology = ""
    else:
        technology = final_base

    # one node per deployable: a compose-claimed Dockerfile feeds the claiming
    # service node(s) (sorted for determinism); only an unclaimed one gets the
    # directory-derived name
    owners = sorted(claimed.get(path.resolve(), ())) if claimed else []
    image_ids = owners or [f"deploy:image:{name}"]
    for image_id in image_ids:
        node = _image_node(image_id[len("deploy:image:"):], technology=technology,
                           ports=expose_ports or None, tags=tags or None)
        _merge_node(nodes, node)

    # packages edge: if a .csproj lives in the same dir or a parent dir (up to repo root)
    search_dir = path.parent
    csproj_path: Path | None = None
    while True:
        candidates = list(search_dir.glob("*.csproj"))
        if candidates:
            # deterministic: sort and pick first
            csproj_path = sorted(candidates)[0]
            break
        if search_dir == repo or search_dir.parent == search_dir:
            break
        search_dir = search_dir.parent

    if csproj_path is not None:
        try:
            rel_csproj = csproj_path.relative_to(repo)
        except ValueError:
            rel_csproj = csproj_path
        csproj_id = f"csharp:csproj:{str(rel_csproj).replace(chr(92), '/')}"
        for image_id in image_ids:
            _merge_edge(edges, _edge(image_id, csproj_id, "packages",
                                     evidence_type="deploy", detail="Dockerfile"))


# ---------------------------------------------------------------------------
# Kubernetes parsing
# ---------------------------------------------------------------------------

def _parse_k8s_doc(
    doc: dict,
    source_path: Path,
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
) -> None:
    """Handle a single Kubernetes resource document."""
    kind = doc.get("kind", "")
    meta = doc.get("metadata") or {}
    name = meta.get("name") or ""
    if not name:
        return

    if kind in ("Deployment", "StatefulSet", "DaemonSet"):
        spec = doc.get("spec") or {}
        replicas = spec.get("replicas")
        template = spec.get("template") or {}
        tspec = template.get("spec") or {}
        containers = tspec.get("containers") or []
        ports: list[str] = []
        for c in containers:
            for p in (c.get("ports") or []):
                cp = p.get("containerPort")
                if cp is not None:
                    ports.append(str(cp))
        node: dict[str, Any] = {"id": f"deploy:image:{name}", "name": name, "kind": "image"}
        if ports:
            node["ports"] = _sort_ports(ports)
        if replicas is not None:
            node["replicas"] = replicas
        _merge_node(nodes, node)

        # ConfigMap/Secret runtime_config references (never values)
        for c in containers:
            for env_var in (c.get("env") or []):
                vf = env_var.get("valueFrom") or {}
                ref = vf.get("configMapKeyRef") or vf.get("secretKeyRef")
                if ref:
                    key_name = ref.get("key") or ref.get("name") or ""
                    if key_name:
                        _merge_edge(edges, _edge(
                            f"deploy:image:{name}", f"deploy:image:{name}",
                            "runtime_config", evidence_type="runtime_config",
                            detail=key_name,
                        ))

    elif kind == "Ingress":
        node = _infra_node("ingress", name, "Kubernetes Ingress")
        node["external"] = True
        _merge_node(nodes, node)


def _parse_k8s_file(
    path: Path,
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
) -> None:
    """Parse a YAML file as potentially multi-document Kubernetes manifests."""
    try:
        with open(path, encoding="utf-8") as fh:
            docs = list(yaml.safe_load_all(fh))
    except Exception as exc:
        log.debug("skipping malformed k8s file %s: %s", path, exc)
        return

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if "apiVersion" in doc and "kind" in doc:
            try:
                _parse_k8s_doc(doc, path, nodes, edges)
            except Exception as exc:
                log.debug("error processing k8s doc in %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Helm parsing (static, no shell-out)
# ---------------------------------------------------------------------------

def _parse_helm_chart(
    chart_dir: Path,
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
) -> None:
    """Parse a Helm Chart.yaml (static analysis, low-confidence, tagged partial:unrendered)."""
    chart_yaml = chart_dir / "Chart.yaml"
    try:
        with open(chart_yaml, encoding="utf-8") as fh:
            chart = yaml.safe_load(fh)
    except Exception as exc:
        log.debug("skipping malformed Chart.yaml %s: %s", chart_yaml, exc)
        return
    if not isinstance(chart, dict):
        return

    chart_name = chart.get("name") or chart_dir.name
    node = _image_node(chart_name, tags=["partial:unrendered"])
    _merge_node(nodes, node)
    chart_id = f"deploy:image:{chart_name}"

    # dependencies as calls edges to subchart nodes
    for dep in (chart.get("dependencies") or []):
        if not isinstance(dep, dict):
            continue
        dep_name = dep.get("name") or ""
        if dep_name:
            sub_node = _image_node(dep_name, tags=["partial:unrendered"])
            _merge_node(nodes, sub_node)
            _merge_edge(edges, _edge(chart_id, f"deploy:image:{dep_name}", "calls",
                                     detail="helm dependency"))

    # values-<env>.yaml -> env nodes
    for vf in sorted(chart_dir.glob("values-*.yaml")):
        stem = vf.stem  # "values-prod"
        if stem.startswith("values-"):
            env_name = stem[len("values-"):]
            if env_name:
                _merge_node(nodes, _env_node(env_name))


# ---------------------------------------------------------------------------
# Infrastructure-as-Code: provider resource *type* -> infra subkind (§4.4)
#
# IaC (Terraform/Bicep/ARM) describes EXTERNAL managed infrastructure beyond the
# cluster (managed DB / queue / blob store / gateway). It is coarse, provider-
# specific and low-confidence by design (§4.4 honesty note): we map only the
# resource *type* to a kind — never resolve computed values, modules, or remote
# state. Nodes are tagged ``external: true`` + ``iac:<flavor>`` so the generator /
# curation can treat them distinctly.
#
# Each entry is a (substring, subkind) probe matched case-insensitively against the
# fully-qualified resource type. Order matters only for readability — the matcher
# below prefers the LONGEST matching substring so a specific probe (``servicebus``)
# wins over a generic one. Terraform, Bicep and ARM share one table because their
# type vocabularies are recognisable by the same keywords.
# ---------------------------------------------------------------------------
_IAC_TYPE_PROBES: list[tuple[str, str]] = [
    # databases
    ("db_instance", "database"),
    ("rds_cluster", "database"),
    ("dbforpostgresql", "database"),
    ("dbformysql", "database"),
    ("dbformariadb", "database"),
    ("sql/servers", "database"),
    ("documentdb", "database"),
    ("cosmosdb", "database"),
    ("sql_database", "database"),
    ("sql_instance", "database"),
    ("spanner", "database"),
    ("bigtable", "database"),
    ("dynamodb", "database"),
    ("database", "database"),
    # queues / messaging
    ("servicebus", "queue"),
    ("eventhub", "queue"),
    ("sqs_queue", "queue"),
    ("sns_topic", "queue"),
    ("kinesis", "queue"),
    ("pubsub", "queue"),
    ("msk", "queue"),
    ("amazonmq", "queue"),
    ("eventgrid", "queue"),
    ("queue", "queue"),
    # caches
    ("elasticache", "cache"),
    ("cache/redis", "cache"),
    ("redis", "cache"),
    ("memcached", "cache"),
    ("memorystore", "cache"),
    # storage / object stores
    ("s3_bucket", "storage"),
    ("storageaccount", "storage"),
    ("storage_account", "storage"),
    ("storage/storageaccounts", "storage"),
    ("blob", "storage"),
    ("storage_bucket", "storage"),
    ("efs_file_system", "storage"),
    ("filestore", "storage"),
    ("storage", "storage"),
    # ingress / gateways / load balancers
    ("application_gateway", "ingress"),
    ("applicationgateways", "ingress"),
    ("api_gateway", "ingress"),
    ("apimanagement", "ingress"),
    ("frontdoor", "ingress"),
    ("cloudfront", "ingress"),
    ("lb_target_group", "ingress"),
    ("_lb", "ingress"),
    ("loadbalancer", "ingress"),
    ("load_balancer", "ingress"),
    ("trafficmanager", "ingress"),
    ("ingress", "ingress"),
]


def _iac_kind(resource_type: str) -> str | None:
    """Map a provider resource *type* to an infra subkind, or ``None`` if unrecognised.

    Matches the LONGEST probe substring (case-insensitive) so a specific probe such as
    ``servicebus`` outranks a generic ``queue``. Coarse by design (§4.4)."""
    rt = (resource_type or "").lower()
    best: tuple[int, str] | None = None
    for probe, subkind in _IAC_TYPE_PROBES:
        if probe in rt:
            if best is None or len(probe) > best[0]:
                best = (len(probe), subkind)
    return best[1] if best else None


def _iac_infra_node(subkind: str, name: str, technology: str, flavor: str) -> dict[str, Any]:
    """An external infra node sourced from IaC — tagged ``iac:<flavor>`` (§4.4)."""
    node = _infra_node(subkind, name, technology)
    node["tags"] = sorted({f"iac:{flavor}"})
    return node


def _read_text_bom(path: Path) -> str | None:
    """Read *path* as text (BOM-tolerant), returning ``None`` on any error (never raises)."""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("skipping unreadable IaC file %s: %s", path, exc)
        return None


# --- Terraform (HCL) -------------------------------------------------------

# `resource "<type>" "<name>" {`  — the only HCL block we need. A pragmatic regex (no
# new HCL dependency, per §16#16): provider quoting is well-defined for the type/name
# labels, so a regex over the block header is robust enough for the coarse type->kind map.
_TF_RESOURCE = re.compile(
    r'resource\s+"(?P<type>[A-Za-z0-9_]+)"\s+"(?P<name>[A-Za-z0-9_.\-]+)"\s*\{'
)


def _parse_terraform(path: Path, nodes: dict[str, dict], edges: dict[tuple, dict]) -> None:
    """Parse a Terraform ``*.tf`` file → external ``infra:<kind>:<name>`` nodes (§4.4)."""
    text = _read_text_bom(path)
    if text is None:
        return
    for m in _TF_RESOURCE.finditer(text):
        rtype = m.group("type")
        rname = m.group("name")
        subkind = _iac_kind(rtype)
        if subkind is None:
            continue
        # The technology is the provider resource type itself (e.g. ``aws_db_instance``).
        _merge_node(nodes, _iac_infra_node(subkind, rname, rtype, "terraform"))


# --- Bicep -----------------------------------------------------------------

# `resource <symbol> '<type>@<apiVersion>' = {`
_BICEP_RESOURCE = re.compile(
    r"resource\s+(?P<symbol>[A-Za-z0-9_]+)\s+'(?P<type>[^'@]+)@(?P<ver>[^']+)'\s*="
)


def _parse_bicep(path: Path, nodes: dict[str, dict], edges: dict[tuple, dict]) -> None:
    """Parse a Bicep ``*.bicep`` file → external ``infra:<kind>:<name>`` nodes (§4.4)."""
    text = _read_text_bom(path)
    if text is None:
        return
    for m in _BICEP_RESOURCE.finditer(text):
        rtype = m.group("type")        # e.g. Microsoft.DBforPostgreSQL/servers
        symbol = m.group("symbol")     # the bicep symbolic name
        subkind = _iac_kind(rtype)
        if subkind is None:
            continue
        _merge_node(nodes, _iac_infra_node(subkind, symbol, rtype, "bicep"))


# --- ARM JSON --------------------------------------------------------------

def _arm_walk_resources(resources: Any) -> list[str]:
    """Recursively collect ``type`` strings from an ARM ``resources[]`` array (incl. nested)."""
    out: list[str] = []
    if not isinstance(resources, list):
        return out
    for res in resources:
        if not isinstance(res, dict):
            continue
        rtype = res.get("type")
        if isinstance(rtype, str):
            out.append(rtype)
        # nested child resources
        out.extend(_arm_walk_resources(res.get("resources")))
    return out


def _parse_arm(path: Path, nodes: dict[str, dict], edges: dict[tuple, dict]) -> None:
    """Parse an ARM deployment-template JSON → external ``infra:<kind>:<name>`` nodes (§4.4).

    Only files whose ``$schema`` names a ``deploymentTemplate`` are treated as ARM, so an
    arbitrary ``*.json`` is never misread. The node name is the resource type's leaf
    segment (ARM resource *names* are usually templated expressions, not static)."""
    text = _read_text_bom(path)
    if text is None:
        return
    try:
        doc = json.loads(text)
    except Exception as exc:
        log.debug("skipping non-JSON ARM candidate %s: %s", path, exc)
        return
    if not isinstance(doc, dict):
        return
    schema = doc.get("$schema")
    if not (isinstance(schema, str) and "deploymentTemplate" in schema):
        return  # not an ARM deployment template
    for rtype in _arm_walk_resources(doc.get("resources")):
        subkind = _iac_kind(rtype)
        if subkind is None:
            continue
        # name after the type's leaf provider segment, deduped by id via _merge_node
        leaf = rtype.rsplit("/", 1)[-1] or rtype
        _merge_node(nodes, _iac_infra_node(subkind, leaf, rtype, "arm"))


# ---------------------------------------------------------------------------
# Helm template rendering (§16#16) — replaces the static partial:unrendered path
# when ``helm`` is on PATH; otherwise the static parser above is used unchanged.
# ---------------------------------------------------------------------------

def _helm_available() -> bool:
    return shutil.which("helm") is not None


def _render_helm(chart_dir: Path, values: Path | None) -> str | None:
    """Run ``helm template <chart> [-f <values>]`` → rendered manifests, or ``None``.

    Graceful (§18.4): any non-zero exit, timeout or OS error returns ``None`` and the
    caller falls back to the static ``partial:unrendered`` path — it never raises."""
    cmd = ["helm", "template", str(chart_dir)]
    if values is not None:
        cmd += ["-f", str(values)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_RENDER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("helm template failed for %s: %s — static fallback (§18.4)", chart_dir, exc)
        return None
    if proc.returncode != 0:
        log.warning("helm template rc=%s for %s — static fallback (§18.4):\n%s",
                    proc.returncode, chart_dir, (proc.stderr or "").strip()[-1000:])
        return None
    return proc.stdout or None


def _parse_k8s_text(
    text: str,
    source_path: Path,
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
) -> bool:
    """Parse a rendered multi-doc k8s YAML *string* (reuses ``_parse_k8s_doc``).

    Returns ``True`` if at least one k8s document was processed. Never raises (§18.4)."""
    try:
        docs = list(yaml.safe_load_all(text))
    except Exception as exc:
        log.debug("skipping malformed rendered manifests from %s: %s", source_path, exc)
        return False
    processed = False
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if "apiVersion" in doc and "kind" in doc:
            try:
                _parse_k8s_doc(doc, source_path, nodes, edges)
                processed = True
            except Exception as exc:
                log.debug("error processing rendered k8s doc from %s: %s", source_path, exc)
    return processed


def _render_helm_chart(
    chart_dir: Path,
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
) -> bool:
    """Render a Helm chart with ``helm template`` (one render per ``values-<env>.yaml``)
    and parse the concrete manifests as k8s — DROPPING ``partial:unrendered`` for those
    nodes (§16#16). Returns ``True`` if anything rendered; ``False`` ⇒ static fallback.

    The base ``values.yaml`` is rendered as ``env:default``; each ``values-<env>.yaml``
    as its own ``env`` so dev/prod topologies stay distinct (§4.4)."""
    values_files = sorted(chart_dir.glob("values-*.yaml"))
    any_rendered = False

    # base render (default env) — only if a values.yaml exists or no per-env files at all,
    # so a chart that ships ONLY per-env values still renders.
    base_values = chart_dir / "values.yaml"
    base_specs: list[tuple[str | None, Path | None]] = []
    if base_values.exists() or not values_files:
        base_specs.append((None, base_values if base_values.exists() else None))

    for env_name, values in base_specs:
        rendered = _render_helm(chart_dir, values)
        if rendered and _parse_k8s_text(rendered, chart_dir / "Chart.yaml", nodes, edges):
            any_rendered = True

    for vf in values_files:
        env_name = vf.stem[len("values-"):] if vf.stem.startswith("values-") else vf.stem
        if env_name:
            _merge_node(nodes, _env_node(env_name))
        rendered = _render_helm(chart_dir, vf)
        if rendered and _parse_k8s_text(rendered, vf, nodes, edges):
            any_rendered = True

    return any_rendered


# ---------------------------------------------------------------------------
# Kustomize overlay rendering (§16#16) — ``kustomize build`` / ``kubectl kustomize``
# when on PATH; otherwise the overlay's static k8s docs are parsed as-is.
# ---------------------------------------------------------------------------

def _kustomize_cmd() -> list[str] | None:
    """Return the kustomize invocation prefix, preferring standalone ``kustomize`` over
    ``kubectl kustomize``; ``None`` if neither tool is on PATH."""
    if shutil.which("kustomize") is not None:
        return ["kustomize", "build"]
    if shutil.which("kubectl") is not None:
        return ["kubectl", "kustomize"]
    return None


def _kustomize_available() -> bool:
    return _kustomize_cmd() is not None


def _render_kustomize(overlay_dir: Path) -> str | None:
    """Run ``kustomize build <overlay>`` (or ``kubectl kustomize``) → manifests, or ``None``.

    Graceful (§18.4): tool absent / non-zero exit / timeout returns ``None``."""
    prefix = _kustomize_cmd()
    if prefix is None:
        return None
    cmd = prefix + [str(overlay_dir)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_RENDER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("kustomize build failed for %s: %s — static fallback (§18.4)",
                    overlay_dir, exc)
        return None
    if proc.returncode != 0:
        log.warning("kustomize build rc=%s for %s — static fallback (§18.4):\n%s",
                    proc.returncode, overlay_dir, (proc.stderr or "").strip()[-1000:])
        return None
    return proc.stdout or None


def _kustomize_env_name(overlay_dir: Path, repo: Path) -> str:
    """Derive an env name for an overlay — its directory name (``overlays/prod`` → ``prod``)."""
    name = overlay_dir.name
    if name in ("overlay", "overlays", "kustomize", ".") and overlay_dir.parent != repo:
        name = overlay_dir.parent.name
    return name or "default"


def _render_kustomize_overlay(
    overlay_dir: Path,
    repo: Path,
    nodes: dict[str, dict],
    edges: dict[tuple, dict],
) -> bool:
    """``kustomize build`` an overlay and parse the result as k8s, scoped to ``env:<name>``
    (§16#16). Returns ``True`` if it rendered; ``False`` ⇒ caller leaves the static path."""
    rendered = _render_kustomize(overlay_dir)
    if not rendered:
        return False
    env_name = _kustomize_env_name(overlay_dir, repo)
    _merge_node(nodes, _env_node(env_name))
    return _parse_k8s_text(rendered, overlay_dir / "kustomization.yaml", nodes, edges)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(ws: Workspace, resolve: bool | None = None) -> Path | None:
    """Scan *ws.repo* for deployment artifacts and write a deployment fragment.

    Returns the fragment path on success, or ``None`` if no deployment artifacts
    were found (graceful degrade — preserves byte-identity of build-only repos).

    ``resolve`` controls the OPT-IN render path (T1.1). By default (``resolve`` left
    ``None``) the env var ``ANON_RESOLVE_DEPLOYMENT`` is consulted; absent that, it
    is **False** — Helm/Kustomize are emitted via the static ``partial:unrendered`` path
    only, making the fragment PATH-independent and the ``content_hash`` byte-identical
    across machines. When ``resolve`` is True, ``helm template`` / ``kustomize build`` are
    attempted (still gated on the tool being present, still graceful on failure) and a
    toolchain fingerprint is stamped under ``provenance.deployment_render``.
    """
    if resolve is None:
        resolve = os.environ.get(
            "ANON_RESOLVE_DEPLOYMENT", ""
        ).strip().lower() in ("1", "true", "yes", "on")

    repo = ws.repo
    nodes: dict[str, dict] = {}   # id -> node dict
    edges: dict[tuple, dict] = {} # (source,target,kind) -> edge dict
    # Dockerfile path -> claiming deploy:image ids (compose `build:` sections). Parsed
    # BEFORE the Dockerfile scan so a built Dockerfile enriches the service's node
    # instead of minting a duplicate path-named image.
    claimed: dict[Path, set[str]] = {}

    # 1. docker-compose files
    compose_patterns = [
        "docker-compose.yml", "docker-compose.yaml",
        "docker-compose.*.yml", "docker-compose.*.yaml",
        "compose.yml", "compose.yaml",
        "compose.*.yml", "compose.*.yaml",
    ]
    compose_paths: list[Path] = []
    for pat in compose_patterns:
        compose_paths.extend(repo.glob(pat))
    # also recurse one level deep (common monorepo pattern)
    for pat in compose_patterns:
        compose_paths.extend(repo.glob(f"*/{pat}"))
    for path in sorted(set(compose_paths)):
        log.debug("parsing compose: %s", path)
        _parse_compose(path, nodes, edges, claimed)

    # 2. Dockerfiles
    dockerfile_paths: list[Path] = []
    dockerfile_paths.extend(repo.rglob("Dockerfile"))
    dockerfile_paths.extend(repo.rglob("*.Dockerfile"))
    for path in sorted(set(dockerfile_paths)):
        log.debug("parsing Dockerfile: %s", path)
        _parse_dockerfile(path, repo, nodes, edges, claimed)

    # 3. Kubernetes YAML files (heuristic: apiVersion + kind present)
    # Avoid double-parsing compose, helm and kustomization files
    compose_stems = {p.resolve() for p in compose_paths}
    helm_chart_dirs: set[Path] = set()
    kustomize_dirs: set[Path] = set()
    k8s_paths: list[Path] = []
    for ext in ("*.yaml", "*.yml"):
        for path in sorted(repo.rglob(ext)):
            resolved = path.resolve()
            if resolved in compose_stems:
                continue
            if (path.parent / "Chart.yaml").exists() and path.name != "Chart.yaml":
                helm_chart_dirs.add(path.parent)
                continue  # will be handled by helm parser
            if path.name == "Chart.yaml":
                helm_chart_dirs.add(path.parent)
                continue
            if path.name in ("kustomization.yaml", "kustomization.yml"):
                kustomize_dirs.add(path.parent)
                continue  # handled by the kustomize section (rendered or static)
            # Quick sniff: only load if it looks like a k8s doc. Read a generous prefix
            # (manifests are small) rather than 512 bytes, so a long leading license/header
            # comment block can't push `apiVersion:`/`kind:` out of the sniff window (§4.4).
            try:
                with open(path, encoding="utf-8") as fh:
                    head = fh.read(1 << 20)
                if "apiVersion" in head and "kind" in head:
                    k8s_paths.append(path)
            except Exception:
                pass

    for path in sorted(set(k8s_paths)):
        log.debug("parsing k8s manifest: %s", path)
        _parse_k8s_file(path, nodes, edges)

    # 4. Helm charts — render via `helm template` ONLY when rendering is opted in
    #    (T1.1) AND helm is on PATH (drops partial:unrendered, §16#16); otherwise the
    #    static low-confidence parser. Default is static so output is PATH-independent.
    helm_present = resolve and _helm_available()
    for chart_dir in sorted(helm_chart_dirs):
        if helm_present and _render_helm_chart(chart_dir, nodes, edges):
            log.debug("rendered Helm chart via `helm template`: %s", chart_dir)
        else:
            log.debug("parsing Helm chart (static): %s", chart_dir)
            _parse_helm_chart(chart_dir, nodes, edges)

    # 5. Kustomize overlays — render via `kustomize build` / `kubectl kustomize` ONLY
    #    when rendering is opted in (T1.1) AND the tool is on PATH (scoped per env,
    #    §16#16); otherwise parse the overlay's static manifests as plain k8s so the
    #    topology is not lost entirely. Default is static ⇒ PATH-independent output.
    kustomize_present = resolve and _kustomize_available()
    for overlay_dir in sorted(kustomize_dirs):
        if kustomize_present and _render_kustomize_overlay(overlay_dir, repo, nodes, edges):
            log.debug("rendered kustomize overlay: %s", overlay_dir)
            continue
        # Static fallback: parse any sibling k8s manifests in the overlay dir directly.
        log.debug("parsing kustomize overlay (static): %s", overlay_dir)
        for ext in ("*.yaml", "*.yml"):
            for path in sorted(overlay_dir.glob(ext)):
                if path.name in ("kustomization.yaml", "kustomization.yml"):
                    continue
                _parse_k8s_file(path, nodes, edges)

    # 6. Infrastructure-as-Code (Terraform / Bicep / ARM) — PURE static parsers,
    #    always run; map provider resource *types* to external infra nodes (§4.4).
    for path in sorted(repo.rglob("*.tf")):
        log.debug("parsing Terraform: %s", path)
        _parse_terraform(path, nodes, edges)
    for path in sorted(repo.rglob("*.bicep")):
        log.debug("parsing Bicep: %s", path)
        _parse_bicep(path, nodes, edges)
    # ARM templates: only files that declare a deploymentTemplate $schema are parsed
    # (the parser self-guards), so scanning *.json broadly is safe but we narrow to the
    # conventional names to avoid reading unrelated large JSON.
    arm_candidates: list[Path] = []
    arm_candidates.extend(repo.rglob("azuredeploy.json"))
    arm_candidates.extend(repo.rglob("*.arm.json"))
    for path in sorted(set(arm_candidates)):
        log.debug("parsing ARM template candidate: %s", path)
        _parse_arm(path, nodes, edges)

    # Graceful degrade: nothing found
    if not nodes:
        log.info("no deployment artifacts found under %s — skipping fragment (§18.4)", repo)
        return None

    # Collect nodes and edges, deterministically sorted
    sorted_nodes = sorted(nodes.values(), key=lambda n: n["id"])
    sorted_edges = sorted(edges.values(), key=lambda e: (e["source"], e["target"], e["kind"]))

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

    # When rendering was opted in (T1.1), record a toolchain fingerprint so the
    # environment-conditional (rendered) detail is honest/attestable. This key is ONLY
    # added on the opt-in path, so the default static fragment stays PATH-independent.
    if resolve:
        fragment["provenance"]["deployment_render"] = {
            "helm": _helm_available(),
            "kustomize": _kustomize_available(),
        }

    out = ws.fragments / "deployment.json"
    dump_json(fragment, out)
    log.info("deployment fragment: %d nodes, %d edges -> %s",
             len(sorted_nodes), len(sorted_edges), out)
    return out
