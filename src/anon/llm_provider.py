"""Live Azure AI Foundry provider for the §8 enrichment stage (plan §16#5, node 4).

This is the *only* place Anon egresses to a model. It implements the
``enrich_llm.Provider`` protocol (``model_id`` + ``complete()``) behind config the
operator supplies; absent that config, :func:`load_llm_config` returns ``None`` and the
pipeline stays on the deterministic ``--no-llm`` baseline — there is never a silent
network call (the enrich stage defaults to ``NullProvider``).

Three client kinds (plan §8.3 — "GPT-5.5 via openai / Claude via azure-ai-inference"):
  - ``azure-openai``  (default): the ``openai.AzureOpenAI`` client against an Azure
    OpenAI deployment (GPT-class);
  - ``azure-inference``        : the ``azure.ai.inference`` client against a Foundry
    model-catalog deployment (the OpenAI-shaped chat-completions surface);
  - ``azure-anthropic``        : the **native** Anthropic Messages API for Claude models
    on Azure AI Foundry, via the dedicated ``anthropic.AnthropicFoundry`` client (it
    speaks ``/anthropic/v1/messages`` — system as a top-level field, one user turn — not
    the OpenAI-compat shim). ``endpoint`` is the resource's ``.../anthropic`` base URL (or
    a bare resource subdomain) and ``deployment`` is the Claude deployment name.

Auth is an API key (env ``ANON_LLM_API_KEY``, preferred) or, with ``use_aad: true``,
Azure AD via ``DefaultAzureCredential`` (workload identity, plan §10.4). SDK imports are
**lazy** so importing this module — and running ``--no-llm`` / the test suite — never
requires the SDKs or credentials.

Config resolution (later overrides earlier):
  1. a YAML file: ``$ANON_LLM_CONFIG`` if set, else ``<anon-repo>/llm.local.yaml``
     (gitignored). Holds non-secret settings (endpoint, deployment, model_id, …).
  2. ``ANON_LLM_*`` environment variables (the secret/api_key path).

``model_id`` flows into the §8.3 cache key, so switching GPT-5.5 <-> Opus is a reviewable
recompute, not silent churn (plan §16#5).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_yaml

# Azure OpenAI REST api-version used when the config/env does not pin one. A recent value
# that supports response_format=json_object; override per deployment via config/env.
DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_CLIENT_KIND = "azure-openai"
# max_tokens for the native Anthropic Messages API (azure-anthropic). The enrichment/
# narrative/scenario replies are small structured JSON objects; 4096 is generous headroom
# (well under any HTTP timeout for a non-streaming call) and never truncates them.
ANTHROPIC_MAX_TOKENS = 4096
# AAD scope for Azure OpenAI / Cognitive Services token auth (plan §10.4).
_AAD_SCOPE = "https://cognitiveservices.azure.com/.default"

_ENV = {
    "endpoint": "ANON_LLM_ENDPOINT",
    "api_key": "ANON_LLM_API_KEY",
    "deployment": "ANON_LLM_DEPLOYMENT",
    "model_id": "ANON_LLM_MODEL_ID",
    "api_version": "ANON_LLM_API_VERSION",
    "client_kind": "ANON_LLM_CLIENT",
    "use_aad": "ANON_LLM_USE_AAD",
}


@dataclass(frozen=True)
class LLMConfig:
    """Resolved, non-secret-by-default Azure AI Foundry settings."""

    endpoint: str
    deployment: str
    model_id: str
    client_kind: str = DEFAULT_CLIENT_KIND
    api_version: str = DEFAULT_API_VERSION
    api_key: str | None = None
    use_aad: bool = False
    # Sampling temperature. Default None = omit it (let the service default apply) —
    # reasoning-tier models (e.g. gpt-5.x) reject a non-default temperature. Determinism is
    # already provided by the §8.3 cache key, so we don't force temperature=0.
    temperature: float | None = None

    def usable(self) -> tuple[bool, str | None]:
        """Whether a live call can be made; second item explains the first missing piece."""
        if not self.endpoint:
            return False, "no endpoint (set ANON_LLM_ENDPOINT or llm.local.yaml `endpoint`)"
        if not self.deployment:
            return False, "no deployment (set ANON_LLM_DEPLOYMENT or `deployment`)"
        if not self.api_key and not self.use_aad:
            return False, ("no credential (set ANON_LLM_API_KEY, or `use_aad: true` for "
                           "workload identity)")
        return True, None


def _anon_repo_root() -> Path | None:
    """The dir containing ``pyproject.toml`` (where ``llm.local.yaml``/``.env`` live), or
    ``None`` when not found above this package (e.g. installed as a wheel).

    We deliberately do NOT fall back to the current working directory: that would resolve
    secret-bearing files (``.env``, ``llm.local.yaml``) relative to wherever ``arch`` is
    invoked, so a stray file in an unrelated CWD could inject an endpoint/key. When the
    repo root can't be found, callers skip implicit discovery and rely on the explicit
    ``ANON_LLM_CONFIG`` / ``ANON_DOTENV`` / ``ANON_LLM_*`` overrides."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def _config_path() -> Path | None:
    env_path = os.environ.get("ANON_LLM_CONFIG")
    if env_path:
        return Path(env_path)
    root = _anon_repo_root()
    return (root / "llm.local.yaml") if root else None


