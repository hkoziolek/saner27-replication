"""T1.2 — fail-closed LLM egress + governance tests (plan §2 / ADR-0001).

Covers the bundled default redaction policy, the over-scrub guard (secret SHAPES scrubbed,
legitimate architectural NAMES preserved), the fail-closed `egress_gate`, the deterministic
`egress-preview` backend (no provider call), the field-hash egress manifest (raw secret never
appears; its sha256 does), and the doctor LLM preflight. No real network call is ever made.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from anon import doctor
from anon.jsonio import dump_json, dumps_json, load_json
from anon.model import canonicalize
from anon.paths import resolve_workspace
from anon.stages import enrich_llm

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY = REPO_ROOT / "tests" / "fixtures" / "toy-repo"

# Planted secrets / codenames the default policy must scrub.
PLANTED_API_KEY = "sk-abcdEFGH0123456789ijklMNOPqrstu"
PLANTED_CONN = "Server=db01;Database=catalog;User ID=sa;Password=Sup3rSecretPw;"
PLANTED_EMAIL = "ops-oncall@internal.example.com"
# Legit architectural names that must survive scrubbing unchanged (over-scrub guard).
LEGIT_NAMES = ["OrderService", "Catalog.API", "NativeMath", "EventBusRabbitMQ"]


def _curated_with_secrets() -> dict:
    return canonicalize({
        "schema_version": "1.0",
        "provenance": {"repo": "r", "commit": "c", "generated_at": "2026-01-01T00:00:00Z",
                       "extractors": []},
        "targets": [
            {"id": "csharp:csproj:src/OrderService/OrderService.csproj",
             "name": "OrderService", "type": "csproj", "language": "csharp",
             "technology": ".NET assembly", "container_name": "Order Service",
             "responsibilities": [f"Connects via {PLANTED_CONN} contact {PLANTED_EMAIL}"]},
            {"id": "csharp:csproj:src/Catalog.API/Catalog.API.csproj",
             "name": "Catalog.API", "type": "csproj", "language": "csharp",
             "technology": ".NET assembly", "container_name": "Catalog API",
             "responsibilities": [f"Auth token {PLANTED_API_KEY} used internally"]},
            {"id": "cpp:target:NativeMath", "name": "NativeMath", "type": "cpp-target",
             "language": "cpp", "technology": "static library",
             "container_name": "Native Math"},
        ],
        "relationships": [],
    })


def _ws(tmp_path: Path, sub: str = "a", *, curated: dict | None = None,
        redaction_yaml: str | None = None):
    """A workspace whose curated facts are seeded; optionally seed a local redaction policy."""
    ws = resolve_workspace(TOY, arch_dir=tmp_path / sub / "arch", rules_dir=tmp_path / sub / "rules")
    dump_json(curated if curated is not None else _curated_with_secrets(), ws.curated_facts)
    if redaction_yaml is not None:
        ws.payload_redaction.parent.mkdir(parents=True, exist_ok=True)
        ws.payload_redaction.write_text(redaction_yaml, encoding="utf-8", newline="")
    return ws


# --- bundled default policy loads ----------------------------------------------

def test_default_redaction_file_is_bundled_and_loadable():
    path = enrich_llm.find_default_redaction()
    assert path.name == "default-payload-redaction.yaml"
    assert path.exists()


def test_effective_redaction_falls_back_to_default(tmp_path):
    ws = _ws(tmp_path, "a")  # no local payload-redaction.yaml
    cfg, source = enrich_llm.effective_redaction(ws)
    assert source == "default"
    assert cfg["secret_patterns"]              # non-empty
    assert cfg["max_snippet_chars"] == 280


def test_effective_redaction_prefers_user_policy(tmp_path):
    ws = _ws(tmp_path, "a", redaction_yaml="deny_list:\n  - AcmeCorp\nmax_snippet_chars: 100\n")
    cfg, source = enrich_llm.effective_redaction(ws)
    assert source == "user"
    assert cfg["deny_list"] == ["AcmeCorp"]
    assert cfg["max_snippet_chars"] == 100


# --- redaction_is_inert --------------------------------------------------------

def test_redaction_is_inert_true_for_empty():
    assert enrich_llm.redaction_is_inert({"secret_patterns": [], "deny_list": []}) is True


def test_redaction_is_inert_false_for_default():
    cfg, _ = enrich_llm.effective_redaction(
        resolve_workspace(TOY, arch_dir=TOY / "nope" / "arch", rules_dir=TOY / "nope" / "rules"))
    assert enrich_llm.redaction_is_inert(cfg) is False


# --- over-scrub guard: secret SHAPES scrubbed, legit NAMES preserved -----------

def test_default_policy_scrubs_planted_secrets_via_element_payload(tmp_path):
    ws = _ws(tmp_path, "a")
    cfg, _ = enrich_llm.effective_redaction(ws)
    curated = load_json(ws.curated_facts)
    blob = dumps_json(enrich_llm.build_egress_payloads(curated, cfg))
    # planted secrets are gone
    assert PLANTED_API_KEY not in blob
    assert "Sup3rSecretPw" not in blob
    assert PLANTED_EMAIL not in blob
    assert "[REDACTED]" in blob
    # legit architectural names present in the fixture survive unchanged (over-scrub guard)
    for name in ["OrderService", "Catalog.API", "NativeMath"]:
        assert name in blob, f"over-scrub: legitimate name {name!r} was redacted"


def test_default_policy_preserves_legit_names_directly():
    cfg, _ = enrich_llm.effective_redaction(
        resolve_workspace(TOY, arch_dir=TOY / "nope" / "arch", rules_dir=TOY / "nope" / "rules"))
    for name in LEGIT_NAMES:
        assert enrich_llm._scrub_text(name, cfg) == name


# --- ADR miner over-scrub guard (ADR-mining §5.3 — same payload as narratives) ----
# The §5 miner sends the SAME narrative CONTAINER payload, so it inherits zero new egress
# categories: secret SHAPES must scrub, architectural NAMES must survive. We assert over the
# container payloads `arch egress-preview` renders (the band-for-band-identical bytes the
# miner sends), mirroring the element-payload over-scrub assertions above.

def test_miner_container_payload_scrubs_secrets_preserves_names(tmp_path):
    ws = _ws(tmp_path, "a")
    cfg, _ = enrich_llm.effective_redaction(ws)
    curated = load_json(ws.curated_facts)
    blob = dumps_json(enrich_llm.build_container_egress_payloads(curated, cfg))
    # planted secrets are gone from the miner's payload
    assert PLANTED_API_KEY not in blob
    assert "Sup3rSecretPw" not in blob
    assert PLANTED_EMAIL not in blob
    assert "[REDACTED]" in blob
    # legitimate architectural names survive (over-scrub guard)
    for name in ["OrderService", "Catalog.API", "NativeMath"]:
        assert name in blob, f"over-scrub: legitimate name {name!r} was redacted from the miner payload"
    # the miner cites from `citable_ids` — every container payload advertises them
    for pl in enrich_llm.build_container_egress_payloads(curated, cfg):
        assert pl.get("citable_ids"), "the miner payload carries the resolvable citable ids"


def test_miner_payload_matches_egress_preview_container_bytes(tmp_path):
    """The egress preview's `container_payloads` are exactly the bytes the miner sends — so
    'what leaves' is auditable before any provider call (§5.3 band-for-band identical)."""
    ws = _ws(tmp_path, "a")
    cfg, _ = enrich_llm.effective_redaction(ws)
    curated = load_json(ws.curated_facts)
    sent = enrich_llm.build_container_egress_payloads(
        curated, cfg, enrich_llm.NarrativeInputs(ws, curated,
                                                 *enrich_llm.narrative_context(curated)[:2]))
    preview = load_json(enrich_llm.write_egress_preview(ws))
    assert dumps_json(preview["container_payloads"]) == dumps_json(sent)


# --- egress_gate fail-closed semantics -----------------------------------------

def test_egress_gate_no_llm_always_ok(tmp_path):
    ws = _ws(tmp_path, "a")
    ok, _ = enrich_llm.egress_gate(ws, "no-llm", accept_unredacted=False)
    assert ok is True


def test_egress_gate_default_policy_proceeds(tmp_path):
    ws = _ws(tmp_path, "a")  # no user policy -> default (non-inert)
    ok, msg = enrich_llm.egress_gate(ws, "llm-propose", accept_unredacted=False)
    assert ok is True
    assert "default" in msg


def test_egress_gate_refuses_when_inert(tmp_path):
    # an explicitly-emptied local policy is INERT and present -> source "user", inert -> refuse
    ws = _ws(tmp_path, "a", redaction_yaml="secret_patterns: []\ndeny_list: []\n")
    ok, msg = enrich_llm.egress_gate(ws, "llm-propose", accept_unredacted=False)
    assert ok is False
    assert "--i-accept-unredacted-egress" in msg


def test_egress_gate_inert_proceeds_with_override(tmp_path):
    ws = _ws(tmp_path, "a", redaction_yaml="secret_patterns: []\ndeny_list: []\n")
    ok, msg = enrich_llm.egress_gate(ws, "llm-propose", accept_unredacted=True)
    assert ok is True


# --- write_egress_preview: deterministic, post-redaction, no provider call ------

def test_write_egress_preview_is_deterministic_and_redacted(tmp_path):
    ws_a = _ws(tmp_path, "a")
    ws_b = _ws(tmp_path, "b")
    p_a = enrich_llm.write_egress_preview(ws_a)
    p_b = enrich_llm.write_egress_preview(ws_b)
    assert p_a.read_bytes() == p_b.read_bytes()        # byte-identical across runs
    preview = load_json(p_a)
    assert preview["policy_source"] == "default"
    blob = p_a.read_text(encoding="utf-8")
    assert PLANTED_API_KEY not in blob and "Sup3rSecretPw" not in blob and PLANTED_EMAIL not in blob
    # one payload per curated target, sorted by element_id
    ids = [pl["element_id"] for pl in preview["payloads"]]
    assert ids == sorted(ids)
    assert len(ids) == 3


def test_write_egress_preview_makes_no_provider_call(tmp_path, monkeypatch):
    # NullProvider raises if its complete() is ever called; the preview must never reach it.
    ws = _ws(tmp_path, "a")

    def _boom(*a, **k):  # pragma: no cover - asserts it is NOT called
        raise AssertionError("egress preview must not call a provider")

    monkeypatch.setattr(enrich_llm.NullProvider, "complete", _boom, raising=True)
    enrich_llm.write_egress_preview(ws)  # must not raise


# --- write_egress_manifest: field hashes, never raw values ----------------------

def test_write_egress_manifest_records_hashes_not_raw(tmp_path):
    ws = _ws(tmp_path, "a")
    path = enrich_llm.write_egress_manifest(
        ws, provider_desc="azure-openai:gpt-5.5", model_id="gpt-5.5", budget=4096)
    raw_bytes = path.read_bytes()
    # the raw planted secret value never appears in the manifest bytes
    assert PLANTED_API_KEY.encode() not in raw_bytes
    assert b"Sup3rSecretPw" not in raw_bytes
    assert PLANTED_EMAIL.encode() not in raw_bytes
    manifest = load_json(path)
    body = manifest["body"]
    assert body["provider"] == "azure-openai:gpt-5.5"
    assert body["model_id"] == "gpt-5.5"
    assert body["token_budget"] == 4096
    assert body["policy_source"] == "default"
    # the snippet field of Catalog.API holds the POST-redaction value; its sha256 is recorded.
    cfg, _ = enrich_llm.effective_redaction(ws)
    curated = load_json(ws.curated_facts)
    payloads = {p["element_id"]: p for p in enrich_llm.build_egress_payloads(curated, cfg)}
    cat = payloads["csharp:csproj:src/Catalog.API/Catalog.API.csproj"]
    entry = next(e for e in body["elements"]
                 if e["element_id"] == "csharp:csproj:src/Catalog.API/Catalog.API.csproj")
    expected = hashlib.sha256(dumps_json(cat["snippet"]).encode()).hexdigest()
    assert entry["field_hashes"]["snippet"] == expected
    assert "snippet" in entry["fields_sent"]


def test_write_egress_manifest_unsigned_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv(enrich_llm.EGRESS_SIGNING_KEY_ENV, raising=False)
    ws = _ws(tmp_path, "a")
    manifest = load_json(enrich_llm.write_egress_manifest(ws, provider_desc="p", model_id="m"))
    assert manifest["signature"] is None


def test_write_egress_manifest_signed_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv(enrich_llm.EGRESS_SIGNING_KEY_ENV, "test-signing-key")
    ws = _ws(tmp_path, "a")
    manifest = load_json(enrich_llm.write_egress_manifest(ws, provider_desc="p", model_id="m"))
    assert manifest["signature"]["alg"] == "HMAC-SHA256"
    # signature verifies over the canonical body
    import hmac
    expected = hmac.new(b"test-signing-key", dumps_json(manifest["body"]).encode(),
                        hashlib.sha256).hexdigest()
    assert manifest["signature"]["value"] == expected


def test_write_egress_manifest_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.delenv(enrich_llm.EGRESS_SIGNING_KEY_ENV, raising=False)
    ws_a = _ws(tmp_path, "a")
    ws_b = _ws(tmp_path, "b")
    a = enrich_llm.write_egress_manifest(ws_a, provider_desc="p", model_id="m")
    b = enrich_llm.write_egress_manifest(ws_b, provider_desc="p", model_id="m")
    assert a.read_bytes() == b.read_bytes()


# --- doctor LLM preflight ------------------------------------------------------

def test_doctor_preflight_warns_on_missing_provider(tmp_path, monkeypatch):
    # No ANON_LLM_* config + point the config discovery away from any llm.local.yaml.
    for var in ("ANON_LLM_ENDPOINT", "ANON_LLM_API_KEY", "ANON_LLM_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANON_LLM_CONFIG", str(tmp_path / "no-such-llm.yaml"))
    ws = _ws(tmp_path, "a")
    finding = doctor.llm_preflight(ws)
    assert finding["check"] == "llm-preflight"
    assert finding["ok"] is False
    assert finding["provider"]["status"] in ("missing-config", "unusable")
    # the default redaction policy half is fine even though the provider half is not
    assert finding["redaction"]["status"] == "ok"
    assert finding["redaction"]["source"] == "default"


def test_doctor_preflight_flags_inert_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("ANON_LLM_CONFIG", str(tmp_path / "no-such-llm.yaml"))
    ws = _ws(tmp_path, "a", redaction_yaml="secret_patterns: []\ndeny_list: []\n")
    finding = doctor.llm_preflight(ws)
    assert finding["redaction"]["status"] == "inert"
    assert finding["ok"] is False
    rendered = doctor.render_llm_preflight(finding)
    assert "redaction" in rendered and "WARN" in rendered


def test_doctor_preflight_render_no_workspace():
    finding = doctor.llm_preflight(None)
    assert finding["redaction"]["status"] == "unknown"
    assert isinstance(doctor.render_llm_preflight(finding), str)
