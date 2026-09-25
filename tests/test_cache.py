"""Tests for the incremental-extraction cache primitive (§10.1).

These exercise the OPT-IN accelerator in isolation (pure, file-based, deterministic):
content-hash keying, fragment put/get round-trip, change detection via ``decide``,
and robustness to a missing/corrupt store. None of this touches default behavior or
committed artifacts.
"""
from __future__ import annotations

from pathlib import Path

from anon.cache import (
    FragmentCache,
    cache_key,
    content_hash_files,
    decide,
    should_reextract,
)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --- content_hash_files -----------------------------------------------------------

def test_content_hash_is_stable_and_order_independent(tmp_path: Path) -> None:
    a = _write(tmp_path / "src" / "a.txt", "alpha")
    b = _write(tmp_path / "src" / "b.txt", "beta")
    h1 = content_hash_files([a, b], root=tmp_path)
    h2 = content_hash_files([b, a], root=tmp_path)  # reversed input order
    assert h1 == h2
    assert len(h1) == 64  # sha256 hexdigest


def test_content_hash_ignores_missing_files(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.txt", "alpha")
    missing = tmp_path / "does-not-exist.txt"
    assert content_hash_files([a, missing], root=tmp_path) == content_hash_files([a], root=tmp_path)


def test_content_hash_changes_when_content_changes(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.txt", "alpha")
    before = content_hash_files([a], root=tmp_path)
    _write(tmp_path / "a.txt", "alpha-modified")
    after = content_hash_files([a], root=tmp_path)
    assert before != after


def test_content_hash_is_path_relative(tmp_path: Path) -> None:
    """Same relative layout + content under two roots -> identical hash (cross-OS)."""
    r1 = tmp_path / "root1"
    r2 = tmp_path / "root2"
    f1 = _write(r1 / "src" / "x.c", "int main(){}")
    f2 = _write(r2 / "src" / "x.c", "int main(){}")
    assert content_hash_files([f1], root=r1) == content_hash_files([f2], root=r2)


def test_content_hash_includes_relative_path(tmp_path: Path) -> None:
    """Identical content at different relative paths must hash differently."""
    f1 = _write(tmp_path / "a" / "x.c", "same")
    f2 = _write(tmp_path / "b" / "x.c", "same")  # same name, different dir
    assert content_hash_files([f1], root=tmp_path) != content_hash_files([f2], root=tmp_path)


# --- cache_key / should_reextract -------------------------------------------------

def test_cache_key_is_deterministic_and_separated() -> None:
    assert cache_key("ext", "1.0", "abc") == cache_key("ext", "1.0", "abc")
    # The NUL separator prevents part-boundary collisions:
    assert cache_key("a", "bc") != cache_key("ab", "c")


def test_should_reextract() -> None:
    assert should_reextract(None, "k") is True
    assert should_reextract("k1", "k2") is True
    assert should_reextract("k", "k") is False


# --- FragmentCache put/get --------------------------------------------------------

def test_put_then_get_round_trips_identical_fragment(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "extract")
    fragment = {"schema_version": "1.1", "targets": [{"id": "t1"}], "relationships": []}
    key = cache_key("csharp-build-graph", "0.2.0", "deadbeef")
    cache.put(key, fragment)
    assert cache.get(key) == fragment


def test_get_is_a_clean_miss_when_absent(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "extract")  # dir never created
    assert cache.get("nonexistent-key") is None


def test_get_is_a_clean_miss_on_corrupt_file(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "extract")
    key = cache_key("ext", "1", "x")
    # Write garbage where a fragment JSON should be.
    path = cache.dir / f"{key}.json"
    _write(path, "{ this is : not valid json ]")
    assert cache.get(key) is None  # corrupt -> miss, never raises


def test_manifest_records_hit_miss_info(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "extract")
    key = cache_key("ext", "1", "h")
    cache.put(key, {"targets": []})
    cache.record("ext", key)
    assert cache.last_key("ext") == key
    assert cache.last_key("never-seen") is None
    manifest = cache.load_manifest()
    assert manifest["ext"] == {"key": key, "fragment_file": f"{key}.json"}


def test_load_manifest_empty_when_absent(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "extract")
    assert cache.load_manifest() == {}


# --- decide (the convenience that drives --incremental) ---------------------------

def test_decide_reports_changed_on_first_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "CMakeLists.txt", "project(toy)")
    cdir = tmp_path / "cache"
    key, changed = decide(repo, "cmake-l0", "1.0", ["**/CMakeLists.txt"], cache_dir=cdir)
    assert changed is True  # never seen before
    assert len(key) == 64


def test_decide_reports_unchanged_on_identical_rerun(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "CMakeLists.txt", "project(toy)")
    cdir = tmp_path / "cache"
    key1, changed1 = decide(repo, "cmake-l0", "1.0", ["**/CMakeLists.txt"], cache_dir=cdir)
    key2, changed2 = decide(repo, "cmake-l0", "1.0", ["**/CMakeLists.txt"], cache_dir=cdir)
    assert changed1 is True
    assert changed2 is False  # nothing changed -> reuse cached fragment
    assert key1 == key2


def test_decide_reports_changed_after_input_mutation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src = _write(repo / "CMakeLists.txt", "project(toy)")
    cdir = tmp_path / "cache"
    key1, _ = decide(repo, "cmake-l0", "1.0", ["**/CMakeLists.txt"], cache_dir=cdir)
    _write(src, "project(toy)\nadd_library(foo foo.c)")  # mutate an input
    key2, changed2 = decide(repo, "cmake-l0", "1.0", ["**/CMakeLists.txt"], cache_dir=cdir)
    assert key1 != key2
    assert changed2 is True


def test_decide_end_to_end_with_fragment_reuse(tmp_path: Path) -> None:
    """Full opt-in flow: decide -> on miss put, on hit get the IDENTICAL fragment."""
    repo = tmp_path / "repo"
    _write(repo / "a.csproj", "<Project/>")
    cdir = tmp_path / "cache" / "extract"
    cache = FragmentCache(cdir)

    # First run: changed -> extract + cache.
    key1, changed1 = decide(repo, "csharp-build-graph", "0.2.0", ["**/*.csproj"], cache_dir=cdir)
    assert changed1 is True
    fragment = {"targets": [{"id": "csharp:csproj:a.csproj"}], "relationships": []}
    cache.put(key1, fragment)
    cache.record("csharp-build-graph", key1)

    # Second run, unchanged inputs: not changed -> serve the cached fragment verbatim.
    key2, changed2 = decide(repo, "csharp-build-graph", "0.2.0", ["**/*.csproj"], cache_dir=cdir)
    assert changed2 is False
    assert key2 == key1
    assert cache.get(key2) == fragment
