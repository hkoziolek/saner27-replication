"""Tier-P kit tests (eval §14.1/§14.3): the anonymized export channel + the offline bundle.

The load-bearing guarantees under test:
  * anonymize: real system ids NEVER appear in the export; whitelist drops are visible;
    a forbidden-token hit REFUSES the whole export; output is byte-deterministic.
  * make_bundle: self-contained deterministic zip whose manifest hashes match its contents
    (built --without-jar so CI needs no gitignored tools/arcade download).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "baselines"))   # make_comparison imports run_arcade as a sibling


def _import(rel: str):
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


anon = _import("analysis/anonymize_results.py")
bundle = _import("baselines/make_bundle.py")
agg = _import("analysis/aggregate_results.py")
tables = _import("analysis/make_tables.py")
stats_mod = _import("analysis/stats.py")
mc = _import("baselines/make_comparison.py")
det = _import("baselines/determinism_check.py")


# ------------------------------------------------------------------ fixtures

REAL_ID = "secretsys"   # must never appear in any export


def make_tree(root: Path, *, rung_extra: str | None = None) -> tuple[Path, Path, Path]:
    """A miniature out/ + references/ tree for one fake proprietary system."""
    base = root / "out" / REAL_ID / "baseline"
    (base / "acdc-target").mkdir(parents=True)
    (base / "acdc-target" / "fidelity.json").write_text(json.dumps({
        "system": REAL_ID, "technique": "acdc", "granularity": "target",
        "clusters": 3, "entities_clustered": 20,
        "vs_reference": {"mojofm": 41.5, "a2a": 80.25, "entities_common": 19,
                         "entities_recovered": 20, "entities_against": 19},
    }), encoding="utf-8", newline="")
    (base / "comparison-target.json").write_text(json.dumps({
        "system": REAL_ID, "granularity": "target", "shared_entities": 19,
        "results": {"pipeline": {"mojofm": 70.0, "a2a": 90.0, "clusters": 9},
                    "acdc": {"mojofm": 6.7, "a2a": 75.9, "clusters": 2}},
    }), encoding="utf-8", newline="")
    rungs = {"none": {"mojofm": 30.0, "a2a": 70.0, "clusters": 19},
             "hand_curation": {"mojofm": 66.7, "a2a": 88.9, "clusters": 14}}
    if rung_extra:
        rungs[rung_extra] = {"mojofm": 1.0, "a2a": 2.0, "clusters": 3}
    (base / "a2-ablation.json").write_text(json.dumps({
        "system": REAL_ID, "granularity": "target", "shared_entities": 19, "rungs": rungs,
    }), encoding="utf-8", newline="")
    (base / "stability-churn.json").write_text(json.dumps({
        "mode": "perturb",
        "techniques": {"anon": {"0.20": {"churn_mojofm": 100.0, "survivors_scored": 18}},
                       "acdc": {"0.20": {"churn_mojofm": 86.67, "survivors_scored": 18}}},
    }), encoding="utf-8", newline="")
    (base / "determinism.json").write_text(json.dumps({
        "runs": 2, "identical": True, "identical_rate": 100.0,
        "facts": {"extracted-facts.json": {"equal": True}}, "dsl": {"equal": True},
    }), encoding="utf-8", newline="")
    gen = root / "out" / REAL_ID / "architecture" / "generated"
    gen.mkdir(parents=True)
    (gen / "run-report.json").write_text(json.dumps({
        "stages": {"extract": 12.5, "curate": 0.3},
        "trust": {"coverage_pct": {"L0": 95.0}, "mean_confidence": 0.82,
                  "unmapped_targets": 2, "abstained_targets": 0, "edges_dropped": 1},
    }), encoding="utf-8", newline="")
    perf = root / "out" / REAL_ID / "perf"
    perf.mkdir(parents=True)
    (perf / "rq7-perf.json").write_text(json.dumps({
        "schema": "rq7-perf/1", "system": REAL_ID, "success": True,
        "size": {"kloc": 320.0, "targets": 41, "files": 730},
        "cold": {"wall_s": 110.6, "peak_rss_mb": 1830},
        "warm": {"wall_s": 6.4, "peak_rss_mb": 900},
        "speedup": 17.28,
        "budget": {"cold_within": True, "warm_within": True},
    }), encoding="utf-8", newline="")
    refs = root / "references" / REAL_ID
    refs.mkdir(parents=True)
    (refs / "provenance.json").write_text(json.dumps({
        "grade": "GT-diag", "clusters": 9, "entities": 19, "kappa": 0.74,
        "source": "manually crafted inception diagram of SecretSys by its architects",
    }), encoding="utf-8", newline="")
    (refs / "diagram-delta.json").write_text(json.dumps({
        "added": ["SecretSys.NewComponent", "SecretSys.Other"], "retired": [],
        "reassigned": ["SecretSys.Moved"],
    }), encoding="utf-8", newline="")
    mapping = root / "tier-p-map.json"
    mapping.write_text(json.dumps({
        "systems": {REAL_ID: {"code": "P-7", "language": "cs",
                              "granularity": "target", "kloc": 320}},
        "forbid": ["SecretSys"],
    }), encoding="utf-8", newline="")
    return root / "out", root / "references", mapping


def run_anon(tmp: Path, mapping: Path) -> int:
    return anon.main(["--map", str(mapping), "--out-root", str(tmp / "out"),
                      "--refs", str(tmp / "references"),
                      "--export-dir", str(tmp / "export-anon")])


# ------------------------------------------------------------------ anonymize

def test_anonymize_happy_path_no_leak(tmp_path):
    _, _, mapping = make_tree(tmp_path)
    assert run_anon(tmp_path, mapping) == 0
    csv_text = (tmp_path / "export-anon" / "results-anon.csv").read_text(encoding="utf-8")
    man_text = (tmp_path / "export-anon" / "manifest-anon.json").read_text(encoding="utf-8")
    combined = (csv_text + man_text).casefold()
    # The core §14.3 guarantee: the real id appears NOWHERE in the export.
    assert REAL_ID not in combined and "secretsys.newcomponent" not in combined
    assert "P-7" in csv_text
    # Metric rows made it through in each axis.
    for needle in ("fidelity,acdc,mojofm,41.5", "A2,hand_curation,mojofm,66.7",
                   "A8,acdc@0.20,churn_mojofm,86.67", "perf,extract,wall_clock_s,12.5",
                   "RQ7,pipeline,cold_s,110.6", "RQ7,pipeline,kloc,320.0",
                   "coverage,pipeline,coverage_pct_L0,95.0",
                   "RQ5,pipeline,identical_rate,100.0"):
        assert any(needle in line for line in csv_text.splitlines()), needle
    man = json.loads(man_text)
    sys7 = man["systems"][0]
    assert sys7["code"] == "P-7" and sys7["ref_grade"] == "GT-diag"
    assert sys7["kappa"] == 0.74
    assert "determinism" in sys7["artifacts_present"]
    # Delta ledger crosses as COUNTS only.
    assert sys7["diagram_delta_counts"] == {"added": 2, "retired": 0, "reassigned": 1}
    # Size fields cross as buckets, never exact.
    assert sys7["ref_entities_bucket"] == "10-25"
    assert sys7["kloc_bucket"] == "250-500"
    assert '"entities": 19' not in man_text


def test_anonymize_is_deterministic(tmp_path):
    _, _, mapping = make_tree(tmp_path)
    assert run_anon(tmp_path, mapping) == 0
    first = (tmp_path / "export-anon" / "results-anon.csv").read_bytes()
    first_man = (tmp_path / "export-anon" / "manifest-anon.json").read_bytes()
    assert run_anon(tmp_path, mapping) == 0
    assert (tmp_path / "export-anon" / "results-anon.csv").read_bytes() == first
    assert (tmp_path / "export-anon" / "manifest-anon.json").read_bytes() == first_man


def test_anonymize_drops_unknown_rung_but_exports(tmp_path, capsys):
    _, _, mapping = make_tree(tmp_path, rung_extra="totally_custom_rung")
    assert run_anon(tmp_path, mapping) == 0
    csv_text = (tmp_path / "export-anon" / "results-anon.csv").read_text(encoding="utf-8")
    assert "totally_custom_rung" not in csv_text          # dropped, never crossed
    assert "hand_curation" in csv_text                     # the rest still exported
    assert "totally_custom_rung" in capsys.readouterr().err   # visible, not silent


def test_anonymize_refuses_on_forbidden_token_hit(tmp_path):
    _, _, mapping = make_tree(tmp_path)
    m = json.loads(mapping.read_text(encoding="utf-8"))
    m["forbid"].append("acdc")   # collides with a legit enum -> scan must refuse everything
    mapping.write_text(json.dumps(m), encoding="utf-8", newline="")
    assert run_anon(tmp_path, mapping) == 2
    assert not (tmp_path / "export-anon" / "results-anon.csv").exists()


def test_anonymize_rejects_bad_map(tmp_path):
    _, _, mapping = make_tree(tmp_path)
    m = json.loads(mapping.read_text(encoding="utf-8"))
    m["systems"][REAL_ID]["code"] = "P-1 (SecretSys)"     # not a bare P-code
    mapping.write_text(json.dumps(m), encoding="utf-8", newline="")
    assert run_anon(tmp_path, mapping) == 2
    assert not (tmp_path / "export-anon").exists()


def test_bucket_edges():
    assert anon.bucket(7, anon.SIZE_EDGES) == "<10"
    assert anon.bucket(10, anon.SIZE_EDGES) == "10-25"
    assert anon.bucket(249, anon.SIZE_EDGES) == "100-250"
    assert anon.bucket(99999, anon.SIZE_EDGES) == ">=25000"


# ------------------------------------------- aggregate --anon -> tables/stats (§14.1/§14.3)

def test_anon_rows_flow_into_tables_marked_and_stats_firewalled(tmp_path):
    _, _, mapping = make_tree(tmp_path)
    assert run_anon(tmp_path, mapping) == 0
    results = tmp_path / "results.csv"
    # Empty OSS tree on purpose: results.csv then carries ONLY the ingested Tier-P rows.
    assert agg.main(["--out-root", str(tmp_path / "empty-out"),
                     "--refs", str(tmp_path / "empty-refs"), "--csv", str(results),
                     "--anon", str(tmp_path / "export-anon" / "results-anon.csv")]) == 0
    text = results.read_text(encoding="utf-8")
    assert REAL_ID not in text.casefold()                     # the guarantee survives the merge
    lines = text.splitlines()
    assert lines[0].endswith(",tier")
    assert all(line.startswith("P-7,") and line.endswith(",P") for line in lines[1:])

    outdir = tmp_path / "tables"
    assert tables.main(["--results", str(results), "--out", str(outdir),
                        "--only", "corpus", "rq1_fidelity", "rq2_baselines"]) == 0
    # corpus.tex is a placeholder since 2026-08-30: the corpus columns live in the merged
    # rq1_fidelity.tex table* (labels tab:corpus + tab:rq1 + tab:rq3, page budget)
    rq1 = (outdir / "rq1_fidelity.tex").read_text(encoding="utf-8")
    assert "P-7$^\\ddagger$" in rq1                           # marked, not blended
    assert "non-replicable" in rq1 and "GT-diag" in rq1
    assert "10-25" in rq1                                     # entity count stays a bucket
    assert "70.0" in (outdir / "rq2_baselines.tex").read_text(encoding="utf-8")

    st = stats_mod.compute_stats(stats_mod.load(results))
    # §14.3 firewall: Tier-P rows are counted apart and excluded from every RQ statistic.
    assert st["tier_p"]["systems"] == ["P-7"]
    assert st["tier_p"]["pipeline_a2a_by_system"] == {"P-7": 90.0}
    assert st["summary_macros"]["tierPSystemCount"] == 1
    assert st["corpus"]["n_systems"] == 0                     # no OSS rows in this tree
    assert st["rq1"]["median_a2a"] is None                    # P-7's 90.0 did NOT leak in


def test_tables_without_tier_p_carry_no_marker(tmp_path):
    rows = [{"system": "eshop", "language": "cs", "granularity": "target",
             "ref_grade": "GT-doc", "ref_clusters": "9", "ref_entities": "19",
             "axis": "fidelity", "technique": "pipeline", "metric": "a2a",
             "value": "88.89", "projection": "all-common",
             "source": "comparison-target.json", "tier": "oss"}]
    tex = tables.t_corpus(rows) + tables.t_rq1(rows)
    assert "\\ddagger" not in tex and "non-replicable" not in tex


# ------------------------------------------------------------------ make_comparison

def _rsf(path: Path, member_cluster: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"contain {c} {m}\n" for m, c in sorted(member_cluster.items())),
                    encoding="utf-8", newline="")


def test_make_comparison_all_common_projection(tmp_path, monkeypatch, capsys):
    # Stub the jar-backed metrics: this tests the projection/consolidation, not ARCADE.
    monkeypatch.setattr(mc, "mojofm", lambda a, b: 55.5)
    monkeypatch.setattr(mc, "a2a", lambda a, b: 66.6)
    _rsf(tmp_path / "out" / "sys" / "architecture" / "generated" / "graph" / "clusters-target.rsf",
         {"a": "c1", "b": "c1", "c": "c2", "pipeonly": "c3"})
    base = tmp_path / "out" / "sys" / "baseline"
    _rsf(base / "acdc-target" / "clusters.rsf", {"a": "x", "b": "y", "c": "y"})
    ref = tmp_path / "reference.rsf"
    _rsf(ref, {"a": "R1", "b": "R1", "c": "R2", "refonly": "R2"})

    argv = ["sys", "--granularity", "target", "--reference", str(ref),
            "--out-root", str(tmp_path / "out"), "--techniques", "acdc,dir"]
    assert mc.main(argv) == 0
    comp = json.loads((base / "comparison-target.json").read_text(encoding="utf-8"))
    assert comp["shared_entities"] == 3                       # pipeonly/refonly projected out
    assert set(comp["results"]) == {"pipeline", "acdc"}       # dir absent -> skipped ...
    assert "dir" in capsys.readouterr().err                   # ... but visibly, never silently
    # ari / nested (2026-09-09, eval plan section 12 (m)) are pure functions of the projected
    # pair: on this 3-entity fixture the two acdc clusters split one reference pair (ARI -50)
    # and one of the two nests inside a reference cluster (50 %).
    assert comp["results"]["acdc"] == {"mojofm": 55.5, "a2a": 66.6, "ari": -50.0,
                                       "nested": 50.0, "clusters": 2}
    assert comp["results"]["pipeline"]["clusters"] == 2       # c3 vanished with pipeonly

    first = (base / "comparison-target.json").read_bytes()
    assert mc.main(argv) == 0
    assert (base / "comparison-target.json").read_bytes() == first   # byte-deterministic


def test_make_comparison_errors_without_pipeline_clusters(tmp_path, capsys):
    ref = tmp_path / "reference.rsf"
    _rsf(ref, {"a": "R1"})
    assert mc.main(["sys", "--granularity", "target", "--reference", str(ref),
                    "--out-root", str(tmp_path / "out")]) == 2
    assert "arch export --graph" in capsys.readouterr().err


# ------------------------------------------------------------------ determinism_check

def _arch_dir(root: Path, name: str, *, commit: str, extra_target: str | None = None,
              dsl: bytes = b"model {}\n") -> Path:
    gen = root / name / "generated"
    gen.mkdir(parents=True)
    targets = [{"id": "t1", "depends_on": []}]
    if extra_target:
        targets.append({"id": extra_target, "depends_on": []})
    for f in ("extracted-facts.json", "curated-facts.json"):
        (gen / f).write_text(json.dumps({
            "schema_version": "1.0.0", "targets": targets, "relationships": [],
            "provenance": {"generated_at": f"2026-07-13T00:00:{commit}", "commit": commit},
        }), encoding="utf-8", newline="")
    (gen / "generated-model.dsl").write_bytes(dsl)
    return root / name


def test_determinism_check_ignores_volatile_provenance(tmp_path):
    a = _arch_dir(tmp_path, "run-a", commit="aaa")
    b = _arch_dir(tmp_path, "run-b", commit="bbb")   # only volatile provenance differs
    out = tmp_path / "determinism.json"
    assert det.main([str(a), str(b), "--out", str(out)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["identical"] is True and rep["identical_rate"] == 100.0
    assert rep["facts"]["extracted-facts.json"]["equal"] is True


def test_determinism_check_flags_real_divergence(tmp_path):
    a = _arch_dir(tmp_path, "run-a", commit="aaa")
    out = tmp_path / "determinism.json"

    b = _arch_dir(tmp_path, "run-b", commit="aaa", extra_target="t2")   # structural change
    assert det.main([str(a), str(b), "--out", str(out)]) == 1
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["identical"] is False and rep["identical_rate"] == 0.0

    c = _arch_dir(tmp_path, "run-c", commit="aaa", dsl=b"model { changed }\n")
    assert det.main([str(a), str(c), "--out", str(out)]) == 1
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["dsl"]["mismatched"] == ["generated-model.dsl"]


# ------------------------------------------------------------------ make_bundle

def test_bundle_without_jar_is_complete_and_deterministic(tmp_path):
    out1, out2 = tmp_path / "kit1.zip", tmp_path / "kit2.zip"
    bundle.build(out1, without_jar=True)
    bundle.build(out2, without_jar=True)
    assert out1.read_bytes() == out2.read_bytes()          # deterministic archive

    with zipfile.ZipFile(out1) as zf:
        names = {n.removeprefix("anon-baseline-kit/") for n in zf.namelist()}
        for required in ("README.md", "BUNDLE-MANIFEST.json", "sha256sums.txt",
                         "baselines/run_arcade.py", "baselines/arc/ArcRunner.java",
                         "baselines/arc/StructuralRunner.java",
                         "baselines/make_comparison.py", "baselines/determinism_check.py",
                         "baselines/no_curation_floor.py", "baselines/a2_ablation.py",
                         "baselines/stability_churn.py",
                         "analysis/anonymize_results.py", "docs/tier-p-baseline-kit.md",
                         "docs/tier-p-org-runbook.md",
                         "smoke/architecture/generated/graph/graph-file.rsf",
                         "references/smoke/reference.rsf"):
            assert required in names, required
        assert "tools/arcade/ARCADE_Core.jar" not in names   # --without-jar honored

        manifest = json.loads(zf.read("anon-baseline-kit/BUNDLE-MANIFEST.json"))
        assert manifest["with_jar"] is False
        assert manifest["arcade_url"].startswith("https://github.com/usc-softarch/")
        # Manifest hashes match the actual zip contents (transfer-integrity contract).
        for name, meta in manifest["files"].items():
            data = zf.read(f"anon-baseline-kit/{name}")
            assert bundle.sha256(data) == meta["sha256"], name
            assert len(data) == meta["bytes"], name
        # The smoke reference is directory-aligned with the smoke graph by construction.
        graph = zf.read("anon-baseline-kit/smoke/architecture/generated/graph/"
                        "graph-file.rsf").decode()
        ref = zf.read("anon-baseline-kit/references/smoke/reference.rsf").decode()
        graph_nodes = {tok for line in graph.splitlines() for tok in line.split()[1:]}
        ref_nodes = {line.split()[2] for line in ref.splitlines() if line}
        assert graph_nodes == ref_nodes


def test_bundle_with_jar_when_available(tmp_path):
    if not bundle.ARCADE_JAR.exists():
        pytest.skip("tools/arcade/ARCADE_Core.jar not downloaded (gitignored, §22.2)")
    out = tmp_path / "kit.zip"
    bundle.build(out, without_jar=False)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "anon-baseline-kit/tools/arcade/ARCADE_Core.jar" in names
        manifest = json.loads(zf.read("anon-baseline-kit/BUNDLE-MANIFEST.json"))
        assert manifest["with_jar"] is True
        assert manifest["files"]["tools/arcade/ARCADE_Core.jar"]["bytes"] > 1_000_000
