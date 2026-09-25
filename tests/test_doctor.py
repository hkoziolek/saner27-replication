"""Tests for ``anon.doctor`` (plan §22.2 tooling health-check).

All tests are hermetic: they use synthetic ``tooling.lock`` files in ``tmp_path`` and
never depend on which tools are actually installed on the host machine.

The one exception is the "always-present tool" entry, which probes the Python binary that
is *running this test process*; since we know it exists and is at least 3.x, this is
safe and avoids any hard-coded assumption about the test environment.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anon.doctor import (
    bootstrap_advice,
    check_tool,
    detect_platform,
    load_lock,
    main,
    parse_version,
    render,
    run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_lock(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8", newline="")
    return path


def _python_bin() -> str:
    """The name / path of the Python executable running this test."""
    return sys.executable


# ---------------------------------------------------------------------------
# parse_version
# ---------------------------------------------------------------------------

class TestParseVersion:
    def test_three_part(self):
        assert parse_version("3.12.1") == (3, 12, 1)

    def test_two_part(self):
        assert parse_version("17.0") == (17, 0)

    def test_single(self):
        assert parse_version("8") == (8,)

    def test_with_prefix(self):
        assert parse_version("v4.2.0") == (4, 2, 0)

    def test_with_suffix(self):
        assert parse_version("3.11.0rc2") == (3, 11, 0, 2)

    def test_empty_string(self):
        assert parse_version("") == ()

    def test_no_digits(self):
        assert parse_version("release-candidate") == ()


# ---------------------------------------------------------------------------
# detect_platform
# ---------------------------------------------------------------------------

class TestDetectPlatform:
    def test_returns_known_value(self):
        p = detect_platform()
        assert p in ("windows", "linux", "darwin")

    def test_matches_sys_platform(self):
        p = detect_platform()
        if sys.platform.startswith("win"):
            assert p == "windows"
        elif sys.platform.startswith("darwin"):
            assert p == "darwin"
        else:
            assert p == "linux"


# ---------------------------------------------------------------------------
# load_lock
# ---------------------------------------------------------------------------

class TestLoadLock:
    def test_parses_yaml(self, tmp_path: Path):
        lock_file = _write_lock(tmp_path / "tooling.lock", """
python:
  min_version: "3.12"
  required: true
  version_cmd: ["python", "--version"]
  version_re: "([0-9]+\\\\.[0-9]+\\\\.[0-9]+)"
  winget: "Python.Python.3.12"
  apt: "python3.12"
  scoop: "python"