def _dotenv_path() -> Path | None:
    env_path = os.environ.get("ANON_DOTENV")
    if env_path:
        return Path(env_path)
    root = _anon_repo_root()
    return (root / ".env") if root else None


def _read_dotenv() -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from a ``.env`` file (gitignored), for the secret/api-key
    path. Real process env still wins; this only fills vars not already exported. Supports
    an optional ``export`` prefix and surrounding quotes. Never logged."""
    path = _dotenv_path()
    out: dict[str, str] = {}
    if path is None or not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        out[key] = val.strip().strip('"').strip("'")
    return out


def _is_v1_endpoint(endpoint: str) -> bool:
    """True for an Azure AI Foundry **v1** endpoint (``.../openai/v1``).

    That surface is OpenAI-compatible and **versionless**: it is driven by the plain
    ``openai.OpenAI`` client with ``base_url`` + ``Authorization: Bearer`` — NOT
    ``AzureOpenAI``, which would append an ``?api-version=`` query the v1 endpoint rejects
    ("API version not supported"). A bare classic host (``https://x.openai.azure.com``)
    returns False and uses ``AzureOpenAI(azure_endpoint=...)`` + ``api-key`` header."""
    return "/openai/v1" in endpoint.rstrip("/")


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def load_llm_config() -> LLMConfig | None:
    """Resolve config from the YAML file (if present) then env overrides.

    Returns ``None`` when no live call is possible (missing endpoint/deployment/credential)
    so the caller can stay on the deterministic baseline rather than degrade every element.
    """
    data: dict[str, Any] = {}
    path = _config_path()
    if path and path.exists():
        loaded = load_yaml(path) or {}
        if isinstance(loaded, dict):
            data.update(loaded)
    dotenv = _read_dotenv()
    for field, env in _ENV.items():
        # precedence: real process env > .env file > llm.local.yaml.
        val = os.environ.get(env) or dotenv.get(env)
        if val is not None and val != "":
            data[field] = val

    endpoint = str(data.get("endpoint", "") or "")
    deployment = str(data.get("deployment", "") or "")
    if not endpoint and not deployment and not data.get("api_key"):
        return None  # nothing configured at all — stay on --no-llm
    # temperature is user-authored (no env var for it); a non-numeric value must fall back
    # to the service default, not crash the whole run with an unhandled ValueError.
    raw_temp = data.get("temperature")
    if raw_temp in (None, ""):
        temperature = None
    else:
        try:
            temperature = float(raw_temp)
        except (TypeError, ValueError):
            temperature = None
    cfg = LLMConfig(
        endpoint=endpoint,
        deployment=deployment,
        # model_id defaults to the deployment so the cache key is always concrete.
        model_id=str(data.get("model_id") or deployment or "unknown"),
        client_kind=str(data.get("client_kind") or DEFAULT_CLIENT_KIND),
        api_version=str(data.get("api_version") or DEFAULT_API_VERSION),
        api_key=(str(data["api_key"]) if data.get("api_key") else None),
        use_aad=_as_bool(data.get("use_aad", False)),
        temperature=temperature,
    )
    return cfg


# --- record normalization (pure; unit-tested without any network) ---------------

def _record_from_content(content: str, element_facts: dict[str, Any]) -> dict[str, Any]:
    """Parse a model JSON reply into a §8.2 enrichment record, defensively normalized.

    ``element_id`` is forced to the known id (identity is ours, never the model's), and
    the shape is coerced to what ``enrich_llm._validate_record`` expects. Raises on
    unparseable JSON so the §8.3 bounded-retry loop can repair-and-retry.
    """
    rec = json.loads(content)
    if not isinstance(rec, dict):
        raise ValueError("model reply was not a JSON object")
    rec["element_id"] = element_facts["element_id"]
    rec.setdefault("element_name", element_facts.get("name", rec["element_id"]))
    resp = rec.get("responsibilities")
    rec["responsibilities"] = [str(x) for x in resp] if isinstance(resp, list) else []
    conf = rec.get("naming_confidence") or rec.get("confidence") or "low"
    rec["naming_confidence"] = conf if conf in {"low", "medium", "high"} else "low"
    ev = rec.get("evidence")
    rec["evidence"] = [str(x) for x in ev] if isinstance(ev, list) else []
    # Project onto the closed-schema key set: LLMs commonly volunteer extra keys
    # (rationale, reasoning, category, …) which `additionalProperties:false` rejects —
    # which would burn every retry and degrade an otherwise-usable name. Keep only the
    # allowed fields so valid proposals survive.
    allowed = {"element_id", "element_name", "description", "responsibilities",
               "naming_confidence", "evidence"}
    return {k: v for k, v in rec.items() if k in allowed}


def _user_message(prompt: str, element_facts: dict[str, Any]) -> str:
    """Wrap the whitelisted facts as untrusted DATA (matches the §8.3 system prompt)."""
    facts = json.dumps(element_facts, ensure_ascii=False, sort_keys=True)
    return (f"{prompt}\n\n<DATA>\n{facts}\n</DATA>\n\n"
            "Return ONLY a JSON object with keys: element_id, element_name, description, "
            "responsibilities (array of 1-3 short strings), naming_confidence "
            "(one of low|medium|high), evidence (array of strings citing supplied facts).")


# --- element-narrative shape (datasheet-enrichment plan §4) ---------------------
# The SAME provider serves two §8 schemas. Naming payloads want the naming record above;
# narrative payloads (system / container / component) want the llm-narrative.schema.json
# shape. Both now carry an element id, so the discriminator in ``complete()`` is the
# presence of ``citable_ids`` (only narrative payloads ship the cite list). Forcing a
# narrative reply through the naming normalizer would mis-shape it — so the wrapper/
# normalizer is chosen by payload shape. Both branches keep IDENTITY ours (forced from the
# payload, never the model's) and project onto the closed key set so a stray field never
# burns every retry.

def _narrative_user_message(prompt: str, element_facts: dict[str, Any]) -> str:
    """Untrusted-DATA wrapper for the §4 element-narrative pass (cf. _user_message)."""
    facts = json.dumps(element_facts, ensure_ascii=False, sort_keys=True)
    return (f"{prompt}\n\n<DATA>\n{facts}\n</DATA>\n\n"
            "Return ONLY a JSON object with keys: element_id, abstract_md (one paragraph, "
            "max 700 chars), key_points (array of 1-5 objects {text, cites}), naming_confidence "
            "(one of low|medium|high). Every key point's cites array must hold ids drawn from "
            "the supplied citable_ids.")


def _narrative_record_from_content(content: str, element_facts: dict[str, Any]) -> dict[str, Any]:
    """Parse a model reply into a §4 narrative record, defensively normalized.

    Mirrors :func:`_record_from_content` but for the llm-narrative schema: ``element_id``
    is forced to the known id (identity is ours), ``key_points`` are coerced to the
    ``{text, cites}`` shape, ``element_kind`` (if the payload carries one) is forced too,
    and the result is projected onto the closed key set so a volunteered extra field does
    not trip ``additionalProperties:false`` and burn the bounded-retry budget. Raises on
    unparseable JSON so the retry loop can repair.
    """
    rec = json.loads(content)
    if not isinstance(rec, dict):
        raise ValueError("model reply was not a JSON object")
    rec["element_id"] = element_facts["element_id"]
    if element_facts.get("element_kind"):
        rec["element_kind"] = element_facts["element_kind"]
    kps_in = rec.get("key_points")
    kps_out: list[dict[str, Any]] = []
    if isinstance(kps_in, list):
        for kp in kps_in:
            if not isinstance(kp, dict):
                continue
            text = str(kp.get("text", "")).strip()
            cites = kp.get("cites")
            cites = [str(c) for c in cites] if isinstance(cites, list) else []
            if text:
                kps_out.append({"text": text, "cites": cites})
    rec["key_points"] = kps_out
    conf = rec.get("naming_confidence") or rec.get("confidence") or "low"
    rec["naming_confidence"] = conf if conf in {"low", "medium", "high"} else "low"
    rec.setdefault("abstract_md", "")
    allowed = {"element_id", "element_kind", "abstract_md", "key_points", "naming_confidence"}
    return {k: v for k, v in rec.items() if k in allowed}


# --- scenario-grouping shape (dynamic-view plan §7, Source F) --------------------
# A THIRD governed egress shape. Source F asks the model to GROUP + ORDER + DESCRIBE the
# supplied evidenced edges into runtime use cases. Unlike the two §8 element schemas it is NOT
# element-keyed (no ``element_id``) and its reply is a ``{"scenarios": [...]}`` list that is
# re-validated downstream by ``scenarios.build_dynamic_view`` (the validator is the control,
# not the provider) — so it needs a plain JSON completion, never the element-id normalization
# the naming/narrative records force.

def _scenario_user_message(prompt: str, payload: dict[str, Any]) -> str:
    """Untrusted-DATA wrapper for the §7 scenario-grouping pass (the PROMPT already pins the
    reply shape, cf. :func:`_user_message`)."""
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"{prompt}\n\n<DATA>\n{data}\n</DATA>"


def _scenario_record_from_content(content: str) -> dict[str, Any]:
    """Parse a model reply into the ``{"scenarios": [...]}`` envelope. Identity/validation stay
    downstream (the §7 hard filter + ``build_dynamic_view``); here we only coerce the envelope.
    Raises on unparseable JSON so the caller can treat it as an honest miss."""
    rec = json.loads(content)
    if not isinstance(rec, dict):
        raise ValueError("model reply was not a JSON object")
    scs = rec.get("scenarios")
    return {"scenarios": scs if isinstance(scs, list) else []}


# --- ADR-draft shape (adr-mining plan §6.1, Tier 3 authoring) -------------------
# A FOURTH governed egress shape. The "Structure my notes" assist asks the model to RESTRUCTURE
# the architect's OWN sectioned ADR prose — improve grammar/flow/structure only, never add content
# (§0.5). It is keyed by ``task: "structure-adr"`` and carries a ``sections`` map (only the
# sections the human actually filled); the reply is the same ``{"sections": {...}}`` shape, which
# ``adr._polish_sections`` re-applies key-by-key (overwriting only a section the human supplied).
# It must NOT go through the naming/narrative element-id normalizers: those force ``element_id``
# and project the reply onto a CLOSED key set that does not include ``sections`` — which silently
# stripped the restructured prose and degraded every draft to a verbatim copy of the input.

def _draft_user_message(prompt: str, payload: dict[str, Any]) -> str:
    """Untrusted-DATA wrapper for the §6.1 ADR-restructure pass. Names the exact section keys the
    reply must carry so it maps 1:1 back onto the human's supplied sections (cf. _user_message)."""
    sections = payload.get("sections")
    keys = ", ".join(sorted(sections)) if isinstance(sections, dict) else ""
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return (f"{prompt}\n\n<DATA>\n{data}\n</DATA>\n\n"
            "Return ONLY a JSON object {\"sections\": {<key>: <improved prose string>}} using "
            f"EXACTLY these keys: {keys}. Improve grammar, flow, and structure only — never add a "
            "decision, rationale, consequence, or alternative the human did not write, and never "
            "fill a section that was not supplied.")


def _draft_record_from_content(content: str, element_facts: dict[str, Any]) -> dict[str, Any]:
    """Parse a model reply into the ``{"sections": {...}}`` envelope for the §6.1 restructure pass.
    Keeps ONLY the keys the human supplied (a defensive §0.5 guard — the model may volunteer an
    empty/extra section) and coerces each value to a string. Identity/content control stay
    downstream in ``adr._polish_sections``. Raises on unparseable JSON so the caller falls back to
    the human's verbatim prose."""
    rec = json.loads(content)
    if not isinstance(rec, dict):
        raise ValueError("model reply was not a JSON object")
    secs = rec.get("sections")
    supplied = element_facts.get("sections")
    supplied = supplied if isinstance(supplied, dict) else {}
    out: dict[str, str] = {}
    if isinstance(secs, dict):
        for k in supplied:           # only sections the human gave us — never invent/fill one
            v = secs.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v
    return {"sections": out}


# --- the provider --------------------------------------------------------------

class AzureFoundryProvider:
    """Live Azure AI Foundry provider (implements ``enrich_llm.Provider``)."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.model_id = config.model_id
        self._client: Any = None  # built lazily on first complete()

    # -- client construction (lazy; raises actionable errors) --
    def _aad_token_provider(self):
        try:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
            raise RuntimeError("use_aad set but azure-identity is not installed") from exc
        return get_bearer_token_provider(DefaultAzureCredential(), _AAD_SCOPE)

    def _build_client(self) -> Any:
        kind = self.config.client_kind
        if kind == "azure-openai":
            ep = self.config.endpoint.rstrip("/")
            if _is_v1_endpoint(ep):
                # Foundry v1 OpenAI-compatible surface: plain OpenAI client, Bearer auth,
                # versionless. For AAD, pass a freshly-minted bearer token as the api_key.
                try:
                    from openai import OpenAI
                except ImportError as exc:  # pragma: no cover
                    raise RuntimeError("client_kind 'azure-openai' needs the `openai` package") from exc
                key = self._aad_token_provider()() if self.config.use_aad else self.config.api_key
                return OpenAI(base_url=ep, api_key=key)
            try:
                from openai import AzureOpenAI
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("client_kind 'azure-openai' needs the `openai` package") from exc
            kwargs: dict[str, Any] = {"azure_endpoint": ep, "api_version": self.config.api_version}
            if self.config.use_aad:
                kwargs["azure_ad_token_provider"] = self._aad_token_provider()
            else:
                kwargs["api_key"] = self.config.api_key
            return AzureOpenAI(**kwargs)
        if kind == "azure-inference":
            try:
                from azure.ai.inference import ChatCompletionsClient
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("client_kind 'azure-inference' needs `azure-ai-inference`") from exc
            if self.config.use_aad:
                from azure.identity import DefaultAzureCredential
                cred: Any = DefaultAzureCredential()
            else:
                from azure.core.credentials import AzureKeyCredential
                cred = AzureKeyCredential(self.config.api_key or "")
            return ChatCompletionsClient(endpoint=self.config.endpoint, credential=cred)
        if kind == "azure-anthropic":
            # Native Anthropic Messages API on Azure AI Foundry (Claude). The dedicated
            # AnthropicFoundry client targets `<resource>.services.ai.azure.com/anthropic`
            # and passes the `model` field through to the Claude deployment.
            try:
                from anthropic import AnthropicFoundry
            except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
                raise RuntimeError("client_kind 'azure-anthropic' needs the `anthropic` package "
                                   "(>=0.69 for the AnthropicFoundry client)") from exc
            ep = self.config.endpoint.rstrip("/")
            # A full URL is the base_url; a bare value is the resource subdomain (the form the
            # SDK's ANTHROPIC_RESOURCE env var takes) — accept either so the operator can paste
            # `https://my-res.services.ai.azure.com/anthropic` or just `my-res`.
            kwargs = {"base_url": ep} if "://" in ep else {"resource": ep}
            if self.config.use_aad:
                # Entra ID: a bearer-token provider (same Cognitive Services scope the
                # azure-openai AAD path uses) instead of an api key.
                kwargs["azure_ad_token_provider"] = self._aad_token_provider()
            else:
                kwargs["api_key"] = self.config.api_key
            return AnthropicFoundry(**kwargs)
        raise RuntimeError(f"unknown client_kind {kind!r} "
                           "(use azure-openai|azure-inference|azure-anthropic)")

    def _client_lazy(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # -- transport (kept thin; record parsing is the unit-tested seam) --
    def _raw_complete(self, system_prompt: str, user_msg: str) -> str:
        client = self._client_lazy()
        if self.config.client_kind == "azure-anthropic":
            # Native Messages API: `system` is a top-level field, the prompt is one user
            # turn, and `max_tokens` is required. Temperature is never sent — modern Claude
            # (4.x) rejects a non-default temperature, and determinism already comes from the
            # §8.3 cache key. The reply is the first text block (content is a typed-block list).
            resp = client.messages.create(
                model=self.config.deployment,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    return block.text or ""
            return ""
        temp = self.config.temperature
        if self.config.client_kind == "azure-openai":
            kwargs: dict[str, Any] = dict(
                model=self.config.deployment,
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user_msg}],
                response_format={"type": "json_object"},
            )
            if temp is not None:
                kwargs["temperature"] = temp
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        # azure-inference
        from azure.ai.inference.models import SystemMessage, UserMessage
        kwargs = dict(
            messages=[SystemMessage(content=system_prompt), UserMessage(content=user_msg)],
            model=self.config.deployment,
        )
        if temp is not None:
            kwargs["temperature"] = temp
        resp = client.complete(**kwargs)
        return resp.choices[0].message.content or ""

    def complete_text(self, system_prompt: str, user_msg: str) -> str:
        """Raw single-turn completion returning the model's reply text verbatim.

        The public seam for callers that own their OWN reply schema and do not want the §8
        element-id normalization ``complete()`` applies — notably the RQ2 §6.2 LLM-recovery
        *baseline* (``baselines/llm_recovery.py``), which asks the model for a whole-graph
        partition rather than a per-element record. Kept deliberately thin (all client-kind
        transport lives in ``_raw_complete``); parsing/validation is the caller's job."""
        return self._raw_complete(system_prompt, user_msg)

    def complete(self, system_prompt: str, prompt: str,
                 element_facts: dict[str, Any]) -> dict[str, Any]:
        # One provider, two §8 schemas — dispatch on payload shape. A §4 narrative payload
        # (system/container/component) always ships a ``citable_ids`` cite list; a naming
        # payload never does. Routing a narrative through the naming normalizer would
        # mis-shape it and fail every narrative (the datasheet-§4 regression class).
        # ADR authoring assist (adr-mining §6.1, Tier 3) — restructure the human's OWN ADR sections.
        # Keyed by the explicit `task` so it never collides with the element schemas; it carries an
        # `element_id` (the candidate slug) but routing it through the naming normalizer dropped the
        # `sections` key and degraded every "Structure my notes" to a verbatim copy of the input.
        # The reply is a {"sections": {...}} envelope re-applied downstream by adr._polish_sections.
        if element_facts.get("task") == "structure-adr":
            content = self._raw_complete(system_prompt,
                                         _draft_user_message(prompt, element_facts))
            return _draft_record_from_content(content, element_facts)
        # Source F (dynamic-view §7) — a scenario-grouping request, NOT §8 element enrichment:
        # the payload carries the evidenced `edges` set and has no `element_id`, and expects a
        # raw {"scenarios": [...]} reply, so skip the element-id normalization the naming/
        # narrative records force (that forcing is what raised KeyError 'element_id' before).
        if "edges" in element_facts and "element_id" not in element_facts:
            content = self._raw_complete(system_prompt,
                                         _scenario_user_message(prompt, element_facts))
            return _scenario_record_from_content(content)
        if "citable_ids" in element_facts:
            content = self._raw_complete(system_prompt,
                                         _narrative_user_message(prompt, element_facts))
            return _narrative_record_from_content(content, element_facts)
        content = self._raw_complete(system_prompt, _user_message(prompt, element_facts))
        return _record_from_content(content, element_facts)


def make_provider(config: LLMConfig) -> AzureFoundryProvider:
    return AzureFoundryProvider(config)
