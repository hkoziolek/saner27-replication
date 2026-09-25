"""Tool configuration for the GUI Settings tab.

This is the engine-side home for *user/tool configuration* — the thing the GUI's
Settings screen reads and writes (serve only delegates here, never computes settings
itself: GUI §2.1 iron rule). It is deliberately separate from the three reviewed
``rules/*.yaml`` curation files and from the hashed deterministic core: changing a
setting never mutates ``extracted-facts.json`` / a golden, it only records a preference
or reconfigures the opt-in LLM connection.

Two persisted stores, mirroring the existing :mod:`anon.llm_provider` precedent:

* **general settings** (``ui`` + ``anon`` categories) live in a gitignored
  ``settings.local.yaml`` next to ``pyproject.toml`` (override with ``ANON_SETTINGS``).
  These are per-install preferences that apply across workspaces.
* **LLM settings** keep their existing home — the gitignored ``llm.local.yaml`` that
  :func:`anon.llm_provider.load_llm_config` already reads (override with
  ``ANON_LLM_CONFIG``). The Settings tab is just a GUI editor for that file, so a
  saved endpoint/deployment/key takes effect on the next ``--llm-propose`` run with no
  extra wiring. The API key is **never** returned to the client (masked to a boolean);
  ``ANON_LLM_*`` environment variables still override the file (and the GUI warns
  when one does), exactly as the resolution order in ``llm.example.yaml`` documents.

The whole module is a pure function of its inputs (every path is injectable) so it is
unit-tested without a server.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from . import SCHEMA_VERSION, __version__
from .config import load_yaml
from .llm_provider import (DEFAULT_API_VERSION, DEFAULT_CLIENT_KIND, _ENV,
                           _config_path, _read_dotenv, _anon_repo_root,
                           load_llm_config)


class SettingsError(ValueError):
    """A rejected settings write (bad category / unknown field / invalid value).

    Carries a stable ``code`` so the serve layer can map it onto the one §7.8 error
    shape without re-classifying the message text."""

    def __init__(self, message: str, code: str = "bad_settings"):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- the spec
# The spec is the single contract that drives BOTH validation here and generic form
# rendering in the GUI (one source of truth — the client never hardcodes field lists).
# Each field: key, label, type, default, help; plus `enum`/`min`/`max`/`step`/`secret`/
# `optional`/`placeholder` where the type needs them. `secret` fields are never echoed.

SETTINGS_SPEC: list[dict[str, Any]] = [
    {
        "key": "ui",
        "label": "User Interface",
        "help": "How the Anon GUI looks and behaves. Stored per install in "
                "settings.local.yaml and applied across every workspace you open.",
        "persisted": True,
        "fields": [
            {"key": "density", "label": "Density", "type": "enum",
             "enum": ["comfortable", "compact"], "default": "comfortable",
             "help": "`compact` tightens padding so more of a large model fits on screen."},
            {"key": "font_scale", "label": "Text size", "type": "number",
             "min": 0.8, "max": 1.4, "step": 0.05, "default": 1.0,
             "help": "Multiplier on the base font size (accessibility / projector use)."},
            {"key": "reduce_motion", "label": "Reduce motion", "type": "bool",
             "default": False,
             "help": "Disable transitions/animations (motion sensitivity, or remote desktops)."},
            {"key": "default_landing", "label": "Default tab", "type": "enum",
             "enum": ["overview", "adr", "curate", "inspect"], "default": "overview",
             "help": "Which screen opens first when you load the GUI with no deep link."},
        ],
    },
    {
        "key": "anon",
        "label": "Anon Engine",
        "help": "Defaults for pipeline runs you start from the GUI (the first-run "
                "wizard and the Run control). These seed the run you launch; they do "
                "not retroactively alter an existing model — re-run to apply.",
        "persisted": True,
        "fields": [
            {"key": "default_llm_mode", "label": "Default enrichment mode", "type": "enum",
             "enum": ["no-llm", "llm-propose", "llm-accepted"], "default": "no-llm",
             "help": "Seeds the mode of a GUI-initiated run. `no-llm` is the "
                     "deterministic, no-network baseline (recommended)."},
            {"key": "default_incremental", "label": "Incremental by default", "type": "bool",
             "default": False,
             "help": "Reuse cached extractor fragments where inputs are unchanged."},
            {"key": "default_strict", "label": "Strict by default", "type": "bool",
             "default": False,
             "help": "Treat advisory drift/coverage findings as failures in a GUI run."},
            {"key": "default_resolve_deployment", "label": "Resolve deployment by default",
             "type": "bool", "default": False,
             "help": "Attempt to render the deployment topology from manifests when present."},
            {"key": "adr_max_candidates", "label": "Max ADR candidates", "type": "number",
             "min": 0, "max": 100, "step": 1, "default": 0,
             "help": "Cap the ADR-candidate inbox to at most this many rows (0 = no cap; use "
                     "the per-category budget). Applies to mined and deterministic candidates "
                     "alike — the overflow is hidden until you raise the cap, never dismissed."},
        ],
    },
    {
        "key": "llm",
        "label": "LLM Enrichment",
        "help": "Azure AI Foundry connection for the opt-in §8 enrichment pass "
                "(names, descriptions, narratives, scenario/ADR proposals). Stored in "
                "the gitignored llm.local.yaml. ANON_LLM_* environment variables "
                "override these values at run time.",
        "persisted": True,
        "fields": [
            {"key": "endpoint", "label": "API endpoint", "type": "string", "default": "",
             "placeholder": "https://YOUR-RESOURCE.openai.azure.com",
             "help": "Azure OpenAI / Foundry endpoint. A `.../openai/v1` URL uses the "
                     "versionless OpenAI-compatible surface automatically."},
            {"key": "deployment", "label": "Deployment", "type": "string", "default": "",
             "placeholder": "gpt-5.4-mini",
             "help": "The deployment name in your Azure resource (not the base model). "
                     "Enrichment is a light labelling task — a cheap/fast model is enough."},
            {"key": "model_id", "label": "Model id (cache key)", "type": "string",
             "default": "", "placeholder": "defaults to the deployment name",
             "help": "Logical model id recorded in the enrichment cache key. Changing it "
                     "is a reviewable recompute. Defaults to the deployment if blank."},
            {"key": "client_kind", "label": "Client", "type": "enum",
             "enum": ["azure-openai", "azure-inference", "azure-anthropic"],
             "default": DEFAULT_CLIENT_KIND,
             "help": "How Anon talks to the model. `azure-openai` (OpenAI SDK, "
                     "GPT-class) · `azure-inference` (azure-ai-inference, Foundry model "
                     "catalog) · `azure-anthropic` (native Anthropic Messages API for Claude "
                     "on Azure AI Foundry — set the endpoint to the resource's `.../anthropic` "
                     "URL and the deployment to the Claude deployment name)."},
            {"key": "api_version", "label": "API version", "type": "string",
             "default": DEFAULT_API_VERSION, "placeholder": DEFAULT_API_VERSION,
             "help": "Azure OpenAI REST api-version (azure-openai client only; ignored "
                     "for the versionless v1 endpoint)."},
            {"key": "use_aad", "label": "Use Azure AD / workload identity", "type": "bool",
             "default": False,
             "help": "Authenticate via DefaultAzureCredential instead of an API key "
                     "(no key needed)."},
            {"key": "temperature", "label": "Temperature", "type": "number",
             "min": 0.0, "max": 2.0, "step": 0.1, "default": None, "optional": True,
             "help": "Sampling temperature. Leave blank to use the service default "
                     "(reasoning-tier models reject a non-default temperature)."},
            {"key": "api_key", "label": "API key", "type": "secret", "default": "",
             "help": "Stored in the gitignored llm.local.yaml. Leave blank to keep the "
                     "current key. Prefer the ANON_LLM_API_KEY environment variable "
                     "on shared machines."},
        ],
    },
    {
        "key": "environment",
        "label": "Environment",
        "help": "Read-only diagnostics for this server, workspace, and LLM resolution.",
        "persisted": False,
        "readonly": True,
        "fields": [
            {"key": "server_version", "label": "Server version", "type": "string"},
            {"key": "schema_version", "label": "Schema version", "type": "string"},
            {"key": "api_version", "label": "API version", "type": "string"},
            {"key": "read_only", "label": "Read-only session", "type": "bool"},
            {"key": "repo", "label": "Repo", "type": "string"},
            {"key": "arch_dir", "label": "Arch dir", "type": "string"},
            {"key": "rules_dir", "label": "Rules dir", "type": "string"},
            {"key": "settings_path", "label": "Settings file", "type": "string"},
            {"key": "llm_config_path", "label": "LLM config file", "type": "string"},
            {"key": "llm_usable", "label": "LLM ready", "type": "bool"},
            {"key": "llm_usable_reason", "label": "LLM status", "type": "string"},
        ],
    },
]

_SPEC_BY_KEY = {c["key"]: c for c in SETTINGS_SPEC}
_GENERAL_CATEGORIES = ("ui", "anon")
# llm fields that map straight into llm.local.yaml (everything except the secret api_key).
_LLM_PLAIN = ("endpoint", "deployment", "model_id", "client_kind", "api_version",
              "use_aad", "temperature")


def _fields(category: str) -> dict[str, dict[str, Any]]:
    cat = _SPEC_BY_KEY.get(category)
    if cat is None:
        raise SettingsError(f"unknown settings category '{category}'", code="unknown_category")
    return {f["key"]: f for f in cat["fields"]}


def _defaults(category: str) -> dict[str, Any]:
    return {f["key"]: f.get("default") for f in _SPEC_BY_KEY[category]["fields"]
            if "default" in f}


# --------------------------------------------------------------------------- coercion

def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce(field: dict[str, Any], value: Any) -> Any:
    """Validate + coerce *value* for *field*; raise :class:`SettingsError` on a bad value."""
    key, ftype = field["key"], field["type"]
    if ftype in ("string", "text", "secret"):
        if value is None:
            return ""
        if not isinstance(value, str):
            raise SettingsError(f"'{key}' must be text", code="bad_value")
        return value
    if ftype == "bool":
        return _as_bool(value)
    if ftype == "enum":
        sval = str(value)
        if sval not in field["enum"]:
            raise SettingsError(
                f"'{key}' must be one of {', '.join(field['enum'])}", code="bad_value")
        return sval
    if ftype == "number":
        if value in (None, "") and field.get("optional"):
            return None
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"'{key}' must be a number", code="bad_value")
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and num < lo:
            raise SettingsError(f"'{key}' must be >= {lo}", code="bad_value")
        if hi is not None and num > hi:
            raise SettingsError(f"'{key}' must be <= {hi}", code="bad_value")
        # keep whole-number steps integral so the YAML stays clean (3, not 3.0)
        if field.get("step") and float(field["step"]).is_integer() and num.is_integer():
            return int(num)
        return num
    raise SettingsError(f"'{key}' has unsupported type '{ftype}'", code="bad_value")  # pragma: no cover


# --------------------------------------------------------------------------- yaml io

def _dump_yaml(data: dict[str, Any], path: Path, header: str) -> None:
    """Write *data* as YAML with a comment *header*, LF newlines (cross-OS stable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=True, allow_unicode=True, default_flow_style=False)
    text = header.rstrip("\n") + "\n\n" + (body if data else "")
    with open(path, "w", encoding="utf-8", newline="") as fh:  # LF always (§22.3)
        fh.write(text)


