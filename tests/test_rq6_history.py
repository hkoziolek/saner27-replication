"""RQ6 real-history mini-arm (eval §8.3, ``mutate/history.py``) tests.

Covers: the first-party L0 scope counting on a synthetic ``diff_facts`` dict (targets,
edges, renames, coverage gaps, new layering violations), the rater worksheet writer
(comment row, header, deterministic ids, empty label/note, round-trip, identical rater
copies), the build-file classifier / owner-file mapping / commit-log parsing (no git
needed), and a smoke run of the real L0-scoped pipeline with the toy fixture as both A
and B (identical → zero findings, zero worksheet rows).
"""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mutate import history  # noqa: E402


def _import(rel: str):
    import importlib.util
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ synthetic facts

def _t(i, external=False, path="", tags=()):
    return {"id": i, "name": i.rsplit(":", 1)[-1], "external": external, "path": path,
            "tags": sorted(tags)}


def _r(s, t, kinds):
    return {"id": f"rel:{s}->{t}", "source": s, "target": t,
            "evidence": [{"type": k, "detail": k} for k in kinds]}


def _facts(targets, rels):
    return {"schema_version": "1.0.0", "targets": targets, "relationships": rels,
            "provenance": {"coverage": {t["id"]: {"L0": True} for t in targets}}}


A = "csharp:csproj:src/A/A.csproj"
B = "csharp:csproj:src/B/B.csproj"
C = "csharp:csproj:src/C/C.csproj"
D = "csharp:csproj:src/D/D.csproj"
NUGET = "nuget:Newtonsoft.Json"
CPP = "cpp:target:xmllint"


def test_scope_findings_restricts_to_first_party_l0():
    facts_a = _facts([_t(A), _t(B), _t(C), _t(NUGET, external=True),
                      _t("cpp:target:old", path="example")],
                     [_r(A, B, ["project_ref"]), _r(A, C, ["symbol_use"]),
                      _r(A, NUGET, ["package_ref"])])
    facts_b = _facts([_t(A), _t(B), _t(D, tags=["needs-curation"]), _t(NUGET, external=True),
                      _t("cpp:target:new", path="example")],
                     [_r(A, B, ["project_ref"]), _r(A, D, ["project_ref"]),
                      _r(B, D, ["interop"]), _r(A, NUGET, ["package_ref"])])
    diff = {
        "new_targets": [D, "cpp:target:new"],
        "removed_targets": [C, "cpp:target:old"],
        "new_edges": [[A, D], [B, D], [A, NUGET]],              # L0 / interop / external
        "removed_edges": [[A, C], [A, NUGET]],                  # L2-only / external
        "coverage_gap_targets": ["cpp:target:gone", NUGET],
        "coverage_gap_edges": [[A, "cpp:target:old"], [A, "zzz:not-in-scope"]],
        "suspected_renames": [{"old": "cpp:target:old", "new": "cpp:target:new"}],
    }
    layering_a = [{"from": "X", "to": "Y", "status": "VIOLATION"}]
    layering_b = [{"from": "X", "to": "Y", "status": "VIOLATION"},
                  {"from": "P", "to": "Q", "status": "VIOLATION"},
                  {"from": "R", "to": "S", "status": "OK"}]
    sc = history.scope_findings(diff, facts_a, facts_b, layering_a, layering_b)
    f, c = sc["findings"], sc["counts"]
    assert f["new_targets"] == ["cpp:target:new", D]          # sorted (cpp: < csharp:)
    assert f["removed_targets"] == ["cpp:target:old", C]
    assert f["new_edges"] == [[A, D]]                # interop + external edges dropped
    assert f["removed_edges"] == []                  # symbol_use-only edge is not L0
    assert f["suspected_renames"] == [["cpp:target:old", "cpp:target:new"]]
    assert f["coverage_gap_targets"] == []           # neither id is first-party in A∪B
    assert f["coverage_gap_edges"] == [[A, "cpp:target:old"]]
    assert f["layering_violations"] == [["P", "Q"]]  # only the violation NEW in B
    assert c["new_targets"] == 2 and c["removed_targets"] == 2
    assert c["new_edges"] == 1 and c["removed_edges"] == 0
    assert c["drift_total"] == 2 + 2 + 1 + 0 + 1
    assert sc["out_of_scope"] == {"new_edges_non_l0_or_external": 2,
                                  "removed_edges_non_l0_or_external": 2}
    assert history.needs_curation_ids(facts_b) == [D]
    assert history.needs_curation_ids(facts_a) == []


