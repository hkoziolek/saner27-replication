"""RQ9 blind-curation tooling tests (eval §10a; `baselines/rq9_blind_curation.py` +
`anon.edit_trace`).

The load-bearing guarantees under test:
  * worksheet: the rater CSV contains the entity list ONLY — the pipeline's cluster
    column never leaks to a blind rater (§10a.1);
  * ingest: a filled worksheet round-trips to a valid RSF; incomplete/duplicate rows
    are refused visibly; EXCLUDE is honored and counted;
  * ceiling: Cohen's κ matches a hand-computed example; jar-less runs degrade MoJoFM
    to null without touching κ;
  * effort: rule/op/propose-derived counts from synthetic rules + trace;
  * score: the container-edge F1 alignment on a hand-computed example; the four-row
    fidelity.json assembles from its parts;
  * edit_trace: off-by-default no-op, JSONL round-trip, never raises; and the serve
    write path records reviewed writes + curate ops when the env var is set.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "baselines"))   # rq9 imports run_arcade as a sibling


def _import(rel: str):
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rq9 = _import("baselines/rq9_blind_curation.py")

from anon import edit_trace  # noqa: E402


# ------------------------------------------------------------------ helpers

def _arch_with_clusters(tmp_path: Path, contains: dict[str, str],
                        granularity: str = "target") -> Path:
    arch = tmp_path / "architecture"
    graph_dir = arch / "generated" / "graph"
    graph_dir.mkdir(parents=True)
    rq9.write_contains(contains, graph_dir / f"clusters-{granularity}.rsf")
    return arch


# ------------------------------------------------------------------ worksheet

def test_worksheet_lists_entities_but_never_the_pipeline_clusters(tmp_path, capsys):
    arch = _arch_with_clusters(tmp_path, {"t:a": "SecretClusterA", "t:b": "SecretClusterB"})
    out = tmp_path / "worksheet.csv"
    rc = rq9.main(["worksheet", "sys", "--arch-dir", str(arch), "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "t:a" in text and "t:b" in text
    assert "SecretCluster" not in text, "pipeline decomposition must not leak to a rater"
    assert text.startswith("#"), "blinding instructions preamble present"
    assert "\r" not in text, "LF only"


def test_worksheet_requires_the_graph_export(tmp_path):
    rc = rq9.main(["worksheet", "sys", "--arch-dir", str(tmp_path / "nope"),
                   "--out", str(tmp_path / "w.csv")])
    assert rc == 2


# ------------------------------------------------------------------ ingest

def _write_worksheet(path: Path, rows: list[str]) -> None:
    path.write_text("# comment\nentity,cluster,notes\n" + "\n".join(rows) + "\n",
                    encoding="utf-8")


def test_ingest_round_trips_and_honors_exclude(tmp_path):
    ws = tmp_path / "w.csv"
    _write_worksheet(ws, ["t:a,Core,", "t:b,Core,solid", "t:c,EXCLUDE,test project"])
    out = tmp_path / "rater.rsf"
    assert rq9.main(["ingest", str(ws), "--out", str(out)]) == 0
    assert rq9.read_contains(out) == {"t:a": "Core", "t:b": "Core"}


def test_ingest_refuses_missing_cluster_and_duplicates(tmp_path, capsys):
    ws = tmp_path / "w.csv"
    _write_worksheet(ws, ["t:a,Core,", "t:a,Other,", "t:b,,"])
    assert rq9.main(["ingest", str(ws), "--out", str(tmp_path / "r.rsf")]) == 2
    err = capsys.readouterr().err
    assert "duplicate entity" in err and "no cluster" in err


# ------------------------------------------------------------------ ceiling / kappa

def test_cohen_kappa_hand_computed():
    # po = 3/4; pe = .5*.5 + .5*.25 = .375; kappa = (0.75-0.375)/0.625 = 0.6
    r1 = {"e1": "A", "e2": "A", "e3": "B", "e4": "B"}
    r2 = {"e1": "a", "e2": "A", "e3": "b", "e4": "C"}
    res = rq9.cohen_kappa(r1, r2)
    assert res["n_common"] == 4
    assert res["raw_agreement"] == 0.75
    assert res["kappa"] == 0.6


def test_kappa_zero_but_partition_identical_is_the_vocabulary_pathology():
    """The eShop case: same partition, disjoint vocabularies -> pre-registered κ = 0.

    This is why the alignment-invariant companions exist (eval §20 residual). κ must stay 0
    (it is the pre-registered statistic and we do not silently repair it); ARI / Rand /
    aligned-κ must all report perfect agreement.
    """
    r1 = {"e1": "Basket", "e2": "Basket", "e3": "Ordering", "e4": "Ordering"}
    r2 = {"e1": "BasketService", "e2": "BasketService",
          "e3": "OrderingService", "e4": "OrderingService"}
    res = rq9.cohen_kappa(r1, r2)
    assert res["labels_shared"] == 0
    assert res["kappa"] == 0.0 and res["raw_agreement"] == 0.0
    assert res["ari"] == 1.0
    assert res["pair_agreement"] == 1.0
    assert res["kappa_aligned"] == 1.0
    assert res["raw_agreement_aligned"] == 1.0


def test_alignment_invariants_are_insensitive_to_renaming():
    """Renaming clusters must move the companions by exactly nothing."""
    r1 = {"e1": "A", "e2": "A", "e3": "B", "e4": "C", "e5": "C"}
    r2 = {"e1": "A", "e2": "B", "e3": "B", "e4": "C", "e5": "C"}
    base = rq9.cohen_kappa(r1, r2)
    renamed = rq9.cohen_kappa(r1, {e: f"cluster-{c}-x" for e, c in r2.items()})
    for key in ("ari", "pair_agreement", "kappa_aligned", "raw_agreement_aligned"):
        assert base[key] == renamed[key], key
    assert renamed["kappa"] == 0.0 and base["kappa"] != 0.0  # raw κ IS sensitive


def test_ari_is_zero_at_chance_and_one_when_identical():
    labels = {f"e{i}": "A" if i < 4 else "B" for i in range(8)}
    same = rq9.cohen_kappa(labels, labels)
    assert same["ari"] == 1.0 and same["pair_agreement"] == 1.0
    # all-singletons vs one-cluster: no pair agrees on "together", ARI is 0 by construction
    singles = {f"e{i}": f"S{i}" for i in range(8)}
    one = {f"e{i}": "ALL" for i in range(8)}
    assert rq9.cohen_kappa(singles, one)["ari"] == 0.0


def test_hungarian_finds_the_optimal_assignment_not_a_greedy_one():
    # greedy on row 0 takes col 0 (9) and is then stuck with 1 -> 10; optimal is 8+7 = 15
    cost = [[-9, -8], [-7, -1]]
    assert rq9._hungarian(cost) == [(0, 1), (1, 0)]
    # rectangular (rows < cols) must not raise and must match every row
    pairs = rq9._hungarian([[-5, -1, -1], [-1, -1, -6]])
    assert sorted(i for i, _ in pairs) == [0, 1]


def test_ceiling_labels_a_vocabulary_induced_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(rq9, "ARCADE_JAR", tmp_path / "missing.jar")
    r1, r2 = tmp_path / "r1.rsf", tmp_path / "r2.rsf"
    rq9.write_contains({"e1": "A", "e2": "A", "e3": "B", "e4": "B"}, r1)
    rq9.write_contains({"e1": "X", "e2": "X", "e3": "Y", "e4": "Y"}, r2)
    out = tmp_path / "ceiling.json"
    assert rq9.main(["ceiling", str(r1), str(r2), "--out", str(out)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert "flag" in res                                   # pre-registered flag still fires
    assert res["flag_interpretation"].startswith("VOCABULARY-INDUCED")


def test_ceiling_labels_a_substantive_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(rq9, "ARCADE_JAR", tmp_path / "missing.jar")
    r1, r2 = tmp_path / "r1.rsf", tmp_path / "r2.rsf"
    rq9.write_contains({"e1": "A", "e2": "A", "e3": "A", "e4": "B"}, r1)
    rq9.write_contains({"e1": "X", "e2": "Y", "e3": "Z", "e4": "Z"}, r2)
    out = tmp_path / "ceiling.json"
    assert rq9.main(["ceiling", str(r1), str(r2), "--out", str(out)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["flag_interpretation"].startswith("SUBSTANTIVE")


def test_normalize_label_collapses_separators():
    assert rq9.normalize_label(" Building-Blocks ") == rq9.normalize_label("building_blocks")
    assert rq9.normalize_label("Web.Apps") == "web apps"


def test_ceiling_without_jar_degrades_mojofm_to_null(tmp_path, monkeypatch):
    monkeypatch.setattr(rq9, "ARCADE_JAR", tmp_path / "missing.jar")
    r1, r2 = tmp_path / "r1.rsf", tmp_path / "r2.rsf"
    rq9.write_contains({"e1": "A", "e2": "A", "e3": "B"}, r1)
    rq9.write_contains({"e1": "A", "e2": "B", "e3": "B"}, r2)
    out = tmp_path / "ceiling.json"
    assert rq9.main(["ceiling", str(r1), str(r2), "--out", str(out)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["agreement"]["kappa"] is not None
    assert res["agreement"]["mojofm_mean"] is None
    assert "flag" in res  # 2/3 agreement -> kappa < 0.6 flagged per §5.3


def test_ceiling_vs_reference_block(tmp_path, monkeypatch):
    monkeypatch.setattr(rq9, "ARCADE_JAR", tmp_path / "missing.jar")
    r1, r2, ref = tmp_path / "r1.rsf", tmp_path / "r2.rsf", tmp_path / "ref.rsf"
    labels = {"e1": "A", "e2": "A", "e3": "B"}
    for p in (r1, r2, ref):
        rq9.write_contains(labels, p)
    out = tmp_path / "ceiling.json"
    assert rq9.main(["ceiling", str(r1), str(r2), "--reference", str(ref),
                     "--out", str(out)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["agreement"]["kappa"] == 1.0
    assert res["vs_reference"]["a"]["kappa"] == 1.0
    assert res["vs_reference"]["b"]["raw_agreement"] == 1.0


# ------------------------------------------------------------------ effort

RULES_YAML = """\
# curated by C
group:
  - container: Core
    members: [t:a, t:b]
  - container: Plugins
    members_glob: ["*Plugin*"]