def _read_yaml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    loaded = load_yaml(path) or {}
    return loaded if isinstance(loaded, dict) else {}


# --------------------------------------------------------------------------- locations

def general_path(override: str | Path | None = None) -> Path | None:
    """Where ``ui``/``anon`` settings persist: ``ANON_SETTINGS`` env override,
    else ``<anon-repo-root>/settings.local.yaml``. ``None`` when neither resolves
    (e.g. installed as a wheel with no env override) — the caller then serves defaults
    and refuses writes with an actionable error."""
    if override is not None:
        return Path(override)
    env = os.environ.get("ANON_SETTINGS")
    if env:
        return Path(env)
    root = _anon_repo_root()
    return (root / "settings.local.yaml") if root else None


def llm_path(override: str | Path | None = None) -> Path | None:
    """Where LLM settings persist — the same file :func:`load_llm_config` reads
    (``ANON_LLM_CONFIG`` env override, else ``<repo-root>/llm.local.yaml``)."""
    if override is not None:
        return Path(override)
    return _config_path()


# --------------------------------------------------------------------------- general

def load_general(path: Path | None) -> dict[str, dict[str, Any]]:
    """The ``ui``/``anon`` values, each merged over its spec defaults. Unknown/legacy
    keys in the file are ignored (forward/backward compatible)."""
    data = _read_yaml(path)
    out: dict[str, dict[str, Any]] = {}
    for cat in _GENERAL_CATEGORIES:
        stored = data.get(cat) if isinstance(data.get(cat), dict) else {}
        merged = _defaults(cat)
        fields = _fields(cat)
        for k, v in (stored or {}).items():
            if k in fields:
                try:
                    merged[k] = _coerce(fields[k], v)
                except SettingsError:
                    pass  # a corrupt persisted value falls back to the default, never crashes
        out[cat] = merged
    return out


