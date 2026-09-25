"""RQ2 §6.2 LLM-recovery baseline tests (baselines/llm_recovery.py).

Guarantees under test, all WITHOUT a network call or Java (score is monkeypatched):
  * identity is ours — parse_partition keeps only real graph nodes, drops hallucinations,
    and normalizes the three reply shapes models actually emit;
  * the ITK case is REFUSED, not truncated — an over-budget file graph returns
    status=refused-oversize with an actionable message (eval §6.2);
  * estimate-only when no provider/config — token + $ estimate, no egress (the --no-llm ethos);
  * the live path (fake provider) scores each rerun and reports MoJoFM mean/stdev — the
    reproducibility axis §6.2 is built around.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "baselines"))   # llm_recovery imports run_arcade as a sibling


def _import(rel: str):
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lr = _import("baselines/llm_recovery.py")


# ------------------------------------------------------------------ pure functions

def test_parse_partition_flat_map_keeps_only_real_nodes():
    nodes = {"a", "b", "c"}
    part = lr.parse_partition('{"a": "x", "b": "x", "c": "y", "ghost": "z"}', nodes)
    assert part == {"a": "x", "b": "x", "c": "y"}       # hallucinated "ghost" dropped


def test_parse_partition_clusters_inversion_and_assignments_envelope():
    nodes = {"a", "b", "c"}
    inv = lr.parse_partition('{"clusters": {"core": ["a", "b"], "io": ["c", "ghost"]}}', nodes)
    assert inv == {"a": "core", "b": "core", "c": "io"}
    env = lr.parse_partition('{"assignments": {"a": "1", "b": "2"}}', nodes)
    assert env == {"a": "1", "b": "2"}


def test_parse_partition_raises_on_non_object():
    try:
        lr.parse_partition("[1, 2, 3]", {"a"})
    except ValueError:
        return
    raise AssertionError("expected ValueError on non-object reply")


def test_estimate_cost_caching_is_cheaper_and_scales_with_reruns():
    per_call = {"input": 100_000, "output": 20_000, "per_call_total": 120_000}
    c = lr.estimate_cost(per_call, reruns=5, price_in=5.0, price_out=30.0)
    assert c["total_input_tokens"] == 500_000 and c["total_output_tokens"] == 100_000
    # uncached: 0.5M*$5 + 0.1M*$30 = 2.5 + 3.0 = 5.5
    assert c["usd_uncached"] == 5.5
    # caching bills input once + 4*10%: 140k*$5/1e6 + 3.0 = 0.7 + 3.0 = 3.7 < uncached
    assert c["usd_with_prompt_caching"] == 3.7
    assert c["usd_with_prompt_caching"] < c["usd_uncached"]


def test_decide_granularity_refuses_oversize_file_but_allows_target_and_override():
    assert lr.decide_granularity("target", 10**9, 180_000, False) is None      # target always ok
    assert lr.decide_granularity("file", 5_000, 180_000, False) is None        # under budget ok
    assert lr.decide_granularity("file", 800_000, 180_000, True) is None       # explicit override
    msg = lr.decide_granularity("file", 800_000, 180_000, False)
    assert msg and "--granularity target" in msg and "§6.2" in msg


# ------------------------------------------------------------------ end-to-end (no network/Java)

def _graph(arch: Path, g: str, edges: list[tuple[str, str]]) -> None:
    p = arch / "generated" / "graph" / f"graph-{g}.rsf"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"depend {a} {b}\n" for a, b in edges), encoding="utf-8", newline="")


def _reference(path: Path, member_cluster: dict[str, str]) -> None:
    path.write_text("".join(f"contain {c} {m}\n" for m, c in member_cluster.items()),
                    encoding="utf-8", newline="")


class _FakeProvider:
    """Returns a canned partition; the counter lets score vary across reruns."""
    model_id = "fake-sol"

    def __init__(self):
        self.calls = 0

    def complete_text(self, system_prompt: str, user_msg: str) -> str:
        self.calls += 1
        # group a,b together and c,d together — a plausible reply over the toy graph
        return json.dumps({"a": "core", "b": "core", "c": "io", "d": "io"})


def test_estimate_only_when_no_provider(tmp_path):
    arch = tmp_path / "out" / "sys" / "architecture"
    _graph(arch, "target", [("a", "b"), ("c", "d"), ("a", "c")])
    ref = tmp_path / "ref.rsf"
    _reference(ref, {"a": "1", "b": "1", "c": "2", "d": "2"})
    art = lr.run("sys", granularity="target", reruns=5, arch=arch, reference_override=str(ref),
                 max_input_tokens=180_000, allow_oversize=False, dry_run=True,
                 price_in=5.0, price_out=30.0, provider=None, out_root=tmp_path / "out")
    assert art["status"] == "estimate-only"
    assert art["token_estimate_per_call"]["input"] > 0
    assert art["cost_estimate"]["usd_uncached"] >= 0
    assert art["runs"] == [] and art["model_id"] is None


def test_refused_oversize_file_graph(tmp_path):
    arch = tmp_path / "out" / "sys" / "architecture"
    _graph(arch, "file", [(f"src/f{i}.c", f"src/h{i}.h") for i in range(50)])
    art = lr.run("sys", granularity="file", reruns=5, arch=arch, reference_override=None,
                 max_input_tokens=5, allow_oversize=False, dry_run=False,
                 price_in=5.0, price_out=30.0, provider=_FakeProvider(),
                 out_root=tmp_path / "out")
    assert art["status"] == "refused-oversize"
    assert "--granularity target" in art["message"]
    assert art["runs"] == []          # refused BEFORE any call


def test_live_path_scores_and_reports_variance(tmp_path, monkeypatch):
    arch = tmp_path / "out" / "sys" / "architecture"
    _graph(arch, "target", [("a", "b"), ("c", "d"), ("a", "c")])
    ref = tmp_path / "ref.rsf"
    _reference(ref, {"a": "1", "b": "1", "c": "2", "d": "2"})

    scores = iter([80.0, 82.0, 78.0])   # vary MoJoFM across reruns -> nonzero stdev

    def fake_score(recovered, against):
        if against is None or not Path(against).exists():
            return None
        return {"entities_recovered": 4, "entities_against": 4, "entities_common": 4,
                "entities_dropped_whitespace": 0, "mojofm": next(scores), "a2a": 90.0}

    monkeypatch.setattr(lr, "score", fake_score)
    prov = _FakeProvider()
    art = lr.run("sys", granularity="target", reruns=3, arch=arch, reference_override=str(ref),
                 max_input_tokens=180_000, allow_oversize=False, dry_run=False,
                 price_in=5.0, price_out=30.0, provider=prov, out_root=tmp_path / "out")

    assert art["status"] == "ran" and art["model_id"] == "fake-sol"
    assert prov.calls == 3 and len(art["runs"]) == 3
    agg = art["aggregate"]["mojofm"]
    assert agg["mean"] == 80.0 and agg["stdev"] > 0 and agg["min"] == 78.0 and agg["max"] == 82.0
    # representative artifacts land where make_comparison/aggregate look for a technique
    assert (tmp_path / "out" / "sys" / "baseline" / "llm-target" / "clusters.rsf").exists()
    assert (tmp_path / "out" / "sys" / "baseline" / "llm-target" / "fidelity.json").exists()
    path = lr.write_artifact(art, tmp_path / "out")
    assert path.exists() and json.loads(path.read_text())["aggregate"]["mojofm"]["n"] == 3
