"""Tests for the deployment extraction + generation stages (plan §4.4/§9.3).

All outputs are written under ``tmp_path``. Follows the conventions established in
``tests/test_generate.py``.

Coverage:
  - extract_deployment: fixture nodes/edges, graceful-degrade on empty repo, schema validation
  - generate_deployment: both DSL files always written; deployment facts -> DSL keywords;
    no deployment facts -> banner-only; two runs byte-identical
"""
from __future__ import annotations

import shutil
from pathlib import Path

import jsonschema
import pytest

from anon.ids import SlugRegistry
from anon.jsonio import dump_json, load_json
from anon.paths import resolve_workspace
from anon.stages import extract_deployment, generate_deployment

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "deploy-repo"
SCHEMA_PATH = REPO_ROOT / "schema" / "fact-model.schema.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ws(tmp_path: Path, repo: Path, sub: str = "") -> object:
    base = tmp_path / sub if sub else tmp_path
    return resolve_workspace(repo, arch_dir=base / "arch", rules_dir=base / "rules")


def _load_deployment_fragment(ws) -> dict:
    frag_path = ws.fragments / "deployment.json"
    assert frag_path.exists(), f"expected fragment at {frag_path}"
    return load_json(frag_path)


# ---------------------------------------------------------------------------
# Part 1 — extract_deployment
# ---------------------------------------------------------------------------

