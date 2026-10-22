"""Embedded scanner launches cannot import workspace Python."""

from __future__ import annotations

import subprocess

import pytest

from brigade import scrub
from brigade.release_cmd import ci


@pytest.mark.parametrize("shadow", ["brigade.py", "brigade/__init__.py"])
@pytest.mark.parametrize("entry", ["scrub", "release-history"])
def test_embedded_scanner_ignores_workspace_modules(tmp_path, monkeypatch, shadow, entry):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "executed"
    planted = workspace / shadow
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("PYTHONPATH", str(workspace))
    monkeypatch.delenv("CONTENT_GUARD_DIR", raising=False)
    if entry == "scrub":
        result = scrub.run_scan(workspace)
        assert result["exit_code"] in (0, 1), result
    else:
        for args in (
            ["init", "-q", str(workspace)],
            [
                "-c",
                "user.name=Example Dev",
                "-c",
                "user.email=dev@example.com",
                "commit",
                "--allow-empty",
                "-qm",
                "init",
            ],
        ):
            subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
        result = ci._run_content_guard_check(workspace, name="introduced", policy="public-repo", base_ref="HEAD")
        assert result["exit_code"] == 0, result
    assert not sentinel.exists(), "scanner launch executed a workspace Brigade module"


@pytest.mark.parametrize("entry", ["scrub", "release-history"])
def test_embedded_scanner_preserves_relative_policy(tmp_path, monkeypatch, entry):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("CONTENT_GUARD_DIR", raising=False)
    policy = workspace / "policy.json"
    policy.write_text((scrub.scanner_dir() / "policies/public-repo.json").read_text())
    (workspace / "example.txt").write_text("ordinary example content\n")
    if entry == "scrub":
        result = scrub.run_scan(workspace, policy="policy.json")
    else:
        for args in (
            ["init", "-q", str(workspace)],
            [
                "-c",
                "user.name=Example Dev",
                "-c",
                "user.email=dev@example.com",
                "commit",
                "--allow-empty",
                "-qm",
                "init",
            ],
        ):
            subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
        result = ci._run_content_guard_check(workspace, name="introduced", policy="policy.json", base_ref="HEAD")
    assert result["exit_code"] == 0, result
