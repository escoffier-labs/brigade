"""Merge-base comparison for LEGACY_OVERSIZED (scripts/check_size_ratchet_baseline.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_size_ratchet_baseline.py"

GIT_IDENTITY = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Size Ratchet Test",
    "GIT_AUTHOR_EMAIL": "size-ratchet@example.invalid",
    "GIT_COMMITTER_NAME": "Size Ratchet Test",
    "GIT_COMMITTER_EMAIL": "size-ratchet@example.invalid",
}

BASELINE = {
    "src/a.py": 3000,
    "src/b.py": 2500,
}


def _mapping_source(mapping: dict[str, int]) -> str:
    body = ",\n".join(f'    "{key}": {cap}' for key, cap in mapping.items())
    return f"LEGACY_OVERSIZED: dict[str, int] = {{\n{body},\n}}\n"


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    test_dir = repo / "tests"
    test_dir.mkdir()
    baseline_file = test_dir / "test_module_size_ratchet.py"
    baseline_file.write_text(_mapping_source(BASELINE), encoding="utf-8")
    subprocess.run(
        ["git", "add", "tests/test_module_size_ratchet.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=GIT_IDENTITY,
    )
    return repo


def _run_check(repo: Path, current: dict[str, int]) -> subprocess.CompletedProcess[str]:
    current_file = repo / "current.py"
    current_file.write_text(_mapping_source(current), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base-ref",
            "HEAD",
            "--current-file",
            str(current_file),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
    )


def test_lowered_cap_and_removed_key_pass(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = _run_check(repo, {"src/a.py": 2800})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "size ratchet baseline: ok (1 entries)" in result.stdout


def test_added_key_fails_with_added_allowlist_entry(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = _run_check(
        repo,
        {"src/a.py": 3000, "src/b.py": 2500, "src/c.py": 2100},
    )
    assert result.returncode == 1
    assert "added allowlist entry" in result.stdout


def test_raised_cap_fails_with_cap_raised(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = _run_check(repo, {"src/a.py": 3100, "src/b.py": 2500})
    assert result.returncode == 1
    assert "cap raised" in result.stdout
