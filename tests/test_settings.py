"""Unit tests for :mod:`anon.settings` — the GUI Settings-tab tool config.

These are pure (no server): the spec contract, validation/coercion, the general
(ui/anon) round-trip into settings.local.yaml, and the LLM round-trip into
llm.local.yaml with the api key MASKED (write-only — never echoed) and the
ANON_LLM_* env-override warning.
"""
from __future__ import annotations

import pytest

from anon import settings


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Pin LLM resolution at a tmp file and clear every ANON_LLM_* override so
    `current_llm`/`load_llm_config` never read the developer's real .env / llm.local.yaml."""
    monkeypatch.setenv("ANON_LLM_CONFIG", str(tmp_path / "llm.local.yaml"))
    monkeypatch.setenv("ANON_DOTENV", str(tmp_path / "nope.env"))
    monkeypatch.setenv("ANON_SETTINGS", str(tmp_path / "settings.local.yaml"))
    for var in settings._ENV.values():
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# --------------------------------------------------------------------------- spec

def test_spec_has_the_required_categories():
    keys = [c["key"] for c in settings.SETTINGS_SPEC]
    for required in ("ui", "anon", "llm"):
        assert required in keys
    # the LLM category exposes the endpoint / model / key the user must configure
    llm = {f["key"] for c in settings.SETTINGS_SPEC if c["key"] == "llm"
           for f in c["fields"]}
    assert {"endpoint", "deployment", "model_id", "api_key"} <= llm


def test_every_field_is_well_formed():
    for cat in settings.SETTINGS_SPEC:
        for f in cat["fields"]:
            assert f["key"] and f["label"] and f["type"]
            if f["type"] == "enum":
                assert f.get("enum"), f"{cat['key']}.{f['key']} enum needs choices"


# --------------------------------------------------------------------------- general

def test_general_defaults_when_no_file():
    g = settings.load_general(None)
    assert g["ui"]["density"] == "comfortable"
    assert g["anon"]["default_llm_mode"] == "no-llm"
    # the ADR-inbox cap (#3) defaults to 0 = no cap
    assert g["anon"]["adr_max_candidates"] == 0


def test_adr_max_candidates_coerces_and_validates(tmp_path):
    """The ADR-inbox cap is a whole-number field stored integral; out-of-range is rejected."""
    p = tmp_path / "settings.local.yaml"
    settings.save_general(p, "anon", {"adr_max_candidates": "8"})
    saved = settings.load_general(p)["anon"]["adr_max_candidates"]
    assert saved == 8 and isinstance(saved, int)
    with pytest.raises(settings.SettingsError):
        settings.save_general(p, "anon", {"adr_max_candidates": 500})  # over max


def test_general_save_roundtrip_and_partial_merge(tmp_path):
    p = tmp_path / "settings.local.yaml"
    settings.save_general(p, "ui", {"density": "compact", "font_scale": "1.1"})
    reread = settings.load_general(p)["ui"]
    assert reread["density"] == "compact"
    assert reread["font_scale"] == 1.1
    # a second partial write must not clobber the first key
    settings.save_general(p, "ui", {"reduce_motion": True})
    reread = settings.load_general(p)["ui"]
    assert reread["density"] == "compact" and reread["reduce_motion"] is True
    # categories stay independent in one file
    settings.save_general(p, "anon", {"default_strict": True})
    both = settings.load_general(p)
    assert both["ui"]["density"] == "compact"
    assert both["anon"]["default_strict"] is True


def test_general_rejects_bad_values(tmp_path):
    p = tmp_path / "settings.local.yaml"
    with pytest.raises(settings.SettingsError):
        settings.save_general(p, "ui", {"density": "spacious"})  # bad enum
    with pytest.raises(settings.SettingsError):
        settings.save_general(p, "ui", {"font_scale": 9})        # out of range
    with pytest.raises(settings.SettingsError):
        settings.save_general(p, "ui", {"bogus": 1})             # unknown field
    with pytest.raises(settings.SettingsError):
        settings.save_general(p, "environment", {"x": 1})        # not writable


def test_corrupt_persisted_value_falls_back_to_default(tmp_path):
    p = tmp_path / "settings.local.yaml"
    p.write_text("ui:\n  density: nonsense\n  font_scale: 1.2\n", encoding="utf-8")
    ui = settings.load_general(p)["ui"]
    assert ui["density"] == "comfortable"   # corrupt value ignored, default applied
    assert ui["font_scale"] == 1.2          # the valid one survives


