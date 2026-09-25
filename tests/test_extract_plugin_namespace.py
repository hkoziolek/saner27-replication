"""Plugin-namespace extractor tests (``extract_plugin_namespace``).

Tags each ``<Vendor>.Plugin.<Type>.<Name>`` project with ``plugin-type:<Type>`` (the generalized
nopCommerce/Orchard/Umbraco plugin convention, §7-A2 follow-up). Synthetic repos under ``tmp_path``.

    .venv/Scripts/python.exe -m pytest tests/test_extract_plugin_namespace.py -q
"""
from __future__ import annotations

from pathlib import Path

from anon.paths import resolve_workspace
from anon.stages import extract_plugin_namespace as pn


def _proj(repo: Path, name: str) -> None:
    d = repo / "src" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"></Project>\n', encoding="utf-8")


def _tags(fragment: dict, rel: str) -> list[str]:
    tid = f"csharp:csproj:{rel}"
    return next((t["tags"] for t in fragment["targets"] if t["id"] == tid), [])


def test_plugin_type_extraction():
    assert pn.plugin_type("Nop.Plugin.Tax.Avalara") == "Tax"
    assert pn.plugin_type("Nop.Plugin.Payments.Stripe") == "Payments"
    assert pn.plugin_type("Acme.Plugins.Shipping.UPS") == "Shipping"   # plural marker too
    assert pn.plugin_type("Nop.Plugin.Misc.Polls") == "Misc"
    # no type token after the marker, or no dotted boundary -> no tag
    assert pn.plugin_type("Foo.Plugin") is None
    assert pn.plugin_type("MyPluginThing") is None
    assert pn.plugin_type("Nop.Core") is None


def test_emits_plugin_type_tag(tmp_path: Path):
    _proj(tmp_path, "Nop.Plugin.Tax.Avalara")
    _proj(tmp_path, "Nop.Plugin.Tax.FixedRate")
    _proj(tmp_path, "Nop.Core")  # not a plugin
    frag = pn.build_fragment(tmp_path)
    assert frag["relationships"] == []
    assert _tags(frag, "src/Nop.Plugin.Tax.Avalara/Nop.Plugin.Tax.Avalara.csproj") == ["plugin-type:Tax"]
    assert _tags(frag, "src/Nop.Plugin.Tax.FixedRate/Nop.Plugin.Tax.FixedRate.csproj") == ["plugin-type:Tax"]
    ids = {t["id"] for t in frag["targets"]}
    assert "csharp:csproj:src/Nop.Core/Nop.Core.csproj" not in ids  # untagged


def test_run_degrades_to_none_without_plugins(tmp_path: Path):
    _proj(tmp_path, "Nop.Core")
    ws = resolve_workspace(str(tmp_path), str(tmp_path / "arch"))
    assert pn.run(ws) is None
    assert not (ws.fragments / "plugin-namespace.json").exists()


def test_build_fragment_is_deterministic(tmp_path: Path):
    _proj(tmp_path, "Acme.Plugin.Search.Lucene")
    _proj(tmp_path, "Acme.Plugin.Widgets.Carousel")
    assert pn.build_fragment(tmp_path) == pn.build_fragment(tmp_path)
