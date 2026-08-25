import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _fake_tool_dir(tmp_path: Path) -> tuple[Path, Path]:
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()
    log_path = tmp_path / "tools.log"
    stub = """#!/bin/sh
printf '%s' "$(basename "$0")" >> "$VERIFY_LOG"
for arg in "$@"; do printf '\\t%s' "$arg" >> "$VERIFY_LOG"; done
printf '\\n' >> "$VERIFY_LOG"
"""
    for name in ("ruff", "mypy", "python", "pytest"):
        path = tool_dir / name
        path.write_text(stub)
        path.chmod(0o755)
    return tool_dir, log_path


def test_verify_script_runs_mypy_and_coverage_floor():
    text = (ROOT / "scripts/verify").read_text()

    assert '"$PY/mypy"' in text
    assert '"$PY/pytest" -q --cov=brigade --cov-report=term --cov-fail-under=78' in text


def test_verify_script_documents_full_coverage_gate():
    text = (ROOT / "scripts/verify").read_text()

    assert "ruff lint, ruff format, mypy, version sync, pytest with coverage" in text


def test_root_ruff_configuration_excludes_non_python_trees():
    text = (ROOT / "pyproject.toml").read_text()
    ruff_config = text.split("[tool.ruff]\n", maxsplit=1)[1].split("\n[tool.ruff.", maxsplit=1)[0]
    exclude_line = next(line for line in ruff_config.splitlines() if line.startswith("extend-exclude = "))
    exclusions = ast.literal_eval(exclude_line.partition("=")[2].strip())

    assert "engines" in exclusions
    assert "*.md" in exclusions
    assert "force-exclude = true" in ruff_config


@pytest.mark.skipif(os.name == "nt", reason="Bash verification entrypoint")
def test_verify_focused_requires_a_pytest_selector():
    completed = subprocess.run(
        [str(ROOT / "scripts/verify-focused")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "usage: ./scripts/verify-focused <pytest-selector>..." in completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="Bash verification entrypoint")
def test_verify_focused_forwards_only_selected_tests(tmp_path, monkeypatch):
    tool_dir, log_path = _fake_tool_dir(tmp_path)
    monkeypatch.setenv("PY", str(tool_dir))
    monkeypatch.setenv("VERIFY_LOG", str(log_path))
    selectors = [
        "tests/test_verify_script.py",
        "tests/test_verify_script.py::test_root_ruff_configuration_excludes_non_python_trees",
    ]
    completed = subprocess.run(
        [str(ROOT / "scripts/verify-focused"), *selectors],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0
    assert log_path.read_text().splitlines()[-1] == f"pytest\t-q\t{selectors[0]}\t{selectors[1]}"


@pytest.mark.skipif(os.name == "nt" or shutil.which("flock") is None, reason="requires flock")
def test_verify_full_gate_fails_fast_when_another_gate_holds_the_lock(tmp_path, monkeypatch):
    import fcntl

    tool_dir, log_path = _fake_tool_dir(tmp_path)
    lock_path = tmp_path / "full.lock"
    lock_path.touch()
    monkeypatch.setenv("PY", str(tool_dir))
    monkeypatch.setenv("VERIFY_LOG", str(log_path))
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_PATH", str(lock_path))
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = subprocess.run(
            [str(ROOT / "scripts/verify")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
            timeout=15,
        )
    assert completed.returncode == 75
    assert "full verification already running" in completed.stderr
    assert "./scripts/verify-focused" in completed.stderr
    assert not log_path.exists() or log_path.read_text() == ""


def test_agents_md_requires_focused_development_and_reserved_full_gate():
    text = (ROOT / "AGENTS.md").read_text()

    assert (
        "brigade work verify run --target . --argv-json "
        '\'["./scripts/verify-focused","<pytest-selector>"]\' --capture brigade-work'
    ) in text
    assert "./scripts/verify-focused" in text
    assert "./scripts/verify" in text
    assert "pull requests" in text
    assert "merges" in text
    assert "releases" in text
    assert "verification-infrastructure" in text
    assert "--no-reuse" in text
    assert "75" in text
    assert "must not be retried" in text
    assert "larger timeout" in text