def test_number_step_keeps_whole_numbers_integral(tmp_path):
    # min_relationship_weight isn't a field, but font_scale (step 0.05) stays float and
    # a whole-step field would stay int — exercise the float branch here.
    p = tmp_path / "settings.local.yaml"
    settings.save_general(p, "ui", {"font_scale": 1.0})
    assert settings.load_general(p)["ui"]["font_scale"] in (1, 1.0)


# --------------------------------------------------------------------------- llm

def test_llm_save_masks_key_and_persists(tmp_path):
    p = tmp_path / "llm.local.yaml"
    masked = settings.save_llm(p, {
        "endpoint": "https://x.openai.azure.com",
        "deployment": "gpt-5.4-mini",
        "api_key": "SECRET-XYZ",
    })
    # the secret is never echoed back to the caller
    assert masked["api_key"] == "" and masked["api_key_set"] is True
    assert "SECRET-XYZ" not in str(masked)
    # ...but it IS persisted to the gitignored file the enrichment stage reads
    assert "SECRET-XYZ" in p.read_text(encoding="utf-8")
    assert masked["endpoint"] == "https://x.openai.azure.com"


def test_llm_blank_key_keeps_existing_clear_removes(tmp_path):
    p = tmp_path / "llm.local.yaml"
    settings.save_llm(p, {"endpoint": "https://x", "api_key": "KEEP-ME"})
    # a blank api_key on a later save leaves the stored key untouched
    settings.save_llm(p, {"deployment": "gpt-5-mini", "api_key": ""})
    assert "KEEP-ME" in p.read_text(encoding="utf-8")
    # an explicit clear removes it
    settings.save_llm(p, {"clear_api_key": True})
    assert "KEEP-ME" not in p.read_text(encoding="utf-8")
    assert settings.current_llm(p)["api_key_set"] is False


def test_llm_blank_plain_field_is_removed(tmp_path):
    p = tmp_path / "llm.local.yaml"
    settings.save_llm(p, {"endpoint": "https://x", "temperature": "0.5"})
    assert settings.current_llm(p)["temperature"] == 0.5
    settings.save_llm(p, {"temperature": ""})
    assert settings.current_llm(p)["temperature"] in (None, "")


def test_llm_env_override_is_reported(tmp_path, monkeypatch):
    p = tmp_path / "llm.local.yaml"
    settings.save_llm(p, {"endpoint": "https://from-file"})
    monkeypatch.setenv("ANON_LLM_ENDPOINT", "https://from-env")
    cur = settings.current_llm(p)
    assert "endpoint" in cur["env_overrides"]


def test_llm_rejects_unknown_and_bad_value(tmp_path):
    p = tmp_path / "llm.local.yaml"
    with pytest.raises(settings.SettingsError):
        settings.save_llm(p, {"client_kind": "made-up"})   # bad enum
    with pytest.raises(settings.SettingsError):
        settings.save_llm(p, {"nonsense": 1})              # unknown field


# --------------------------------------------------------------------------- locations / aggregate

def test_general_and_llm_path_respect_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ANON_SETTINGS", str(tmp_path / "s.yaml"))
    monkeypatch.setenv("ANON_LLM_CONFIG", str(tmp_path / "l.yaml"))
    assert settings.general_path() == tmp_path / "s.yaml"
    assert settings.llm_path() == tmp_path / "l.yaml"
    # explicit override beats env
    assert settings.general_path(tmp_path / "x.yaml") == tmp_path / "x.yaml"


def test_get_all_and_put_category(tmp_path, monkeypatch):
    from anon.paths import resolve_workspace
    ws = resolve_workspace(tmp_path)
    monkeypatch.setenv("ANON_SETTINGS", str(tmp_path / "settings.local.yaml"))
    monkeypatch.setenv("ANON_LLM_CONFIG", str(tmp_path / "llm.local.yaml"))
    payload = settings.get_all(ws, read_only=False)
    assert [c["key"] for c in payload["spec"]] == ["ui", "anon", "llm", "environment"]
    assert payload["values"]["environment"]["read_only"] is False
    # write through put_category, the refreshed payload reflects it
    out = settings.put_category(ws, "ui", {"density": "compact"})
    assert out["values"]["ui"]["density"] == "compact"
    # environment is read-only
    with pytest.raises(settings.SettingsError) as ei:
        settings.put_category(ws, "environment", {})
    assert ei.value.code == "read_only_category"