# ------------------------------------------------------------------ worksheet

def test_worksheet_rows_ids_and_roundtrip(tmp_path):
    findings = {"new_targets": [D], "removed_targets": [C], "new_edges": [[A, D]],
                "removed_edges": [], "suspected_renames": [["cpp:target:old", "cpp:target:new"]],
                "coverage_gap_targets": [], "coverage_gap_edges": [],
                "layering_violations": [["P", "Q"]]}
    rows = history.finding_rows("1.0__2.0", findings)
    assert [r["finding_id"] for r in rows] == [f"1.0__2.0-00{i}" for i in range(1, 6)]
    assert [r["kind"] for r in rows] == ["new_target", "removed_target", "new_edge",
                                         "suspected_rename", "layering_violation"]
    assert rows[2]["subject"] == A and rows[2]["counterpart"] == D
    assert rows[0]["counterpart"] == ""
    assert all(r["label"] == "" and r["note"] == "" for r in rows)
    rows[0]["hint"] = 'abc1234 Add D, "quoted" subject'
    text = history.worksheet_text("toy", "1.0", "2.0", rows)
    lines = text.split("\n")
    assert lines[0].startswith("#") and all(lbl in lines[0] for lbl in history.LABELS)
    assert lines[1] == ",".join(history.WORKSHEET_COLUMNS)
    assert "\r" not in text and text.endswith("\n")
    # deterministic: same rows → same bytes
    assert history.worksheet_text("toy", "1.0", "2.0", rows) == text
    history.write_worksheets(tmp_path, text)
    for name in ("worksheet.csv", "worksheet-R1.csv", "worksheet-R2.csv"):
        assert (tmp_path / name).read_bytes() == text.encode("utf-8")
    back = history.read_worksheet(tmp_path / "worksheet-R1.csv")
    assert [r["finding_id"] for r in back] == [r["finding_id"] for r in rows]
    assert back[0]["hint"] == 'abc1234 Add D, "quoted" subject'
    assert list(back[0].keys()) == list(history.WORKSHEET_COLUMNS)
    # plain csv readers see the comment row as a one-column row — the doc'd contract
    first = next(csv.reader(io.StringIO(text)))
    assert first[0].startswith("#")


def test_build_file_classifier_and_owner_mapping(tmp_path):
    for p in ("CMakeLists.txt", "x/y/CMakeLists.txt", "cmake/Foo.cmake", "src/A/A.csproj",
              "Nop.sln", "backend/Squidex.slnx", "Directory.Build.props", "Makefile.am",
              "python/Makefile.in", "configure.ac", "meson.build"):
        assert history.is_build_file(p), p
    for p in ("src/A/Program.cs", "README.md", "Makefile", "configure", "a.cmake.in",
              "Directory.Build.targets"):
        assert not history.is_build_file(p), p

    facts_a = _facts([_t(CPP, path=""), _t("cpp:target:gjobread", path="example")], [])
    facts_b = _facts([_t(CPP, path="")], [])
    repo = tmp_path / "repo"
    (repo / "example").mkdir(parents=True)
    (repo / "Makefile.am").write_text("", encoding="utf-8")
    (repo / "configure.ac").write_text("", encoding="utf-8")
    (repo / "example" / "Makefile.am").write_text("", encoding="utf-8")
    assert history.owner_build_files(A, facts_a, facts_b, repo, repo) == ["src/A/A.csproj"]
    assert history.owner_build_files(CPP, facts_a, facts_b, repo, repo) == ["Makefile.am"]
    # removed target: its path is only known from A's facts
    assert history.owner_build_files("cpp:target:gjobread", facts_a, facts_b, repo, repo) \
        == ["example/Makefile.am"]
    # unknown id → the top-level build files
    assert history.owner_build_files("cpp:target:nowhere", facts_a, facts_b, repo, repo) \
        == ["Makefile.am", "configure.ac"]
    assert history.top_level_build_files(repo, None) == ["Makefile.am", "configure.ac"]
    # the `git log -S` needle: csproj basename / automake target name
    assert history.target_needle(A) == "A"
    assert history.target_needle("csharp:csproj:src/Nop.Web/Nop.Web.csproj") == "Nop.Web"
    assert history.target_needle(CPP) == "xmllint"
    assert history.target_needle("nuget:Newtonsoft.Json") is None