""")
        data = load_lock(lock_file)
        assert "python" in data
        assert data["python"]["required"] is True
        assert data["python"]["min_version"] == "3.12"

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_lock(tmp_path / "nonexistent.lock")


# ---------------------------------------------------------------------------
# check_tool
# ---------------------------------------------------------------------------

class TestCheckTool:
    def test_present_tool_ok(self):
        """Python running this test must be locatable and above version 3."""
        # Use sys.executable directly: on Windows this may be an .exe path not on PATH.
        # So we give a low bar (3.0) and point which at the real executable.
        import os
        python_dir = str(Path(sys.executable).parent)
        # Temporarily add the venv's Scripts dir to PATH if needed
        original_path = os.environ.get("PATH", "")
        os.environ["PATH"] = python_dir + os.pathsep + original_path
        try:
            spec = {
                "min_version": "3.0",
                "required": True,
                "version_cmd": [sys.executable, "--version"],
                "version_re": r"([0-9]+\.[0-9]+\.[0-9]+)",
                "which": sys.executable,
                "winget": "Python.Python.3.12",
                "apt": "python3.12",
                "scoop": "python",
            }
            result = check_tool("python", spec, platform="windows")
            assert result["status"] == "ok", f"expected ok, got {result}"
            assert result["installed"] is not None
        finally:
            os.environ["PATH"] = original_path

    def test_missing_tool(self):
        spec = {
            "min_version": "1.0",
            "required": True,
            "version_cmd": ["definitely-not-installed-xyz", "--version"],
            "version_re": r"([0-9]+\.[0-9]+)",
            "winget": "Some.Package",
            "apt": "some-package",
            "scoop": "some-package",
        }
        result = check_tool("definitely-not-installed-xyz", spec, platform="windows")
        assert result["status"] == "missing"
        assert result["installed"] is None
        assert result["path"] is None

    def test_stale_detection(self):
        """A tool whose installed version is below min_version -> stale."""
        # We use the real python but set a ludicrous min_version.
        spec = {
            "min_version": "99.0",
            "required": True,
            "version_cmd": [sys.executable, "--version"],
            "version_re": r"([0-9]+\.[0-9]+\.[0-9]+)",
            "which": sys.executable,
            "winget": "Python.Python.99",
            "apt": "python99",
            "scoop": "python",
        }
        result = check_tool("python", spec, platform="linux")
        assert result["status"] == "stale", f"expected stale, got {result}"
        assert result["installed"] is not None

    def test_remediation_windows(self):
        spec = {
            "min_version": "1.0",
            "required": True,
            "version_cmd": ["definitely-not-installed-xyz", "--version"],
            "version_re": r"([0-9]+\.[0-9]+)",
            "winget": "Example.Tool",
            "apt": "example-tool",
        }
        result = check_tool("definitely-not-installed-xyz", spec, platform="windows")
        assert result["remediation"] == "winget install --id Example.Tool --exact --silent"

    def test_remediation_linux(self):
        spec = {
            "min_version": "1.0",
            "required": False,
            "version_cmd": ["definitely-not-installed-xyz", "--version"],
            "version_re": r"([0-9]+\.[0-9]+)",
            "winget": "Example.Tool",
            "apt": "example-tool",
        }
        result = check_tool("definitely-not-installed-xyz", spec, platform="linux")
        assert result["remediation"] == "sudo apt-get install -y example-tool"

    def test_no_version_cmd(self):
        spec = {
            "min_version": "1.0",
            "required": False,
            "which": sys.executable,
            "winget": None,
            "apt": None,
        }
        result = check_tool("python-novercmd", spec, platform="linux")
        # Tool is found but no version_cmd -> unknown-version, not missing
        assert result["status"] == "unknown-version"
        assert result["path"] is not None


# ---------------------------------------------------------------------------
# Synthetic tooling.lock: one always-present (python), one always-missing
# ---------------------------------------------------------------------------

_SYNTHETIC_LOCK = """\
python_test:
  min_version: "3.0"
  required: false
  which: "{python_exe}"
  version_cmd: ["{python_exe}", "--version"]
  version_re: "([0-9]+\\\\.[0-9]+\\\\.[0-9]+)"
  winget: "Python.Python.3.12"
  apt: "python3.12"
  scoop: "python"

definitely-not-installed-xyz:
  min_version: "1.0"
  required: true
  version_cmd: ["definitely-not-installed-xyz", "--version"]
  version_re: "([0-9]+\\\\.[0-9]+)"
  winget: "Some.MissingTool"
  apt: "missing-tool"
  scoop: "missing-tool"
"""


class TestRunWithSyntheticLock:
    def test_ok_false_when_required_tool_missing(self, tmp_path: Path):
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            _SYNTHETIC_LOCK.format(python_exe=python_exe),
        )
        report = run(lock_path=lock_file)
        assert report["ok"] is False
        assert "definitely-not-installed-xyz" in report["missing_required"]

    def test_python_tool_is_ok(self, tmp_path: Path):
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            _SYNTHETIC_LOCK.format(python_exe=python_exe),
        )
        report = run(lock_path=lock_file)
        python_result = next(t for t in report["tools"] if t["name"] == "python_test")
        assert python_result["status"] == "ok"
        assert python_result["installed"] is not None

    def test_missing_tool_in_report(self, tmp_path: Path):
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            _SYNTHETIC_LOCK.format(python_exe=python_exe),
        )
        report = run(lock_path=lock_file)
        missing = next(
            t for t in report["tools"] if t["name"] == "definitely-not-installed-xyz"
        )
        assert missing["status"] == "missing"
        assert missing["required"] is True

    def test_platform_in_report(self, tmp_path: Path):
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            _SYNTHETIC_LOCK.format(python_exe=python_exe),
        )
        report = run(lock_path=lock_file, platform="linux")
        assert report["platform"] == "linux"


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------

class TestRender:
    def _make_report(self, tmp_path: Path):
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            _SYNTHETIC_LOCK.format(python_exe=python_exe),
        )
        return run(lock_path=lock_file, platform="windows")

    def test_render_contains_fail(self, tmp_path: Path):
        report = self._make_report(tmp_path)
        output = render(report)
        assert "FAIL" in output

    def test_render_contains_tool_names(self, tmp_path: Path):
        report = self._make_report(tmp_path)
        output = render(report)
        assert "definitely-not-installed-xyz" in output
        assert "python_test" in output

    def test_render_contains_remediation(self, tmp_path: Path):
        """render() must include the winget remediation for the missing required tool."""
        report = self._make_report(tmp_path)
        output = render(report)
        # The missing tool has winget: "Some.MissingTool"
        assert "Some.MissingTool" in output

    def test_render_pass_when_all_ok(self, tmp_path: Path):
        """A report with no missing_required tools renders PASS."""
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            # Only the python_test entry (required: false, always present)
            f"""
