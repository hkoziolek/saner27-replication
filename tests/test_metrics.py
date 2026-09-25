"""Deterministic source metrics, ``metrics/1`` (datasheet-enrichment plan §3).

Locked down here:
  * the #1 determinism trap — a CRLF working tree and an LF working tree count
    IDENTICALLY (plan §3.2);
  * the naive comment state machine's documented conventions (mixed code+comment
    line = code; blank-in-block = blank; TODO/FIXME/HACK found in comments and in
    trailing line comments);
  * deepest-owner file attribution (a parent-path target never swallows a nested
    target's files) and the default + knob exclusions;
  * binary sniff and the oversize files-only rule;
  * normalize folds the counts onto ``targets[].metrics`` without clobbering
    another extractor's keys.
"""
from __future__ import annotations

from anon import metrics


def _counts(text: str, lang: str = "C#"):
    return metrics.count_text(text, lang)


# ----------------------------------------------------------------- counting rules

def test_csharp_basic_counts():
    c = _counts("using System;\n\n// a comment\nint x = 1; // trailing\n")
    assert c == {"blank": 1, "comment": 1, "code": 2, "todo": 0}


def test_block_comment_spans_lines_and_blank_inside_stays_blank():
    text = "/*\n line\n\n*/\nint x;\n"
    c = _counts(text)
    assert c["comment"] == 3   # '/*', ' line', '*/'
    assert c["blank"] == 1     # the blank INSIDE the block
    assert c["code"] == 1


def test_block_comment_opened_mid_line_continues():
    text = "int x; /* opens here\nstill comment\n*/ int y;\n"
    c = _counts(text)
    assert c["code"] == 2      # 'int x;…' and '*/ int y;'
    assert c["comment"] == 1   # 'still comment'


def test_mixed_code_and_comment_counts_as_code():
    assert _counts("int x; // hi\n")["code"] == 1


def test_todo_markers_in_comments_and_trailing():
    text = "// TODO fix\nint x; // FIXME later\n/* HACK\n*/\n"
    assert _counts(text)["todo"] == 3


def test_hash_language_and_xml_language():
    assert metrics.count_text("# c\nkey: v\n", "YAML") == \
        {"blank": 0, "comment": 1, "code": 1, "todo": 0}
    assert metrics.count_text("<!-- c -->\n<a/>\n", "XML") == \
        {"blank": 0, "comment": 1, "code": 1, "todo": 0}


def test_language_without_comment_syntax_counts_all_code():
    assert metrics.count_text('{"a": 1}\n', "JSON")["code"] == 1


def test_crlf_and_lf_count_identically():
    """The #1 determinism trap (plan §3.2): checkout line endings must not matter."""
    lf = "// c\n\nint x;\n/* b\n*/\n"
    crlf = lf.replace("\n", "\r\n")
    assert metrics.count_text(lf, "C#") == metrics.count_text(crlf, "C#")


def test_classify_language_is_pinned():
    assert metrics.classify_language("Foo.cs") == "C#"
    assert metrics.classify_language("Foo.csproj") == "MSBuild"
    assert metrics.classify_language("CMakeLists.txt") == "CMake"
    assert metrics.classify_language("Dockerfile") == "Dockerfile"
    assert metrics.classify_language("noext") is None
    assert metrics.classify_language("weird.zzz") is None


# ------------------------------------------------------------- walk + attribution

def _repo(tmp_path):
    (tmp_path / "src" / "A").mkdir(parents=True)
    (tmp_path / "src" / "A" / "Nested").mkdir()
    (tmp_path / "src" / "A" / "a.cs").write_text("int a;\n// c\n", encoding="utf-8")
    (tmp_path / "src" / "A" / "Nested" / "n.cs").write_text("int n;\n", encoding="utf-8")
    (tmp_path / "src" / "A" / "obj").mkdir()
    (tmp_path / "src" / "A" / "obj" / "gen.cs").write_text("int g;\n", encoding="utf-8")
    (tmp_path / "src" / "A" / "skip.g.cs").write_text("int s;\n", encoding="utf-8")
    return tmp_path


def _facts():
    return {"targets": [
        {"id": "t:a", "path": "src/A"},
        {"id": "t:nested", "path": "src/A/Nested"},
        {"id": "t:pkg", "external": True},  # no path -> never measured
    ]}


def test_deepest_owner_wins_and_defaults_excluded(tmp_path):
    measured = metrics.measure(_repo(tmp_path), _facts())
    # nested file belongs to the NESTED target, not the parent (plan §3 attribution)
    assert measured["t:nested"]["by_language"][0]["files"] == 1
    # parent counts only its own a.cs (obj/** and *.g.cs are default-excluded)
    a = measured["t:a"]
    assert a["by_language"] == [{"language": "C#", "files": 1, "blank": 0,
                                 "comment": 1, "code": 1}]
    assert a["engine"] == "metrics/1"
    assert "t:pkg" not in measured


def test_exclude_knob_extends_defaults(tmp_path):
    repo = _repo(tmp_path)
    measured = metrics.measure(repo, _facts(), exclude=["**/Nested/**"])
    assert "t:nested" not in measured


def test_binary_files_are_skipped(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "A" / "blob.cs").write_bytes(b"\x00\x01binary")
    measured = metrics.measure(repo, _facts())
    assert measured["t:a"]["by_language"][0]["files"] == 1  # blob not counted at all


def test_oversize_file_counts_files_only(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(metrics, "MAX_FILE_BYTES", 4)
    measured = metrics.measure(repo, _facts())
    a = measured["t:a"]["by_language"][0]
    assert a["files"] == 1 and a["code"] == 0  # counted, not line-counted


def test_annotate_preserves_other_extractor_keys(tmp_path):
    facts = _facts()
    facts["targets"][0]["metrics"] = {"files": 99}  # e.g. cmake's declared sources
    metrics.annotate(facts, _repo(tmp_path))
    m = facts["targets"][0]["metrics"]
    assert m["files"] == 99 and m["engine"] == "metrics/1" and m["code"] == 1


def test_measure_is_deterministic(tmp_path):
    repo = _repo(tmp_path)
    assert metrics.measure(repo, _facts()) == metrics.measure(repo, _facts())