class TestExtractDeployment:
    def test_extracts_image_node_from_compose(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        result = extract_deployment.run(ws)
        assert result is not None
        frag = _load_deployment_fragment(ws)
        nodes = frag["deployment"]["nodes"]
        node_ids = {n["id"] for n in nodes}
        # The 'api' service has a build context -> deploy:image:api
        assert "deploy:image:api" in node_ids

    def test_extracts_infra_database_node_from_compose(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        extract_deployment.run(ws)
        frag = _load_deployment_fragment(ws)
        nodes = frag["deployment"]["nodes"]
        node_ids = {n["id"] for n in nodes}
        # The 'db' service uses postgres:15 -> infra:database:db
        assert "infra:database:db" in node_ids

    def test_infra_node_has_external_true(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        extract_deployment.run(ws)
        frag = _load_deployment_fragment(ws)
        nodes = {n["id"]: n for n in frag["deployment"]["nodes"]}
        db = nodes.get("infra:database:db")
        assert db is not None
        assert db.get("external") is True
        assert db.get("kind") == "infra"

    def test_packages_edge_from_dockerfile(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        extract_deployment.run(ws)
        frag = _load_deployment_fragment(ws)
        edges = frag["deployment"]["edges"]
        # The Api/Dockerfile should create a packages edge from deploy:image:Api to
        # csharp:csproj:Api/Api.csproj
        packages_edges = [e for e in edges if e["kind"] == "packages"]
        assert len(packages_edges) >= 1
        # source is the image node
        assert any(e["source"].startswith("deploy:image:") for e in packages_edges)
        # target is a csproj id
        assert any("csharp:csproj:" in e["target"] for e in packages_edges)

    def test_calls_edge_from_depends_on(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        extract_deployment.run(ws)
        frag = _load_deployment_fragment(ws)
        edges = frag["deployment"]["edges"]
        calls_edges = [e for e in edges if e["kind"] == "calls"]
        assert len(calls_edges) >= 1
        # api depends_on db -> edge from deploy:image:api to infra:database:db
        api_to_db = [e for e in calls_edges
                     if e["source"] == "deploy:image:api" and e["target"] == "infra:database:db"]
        assert len(api_to_db) == 1

    def test_env_node_created(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        extract_deployment.run(ws)
        frag = _load_deployment_fragment(ws)
        node_ids = {n["id"] for n in frag["deployment"]["nodes"]}
        # docker-compose.yml -> env:default
        assert "env:default" in node_ids

    def test_compose_build_claim_yields_one_node_per_service(self, tmp_path):
        """A compose service whose ``build:`` points at a Dockerfile in a DIFFERENTLY
        named directory must yield ONE image node (the service's), not a second
        path-named duplicate — the Dockerfile's evidence (base image, EXPOSE,
        packages edge) merges into the claimed node. Regression: a `web` service
        building `src/Web/Dockerfile` minted both deploy:image:web and
        deploy:image:src-web, the latter env-less outside every boundary."""
        repo = tmp_path / "claim-repo"
        (repo / "src" / "Web").mkdir(parents=True)
        (repo / "docker-compose.yml").write_text(
            "services:\n"
            "  web:\n"
            "    build:\n"
            "      context: .\n"
            "      dockerfile: src/Web/Dockerfile\n"
            "    ports: ['8080:80']\n",
            encoding="utf-8",
        )
        (repo / "src" / "Web" / "Dockerfile").write_text(
            "FROM mcr.microsoft.com/dotnet/aspnet:9.0\nEXPOSE 80\n",
            encoding="utf-8",
        )
        (repo / "src" / "Web" / "Web.csproj").write_text(
            "<Project Sdk='Microsoft.NET.Sdk.Web' />", encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="claim")
        extract_deployment.run(ws)
        frag = _load_deployment_fragment(ws)
        nodes = {n["id"]: n for n in frag["deployment"]["nodes"]}
        images = [nid for nid in nodes if nid.startswith("deploy:image:")]
        assert images == ["deploy:image:web"], \
            f"one deployable -> one node; got {images}"
        web = nodes["deploy:image:web"]
        assert web.get("env") == "default", "the compose env scopes the merged node"
        assert web.get("technology") == "mcr.microsoft.com/dotnet/aspnet:9.0", \
            "the Dockerfile's resolved base fills the missing technology"
        assert set(web.get("ports") or []) >= {"80", "8080:80"}, \
            "EXPOSE and compose ports merge"
        pkg = [e for e in frag["deployment"]["edges"] if e["kind"] == "packages"]
        assert [(e["source"], e["target"]) for e in pkg] == \
            [("deploy:image:web", "csharp:csproj:src/Web/Web.csproj")], \
            "the packages edge attaches to the service node, not a duplicate"

    def test_compose_string_build_claims_context_dockerfile(self, tmp_path):
        """``build: <ctx>`` (string form) claims ``<ctx>/Dockerfile``."""
        repo = tmp_path / "strbuild-repo"
        (repo / "backend").mkdir(parents=True)
        (repo / "docker-compose.yml").write_text(
            "services:\n  svc:\n    build: ./backend\n", encoding="utf-8",
        )
        (repo / "backend" / "Dockerfile").write_text(
            "FROM python:3.12\n", encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="strbuild")
        extract_deployment.run(ws)
        frag = _load_deployment_fragment(ws)
        images = sorted(n["id"] for n in frag["deployment"]["nodes"]
                        if n["id"].startswith("deploy:image:"))
        assert images == ["deploy:image:svc"]

    def test_unclaimed_dockerfile_keeps_directory_name(self, tmp_path):
        """A Dockerfile no compose service builds still mints its path-named node
        (the pre-existing standalone behavior is untouched)."""
        repo = tmp_path / "loose-repo"
        (repo / "tools" / "Worker").mkdir(parents=True)
        (repo / "tools" / "Worker" / "Dockerfile").write_text(
            "FROM alpine:3\n", encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="loose")
        extract_deployment.run(ws)
        frag = _load_deployment_fragment(ws)
        images = sorted(n["id"] for n in frag["deployment"]["nodes"]
                        if n["id"].startswith("deploy:image:"))
        assert images == ["deploy:image:tools-worker"]

    def test_graceful_degrade_empty_repo(self, tmp_path):
        empty_repo = tmp_path / "empty-repo"
        empty_repo.mkdir()
        ws = _make_ws(tmp_path, empty_repo, sub="empty")
        result = extract_deployment.run(ws)
        assert result is None
        frag_path = ws.fragments / "deployment.json"
        assert not frag_path.exists(), "no fragment should be written for empty repo"

    def test_graceful_degrade_returns_none_no_artifacts(self, tmp_path):
        # A repo that has only .cs files (no compose/Dockerfile/k8s/helm)
        cs_repo = tmp_path / "cs-only"
        cs_repo.mkdir()
        (cs_repo / "main.cs").write_text("// hello", encoding="utf-8")
        ws = _make_ws(tmp_path, cs_repo, sub="cs_only")
        result = extract_deployment.run(ws)
        assert result is None

    def test_fragment_conforms_to_schema(self, tmp_path):
        schema = load_json(SCHEMA_PATH)
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        extract_deployment.run(ws)
        frag = _load_deployment_fragment(ws)

        # The fragment must have provenance.repo and provenance.generated_at to pass
        # the schema's required fields, but we only emit the extractor-fragment shape.
        # Inject the missing required provenance fields for validation.
        frag.setdefault("provenance", {})
        frag["provenance"].setdefault("repo", "test")
        frag["provenance"].setdefault("commit", "abc")
        frag["provenance"].setdefault("generated_at", "2026-01-01T00:00:00Z")

        jsonschema.validate(instance=frag, schema=schema)

    def test_determinism_extract(self, tmp_path):
        ws_a = _make_ws(tmp_path, DEPLOY_FIXTURE, sub="run_a")
        ws_b = _make_ws(tmp_path, DEPLOY_FIXTURE, sub="run_b")
        extract_deployment.run(ws_a)
        extract_deployment.run(ws_b)
        bytes_a = (ws_a.fragments / "deployment.json").read_bytes()
        bytes_b = (ws_b.fragments / "deployment.json").read_bytes()
        assert bytes_a == bytes_b, "fragment output must be byte-identical across runs"

    def test_multistage_from_helpers(self):
        """Multi-stage FROM parsing: skip --platform flags, follow stage-alias chains, and
        tag an ARG-templated runtime base partial:unrendered (the OrchardCore root Dockerfile
        shape)."""
        # FROM with a --platform flag + AS alias
        assert extract_deployment._parse_from(
            "FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:10.0 AS build-env"
        ) == ("mcr.microsoft.com/dotnet/sdk:10.0", "build-env")
        # final stage aliases an earlier stage -> resolve through the chain
        stages = [
            ("mcr.microsoft.com/dotnet/sdk:10.0", "build-env"),
            ("mcr.microsoft.com/dotnet/aspnet:10.0", "build_linux"),
            ("build_linux", "aspnet"),
        ]
        assert extract_deployment._resolve_final_base(stages) == "mcr.microsoft.com/dotnet/aspnet:10.0"
        # an unresolved ARG in the final base (build_${TARGETOS}) stays templated -> partial
        stages_arg = [
            ("mcr.microsoft.com/dotnet/aspnet:10.0", "build_linux"),
            ("build_${TARGETOS}", "aspnet"),
        ]
        resolved = extract_deployment._resolve_final_base(stages_arg)
        assert extract_deployment._ARG_IN_REF.search(resolved), "templated base must remain detectable"


# ---------------------------------------------------------------------------
# Part 1b — IaC (Terraform / Bicep / ARM) + Helm-render / Kustomize (§4.4 / §16#16)
# ---------------------------------------------------------------------------

def _nodes_by_id(ws) -> dict:
    return {n["id"]: n for n in _load_deployment_fragment(ws)["deployment"]["nodes"]}


class TestExtractTerraform:
    def test_aws_db_and_sqs(self, tmp_path):
        repo = tmp_path / "tf"
        repo.mkdir()
        (repo / "main.tf").write_text(
            'resource "aws_db_instance" "orders" {\n'
            '  engine = "postgres"\n'
            '}\n'
            '\n'
            'resource "aws_sqs_queue" "events" {\n'
            '  name = "events"\n'
            '}\n',
            encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="tf")
        result = extract_deployment.run(ws)
        assert result is not None
        nodes = _nodes_by_id(ws)
        assert "infra:database:orders" in nodes
        assert "infra:queue:events" in nodes
        db = nodes["infra:database:orders"]
        assert db["external"] is True
        assert db["technology"] == "aws_db_instance"
        assert "iac:terraform" in db.get("tags", [])
        assert "iac:terraform" in nodes["infra:queue:events"].get("tags", [])

    def test_azure_and_gcp_mappings(self, tmp_path):
        repo = tmp_path / "tf2"
        repo.mkdir()
        (repo / "infra.tf").write_text(
            'resource "azurerm_storage_account" "blobs" {}\n'
            'resource "azurerm_servicebus_namespace" "bus" {}\n'
            'resource "aws_elasticache_cluster" "sessions" {}\n'
            'resource "google_sql_database_instance" "main" {}\n'
            'resource "aws_lb" "edge" {}\n',
            encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="tf2")
        extract_deployment.run(ws)
        nodes = _nodes_by_id(ws)
        assert "infra:storage:blobs" in nodes
        assert "infra:queue:bus" in nodes
        assert "infra:cache:sessions" in nodes
        assert "infra:database:main" in nodes
        assert "infra:ingress:edge" in nodes

    def test_unknown_resource_type_ignored(self, tmp_path):
        repo = tmp_path / "tf3"
        repo.mkdir()
        (repo / "iam.tf").write_text(
            'resource "aws_iam_role" "exec" {}\n'
            'resource "random_pet" "name" {}\n',
            encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="tf3")
        # No recognised infra -> no nodes -> graceful no-op (None)
        result = extract_deployment.run(ws)
        assert result is None


class TestExtractBicep:
    def test_postgres_resource(self, tmp_path):
        repo = tmp_path / "bicep"
        repo.mkdir()
        (repo / "main.bicep").write_text(
            "resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2022-12-01' = {\n"
            "  name: 'orders-db'\n"
            "}\n"
            "resource bus 'Microsoft.ServiceBus/namespaces@2021-11-01' = {\n"
            "  name: 'events'\n"
            "}\n",
            encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="bicep")
        result = extract_deployment.run(ws)
        assert result is not None
        nodes = _nodes_by_id(ws)
        assert "infra:database:pg" in nodes
        db = nodes["infra:database:pg"]
        assert db["external"] is True
        assert "iac:bicep" in db.get("tags", [])
        assert db["technology"] == "Microsoft.DBforPostgreSQL/flexibleServers"
        assert "infra:queue:bus" in nodes


class TestExtractArm:
    def test_arm_deployment_template(self, tmp_path):
        repo = tmp_path / "arm"
        repo.mkdir()
        (repo / "azuredeploy.json").write_text(
            '{\n'
            '  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/'
            'deploymentTemplate.json#",\n'
            '  "contentVersion": "1.0.0.0",\n'
            '  "resources": [\n'
            '    {"type": "Microsoft.Storage/storageAccounts", "name": "blobs"},\n'
            '    {"type": "Microsoft.Cache/Redis", "name": "cache"}\n'
            '  ]\n'
            '}\n',
            encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="arm")
        result = extract_deployment.run(ws)
        assert result is not None
        nodes = _nodes_by_id(ws)
        # name derived from the type's leaf segment
        assert "infra:storage:storageAccounts" in nodes
        assert "infra:cache:Redis" in nodes
        assert "iac:arm" in nodes["infra:storage:storageAccounts"].get("tags", [])

    def test_non_arm_json_ignored(self, tmp_path):
        repo = tmp_path / "arm2"
        repo.mkdir()
        # A plain JSON that happens to match *.arm.json but is NOT a deploymentTemplate.
        (repo / "config.arm.json").write_text(
            '{"resources": [{"type": "Microsoft.Storage/storageAccounts", "name": "x"}]}',
            encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="arm2")
        result = extract_deployment.run(ws)
        # No $schema deploymentTemplate -> not treated as ARM -> no-op
        assert result is None


class TestIacDeterminism:
    def test_terraform_byte_identical(self, tmp_path):
        repo = tmp_path / "tfd"
        repo.mkdir()
        (repo / "main.tf").write_text(
            'resource "aws_db_instance" "orders" {}\n'
            'resource "aws_s3_bucket" "uploads" {}\n',
            encoding="utf-8",
        )
        ws_a = _make_ws(tmp_path, repo, sub="tfd_a")
        ws_b = _make_ws(tmp_path, repo, sub="tfd_b")
        extract_deployment.run(ws_a)
        extract_deployment.run(ws_b)
        a = (ws_a.fragments / "deployment.json").read_bytes()
        b = (ws_b.fragments / "deployment.json").read_bytes()
        assert a == b


class TestNoIacNoOp:
    def test_repo_without_iac_is_noop(self, tmp_path):
        repo = tmp_path / "plain"
        repo.mkdir()
        (repo / "README.md").write_text("# nothing here", encoding="utf-8")
        ws = _make_ws(tmp_path, repo, sub="plain")
        assert extract_deployment.run(ws) is None
        assert not (ws.fragments / "deployment.json").exists()


# ---------------------------------------------------------------------------
# Part 1c — Helm render (§16#16): tool present => render, absent => static fallback
# ---------------------------------------------------------------------------

def _write_helm_chart(repo: Path) -> Path:
    chart = repo / "chart"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: demo\nversion: 0.1.0\n", encoding="utf-8"
    )
    (chart / "values.yaml").write_text("replicaCount: 2\n", encoding="utf-8")
    (chart / "templates" / "deployment.yaml").write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: web\n"
        "spec:\n"
        "  replicas: {{ .Values.replicaCount }}\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: web\n"
        "          ports:\n"
        "            - containerPort: 8080\n",
        encoding="utf-8",
    )
    return chart


class TestHelmRender:
    def test_static_fallback_when_helm_absent(self, tmp_path, monkeypatch):
        """When helm is NOT on PATH, the existing static partial:unrendered path is used
        unchanged — the chart node is tagged partial:unrendered."""
        repo = tmp_path / "helm"
        repo.mkdir()
        _write_helm_chart(repo)

        real_which = shutil.which

        def fake_which(name, *a, **k):
            if name in ("helm", "kustomize", "kubectl"):
                return None
            return real_which(name, *a, **k)

        monkeypatch.setattr(extract_deployment.shutil, "which", fake_which)

        ws = _make_ws(tmp_path, repo, sub="helm_static")
        result = extract_deployment.run(ws)
        assert result is not None
        nodes = _nodes_by_id(ws)
        # static helm parse names the node after the chart -> deploy:image:demo, partial
        assert "deploy:image:demo" in nodes
        assert "partial:unrendered" in nodes["deploy:image:demo"].get("tags", [])

    @pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH")
    def test_helm_render_drops_unrendered(self, tmp_path):
        """Integration: with helm present, the chart renders to concrete k8s manifests and
        the rendered Deployment node carries NO partial:unrendered tag (§16#16)."""
        repo = tmp_path / "helm_real"
        repo.mkdir()
        _write_helm_chart(repo)
        ws = _make_ws(tmp_path, repo, sub="helm_real")
        result = extract_deployment.run(ws)
        assert result is not None
        nodes = _nodes_by_id(ws)
        # rendered manifest yields a Deployment named 'web' as a concrete image node
        web = nodes.get("deploy:image:web")
        assert web is not None, f"expected rendered web node, got {sorted(nodes)}"
        assert "partial:unrendered" not in web.get("tags", [])


class TestKustomize:
    def test_static_fallback_when_kustomize_absent(self, tmp_path, monkeypatch):
        """Kustomize absent => overlay's sibling k8s manifests are parsed statically."""
        repo = tmp_path / "kz"
        overlay = repo / "overlays" / "prod"
        overlay.mkdir(parents=True)
        (overlay / "kustomization.yaml").write_text(
            "resources:\n  - deployment.yaml\n", encoding="utf-8"
        )
        (overlay / "deployment.yaml").write_text(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: worker\n"
            "spec:\n"
            "  replicas: 3\n",
            encoding="utf-8",
        )

        real_which = shutil.which

        def fake_which(name, *a, **k):
            if name in ("kustomize", "kubectl"):
                return None
            return real_which(name, *a, **k)

        monkeypatch.setattr(extract_deployment.shutil, "which", fake_which)

        ws = _make_ws(tmp_path, repo, sub="kz_static")
        result = extract_deployment.run(ws)
        assert result is not None
        nodes = _nodes_by_id(ws)
        assert "deploy:image:worker" in nodes

    @pytest.mark.skipif(
        shutil.which("kustomize") is None and shutil.which("kubectl") is None,
        reason="kustomize / kubectl not on PATH",
    )
    def test_kustomize_render_scopes_env(self, tmp_path):
        """Integration: with kustomize present, the overlay renders and an env:prod node is
        created scoping the overlay's manifests (§16#16)."""
        repo = tmp_path / "kz_real"
        overlay = repo / "overlays" / "prod"
        overlay.mkdir(parents=True)
        (overlay / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\n"
            "kind: Kustomization\n"
            "resources:\n  - deployment.yaml\n",
            encoding="utf-8",
        )
        (overlay / "deployment.yaml").write_text(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: worker\n"
            "spec:\n"
            "  replicas: 3\n",
            encoding="utf-8",
        )
        ws = _make_ws(tmp_path, repo, sub="kz_real")
        result = extract_deployment.run(ws)
        assert result is not None
        nodes = _nodes_by_id(ws)
        assert "env:prod" in nodes
        assert "deploy:image:worker" in nodes


class TestIacUnitMappings:
    def test_iac_kind_prefers_longest_probe(self):
        # 'servicebus' (queue) must outrank a generic match
        assert extract_deployment._iac_kind("azurerm_servicebus_namespace") == "queue"
        assert extract_deployment._iac_kind("aws_db_instance") == "database"
        assert extract_deployment._iac_kind("aws_s3_bucket") == "storage"
        assert extract_deployment._iac_kind("aws_elasticache_cluster") == "cache"
        assert extract_deployment._iac_kind("aws_lb") == "ingress"
        assert extract_deployment._iac_kind("Microsoft.Cache/Redis") == "cache"
        assert extract_deployment._iac_kind("aws_iam_role") is None
        assert extract_deployment._iac_kind("") is None


# ---------------------------------------------------------------------------
# Part 2 — generate_deployment
# ---------------------------------------------------------------------------

def _minimal_facts_with_deployment() -> dict:
    """A minimal curated fact model with a deployment facet."""
    return {
        "schema_version": "1.1",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {
                "id": "csharp:csproj:Api/Api.csproj",
                "name": "Api",
                "type": "csproj",
                "language": "csharp",
                "container_id": "container:api",
                "container_name": "Api Service",
            }
        ],
        "relationships": [],
        "deployment": {
            "nodes": [
                {"id": "env:default", "name": "default", "kind": "env"},
                {"id": "deploy:image:api", "name": "api", "kind": "image",
                 "technology": "mcr.microsoft.com/dotnet/aspnet:9.0",
                 "env": "default"},
                {"id": "infra:database:db", "name": "db", "kind": "infra",
                 "subkind": "database", "technology": "PostgreSQL",
                 "external": True, "env": "default"},
            ],
            "edges": [
                {"source": "deploy:image:api", "target": "csharp:csproj:Api/Api.csproj",
                 "kind": "packages",
                 "evidence": [{"type": "deploy", "detail": "Dockerfile"}]},
                {"source": "deploy:image:api", "target": "infra:database:db",
                 "kind": "calls", "env": "default",
                 "evidence": [{"type": "deploy", "detail": "compose depends_on"}]},
            ],
        },
    }


def _minimal_facts_no_deployment() -> dict:
    return {
        "schema_version": "1.1",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [],
        "relationships": [],
    }


class TestGenerateDeployment:
    def test_both_files_written_with_facts(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_with_deployment()
        slugs = SlugRegistry()
        # pre-register the container identifier (as generate_structurizr would)
        slugs.assign("container:api")
        generate_deployment.run(ws, facts, slugs)
        assert ws.deployment_dsl.exists(), "deployment_dsl must always be written"
        assert ws.deployment_views_dsl.exists(), "deployment_views_dsl must always be written"

    def test_both_files_written_without_facts(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_no_deployment()
        slugs = SlugRegistry()
        generate_deployment.run(ws, facts, slugs)
        assert ws.deployment_dsl.exists()
        assert ws.deployment_views_dsl.exists()

    def test_deployment_dsl_contains_deployment_environment(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_with_deployment()
        slugs = SlugRegistry()
        slugs.assign("container:api")
        generate_deployment.run(ws, facts, slugs)
        content = ws.deployment_dsl.read_text(encoding="utf-8")
        assert "deploymentEnvironment" in content

    def test_deployment_dsl_contains_deployment_node(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_with_deployment()
        slugs = SlugRegistry()
        slugs.assign("container:api")
        generate_deployment.run(ws, facts, slugs)
        content = ws.deployment_dsl.read_text(encoding="utf-8")
        assert "deploymentNode" in content

    def test_deployment_dsl_contains_infrastructure_node(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_with_deployment()
        slugs = SlugRegistry()
        slugs.assign("container:api")
        generate_deployment.run(ws, facts, slugs)
        content = ws.deployment_dsl.read_text(encoding="utf-8")
        assert "infrastructureNode" in content

    def test_deployment_dsl_contains_container_instance(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_with_deployment()
        slugs = SlugRegistry()
        slugs.assign("container:api")
        generate_deployment.run(ws, facts, slugs)
        content = ws.deployment_dsl.read_text(encoding="utf-8")
        assert "containerInstance" in content

    def test_views_dsl_contains_deployment_view(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_with_deployment()
        slugs = SlugRegistry()
        slugs.assign("container:api")
        generate_deployment.run(ws, facts, slugs)
        views_content = ws.deployment_views_dsl.read_text(encoding="utf-8")
        assert "deployment system" in views_content

    def test_banner_only_when_no_deployment_facts(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_no_deployment()
        slugs = SlugRegistry()
        generate_deployment.run(ws, facts, slugs)
        model_content = ws.deployment_dsl.read_text(encoding="utf-8")
        views_content = ws.deployment_views_dsl.read_text(encoding="utf-8")
        # banner-only: no deploymentEnvironment or deployment view keywords
        assert "deploymentEnvironment" not in model_content
        assert "deployment system" not in views_content
        # but banner present
        assert "GENERATED BY ANON" in model_content
        assert "GENERATED BY ANON" in views_content

    def test_return_value_with_facts(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_with_deployment()
        slugs = SlugRegistry()
        slugs.assign("container:api")
        result = generate_deployment.run(ws, facts, slugs)
        assert result["deployment_nodes"] == 3   # 2 non-env + 1 env
        assert "default" in result["envs"]
        assert "deployment-default" in result["views"]

    def test_return_value_without_facts(self, tmp_path):
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        facts = _minimal_facts_no_deployment()
        slugs = SlugRegistry()
        result = generate_deployment.run(ws, facts, slugs)
        assert result["deployment_nodes"] == 0
        assert result["envs"] == []
        assert result["views"] == []

    def test_determinism_generate_with_facts(self, tmp_path):
        facts = _minimal_facts_with_deployment()

        def run_once(sub: str) -> tuple[bytes, bytes]:
            ws = _make_ws(tmp_path, DEPLOY_FIXTURE, sub=sub)
            slugs = SlugRegistry()
            slugs.assign("container:api")
            generate_deployment.run(ws, facts, slugs)
            return (ws.deployment_dsl.read_bytes(), ws.deployment_views_dsl.read_bytes())

        a = run_once("det_a")
        b = run_once("det_b")
        assert a == b, "generate_deployment output must be byte-identical across runs"

    def test_determinism_generate_without_facts(self, tmp_path):
        facts = _minimal_facts_no_deployment()

        def run_once(sub: str) -> tuple[bytes, bytes]:
            ws = _make_ws(tmp_path, DEPLOY_FIXTURE, sub=sub)
            slugs = SlugRegistry()
            generate_deployment.run(ws, facts, slugs)
            return (ws.deployment_dsl.read_bytes(), ws.deployment_views_dsl.read_bytes())

        a = run_once("det_no_a")
        b = run_once("det_no_b")
        assert a == b

    def test_extract_then_generate_round_trip(self, tmp_path):
        """End-to-end: extract from fixture -> generate -> both DSL files exist and valid."""
        ws = _make_ws(tmp_path, DEPLOY_FIXTURE)
        frag_path = extract_deployment.run(ws)
        assert frag_path is not None
        frag = load_json(frag_path)

        # Build a minimal facts model incorporating the deployment facet
        facts: dict = {
            "schema_version": "1.1",
            "provenance": {"repo": "deploy-repo", "commit": "abc",
                           "generated_at": "2026-01-01T00:00:00Z", "extractors": []},
            "targets": [
                {"id": "csharp:csproj:Api/Api.csproj", "name": "Api",
                 "type": "csproj", "language": "csharp",
                 "container_id": "container:api", "container_name": "Api Service"},
            ],
            "relationships": [],
            "deployment": frag["deployment"],
        }

        slugs = SlugRegistry()
        slugs.assign("container:api")
        result = generate_deployment.run(ws, facts, slugs)

        assert ws.deployment_dsl.exists()
        assert ws.deployment_views_dsl.exists()
        assert result["deployment_nodes"] > 0

        model_content = ws.deployment_dsl.read_text(encoding="utf-8")
        views_content = ws.deployment_views_dsl.read_text(encoding="utf-8")
        assert "deploymentEnvironment" in model_content
        assert "deployment system" in views_content