def save_general(path: Path, category: str, values: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist *values* into *category* (``ui``/``anon``); returns the
    category merged with defaults. Partial updates are merged into the existing file."""
    if category not in _GENERAL_CATEGORIES:
        raise SettingsError(
            f"'{category}' is not a writable general category", code="unknown_category")
    fields = _fields(category)
    validated: dict[str, Any] = {}
    for k, v in (values or {}).items():
        if k not in fields:
            raise SettingsError(f"unknown '{category}' setting '{k}'", code="unknown_field")
        validated[k] = _coerce(fields[k], v)
    data = _read_yaml(path)
    cat_now = data.get(category) if isinstance(data.get(category), dict) else {}
    data[category] = {**cat_now, **validated}
    _dump_yaml(data, path,
               "# Anon GUI settings (gitignored). Managed by the Settings tab —\n"
               "# ANON_SETTINGS overrides this location. See anon.settings.")
    return load_general(path)[category]


# --------------------------------------------------------------------------- llm

def _llm_env_overrides() -> list[str]:
    """Which LLM fields are currently forced by the environment (process env or .env),
    so the GUI can warn that a saved file value will not take effect."""
    dotenv = _read_dotenv()
    out: list[str] = []
    for field, var in _ENV.items():
        if os.environ.get(var) or dotenv.get(var):
            out.append("api_key" if field == "api_key" else field)
    return out


def current_llm(path: Path | None) -> dict[str, Any]:
    """The LLM settings the GUI shows — the persisted file values (the api key MASKED to
    a boolean), plus env-override warnings and the live ``usable`` verdict from the fully
    resolved config (no network call)."""
    raw = _read_yaml(path)
    cfg = load_llm_config()
    usable, reason = cfg.usable() if cfg else (False, "no LLM configuration yet")
    overrides = _llm_env_overrides()
    dotenv = _read_dotenv()
    key_set = bool(raw.get("api_key")) or bool(
        os.environ.get(_ENV["api_key"]) or dotenv.get(_ENV["api_key"]))
    return {
        "endpoint": str(raw.get("endpoint", "") or ""),
        "deployment": str(raw.get("deployment", "") or ""),
        "model_id": str(raw.get("model_id", "") or ""),
        "client_kind": str(raw.get("client_kind") or DEFAULT_CLIENT_KIND),
        "api_version": str(raw.get("api_version") or DEFAULT_API_VERSION),
        "use_aad": _as_bool(raw.get("use_aad", False)),
        "temperature": raw.get("temperature"),
        # secret: never echoed — only whether one is configured.
        "api_key": "",
        "api_key_set": key_set,
        "env_overrides": overrides,
        "usable": usable,
        "usable_reason": reason,
    }


def save_llm(path: Path, values: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist LLM settings into llm.local.yaml, merging over what is there.

    The secret api key is handled specially: a non-empty ``api_key`` sets it, a truthy
    ``clear_api_key`` removes it, and a blank/absent ``api_key`` leaves it untouched —
    so the masked round-trip from the GUI never wipes a configured key."""
    fields = _fields("llm")
    data = _read_yaml(path)
    for k, v in (values or {}).items():
        if k in ("api_key", "clear_api_key"):
            continue  # secret handled below
        if k not in fields:
            raise SettingsError(f"unknown 'llm' setting '{k}'", code="unknown_field")
        coerced = _coerce(fields[k], v)
        # blank optional/string fields are removed so the file stays clean and the loader's
        # own defaults apply (rather than persisting "").
        if coerced in (None, "") and k in _LLM_PLAIN:
            data.pop(k, None)
        else:
            data[k] = coerced
    if values.get("clear_api_key"):
        data.pop("api_key", None)
    elif values.get("api_key"):
        data["api_key"] = str(values["api_key"])
    _dump_yaml(data, path,
               "# Anon LLM enrichment settings (gitignored). Managed by the Settings\n"
               "# tab; ANON_LLM_* env vars override these. See llm.example.yaml.")
    return current_llm(path)


# --------------------------------------------------------------------------- environment

def environment_info(ws, *, read_only: bool, gp: Path | None, lp: Path | None) -> dict[str, Any]:
    """Read-only diagnostics for the Environment category (no persistence, no network)."""
    cfg = load_llm_config()
    usable, reason = cfg.usable() if cfg else (False, "no LLM configuration yet")
    return {
        "server_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "api_version": "v1",
        "read_only": read_only,
        "repo": str(ws.repo),
        "arch_dir": str(ws.arch_dir),
        "rules_dir": str(ws.rules_dir),
        "settings_path": str(gp) if gp else "(unresolved — set ANON_SETTINGS)",
        "llm_config_path": str(lp) if lp else "(unresolved — set ANON_LLM_CONFIG)",
        "llm_usable": usable,
        "llm_usable_reason": reason,
    }


# --------------------------------------------------------------------------- aggregate

def get_all(ws, *, read_only: bool = False,
            general_override: str | Path | None = None,
            llm_override: str | Path | None = None) -> dict[str, Any]:
    """The full Settings payload the GUI renders: the spec + every category's values."""
    gp = general_path(general_override)
    lp = llm_path(llm_override)
    general = load_general(gp)
    return {
        "spec": SETTINGS_SPEC,
        "values": {
            "ui": general["ui"],
            "anon": general["anon"],
            "llm": current_llm(lp),
            "environment": environment_info(ws, read_only=read_only, gp=gp, lp=lp),
        },
        "paths": {"general": str(gp) if gp else None,
                  "llm": str(lp) if lp else None},
    }


def put_category(ws, category: str, values: dict[str, Any], *,
                 read_only: bool = False,
                 general_override: str | Path | None = None,
                 llm_override: str | Path | None = None) -> dict[str, Any]:
    """Persist one category's *values*, then return the refreshed full payload."""
    if category in _GENERAL_CATEGORIES:
        gp = general_path(general_override)
        if gp is None:
            raise SettingsError(
                "cannot locate a settings file (installed without a repo root); set "
                "ANON_SETTINGS to a writable path", code="no_location")
        save_general(gp, category, values)
    elif category == "llm":
        lp = llm_path(llm_override)
        if lp is None:
            raise SettingsError(
                "cannot locate llm.local.yaml; set ANON_LLM_CONFIG to a writable path",
                code="no_location")
        save_llm(lp, values)
    elif category == "environment":
        raise SettingsError("the environment category is read-only", code="read_only_category")
    else:
        raise SettingsError(f"unknown settings category '{category}'", code="unknown_category")
    return get_all(ws, read_only=read_only,
                   general_override=general_override, llm_override=llm_override)