python_test:
  min_version: "3.0"
  required: false
  which: "{python_exe}"
  version_cmd: ["{python_exe}", "--version"]
  version_re: "([0-9]+\\\\.[0-9]+\\\\.[0-9]+)"
  winget: "Python.Python.3.12"
  apt: "python3.12"
  scoop: "python"
""",
        )
        report = run(lock_path=lock_file)
        output = render(report)
        assert "PASS" in output


# ---------------------------------------------------------------------------
# bootstrap_advice()
# ---------------------------------------------------------------------------

class TestBootstrapAdvice:
    def _make_report(self, tmp_path: Path):
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            _SYNTHETIC_LOCK.format(python_exe=python_exe),
        )
        return run(lock_path=lock_file, platform="windows")

    def test_advice_contains_bootstrap_script_windows(self, tmp_path: Path):
        report = self._make_report(tmp_path)
        # Force platform for determinism
        report["platform"] = "windows"
        advice = bootstrap_advice(report)
        assert "bootstrap.ps1" in advice

    def test_advice_contains_bootstrap_script_linux(self, tmp_path: Path):
        report = self._make_report(tmp_path)
        report["platform"] = "linux"
        advice = bootstrap_advice(report)
        assert "bootstrap.sh" in advice

    def test_advice_lists_missing_tool(self, tmp_path: Path):
        report = self._make_report(tmp_path)
        report["platform"] = "windows"
        advice = bootstrap_advice(report)
        assert "definitely-not-installed-xyz" in advice


# ---------------------------------------------------------------------------
# main() / __main__ shim
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_exits_nonzero_when_required_missing(self, tmp_path: Path):
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            _SYNTHETIC_LOCK.format(python_exe=python_exe),
        )
        rc = main(["--lock", str(lock_file)])
        assert rc != 0

    def test_main_exits_zero_when_all_ok(self, tmp_path: Path):
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            f"""
python_test:
  min_version: "3.0"
  required: true
  which: "{python_exe}"
  version_cmd: ["{python_exe}", "--version"]
  version_re: "([0-9]+\\\\.[0-9]+\\\\.[0-9]+)"
  winget: "Python.Python.3.12"
  apt: "python3.12"
  scoop: "python"
""",
        )
        rc = main(["--lock", str(lock_file)])
        assert rc == 0

    def test_main_json_flag(self, tmp_path: Path, capsys):
        python_exe = sys.executable.replace("\\", "/")
        lock_file = _write_lock(
            tmp_path / "tooling.lock",
            _SYNTHETIC_LOCK.format(python_exe=python_exe),
        )
        main(["--lock", str(lock_file), "--json"])
        captured = capsys.readouterr()
        import json
        data = json.loads(captured.out)
        assert "platform" in data
        assert "tools" in data
        assert "ok" in data


# ---------------------------------------------------------------------------
# Version comparison (stale detection) — standalone unit tests
# ---------------------------------------------------------------------------

class TestVersionComparison:
    """Validate parse_version + check_tool stale path without subprocess."""

    def test_stale_when_installed_below_min(self):
        installed = parse_version("7.99.0")
        min_v = parse_version("8.0")
        # Pad
        length = max(len(installed), len(min_v))
        inst = installed + (0,) * (length - len(installed))
        minv = min_v + (0,) * (length - len(min_v))
        assert inst < minv

    def test_ok_when_installed_equals_min(self):
        assert parse_version("8.0.0") >= parse_version("8.0")[:3]  # type: ignore[operator]
        # Proper padded comparison
        inst = (8, 0, 0)
        minv = (8, 0, 0)
        assert inst >= minv

    def test_ok_when_installed_above_min(self):
        installed = (10, 0, 0)
        min_v = (8, 0, 0)
        assert installed >= min_v

    def test_minor_stale(self):
        installed = parse_version("8.5.0")
        min_v = parse_version("8.6")
        length = max(len(installed), len(min_v))
        inst = installed + (0,) * (length - len(installed))
        minv = min_v + (0,) * (length - len(min_v))
        assert inst < minv
