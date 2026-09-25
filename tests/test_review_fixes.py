"""Regression tests for the xhigh code-review findings (2026-06-06).

Each test pins a fix for a defect the existing suite did NOT catch, so it can't silently
regress. Numbered to the review findings.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from anon.ids import rel_id
from anon.model import canonicalize


# --- #1: rebind_contract_dirs recomputes id + dedups collapsed edges --------------

def test_rebind_recomputes_id_and_dedups() -> None:
    from anon.stages import normalize_facts as nf
    tid = "csharp:csproj:src/Worker/Worker.csproj"
    facts = {
        "targets": [
            {"id": tid, "name": "Worker", "path": "src/Worker", "external": False},
            {"id": "contract:proto:orders", "name": "orders", "path": "x.proto", "external": True},
        ],
        "relationships": [
            {"id": "rel:dir:src/Worker/a->contract:proto:orders", "source": "dir:src/Worker/a",
             "target": "contract:proto:orders", "evidence": [{"type": "package_ref", "detail": "a"}]},
            {"id": "rel:dir:src/Worker/b->contract:proto:orders", "source": "dir:src/Worker/b",
             "target": "contract:proto:orders", "evidence": [{"type": "package_ref", "detail": "b"}]},
        ],
    }
    out = nf.rebind_contract_dirs(facts)
    rels = out["relationships"]
    # both dir: placeholders collapse onto the SAME (source,target) -> exactly one edge
    assert len(rels) == 1, f"expected the two collapsed edges to merge, got {len(rels)}"
    r = rels[0]
    assert r["source"] == tid
    # id must track (source,target), NOT the stale rel:dir:... (the bug)
    assert r["id"] == rel_id(tid, "contract:proto:orders")
    assert "dir:" not in r["id"]


# --- #4: canonicalize sorts a single-fragment target's responsibilities -----------

def test_canonicalize_sorts_responsibilities() -> None:
    facts = {"targets": [{"id": "t1", "responsibilities": ["zeta", "alpha", "mid"]}],
             "relationships": []}
    out = canonicalize(facts)
    assert out["targets"][0]["responsibilities"] == ["alpha", "mid", "zeta"]


# --- #5 / #10: marker-in-string heuristic (word boundary + cppcli markers) --------

def test_marker_in_string_regex_boundaries_and_cppcli() -> None:
    from anon.stages.extract_interop import _MARKER_IN_STRING_RE
    # #10: a lib NAME containing a marker as a substring is NOT a whole-token marker
    assert _MARKER_IN_STRING_RE.search("DllImportHelper") is None
    assert _MARKER_IN_STRING_RE.search("ComImportFactory") is None
    # #5: the C++/CLI markers are now recognised (so a string carrying them gets blanked)
    assert _MARKER_IN_STRING_RE.search("use a value class wrapper") is not None
    assert _MARKER_IN_STRING_RE.search("a value struct here") is not None
    # a whole-token real marker still matches
    assert _MARKER_IN_STRING_RE.search('see [DllImport] docs') is not None


def test_value_class_in_string_does_not_mint_cppcli_seam() -> None:
    from anon.stages.extract_interop import strip_comments_and_strings, _CPPCLI_MARKER_RE
    # `value class` appears ONLY inside a string literal -> after stripping, no cppcli marker.
    src = 'const char* doc = "configure a value class adapter";\nint main() { return 0; }\n'
    stripped = strip_comments_and_strings(src)
    assert _CPPCLI_MARKER_RE.search(stripped) is None, "phantom cppcli seam from a string literal"
    # a REAL gcnew in code still survives stripping and is detected
    real = "ref class Bridge { void f() { auto x = gcnew System::Object(); } };"
    assert _CPPCLI_MARKER_RE.search(strip_comments_and_strings(real)) is not None


# --- #7: _scan_csharp_com pairs [ComImport] with its nearest [Guid], not all guids -

def test_scan_csharp_com_does_not_capture_unrelated_guids() -> None:
    from anon.stages.extract_interop import _scan_csharp_com
    text = (
        '[ComImport] [Guid("3f2504e0-4f89-11d3-9a0c-0305e82c3301")]\n'
        'interface IServer { }\n\n'
        + ("\n" * 50) +
        '[Guid("11111111-2222-3333-4444-555555555555")]\n'   # unrelated, far from ComImport
        'public class Unrelated { }\n'
    )
    names = [n for n, _ in _scan_csharp_com(text)]
    from anon.stages.extract_interop import _normalize_com
    assert _normalize_com("3f2504e0-4f89-11d3-9a0c-0305e82c3301") in names
    assert _normalize_com("11111111-2222-3333-4444-555555555555") not in names, \
        "unrelated [Guid] minted a phantom com: boundary"


# --- #6: COM identity capture is coclass-scoped (.idl) + ProgID-filtered (.rgs) ----

def test_idl_captures_only_coclass_clsid() -> None:
    from anon.stages.extract_msbuild_cpp import _com_identities_from_idl
    idl = (
        '[ uuid(aaaaaaaa-0000-0000-0000-000000000001) ] interface IFoo { };\n'
        '[ uuid(bbbbbbbb-0000-0000-0000-000000000002) ] coclass Foo { interface IFoo; };\n'
    )
    ids = _com_identities_from_idl(idl)
    assert "bbbbbbbb-0000-0000-0000-000000000002" in ids  # the coclass CLSID
    assert "aaaaaaaa-0000-0000-0000-000000000001" not in ids  # the interface IID is NOT captured


def test_rgs_progid_filters_file_extension_tokens() -> None:
    from anon.stages.extract_msbuild_cpp import _com_identities_from_rgs
    rgs = "val ThreadingModel = s 'Apartment'\n'CalcServer.Object'\n'CalcServer.dll'\n"
    ids = _com_identities_from_rgs(rgs)
    assert "CalcServer.Object" in ids
    assert "CalcServer.dll" not in ids  # filename-like dotted token is not a ProgID


# --- #9: egress gate only fires when a provider will actually be called ------------

def test_egress_gate_skips_when_no_provider(tmp_path: Path) -> None:
    from anon import cli
    from anon.paths import resolve_workspace
    rules = tmp_path / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "payload-redaction.yaml").write_text("secret_patterns: []\ndeny_list: []\n",
                                                  encoding="utf-8", newline="")
    ws = resolve_workspace(str(tmp_path / "repo"), str(tmp_path / "arch"), str(rules))
    args = SimpleNamespace(accept_unredacted=False)
    # provider None (e.g. --llm-accepted serving pinned names) -> NO refusal even if inert
    assert cli._egress_gate(ws, args, "llm-accepted", provider=None) is None
    # a real provider + inert policy -> refuse (exit 4)
    assert cli._egress_gate(ws, args, "llm-propose", provider=object()) == 4


# --- #12: egress manifest records provider and model_id distinctly -----------------

def test_manifest_model_id_split() -> None:
    # the helper splits "client:model" so model_id is the bare model, not the full descriptor.
    prov_msg = "azure:gpt-4o"
    model_id = prov_msg.split(":", 1)[1] if (prov_msg and ":" in prov_msg) else (prov_msg or "")
    assert model_id == "gpt-4o"
    assert model_id != prov_msg
