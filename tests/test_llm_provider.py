"""Tests for the live Azure AI Foundry provider + config plumbing (plan §16#5).

No network call is ever made: the transport seam (``_raw_complete``) is monkeypatched and
the SDK clients are never built. Env/file config is isolated per test so neither a dev-box
``llm.local.yaml`` nor ambient ``ANON_LLM_*`` can leak in.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anon import cli, llm_provider
from anon.jsonio import dump_json
from anon.llm_provider import LLMConfig
from anon.model import canonicalize
from anon.paths import resolve_workspace
from anon.stages import enrich_llm

TOY = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "toy-repo"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    for env in llm_provider._ENV.values():
        monkeypatch.delenv(env, raising=False)
    # Point both config sources at non-existent paths so neither a real dev-box
    # llm.local.yaml nor .env (which carries the live API key) leaks into the tests.
    monkeypatch.setenv("ANON_LLM_CONFIG", str(tmp_path / "nonexistent.yaml"))
    monkeypatch.setenv("ANON_DOTENV", str(tmp_path / "nonexistent.env"))


# --- config resolution ---------------------------------------------------------

def test_load_config_none_when_unconfigured():
    assert llm_provider.load_llm_config() is None


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("ANON_LLM_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("ANON_LLM_DEPLOYMENT", "gpt-5.5")
    monkeypatch.setenv("ANON_LLM_API_KEY", "sk-test")
    cfg = llm_provider.load_llm_config()
    assert cfg is not None
    assert cfg.endpoint == "https://x.openai.azure.com"
    assert cfg.model_id == "gpt-5.5"          # defaults to deployment
    assert cfg.client_kind == "azure-openai"  # default
    assert cfg.usable() == (True, None)


def test_load_config_file_then_env_override(tmp_path, monkeypatch):
    cfgfile = tmp_path / "llm.yaml"
    cfgfile.write_text("endpoint: https://file.example\ndeployment: from-file\n"
                       "model_id: opus-4.8\nclient_kind: azure-inference\n", encoding="utf-8")
    monkeypatch.setenv("ANON_LLM_CONFIG", str(cfgfile))
    monkeypatch.setenv("ANON_LLM_DEPLOYMENT", "from-env")   # env overrides file
    monkeypatch.setenv("ANON_LLM_API_KEY", "k")
    cfg = llm_provider.load_llm_config()
    assert cfg.deployment == "from-env"          # env wins
    assert cfg.model_id == "opus-4.8"            # from file
    assert cfg.client_kind == "azure-inference"  # from file


def test_dotenv_fills_api_key(tmp_path, monkeypatch):
    # endpoint/deployment from the yaml; the secret comes from .env (env not exported).
    cfgfile = tmp_path / "llm.yaml"
    cfgfile.write_text("endpoint: https://x\ndeployment: d\n", encoding="utf-8")
    dotenv = tmp_path / ".env"
    dotenv.write_text('export ANON_LLM_API_KEY="sk-from-dotenv"\n', encoding="utf-8")
    monkeypatch.setenv("ANON_LLM_CONFIG", str(cfgfile))
    monkeypatch.setenv("ANON_DOTENV", str(dotenv))
    cfg = llm_provider.load_llm_config()
    assert cfg is not None and cfg.usable()[0]
    assert cfg.api_key == "sk-from-dotenv"   # quotes + export prefix stripped


def test_real_env_overrides_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("ANON_LLM_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("ANON_DOTENV", str(dotenv))
    monkeypatch.setenv("ANON_LLM_ENDPOINT", "https://x")
    monkeypatch.setenv("ANON_LLM_DEPLOYMENT", "d")
    monkeypatch.setenv("ANON_LLM_API_KEY", "from-real-env")
    assert llm_provider.load_llm_config().api_key == "from-real-env"


def test_is_v1_endpoint():
    # a Foundry v1 endpoint -> OpenAI client (Bearer, versionless)
    assert llm_provider._is_v1_endpoint("https://r.services.ai.azure.com/openai/v1")
    assert llm_provider._is_v1_endpoint("https://r.services.ai.azure.com/openai/v1/")  # trailing slash
    # a bare classic resource host -> AzureOpenAI (azure_endpoint + api-version)
    assert not llm_provider._is_v1_endpoint("https://r.openai.azure.com")


def test_build_client_uses_openai_for_v1():
    # v1 endpoint constructs the plain OpenAI client (no network at construction)
    from openai import AzureOpenAI, OpenAI
    v1 = llm_provider.make_provider(LLMConfig(
        endpoint="https://r.services.ai.azure.com/openai/v1", deployment="gpt-5.5",
        model_id="gpt-5.5", api_key="k"))._build_client()
    assert isinstance(v1, OpenAI) and not isinstance(v1, AzureOpenAI)
    classic = llm_provider.make_provider(LLMConfig(
        endpoint="https://r.openai.azure.com", deployment="d", model_id="d",
        api_key="k", api_version="2024-10-21"))._build_client()
    assert isinstance(classic, AzureOpenAI)


def test_build_client_unknown_kind_rejected():
    with pytest.raises(RuntimeError):
        llm_provider.make_provider(LLMConfig(
            endpoint="e", deployment="d", model_id="m", client_kind="bogus",
            api_key="k"))._build_client()


def test_build_client_anthropic_foundry():
    """azure-anthropic constructs the native AnthropicFoundry client (no network at build)."""
    anthropic = pytest.importorskip("anthropic")
    if not hasattr(anthropic, "AnthropicFoundry"):
        pytest.skip("installed anthropic SDK predates the AnthropicFoundry client")
    c = llm_provider.make_provider(LLMConfig(
        endpoint="https://r.services.ai.azure.com/anthropic", deployment="claude-opus-4-8",
        model_id="claude-opus-4-8", client_kind="azure-anthropic", api_key="k"))._build_client()
    assert isinstance(c, anthropic.AnthropicFoundry)


def test_raw_complete_anthropic_uses_messages_api():
    """azure-anthropic routes through the native Messages API (client.messages.create) and
    returns the FIRST text block — not the OpenAI-shaped choices[0].message.content. Uses a
    fake client so the test needs neither the SDK nor a network call."""
    prov = llm_provider.make_provider(LLMConfig(
        endpoint="https://r.services.ai.azure.com/anthropic", deployment="claude-opus-4-8",
        model_id="claude-opus-4-8", client_kind="azure-anthropic", api_key="k"))

    class _Block:
        def __init__(self, type, text):
            self.type, self.text = type, text

    class _Messages:
        captured: dict = {}

        def create(self, **kw):
            _Messages.captured = kw
            # a thinking block first, then the JSON text block — only the text is returned
            return type("R", (), {"content": [_Block("thinking", ""),
                                              _Block("text", '{"ok": true}')]})

    prov._client = type("C", (), {"messages": _Messages()})  # skip _build_client (no SDK)
    out = prov._raw_complete("SYS", "USER")
    assert out == '{"ok": true}'
    kw = _Messages.captured
    assert kw["model"] == "claude-opus-4-8"                       # the deployment is the model
    assert kw["system"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert "temperature" not in kw                               # modern Claude rejects it
    assert kw["max_tokens"] >= 1


def test_usable_reports_first_missing_piece():
    assert not LLMConfig(endpoint="", deployment="d", model_id="d", api_key="k").usable()[0]
    assert "endpoint" in LLMConfig(endpoint="", deployment="d", model_id="d", api_key="k").usable()[1]
    assert "deployment" in LLMConfig(endpoint="e", deployment="", model_id="m", api_key="k").usable()[1]
    assert "credential" in LLMConfig(endpoint="e", deployment="d", model_id="m").usable()[1]
    assert LLMConfig(endpoint="e", deployment="d", model_id="m", use_aad=True).usable()[0]  # AAD ok


# --- record normalization (the no-network seam, §8.2) --------------------------

def test_record_from_content_normalizes():
    facts = {"element_id": "csharp:csproj:src/Web/Web.csproj", "name": "Web"}
    content = json.dumps({
        "element_id": "MODEL-MUST-NOT-SET-THIS",
        "element_name": "Web Frontend",
        "description": "Serves UI.",
        "responsibilities": ["a", "b"],
        "confidence": "high",            # alias accepted, mapped to naming_confidence
        "evidence": ["x"],
    })
    rec = llm_provider._record_from_content(content, facts)
    assert rec["element_id"] == facts["element_id"]   # forced to our id, never the model's
    assert rec["naming_confidence"] == "high"
    assert "confidence" not in rec
    assert rec["responsibilities"] == ["a", "b"]


def test_record_from_content_coerces_bad_shapes():
    facts = {"element_id": "x:t:1", "name": "n"}
    rec = llm_provider._record_from_content(
        json.dumps({"responsibilities": "not-a-list", "naming_confidence": "bogus"}), facts)
    assert rec["element_name"] == "n"          # defaulted from facts
    assert rec["responsibilities"] == []       # non-list coerced
    assert rec["naming_confidence"] == "low"   # invalid level -> low
    assert rec["evidence"] == []


def test_record_from_content_raises_on_bad_json():
    with pytest.raises(Exception):
        llm_provider._record_from_content("not json", {"element_id": "x", "name": "n"})


# --- provider.complete() with the transport monkeypatched (no network) ---------

def test_provider_complete_wraps_facts_as_data(monkeypatch):
    cfg = LLMConfig(endpoint="e", deployment="d", model_id="gpt-5.5", api_key="k")
    prov = llm_provider.make_provider(cfg)
    assert prov.model_id == "gpt-5.5"
    captured: dict[str, str] = {}

    def fake_raw(system_prompt, user_msg):
        captured["system"], captured["user"] = system_prompt, user_msg
        return json.dumps({"element_name": "Pretty", "description": "d",
                           "responsibilities": ["r"], "naming_confidence": "medium",
                           "evidence": ["e"]})

    monkeypatch.setattr(prov, "_raw_complete", fake_raw)
    rec = prov.complete("SYS", "PROMPT", {"element_id": "x:t:1", "name": "n"})
    assert rec["element_id"] == "x:t:1" and rec["element_name"] == "Pretty"
    assert captured["system"] == "SYS"
    assert "<DATA>" in captured["user"] and "x:t:1" in captured["user"]   # untrusted-data framing


def test_narrative_record_from_content_normalizes():
    # The §4 narrative shape, not the naming shape: element_id (+element_kind) forced to
    # ours, key_points coerced to {text, cites}, extra fields dropped (closed schema).
    facts = {"element_id": "container:basketAPI", "element_kind": "container",
             "name": "Basket API", "citable_ids": ["container:basketAPI"]}
    content = json.dumps({
        "element_id": "MODEL-MUST-NOT-SET-THIS",
        "abstract_md": "The **Basket API** stores carts.",
        "key_points": [{"text": "Persists to Redis", "cites": ["container:basketAPI"]},
                       {"text": "dropped: cites not a list", "cites": "nope"}],
        "naming_confidence": "high",
        "rationale": "an extra field additionalProperties:false would reject",
    })
    rec = llm_provider._narrative_record_from_content(content, facts)
    assert rec["element_id"] == facts["element_id"]         # identity is ours, never the model's
    assert rec["element_kind"] == "container"               # forced from the payload too
    assert set(rec) == {"element_id", "element_kind", "abstract_md", "key_points",
                        "naming_confidence"}
    assert rec["key_points"][0] == {"text": "Persists to Redis", "cites": ["container:basketAPI"]}
    assert rec["key_points"][1]["cites"] == []              # non-list coerced (then schema rejects)


def test_provider_complete_dispatches_on_payload_shape(monkeypatch):
    """A §4 narrative payload (carries citable_ids) must NOT be routed through the naming
    normalizer — routing it there mis-shapes the record and failed every narrative against
    the live provider (the datasheet-§4 regression class)."""
    prov = llm_provider.make_provider(
        LLMConfig(endpoint="e", deployment="d", model_id="m", api_key="k"))
    captured: dict[str, str] = {}

    def fake_raw(system_prompt, user_msg):
        captured["user"] = user_msg
        return json.dumps({"element_id": "IGNORED", "abstract_md": "x",
                           "key_points": [{"text": "t", "cites": ["container:c"]}],
                           "naming_confidence": "low"})

    monkeypatch.setattr(prov, "_raw_complete", fake_raw)
    rec = prov.complete("SYS", "PROMPT",
                        {"element_id": "component:c", "element_kind": "component",
                         "name": "C", "citable_ids": ["component:c"]})
    assert rec["element_id"] == "component:c"            # narrative branch, identity forced
    assert "abstract_md" in rec and "element_name" not in rec
    assert "element_id" in captured["user"]              # narrative user-message framing


def test_provider_complete_dispatches_scenario_payload(monkeypatch):
    """Source F (dynamic-view §7) sends a {containers, edges} payload with NO element_id and
    expects a {"scenarios": [...]} reply — it must NOT be routed through the naming normalizer
    (which forced rec['element_id'] = facts['element_id'] -> KeyError 'element_id' against the
    LIVE provider; the mock in the F unit test hid it). The §7 branch returns the parsed
    envelope verbatim, identity/validation staying downstream."""
    prov = llm_provider.make_provider(
        LLMConfig(endpoint="e", deployment="d", model_id="m", api_key="k"))
    captured: dict[str, str] = {}

    def fake_raw(system_prompt, user_msg):
        captured["user"] = user_msg
        return json.dumps({"scenarios": [
            {"name": "Order flow",
             "steps": [{"from": "container:web", "to": "container:ordering",
                        "description": "places order"}]}]})

    monkeypatch.setattr(prov, "_raw_complete", fake_raw)
    payload = {"containers": [{"id": "container:web", "name": "Web"}],
               "edges": [{"from": "container:web", "to": "container:ordering",
                          "evidence_layer": "build"}]}
    rec = prov.complete("SYS", "PROMPT", payload)  # would KeyError 'element_id' before the fix
    assert [s["name"] for s in rec["scenarios"]] == ["Order flow"]
    assert rec["scenarios"][0]["steps"][0]["from"] == "container:web"
    # the {containers, edges} payload was wrapped as untrusted DATA, no element-id framing.
    assert "edges" in captured["user"] and "element_id" not in captured["user"]


def test_provider_complete_dispatches_adr_draft_payload(monkeypatch):
    """The ADR authoring assist (adr-mining §6.1) sends a {task:"structure-adr", sections}
    payload. It carries an element_id (the candidate slug) but must NOT be routed through the
    naming normalizer — that projects the reply onto a closed key set with no `sections`, so the
    restructured prose was silently stripped and every "Structure my notes" degraded to a
    verbatim copy of the input (the regression the user hit). The draft branch returns the
    {"sections": {...}} envelope, keeping ONLY the keys the human supplied (§0.5)."""
    prov = llm_provider.make_provider(
        LLMConfig(endpoint="e", deployment="d", model_id="m", api_key="k"))
    captured: dict[str, str] = {}

    def fake_raw(system_prompt, user_msg):
        captured["user"] = user_msg
        # the model restructures the supplied section AND (mis)volunteers one the human left out
        return json.dumps({"sections": {
            "decision": "We standardize on Serilog for all structured logging.",
            "rationale": "INVENTED — the human supplied no rationale"}})

    monkeypatch.setattr(prov, "_raw_complete", fake_raw)
    rec = prov.complete("SYS", "PROMPT",
                        {"element_id": "tech-serilog", "task": "structure-adr",
                         "sections": {"decision": "use serilog everywhere"}})
    # the restructured prose survives (the fix) ...
    assert rec == {"sections": {
        "decision": "We standardize on Serilog for all structured logging."}}
    # ... and a section the human never supplied is dropped, never invented (§0.5).
    assert "rationale" not in rec["sections"]
    # draft framing names the supplied section keys — NOT the naming-record keys.
    assert "EXACTLY these keys: decision" in captured["user"]
    assert "element_name" not in captured["user"]


def test_scenario_record_from_content_coerces_envelope():
    assert llm_provider._scenario_record_from_content('{"scenarios": []}') == {"scenarios": []}
    # a non-list / missing `scenarios` -> empty list (an honest miss), never a crash.
    assert llm_provider._scenario_record_from_content('{"scenarios": "x"}') == {"scenarios": []}
    assert llm_provider._scenario_record_from_content("{}") == {"scenarios": []}
    with pytest.raises(ValueError):
        llm_provider._scenario_record_from_content("not json")


def test_provider_plugs_into_enrich_stage(tmp_path, monkeypatch):
    """The provider satisfies enrich_llm.Provider and drives a real propose run (no network)."""
    ws = resolve_workspace(TOY, arch_dir=tmp_path / "arch", rules_dir=tmp_path / "rules")
    dump_json(canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [{"id": "csharp:csproj:src/Web/Web.csproj", "name": "Web", "type": "csproj",
                     "language": "csharp", "container_name": "Web Frontend"}],
        "relationships": [],
    }), ws.curated_facts)
    ws.mapping_rules.parent.mkdir(parents=True, exist_ok=True)
    ws.mapping_rules.write_text("version: 1\n", encoding="utf-8")

    prov = llm_provider.make_provider(LLMConfig(endpoint="e", deployment="d", model_id="m", api_key="k"))
    monkeypatch.setattr(prov, "_raw_complete", lambda sp, um: json.dumps(
        {"element_name": "Web Frontend", "description": "UI", "responsibilities": ["serve"],
         "naming_confidence": "high", "evidence": ["Web"]}))
    enriched = enrich_llm.run(ws, "llm-propose", provider=prov)
    web = enriched["targets"][0]
    assert web["llm_name"] == "Web Frontend" and web["enrich_status"] == "proposed"


# --- CLI provider resolution (plan §8.5) ---------------------------------------

def test_cli_resolve_no_llm_builds_nothing():
    assert cli._resolve_provider("no-llm") == (None, None)


def test_cli_resolve_missing_config_explains():
    prov, msg = cli._resolve_provider("llm-propose")
    assert prov is None and "no LLM config" in msg


def test_cli_resolve_builds_from_env(monkeypatch):
    monkeypatch.setenv("ANON_LLM_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("ANON_LLM_DEPLOYMENT", "gpt-5.5")
    monkeypatch.setenv("ANON_LLM_API_KEY", "k")
    prov, msg = cli._resolve_provider("llm-propose")
    assert prov is not None and msg == "azure-openai:gpt-5.5"