def test_parse_pairs():
    assert history.parse_pairs(None, ["a..b", "b..c"]) == [("a", "b"), ("b", "c")]
    assert history.parse_pairs(" v1..v2 , v2..v3", ["x..y"]) == [("v1", "v2"), ("v2", "v3")]
    try:
        history.parse_pairs("v1-v2", [])
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("malformed pair accepted")


# ------------------------------------------------------------------ toy smoke (real pipeline)

def test_toy_identical_releases_yield_zero_findings(tmp_path, monkeypatch):
    """The toy fixture as both A and B through the real L0-scoped pipeline: no findings,
    no worksheet rows, the pair artifacts written with the documented schema."""
    monkeypatch.setenv("ANON_SKIP_DOXYGEN", "1")
    monkeypatch.setenv("ANON_SKIP_ROSLYN", "1")
    toy = REPO / "tests" / "fixtures" / "toy-repo"
    rules = toy / "architecture" / "rules"
    arch_a, arch_b = tmp_path / "arch-a", tmp_path / "arch-b"
    history.build_release(toy, arch_a, rules)
    history.build_release(toy, arch_b, rules)
    ev = history.evaluate_pair("toy", "A", "B", toy, arch_a, toy, arch_b, rules, fixture=None)
    r = ev["result"]
    assert r["schema"] == history.PAIR_SCHEMA
    assert r["targets"]["a_first_party"] == r["targets"]["b_first_party"] > 0
    assert all(r["counts"][k] == 0 for k in history.FINDING_KINDS)
    assert r["counts"]["drift_total"] == 0 and r["worksheet_rows"] == 0
    assert r["git"] is None and not r["advisory"] and r["warnings"] == []
    assert ev["rows"] == []
    pair_dir = tmp_path / "A__B"
    history.write_pair(pair_dir, "toy", "A", "B", ev)
    for name in ("result.json", "drift.md", "worksheet.csv", "worksheet-R1.csv",
                 "worksheet-R2.csv"):
        assert (pair_dir / name).exists(), name
    raw = (pair_dir / "result.json").read_bytes()
    assert b"\r\n" not in raw and b"generated_at" not in raw
    assert json.loads(raw)["pair"] == {"a": "A", "b": "B"}
    assert history.read_worksheet(pair_dir / "worksheet-R1.csv") == []


# ------------------------------------------------------------------ aggregation (analysis/)

def _hist_tree(root: Path, labels_tracked=None, labels_out=None):
    """out/<sys>/history with one 'ok' pair and two rater worksheets (labels from
    ``labels_out``), plus — when ``labels_tracked`` is given — the TRACKED
    references/<sys>/history copies carrying ``labels_tracked`` (None = unlabelled)."""
    out, refs = root / "out", root / "references"
    hist = out / "libxml2" / "history"
    pdir = hist / "v1__v2"
    pdir.mkdir(parents=True)
    (hist / "history-summary.json").write_text(json.dumps({
        "schema": "rq6-history/1", "system": "libxml2",
        "pairs": [{"a": "v1", "b": "v2", "status": "ok", "commits_total": 3,
                   "commits_build_files": 1, "worksheet_rows": 2, "advisory": False,
                   "counts": {"new_targets": 2, "drift_total": 2}}]}), encoding="utf-8")
    fields = ["finding_id", "kind", "subject", "counterpart", "hint", "label", "note"]
    findings = [("v1__v2-001", "cpp:target:a"), ("v1__v2-002", "cpp:target:b")]

    def write(dirpath: Path, name: str, labels):
        dirpath.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for (fid, subj), lab in zip(findings, labels or ["", ""]):
            w.writerow({"finding_id": fid, "kind": "new_target", "subject": subj,
                        "counterpart": "", "hint": "", "label": lab, "note": ""})
        (dirpath / name).write_text("# RQ6 real-history worksheet — libxml2 v1..v2. Fill ONLY "
                                    "`label` and `note`.\n" + buf.getvalue(), encoding="utf-8")
    for rater in ("R1", "R2"):
        write(pdir, f"worksheet-{rater}.csv", (labels_out or {}).get(rater))
        if labels_tracked is not None:
            write(refs / "libxml2" / "history" / "v1__v2", f"worksheet-{rater}.csv",
                  labels_tracked.get(rater))
    return out, refs