exclude:
  targets:
    - glob: "*Tests*"
"""

PROPOSAL_YAML = """\
group:
  - container: Core
    members: [t:a, t:b, t:x]
  - container: Unused
    members: [t:z]
"""


def test_effort_counts_rules_trace_and_propose_fraction(tmp_path):
    rules = tmp_path / "mapping-rules.yaml"
    rules.write_text(RULES_YAML, encoding="utf-8")
    proposal = tmp_path / "proposed-mapping-rules.yaml"
    proposal.write_text(PROPOSAL_YAML, encoding="utf-8")
    trace = tmp_path / "edit-trace.jsonl"
    trace.write_text(
        '{"ts": "2026-07-17T10:00:00+00:00", "event": "curate_ops",'
        ' "kinds": ["add_member", "add_member", "create_group"]}\n'
        '{"ts": "2026-07-17T10:30:00+00:00", "event": "reviewed_write",'
        ' "file": "mapping-rules.yaml", "added": 3, "removed": 1}\n',
        encoding="utf-8")
    out = tmp_path / "effort.json"
    assert rq9.main(["effort", "--rules", str(rules), "--trace", str(trace),
                     "--proposal", str(proposal), "--wall-clock-minutes", "45",
                     "--out", str(out)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["rules"]["groups"] == 2
    assert res["rules"]["explicit_members"] == 2
    assert res["rules"]["member_globs"] == 1
    assert res["rules"]["excludes"] == 1
    # Core matches the draft (2/3 member overlap >= 50%); Plugins is hand-authored.
    assert res["propose"]["final_groups_propose_derived"] == 1
    assert res["propose"]["propose_derived_fraction"] == 0.5
    assert res["trace"]["ops_by_kind"] == {"add_member": 2, "create_group": 1}
    assert res["trace"]["wall_clock_minutes_trace"] == 30.0
    assert res["wall_clock_minutes_reported"] == 45


# ------------------------------------------------------------------ score / edge F1

def test_container_edge_f1_hand_computed():
    ref = {"a1": "X", "a2": "X", "b1": "Y", "b2": "Y"}
    cand = {"a1": "P", "a2": "P", "b1": "Q", "b2": "X"}
    edges = [("a1", "b1"), ("a2", "b2")]
    res = rq9.container_edge_f1(cand, ref, edges)
    # P->X (overlap 2), Q->Y (overlap 1); cand's own 'X' stays unmatched.
    assert res["clusters_aligned"] == 2 and res["clusters_unmatched"] == 1
    # lifted cand: {X->Y (tp), X->unmatched::X}; lifted ref: {X->Y}
    assert res["true_positive_edges"] == 1
    assert res["precision"] == 0.5 and res["recall"] == 1.0
    assert res["f1"] == round(2 * 0.5 * 1.0 / 1.5, 3)


def test_score_assembles_the_four_row_table(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rq9, "ARCADE_JAR", tmp_path / "missing.jar")
    ref = tmp_path / "reference.rsf"
    rq9.write_contains({"a1": "X", "a2": "X", "b1": "Y"}, ref)
    curator = tmp_path / "curator.rsf"
    rq9.write_contains({"a1": "C1", "a2": "C1", "b1": "C2"}, curator)
    graph = tmp_path / "graph.rsf"
    graph.write_text("depend a1 b1\ndepend a1 a2\n", encoding="utf-8")
    comparison = tmp_path / "comparison.json"
    comparison.write_text(json.dumps({"results": {
        "dir": {"mojofm": 30.0, "a2a": 60.0, "clusters": 3},
        "acdc": {"mojofm": 25.0, "a2a": 55.0, "clusters": 4},
        "arc": {"mojofm": 41.0, "a2a": 58.0, "clusters": 5},
    }}), encoding="utf-8")
    ceiling = tmp_path / "ceiling.json"
    ceiling.write_text(json.dumps({"agreement": {
        "kappa": 0.8, "mojofm_mean": 90.0, "a2a": 92.0, "n_common": 3}}),
        encoding="utf-8")
    out = tmp_path / "fidelity.json"
    assert rq9.main(["score", "sys", "--curator", str(curator),
                     "--reference", str(ref), "--graph", str(graph),
                     "--comparison", str(comparison), "--ceiling", str(ceiling),
                     "--out", str(out)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    rows = res["rows"]
    assert rows["floor"]["mojofm"] == 30.0 and rows["floor"]["kind"] == "detection"
    assert rows["best_sar"]["technique"] == "arc" and rows["best_sar"]["mojofm"] == 41.0
    assert rows["blind_curator"]["kind"] == "expression"
    assert rows["blind_curator"]["container_edge_f1"]["f1"] == 1.0
    assert rows["human_ceiling"]["kappa"] == 0.8
    table = capsys.readouterr().out
    assert "blind_curator" in table and "human_ceiling" in table


# ------------------------------------------------------------------ edit_trace module

def test_edit_trace_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(edit_trace.ENV_VAR, raising=False)
    assert not edit_trace.enabled()
    edit_trace.record("noop", x=1)  # must not raise, must not create anything
    assert list(tmp_path.iterdir()) == []


def test_edit_trace_round_trip_and_never_raises(tmp_path, monkeypatch):
    trace = tmp_path / "sub" / "edit-trace.jsonl"
    monkeypatch.setenv(edit_trace.ENV_VAR, str(trace))
    assert edit_trace.enabled()
    edit_trace.record("reviewed_write", file="mapping-rules.yaml", added=2, removed=0)
    edit_trace.record("curate_ops", kinds=["move_member"])
    entries = edit_trace.read_trace(trace)
    assert [e["event"] for e in entries] == ["reviewed_write", "curate_ops"]
    assert all("ts" in e for e in entries)
    # A path that cannot be a file (its parent is a file) is swallowed, not raised.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(edit_trace.ENV_VAR, str(blocker / "trace.jsonl"))
    edit_trace.record("still_fine")


# ------------------------------------------------------------------ serve hook

def test_serve_records_reviewed_writes_and_curate_ops(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi", reason="[serve] extra not installed")
    from fastapi.testclient import TestClient
    from conftest import run_toy_pipeline
    from anon.serve import create_app

    trace = tmp_path / "edit-trace.jsonl"
    monkeypatch.setenv(edit_trace.ENV_VAR, str(trace))
    tr = run_toy_pipeline(tmp_path / "architecture")
    c = TestClient(create_app(tr.ws, token="t", allowed_hosts=None))
    headers = {"X-Anon-Token": "t"}

    sha = c.get("/api/v1/rules/mapping").json()["sha256"]
    resp = c.post("/api/v1/rules/mapping/curate-edit",
                  json={"ops": [{"op": "create_group", "container": "TraceProbe",
                                 "members_glob": ["*TraceProbeMatchesNothing*"]}],
                        "apply": True},
                  headers={**headers, "If-Match": sha})
    assert resp.status_code == 200, resp.text
    events = [e["event"] for e in edit_trace.read_trace(trace)]
    assert "curate_ops" in events and "reviewed_write" in events
    ops_entry = next(e for e in edit_trace.read_trace(trace)
                     if e["event"] == "curate_ops")
    assert ops_entry["kinds"] == ["create_group"]


# ------------------------------------------------------------------ analysis/ RQ9 collector

agg = _import("analysis/aggregate_results.py")
stats_mod = _import("analysis/stats.py")


def test_aggregate_rq9_rows_tracked_home_with_out_fallback(tmp_path):
    """The §15 collector reads the TRACKED references/<sys>/blind-curation/ first and the
    out/<sys>/blind-curation/ mirror only as a per-file fallback; every four-row-table number
    carries its §10a.7 label as the projection; the partition relation separates granularity
    (merges/splits) from boundary conflicts; the human_ceiling row is sourced from
    ceiling.json alone."""
    refs, out = tmp_path / "references", tmp_path / "out"
    sysdir, bc = refs / "eshop", refs / "eshop" / "blind-curation"
    bc.mkdir(parents=True)
    ob = out / "eshop" / "blind-curation"
    ob.mkdir(parents=True)
    # committed (scored) reference: X={a1,a2}, Y={b1,b2}
    rq9.write_contains({"a1": "X", "a2": "X", "b1": "Y", "b2": "Y"}, sysdir / "reference.rsf")
    (sysdir / "provenance.json").write_text(
        json.dumps({"grade": "GT-doc", "clusters": 2, "entities": 4}), encoding="utf-8")
    # R-C: strict refinement (splits X, keeps Y); R-D: cuts across both boundaries;
    # consensus == the reference
    rq9.write_contains({"a1": "P", "a2": "Q", "b1": "R", "b2": "R"}, bc / "rater-R-C.rsf")
    rq9.write_contains({"a1": "M", "b1": "M", "a2": "N", "b2": "N"}, bc / "rater-R-D.rsf")
    rq9.write_contains({"a1": "X", "a2": "X", "b1": "Y", "b2": "Y"}, bc / "consensus.rsf")
    (bc / "ceiling.json").write_text(json.dumps({
        "raters": {"a": "R-C", "b": "R-D"},
        "agreement": {"kappa": 0.0, "raw_agreement": 0.0, "labels_shared": 0,
                      "mojofm_ab": 70.0, "mojofm_ba": 60.0, "mojofm_mean": 65.0,
                      "a2a": 88.0, "n_common": 4},
        "flag": "kappa < 0.6 — lower-confidence reference",
        "vs_reference": {"a": {"kappa": 0.4, "mojofm_mean": 93.0, "a2a": 97.0,
                               "raw_agreement": 0.5},
                         "b": {"kappa": None, "mojofm_mean": 74.0, "a2a": 91.0,
                               "raw_agreement": 0.0}}}), encoding="utf-8")
    # effort: R-A exists in BOTH places (tracked must win); R-B only in the out/ mirror
    effort = {"rules": {"rule_lines": 40, "groups": 10},
              "propose": {"propose_derived_fraction": 0.5},
              "trace": {"events": 89, "ops_total": 30,
                        "ops_by_kind": {"create_group": 10, "move_member": 20},
                        "wall_clock_minutes_trace": 41.0},
              "wall_clock_minutes_reported": 43}
    (bc / "effort-R-A.json").write_text(json.dumps(effort), encoding="utf-8")
    (ob / "effort-R-A.json").write_text(
        json.dumps({**effort, "rules": {"groups": 99}}), encoding="utf-8")
    (ob / "effort-R-B.json").write_text(
        json.dumps({"rules": {"groups": 7}}), encoding="utf-8")
    # the four-row table vs the committed reference + the consensus sensitivity scoring
    fid = {"reference": "references\\eshop\\reference.rsf", "rows": {
        "floor": {"mojofm": 30.0, "a2a": 60.0, "clusters": 3, "kind": "detection"},
        "best_sar": {"technique": "arc", "mojofm": 41.0, "a2a": 58.0, "kind": "detection"},
        "blind_curator": {"mojofm": 66.7, "a2a": 90.0, "n_common": 4, "clusters": 10,
                          "kind": "expression",
                          "container_edge_f1": {"precision": 0.5, "recall": 1.0, "f1": 0.667}},
        "human_ceiling": {"kind": "ceiling", "mojofm": 65.0, "kappa": 0.0}}}
    (bc / "fidelity-R-A.json").write_text(json.dumps(fid), encoding="utf-8")
    (bc / "fidelity-R-A-vs-consensus.json").write_text(json.dumps(
        {**fid, "reference": "references/eshop/blind-curation/consensus.rsf"}),
        encoding="utf-8")

    rows = [r for r in agg.iter_rows(out, refs) if r["axis"] == "RQ9"]
    assert rows and all(r["system"] == "eshop" and r["ref_grade"] == "GT-doc" for r in rows)

    def val(technique, metric, projection):
        got = [r for r in rows if r["technique"] == technique and r["metric"] == metric
               and r["projection"] == projection]
        assert len(got) == 1, (technique, metric, projection, got)
        return got[0]["value"]

    # ceiling: the pair cells + the pre-registered kappa flag as 0/1
    assert val("ceiling", "kappa", "rater-pair") == 0.0
    assert val("ceiling", "mojofm", "rater-pair") == 65.0
    assert val("ceiling", "kappa_flag", "rater-pair") == 1
    assert val("ceiling", "entities_common", "rater-pair") == 4
    # W3a each-rater-vs-committed; a None kappa is skipped, never emitted as 0
    assert val("rater:R-C", "mojofm", "rater-vs-reference") == 93.0
    assert val("rater:R-C", "kappa", "rater-vs-reference") == 0.4
    assert not [r for r in rows if r["technique"] == "rater:R-D" and r["metric"] == "kappa"]
    # partitions: nested (granularity) vs cut-across (boundary conflict)
    assert val("rater:R-C", "clusters", "partition") == 3
    assert val("rater:R-C", "boundary_conflicts", "vs-reference") == 0
    assert val("rater:R-C", "splits", "vs-reference") == 1
    assert val("rater:R-C", "merges", "vs-reference") == 0
    assert val("rater:R-D", "boundary_conflicts", "vs-reference") == 2
    assert val("rater:R-D", "splits", "vs-reference") == 0
    assert val("consensus", "clusters", "partition") == 2
    assert val("consensus", "boundary_conflicts", "vs-reference") == 0
    assert val("consensus", "splits", "vs-reference") == 0
    # effort: tracked shadows the out/ mirror for the same file; out/-only files still count
    assert val("curator:R-A", "groups", "effort") == 10
    assert val("curator:R-B", "groups", "effort") == 7
    assert val("curator:R-A", "ops:move_member", "effort") == 20
    assert val("curator:R-A", "wall_clock_minutes_reported", "effort") == 43
    assert val("curator:R-A", "propose_derived_fraction", "effort") == 0.5
    src = {r["technique"]: r["source"] for r in rows if r["projection"] == "effort"}
    assert src["curator:R-A"].startswith("references/eshop/blind-curation/")
    assert src["curator:R-B"].startswith("out/eshop/blind-curation/")
    # four-row table: the §10a.7 label IS the projection; the consensus scoring is suffixed
    assert val("floor", "mojofm", "detection") == 30.0
    assert val("sar:arc", "mojofm", "detection") == 41.0
    assert val("curator:R-A", "mojofm", "expression") == 66.7
    assert val("curator:R-A", "entities_common", "expression") == 4
    assert val("curator:R-A", "edge_f1", "expression") == 0.667
    assert val("curator:R-A", "mojofm", "expression-vs-consensus") == 66.7
    assert val("floor", "mojofm", "detection-vs-consensus") == 30.0
    # human_ceiling from fidelity.json is NOT re-emitted (ceiling.json is its source)
    assert not [r for r in rows if r["projection"].startswith("ceiling")]
    # every value numeric, and the downstream stats stage is indifferent to the new axis
    assert all(isinstance(r["value"], (int, float)) for r in rows)
    stats = stats_mod.compute_stats([{k: str(v) for k, v in r.items()} for r in rows])
    assert isinstance(stats, dict)


def test_aggregate_rq9_absent_dirs_emit_nothing(tmp_path):
    (tmp_path / "references" / "eshop").mkdir(parents=True)
    rows = [r for r in agg.iter_rows(tmp_path / "out", tmp_path / "references")
            if r["axis"] == "RQ9"]
    assert rows == []


def test_partition_relation_separates_granularity_from_conflict():
    ref = {"a1": "X", "a2": "X", "b1": "Y", "b2": "Y"}
    finer = {"a1": "P", "a2": "Q", "b1": "R", "b2": "R"}
    coarser = {"a1": "M", "a2": "M", "b1": "M", "b2": "M"}
    recut = {"a1": "M", "b1": "M", "a2": "N", "b2": "N"}
    # the eShop curator shape: one cluster swallows a whole ref cluster AND a piece of another
    straddle = {"a1": "C", "a2": "C", "b1": "C", "b2": "D"}
    assert agg._partition_relation(finer, ref) == \
        {"boundary_conflicts": 0, "merges": 0, "splits": 1}
    assert agg._partition_relation(coarser, ref) == \
        {"boundary_conflicts": 0, "merges": 1, "splits": 0}
    assert agg._partition_relation(recut, ref) == \
        {"boundary_conflicts": 2, "merges": 0, "splits": 0}
    assert agg._partition_relation(straddle, ref) == \
        {"boundary_conflicts": 1, "merges": 0, "splits": 0}
    assert agg._partition_relation(ref, ref) == \
        {"boundary_conflicts": 0, "merges": 0, "splits": 0}
    assert agg._partition_relation({"zz": "M"}, ref) == \
        {"boundary_conflicts": 0, "merges": 0, "splits": 0}     # no common entities

import csv  # noqa: E402


# ------------------------------------------------------------------ consensus template

def test_consensus_template_seeds_labels_and_prefills_only_agreement(tmp_path, capsys):
    header = "entity,cluster,notes\n"
    (tmp_path / "ws-R-A.csv").write_text(
        header + "t:a,Core,\nt:b,Core,\nt:c,Web,\nt:t,EXCLUDE,tests\nt:only-a,Tools,\n",
        encoding="utf-8")
    (tmp_path / "ws-R-B.csv").write_text(
        header + "t:a,Core,\nt:b,Data,\nt:c,Web,\nt:t,EXCLUDE,\n", encoding="utf-8")
    out = tmp_path / "worksheet-consensus.csv"
    assert rq9.main(["consensus", str(tmp_path / "ws-R-A.csv"), str(tmp_path / "ws-R-B.csv"),
                     "--names", "R-A,R-B", "--system", "toy", "--out", str(out)]) == 0
    assert "5 entities, 3 pre-filled (agreed), 2 open" in capsys.readouterr().out
    rows = {r[0]: r for r in csv.reader(
        ln for ln in out.read_text(encoding="utf-8").splitlines()
        if ln and not ln.startswith("#") and not ln.startswith("entity,"))}
    assert rows["t:a"][1] == "Core" and rows["t:a"][2] == "R-A=Core | R-B=Core"
    assert rows["t:b"][1] == "" and rows["t:b"][2] == "R-A=Core | R-B=Data"
    assert rows["t:t"][1] == "EXCLUDE"                       # agreed exclusion is a consensus
    assert rows["t:only-a"][1] == "" and rows["t:only-a"][2] == "R-A=Tools | R-B=—"
    # the template is NOT ingestible until every blank is settled (by design) …
    assert rq9.main(["ingest", str(out), "--out", str(tmp_path / "c.rsf")]) == 2
    # … and is once it is
    filled = out.read_text(encoding="utf-8").replace("t:b,,", "t:b,Data,") \
        .replace("t:only-a,,", "t:only-a,EXCLUDE,")
    out.write_text(filled, encoding="utf-8")
    assert rq9.main(["ingest", str(out), "--out", str(tmp_path / "c.rsf")]) == 0
    assert rq9.read_contains(tmp_path / "c.rsf") == {"t:a": "Core", "t:b": "Data",
                                                     "t:c": "Web"}


# ------------------------------------------------------------------ analysis/ RQ9 stats + tables

tables_mod = _import("analysis/make_tables.py")


def _rq9_rows_fixture(system="eshop", second_curator=False):
    """A minimal RQ9 row set in the tidy schema (as aggregate_results emits it);
    ``second_curator`` adds the curator-variance arm (a curator:R-B row set + the
    curator-vs-curator agreement cells of curator-pair.json)."""
    meta = {"system": system, "language": "cs", "granularity": "target", "ref_grade": "GT-doc",
            "ref_clusters": "9", "ref_entities": "19", "tier": "oss", "source": "x"}
    spec = [  # technique, metric, value, projection
        ("floor", "mojofm", 33.33, "detection"), ("floor", "a2a", 80.77, "detection"),
        ("sar:limbo", "mojofm", 26.67, "detection"), ("sar:limbo", "a2a", 85.87, "detection"),
        ("curator:R-A", "mojofm", 86.67, "expression"), ("curator:R-A", "a2a", 95.79, "expression"),
        ("curator:R-A", "edge_f1", 0.629, "expression"), ("curator:R-A", "clusters", 10, "expression"),
        ("curator:R-A", "mojofm", 75.0, "expression-vs-consensus"),
        ("curator:R-A", "a2a", 92.0, "expression-vs-consensus"),
        ("curator:R-A", "edge_f1", 0.553, "expression-vs-consensus"),
        ("ceiling", "mojofm", 66.52, "rater-pair"), ("ceiling", "a2a", 89.69, "rater-pair"),
        ("ceiling", "kappa", 0.0, "rater-pair"), ("ceiling", "kappa_flag", 1, "rater-pair"),
        # the label-invariant companions to the pre-registered kappa (§20 residual)
        ("ceiling", "kappa_aligned", 0.702, "rater-pair"), ("ceiling", "ari", 0.527, "rater-pair"),
        ("ceiling", "pair_agreement", 0.918, "rater-pair"),
        ("ceiling", "labels_shared", 0, "rater-pair"),
        ("rater:R-C", "boundary_conflicts", 0, "vs-reference"),
        ("rater:R-D", "boundary_conflicts", 0, "vs-reference"),
        ("consensus", "clusters", 14, "partition"),
        ("curator:R-A", "wall_clock_minutes_reported", 43.0, "effort"),
        ("curator:R-A", "ops_total", 43, "effort"), ("curator:R-A", "rule_lines", 63, "effort"),
        ("curator:R-A", "groups", 11, "effort"),
        ("curator:R-A", "final_groups_propose_derived", 0, "effort"),
        ("curator:R-A", "propose_derived_fraction", 0.0, "effort"),
    ]
    if second_curator:
        spec += [
            ("curator:R-B", "mojofm", 80.0, "expression"), ("curator:R-B", "a2a", 93.0, "expression"),
            ("curator:R-B", "edge_f1", 0.6, "expression"), ("curator:R-B", "clusters", 9, "expression"),
            ("curator:R-B", "wall_clock_minutes_reported", 55.0, "effort"),
            ("curator:R-B", "ops_total", 51, "effort"), ("curator:R-B", "rule_lines", 58, "effort"),
            ("curator:R-B", "groups", 9, "effort"),
            ("curator:R-B", "final_groups_propose_derived", 0, "effort"),
            ("curator:R-B", "propose_derived_fraction", 0.0, "effort"),
            ("curator-pair", "mojofm", 78.5, "curator-pair"), ("curator-pair", "a2a", 94.0, "curator-pair"),
            ("curator-pair", "ari", 0.61, "curator-pair"),
            ("curator-pair", "kappa_aligned", 0.7, "curator-pair"),
        ]
    rows = [{**meta, "axis": "RQ9", "technique": t, "metric": m, "value": str(v),
             "projection": p} for t, m, v, p in spec]
    # the RQ1 pipeline row (the earlier NON-blind curation) — the §10a.6 leakage comparator
    rows.append({**meta, "axis": "fidelity", "technique": "pipeline", "metric": "mojofm",
                 "value": "66.67", "projection": "all-common"})
    rows.append({**meta, "axis": "fidelity", "technique": "pipeline", "metric": "a2a",
                 "value": "88.9", "projection": "all-common"})
    return rows


def test_stats_rq9_block_and_macros():
    stats = stats_mod.compute_stats(_rq9_rows_fixture())
    d = stats["rq9"]["by_system"]["eshop"]
    assert d["blind_curator"]["mojofm"] == 86.67 and d["best_sar"]["technique"] == "limbo"
    assert d["curator_minus_raters_mojofm"] == 20.15      # curator above the R1-vs-R2 agreement
    assert d["curator_over_floor_mojofm"] == 53.34
    assert d["rater_boundary_conflicts"] == 0
    assert d["effort"]["minutes"] == 43.0 and d["consensus_clusters"] == 14
    assert d["blind_curator_vs_consensus"]["mojofm"] == 75.0
    assert stats["rq9"]["all_curators_propose_derived_fraction_zero"] is True
    m = stats["summary_macros"]
    assert m["rqNineSystems"] == 1
    assert m["rqNineEshopCuratorMoJo"] == "86.67" and m["rqNineEshopBestSarName"] == "LIMBO"
    assert m["rqNineEshopCuratorEdgeFone"] == "0.629"   # no digits in macro names
    assert m["rqNineEshopProposeDerivedPct"] == "0"
    assert m["rqNineEshopRaterConflicts"] == "0"
    assert m["rqNineEshopCuratorVsConsensusMoJo"] == "75.0"
    # §10a.6 leakage: blind − non-blind, from the RQ1 pipeline row
    assert m["rqNineEshopNonBlindMoJo"] == "66.67" and m["rqNineEshopLeakageDelta"] == "20.0"
    assert not any(ch.isdigit() for name in m if name.startswith("rqNine") for ch in name)


def test_second_curator_reported_beside_primary_never_in_its_place():
    """The curator-variance arm (response plan 1.5b): a second curator on a system lands under
    `curators` / `curator_variance` and its own table rows; the pre-registered curator
    (stats.RQ9_PRIMARY_CURATOR) keeps every headline number even where the second scores
    differently; a single-curator system shows none of it."""
    rows = _rq9_rows_fixture(second_curator=True)
    stats = stats_mod.compute_stats(rows)
    d = stats["rq9"]["by_system"]["eshop"]
    assert d["curator_code"] == "R-A" and d["blind_curator"]["mojofm"] == 86.67
    assert d["effort"]["minutes"] == 43.0                      # the primary's effort, still
    assert sorted(d["curators"]) == ["R-A", "R-B"] and d["curators"]["R-B"]["mojofm"] == 80.0
    cv = d["curator_variance"]
    assert cv["n_curators"] == 2 and cv["primary"] == "R-A"
    assert cv["mojofm"]["spread"] == 6.67 and cv["minutes"]["spread"] == 12.0
    assert cv["pair"]["ari"] == 0.61 and cv["pair"]["nmi"] is None
    assert stats["rq9"]["systems_with_two_curators"] == ["eshop"]
    assert stats["rq9"]["all_curators_propose_derived_fraction_zero"] is True
    m = stats["summary_macros"]
    assert m["rqNineEshopCuratorCode"] == "R-A" and m["rqNineEshopCuratorCount"] == 2
    assert m["rqNineEshopSecondCuratorCode"] == "R-B"
    assert m["rqNineEshopSecondCuratorMoJo"] == "80.0" and m["rqNineEshopSecondCuratorMinutes"] == "55"
    assert m["rqNineEshopCuratorMoJoSpread"] == "6.67" and m["rqNineEshopCuratorPairMoJo"] == "78.5"
    assert m["rqNineEshopCuratorPairNmi"] == "TBD"           # step 5c ran without nmi here
    assert m["rqNineSystemsWithTwoCurators"] == 1 and m["rqNineTwoCuratorSystemsList"] == "eShop"
    assert not any(ch.isdigit() for name in m if name.startswith("rqNine") for ch in name)
    tex = tables_mod.build_table("rq9_curation", rows)
    assert "\\textbf{blind curator} & expr. & \\textbf{86.7} & \\textbf{95.8} & 0.63" in tex
    assert "\\quad second blind curator (R-B) & expr. & 80.0 & 93.0 & 0.60" in tex
    assert "\\quad curator vs.\\ curator agreement & agr. & 78.5 & 94.0 & ARI\\,0.61" in tex
    eff = tables_mod.build_table("rq9_effort", rows)
    assert "eShop & 43 & 43 & 63 & 11 & 0/11 & 0.00 & 0.70 & 0.53 & -- & 0 & 14" in eff
    assert "\\quad R-B & 55 & 51 & 58 & 9 & 0/9 & -- & -- & -- & -- & -- & --" in eff
    # a single-curator system: no variance block, no second-curator macros, no extra rows
    single = stats_mod.compute_stats(_rq9_rows_fixture())
    assert single["rq9"]["by_system"]["eshop"]["curator_variance"] is None
    assert single["rq9"]["systems_with_two_curators"] == []
    assert "rqNineEshopSecondCuratorMoJo" not in single["summary_macros"]
    assert single["summary_macros"]["rqNineTwoCuratorSystemsList"] == "none"
    assert "second blind curator" not in tables_mod.build_table("rq9_curation", _rq9_rows_fixture())


def test_aggregate_rq9_curator_pair_rows(tmp_path):
    """curator-pair.json (runbook §8 step 5c: the `ceiling` subcommand over two curators'
    cluster RSFs) is ingested as technique `curator-pair`, projection `curator-pair`, and
    emits no rater rows."""
    refs, out = tmp_path / "references", tmp_path / "out"
    bc = refs / "eshop" / "blind-curation"
    bc.mkdir(parents=True)
    (bc / "curator-pair.json").write_text(json.dumps({
        "raters": {"a": "R-A", "b": "R-B"},
        "agreement": {"kappa": 0.3, "mojofm_mean": 78.5, "mojofm_ab": 80.0, "mojofm_ba": 77.0,
                      "a2a": 94.0, "ari": 0.61, "kappa_aligned": 0.7, "n_common": 19}}),
        encoding="utf-8")
    meta = {"system": "eshop", "language": "cs", "granularity": "target", "ref_grade": "GT-doc",
            "ref_clusters": "9", "ref_entities": "19", "tier": "oss"}
    rows = list(agg._rq9_rows(out, refs, "eshop", meta))
    pair = {r["metric"]: r["value"] for r in rows if r["technique"] == "curator-pair"}
    assert pair["mojofm"] == 78.5 and pair["ari"] == 0.61 and pair["kappa"] == 0.3
    assert pair["entities_common"] == 19 and "kappa_flag" not in pair
    assert all(r["projection"] == "curator-pair" for r in rows if r["technique"] == "curator-pair")
    assert not any(r["technique"].startswith(("rater:", "ceiling")) for r in rows)
    assert all(r["source"].startswith("references/eshop/blind-curation/") for r in rows)


def test_tables_rq9_render_labels_and_sensitivity_row():
    rows = _rq9_rows_fixture()
    tex = tables_mod.build_table("rq9_curation", rows)
    assert "placeholder" not in tex and "\\label{tab:rq9}" in tex
    assert "eShop & no-curation floor & det. & 33.3 & 80.8 & --" in tex
    assert "best SAR (LIMBO) & det. & 26.7" in tex  # label scoped to ACDC/WCA/LIMBO (review 2026-09-13)
    assert "\\textbf{blind curator} & expr. & \\textbf{86.7} & \\textbf{95.8} & 0.63" in tex
    assert "vs.\\ consensus & expr. & 75.0 & 92.0 & 0.55" in tex
    assert "R$_1$ vs.\\ R$_2$ agreement & agr. & 66.5 & 89.7 & $\\kappa$\\,0.00; 0\\,confl." in tex
    eff = tables_mod.build_table("rq9_effort", rows)
    assert "\\label{tab:rq9effort}" in eff
    # effort columns, then the ceiling-structure block: raw kappa | aligned kappa | ARI
    assert "eShop & 43 & 43 & 63 & 11 & 0/11 & 0.00 & 0.70 & 0.53 & -- & 0 & 14" in eff  # -- = no nmi in the synthetic rows
    assert "$\\kappa_{\\mathrm{al}}$" in eff and "ARI" in eff
    # no RQ9 rows -> honest placeholder, never an empty tabular
    assert "placeholder" in tables_mod.build_table("rq9_curation", rows[-2:])
