"""A hung git/scanner subprocess must not hang Brigade.

Each of these sites invokes an external process; without an explicit timeout a
process that never exits (a stuck git over a dead remote, a wedged scanner) would
hang the whole command. These guard against that regression.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from brigade import localio, scrub
from brigade.work_cmd import helpers


def test_check_git_ignored_returns_unknown_on_timeout(tmp_path: Path, monkeypatch):
    repo = tmp_path.resolve()

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "check-ignore"], timeout=10)

    monkeypatch.setattr(localio.subprocess, "run", _timeout)
    assert localio.check_git_ignored(repo, repo / "artifact") == "unknown"


def test_work_git_reports_failure_on_timeout(tmp_path: Path, monkeypatch):
    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "status"], timeout=30)

    monkeypatch.setattr(helpers.subprocess, "run", _timeout)
    result = helpers._git(tmp_path, "status")
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_scrub_egress_gate_fails_closed_on_timeout(tmp_path: Path, monkeypatch):
    # The scrub scan is the egress gate; a scanner that hangs must block, not pass.
    policy_file = tmp_path / "policy.json"
    policy_file.write_text("{}\n")
    monkeypatch.setattr(scrub, "scanner_dir", lambda: tmp_path)
    monkeypatch.setattr(scrub, "policy_path", lambda repo, policy: policy_file)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["content_guard"], timeout=120)

    monkeypatch.setattr(scrub.subprocess, "run", _timeout)
    result = scrub.run_scan(tmp_path, policy="public-repo")
    assert result["status"] == "blocked"
    assert result["exit_code"] == 124
    assert "timed out" in result["detail"]