def test_aggregate_history_labels_tracked_home_first(tmp_path, capsys):
    """Filled labels are human artifacts: the aggregator reads the TRACKED
    references/<sys>/history/<pair>/ worksheets first, file by file, and the regenerable
    out/ mirror only as a fallback; labels typed into the mirror beside a differing tracked
    copy are refused loudly, never silently dropped."""
    agg = _import("analysis/aggregate_results.py")
    meta = {"system": "libxml2", "language": "cpp", "granularity": "file",
            "ref_grade": "GT-pub", "ref_clusters": "18", "ref_entities": "58", "tier": "oss"}

    # (a) nothing labelled anywhere -> pending, no per-rater counts
    out, refs = _hist_tree(tmp_path / "a")
    by = {r["metric"]: r for r in agg._rq6_history_rows(out, refs, "libxml2", meta)}
    assert by["labels_pending"]["value"] == 1 and "label_architectural_R1" not in by
    assert by["new_targets"]["value"] == 2 and by["commits_total"]["value"] == 3

    # (b) the tracked copies carry the labels -> counts, agreement, pooled kappa cells
    out, refs = _hist_tree(tmp_path / "b",
                           labels_tracked={"R1": ["architectural", "extraction-noise"],
                                           "R2": ["architectural", "architectural"]})
    by = {r["metric"]: r for r in agg._rq6_history_rows(out, refs, "libxml2", meta)}
    assert by["labels_pending"]["value"] == 0
    assert by["label_architectural_R1"]["value"] == 1
    assert by["label_extraction-noise_R1"]["value"] == 1
    assert by["label_architectural_R2"]["value"] == 2
    assert by["labels_compared"]["value"] == 2 and by["labels_agree"]["value"] == 1
    assert by["label_architectural_agreed"]["value"] == 1
    assert by["labels_conf_extraction-noise_architectural"]["value"] == 1
    assert by["label_architectural_R1"]["source"] == "references/libxml2/history/v1__v2/worksheet-R1.csv"

    # (c) the out/ mirror was labelled while an unlabelled tracked copy exists: the tracked
    #     copy wins (still pending) and the mismatch is reported on stderr
    out, refs = _hist_tree(tmp_path / "c", labels_tracked={"R1": None, "R2": None},
                           labels_out={"R1": ["architectural", "architectural"]})
    by = {r["metric"]: r for r in agg._rq6_history_rows(out, refs, "libxml2", meta)}
    assert by["labels_pending"]["value"] == 1 and "label_architectural_R1" not in by
    assert "which wins" in capsys.readouterr().err

    # (d) no tracked dir at all: the mirror is the fallback (an experiment mid-flight)
    out, refs = _hist_tree(tmp_path / "d", labels_out={"R1": ["architectural", "architectural"],
                                                       "R2": ["architectural", "non-architectural"]})
    by = {r["metric"]: r for r in agg._rq6_history_rows(out, refs, "libxml2", meta)}
    assert by["labels_pending"]["value"] == 0 and by["labels_agree"]["value"] == 1
    assert by["label_architectural_R1"]["source"] == "out/libxml2/history/v1__v2/worksheet-R1.csv"

    # (e) ONE rater labelled, the other sheet still empty: the pair stays pending (the paper's
    #     cells are the findings BOTH raters agreed on, so an empty intersection must print "--",
    #     not 0/0/0); the single rater's own counts are still emitted for the record
    out, refs = _hist_tree(tmp_path / "e",
                           labels_tracked={"R1": ["architectural", "extraction-noise"], "R2": None})
    by = {r["metric"]: r for r in agg._rq6_history_rows(out, refs, "libxml2", meta)}
    assert by["labels_pending"]["value"] == 1
    assert by["label_architectural_R1"]["value"] == 1
    assert "label_architectural_agreed" not in by and "labels_agree" not in by
