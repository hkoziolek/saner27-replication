"""Tests for the shared salience module — the entry-point detector (dynamic-view plan §1a#3/§6).

The detector is lifted out of ``generate_docs.build_tour`` so Source C's graph-walk and the
guided tour AGREE on entry roots (plan §6). It folds four signals on top of the zero-in-degree
heuristic: (1) ``sdk-role:service-web``/``worker`` tags; (2) zero combined build+runtime
in-degree; (3) an ``api-skeleton/1`` ``[ApiController]``/route / ``[HttpGet/...]`` type →
high-confidence root; (4) for C++, a build-target ``type == "executable"`` so test/header
library targets are never mis-rooted.
"""
from __future__ import annotations

from anon import salience


def _tgt(cid, name, **kw):
    return {"id": cid, "container_id": cid, "container_name": name,
            "tags": kw.get("tags", []), "type": kw.get("type"),
            "external": kw.get("external", False)}


def test_zero_in_degree_is_an_entry():
    facts = {
        "targets": [_tgt("c:web", "Web"), _tgt("c:lib", "Lib")],
        "relationships": [{"source": "c:web", "target": "c:lib"}],
    }
    entries, reasons = salience.detect_entries(facts)
    assert entries == ["c:web"]
    assert "entry-point (zero in-degree)" in reasons["c:web"]


def test_sdk_role_service_web_is_high_confidence_root_even_with_in_degree():
    """A service-web container ranks as an entry root even if something depends on it
    (high-confidence signal beats the zero-in-degree heuristic, plan §6)."""
    facts = {
        "targets": [
            _tgt("c:api", "Api", tags=["sdk-role:service-web"]),
            _tgt("c:gw", "Gateway"),
        ],
        # gateway -> api gives api a non-zero in-degree; the tag still makes it a root.
        "relationships": [{"source": "c:gw", "target": "c:api"}],
    }
    entries, reasons = salience.detect_entries(facts)
    # both are entries (gw is zero-in-degree); api sorts FIRST as a high-confidence root.
    assert entries[0] == "c:api"
    assert "entry-point (sdk-role:service-web)" in reasons["c:api"]


def test_api_controller_signal_makes_a_high_confidence_root():
    facts = {
        "targets": [_tgt("c:svc", "Svc"), _tgt("c:other", "Other")],
        "relationships": [{"source": "c:other", "target": "c:svc"}],
    }
    api = {"c:svc": [{"name": "OrdersController", "kind": "class",
                      "attributes": ["ApiController", "Route(\"api/[controller]\")"]}]}
    entries, reasons = salience.detect_entries(facts, api_skeletons=api)
    assert entries[0] == "c:svc"
    assert "entry-point (api controller/route)" in reasons["c:svc"]


def test_http_method_attr_on_a_member_is_a_route_signal():
    facts = {"targets": [_tgt("c:svc", "Svc"), _tgt("c:dep", "Dep")],
             "relationships": [{"source": "c:dep", "target": "c:svc"}]}
    api = {"c:svc": [{"name": "Endpoints", "kind": "class",
                      "members": [{"sig": "Get()", "attrs": ["HttpGet(\"/things\")"]}]}]}
    entries, reasons = salience.detect_entries(facts, api_skeletons=api)
    assert "c:svc" in entries
    assert "entry-point (api controller/route)" in reasons["c:svc"]


def test_cpp_executable_type_roots_but_a_library_does_not():
    """A C++ executable target roots the walk; a library/header target with in-degree does
    not (plan §6 — never mis-root a test/header target)."""
    facts = {
        "targets": [
            _tgt("c:app", "App", type="executable"),
            _tgt("c:lib", "Lib", type="library"),
        ],
        # lib -> app: app has in-degree but is an executable -> still a root; lib is not.
        "relationships": [{"source": "c:lib", "target": "c:app"}],
    }
    entries, reasons = salience.detect_entries(facts)
    assert entries[0] == "c:app"
    assert "entry-point (executable target)" in reasons["c:app"]
    # lib has in-degree 0? no — lib has out-degree only, in-degree 0, so lib IS a zero-in
    # entry too; but the executable sorts first.
    assert entries.index("c:app") < entries.index("c:lib")


def test_external_container_is_never_an_entry():
    facts = {
        "targets": [
            _tgt("c:web", "Web"),
            _tgt("c:ext", "External", tags=["external"], external=True),
        ],
        "relationships": [{"source": "c:web", "target": "c:ext"}],
    }
    entries, _reasons = salience.detect_entries(facts)
    assert "c:ext" not in entries
    assert entries == ["c:web"]


def test_detect_entries_is_deterministic():
    facts = {
        "targets": [_tgt(f"c:{x}", x.upper()) for x in ("z", "a", "m")],
        "relationships": [],   # all isolated -> all zero in-degree entries
    }
    e1, _ = salience.detect_entries(facts)
    e2, _ = salience.detect_entries(facts)
    assert e1 == e2 == ["c:a", "c:m", "c:z"]   # sorted by container id
