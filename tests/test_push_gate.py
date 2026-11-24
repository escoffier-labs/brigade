"""Clone-local push gate installation, reporting and real Git execution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import doctor
from brigade.cli import main
from tests.test_verify_push import _commit, _git, _policy, _tree
from tests.test_verify_push import push_repo as push_repo


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("BRIGADE_NO_UPDATE_CHECK", "1")


def _gate(target, *args):
    return main(["work", "verify", "push-gate", *args, "--target", str(target)])


def _status(target, capsys):
    assert _gate(target, "status", "--json") == 0
    return json.loads(capsys.readouterr().out)


def test_install_upgrade_uninstall(push_repo, capsys, monkeypatch):
    assert _status(push_repo, capsys)["mode"] == "off"
    assert _gate(push_repo, "install", "--mode", "enforce") == 2
    assert "does not exist yet" in capsys.readouterr().err
    assert _gate(push_repo, "install") == 0
    capsys.readouterr()
    status = _status(push_repo, capsys)
    assert status["installed"] and status["current"] and status["mode"] == "warn"
    hook = Path(status["hook_path"])
    monkeypatch.setattr(sys, "executable", sys.executable + "-new")
    assert not _status(push_repo, capsys)["current"]
    assert _gate(push_repo, "install", "--mode", "off") == 0
    capsys.readouterr()
    assert _status(push_repo, capsys)["current"]
    _git(push_repo, "config", "brigade.pushGate", "enforce")
    status = _status(push_repo, capsys)
    assert status["configured_mode"] == "enforce" and status["mode"] == "warn"
    assert _gate(push_repo, "uninstall") == 0
    capsys.readouterr()
    assert not hook.exists()
    assert _status(push_repo, capsys)["mode"] == "off"


def test_refuses_foreign_hook(push_repo, capsys):
    hook = push_repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    assert _gate(push_repo, "install") == 1
    capsys.readouterr()
    assert _status(push_repo, capsys)["problem"] == "foreign hook"
    assert _gate(push_repo, "uninstall") == 1
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nexit 3\n"


def test_refuses_outside_hooks_path(push_repo, capsys):
    outside = push_repo.parent / "outside"
    _git(push_repo, "config", "core.hooksPath", str(outside))
    assert _gate(push_repo, "install") == 1
    capsys.readouterr()
    assert _status(push_repo, capsys)["problem"] == "outside core.hooksPath"
    assert not outside.exists()


def test_relative_content_guard_is_restored(push_repo, capsys):
    from brigade.work_cmd.push_gate import _guard_bytes

    hook = push_repo / "hooks" / "pre-push"
    hook.parent.mkdir()
    original = _guard_bytes()
    hook.write_bytes(original)
    _git(push_repo, "config", "core.hooksPath", "hooks")
    assert _gate(push_repo, "install") == 0
    capsys.readouterr()
    assert _status(push_repo, capsys)["content_guard"]
    assert _git(push_repo, "config", "core.hooksPath") == str(hook.parent)
    assert _gate(push_repo, "uninstall") == 0
    assert _git(push_repo, "config", "core.hooksPath") == "hooks"
    assert hook.read_bytes() == original


def test_bounded_records_and_report(push_repo, capsys, monkeypatch):
    from brigade.work_cmd import push_gate_log

    monkeypatch.setattr(push_gate_log, "RECORD_LIMIT", 3)
    path = push_gate_log.log_path(push_repo / ".git")
    rows = [
        {
            "schema": push_gate_log.SCHEMA,
            "time": f"2026-01-0{day}T00:00:00+00:00",
            "result": result,
            "would_block": result == "rejected",
            "duration_ms": duration,
        }
        for day, result, duration in [(1, "ok", 1), (2, "ok", 10), (2, "rejected", 20), (3, "gate_error", 100)]
    ]
    push_gate_log.append(path, rows[:2])
    push_gate_log.append(path, rows[2:])
    assert len(path.read_text().splitlines()) == 3
    assert _gate(push_repo, "report", "--json", "--since", "2026-01-02") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["results"] == {"ok": 1, "rejected": 1, "gate_error": 1}
    assert report["would_block_share"] == pytest.approx(1 / 3)
    assert report["gate_errors"] == 1
    assert report["median_ms"] == 20
    assert report["p95_ms"] == 100
    assert report["days"]["2026-01-02"]["results"] == {"ok": 1, "rejected": 1}
    assert _gate(push_repo, "report", "--json", "--since", "2026-01-03") == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    assert any(
        check[1] == "verify push gate" and "last push=2026-01-03" in check[2]
        for check in doctor._check_publish_gate(push_repo)
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pre-push hook and restricted PATH fixture")
def test_linked_worktree_push(push_repo, capsys):
    _policy(push_repo)
    remote = push_repo.parent / "remote.git"
    _git(push_repo, "init", "--bare", str(remote))
    _git(push_repo, "remote", "set-url", "origin", str(remote))
    assert _gate(push_repo, "install") == 0
    capsys.readouterr()
    linked = push_repo.parent / "linked"
    _git(push_repo, "worktree", "add", "-b", "feature", str(linked))
    tip = _commit(linked)
    git = shutil.which("git")
    assert git is not None
    path = str(Path(git).parent) + os.pathsep + os.defpath

    def push(bin_path):
        return subprocess.run(
            [git, "push", "origin", "feature"],
            cwd=linked,
            env={**os.environ, "PATH": bin_path},
            capture_output=True,
            text=True,
            check=True,
        )

    first = push(str(Path(sys.executable).parent) + os.pathsep + path)
    assert "missing_receipt" in first.stderr
    assert "brigade work verify run" in first.stderr
    log = push_repo / ".git" / "brigade" / "push-gate.jsonl"
    (row,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert (row["ref"], row["tip"], row["tree"]) == ("refs/heads/feature", tip, _tree(linked))
    assert row["would_block"] is True and row["worktree"] == str(linked)

    _commit(linked)
    isolated = push_repo.parent / "isolated-bin"
    isolated.mkdir()
    for command in ("git", "bash", "cat"):
        executable = shutil.which(command)
        assert executable is not None
        (isolated / command).symlink_to(executable)
    push(str(isolated))
    row = json.loads(log.read_text().splitlines()[-1])
    assert row["result"] == "gate_error"
    assert row["reason"] == "brigade_missing"
    assert _gate(linked, "install", "--mode", "off") == 0
    prior = log.read_bytes()
    _commit(linked)
    push(str(isolated))
    assert log.read_bytes() == prior


def test_doctor_reports_claude_latch(tmp_path):
    from brigade.claude_hooks import latch_doctor, runtime

    session = "example-test-session"
    runtime.write_session_state(tmp_path, session, {"session_id": session, "hook_timeout_count": 1})
    assert latch_doctor.check(tmp_path) is None
    runtime.write_session_state(tmp_path, session, {"session_id": session, "hook_timeout_count": 2})
    check = latch_doctor.check(tmp_path)
    assert check is not None
    assert check[0] == "WARN"
    assert "1 recent session(s)" in check[2]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX seeded content-guard shell hook fixture")
def test_managed_hook_preserves_guard_exit(push_repo, capsys):
    from brigade.work_cmd.push_gate import _guard_bytes

    hook = push_repo / ".git" / "hooks" / "pre-push"
    hook.write_bytes(_guard_bytes())
    hook.chmod(0o755)
    env = {**os.environ, "CONTENT_GUARD_DIR": str(push_repo / "nonexistent")}

    def run():
        return subprocess.run(
            [str(hook)], cwd=push_repo, input="", text=True, capture_output=True, env=env, check=False
        )

    before = run()
    assert before.returncode == 1
    assert _gate(push_repo, "install") == 0
    capsys.readouterr()
    after = run()
    assert (after.returncode, after.stdout, after.stderr) == (before.returncode, before.stdout, before.stderr)


@pytest.mark.parametrize("failure", ["timeout", "crash", "invalid"])
def test_unavailable_push_check_is_a_gate_error(push_repo, monkeypatch, failure):
    from brigade.work_cmd import push_gate_hook, push_gate_log

    tip = _git(push_repo, "rev-parse", "HEAD")
    tree = _tree(push_repo)

    def child(argv, *, stdout, **kwargs):
        assert argv[0] == "brigade"
        assert kwargs["timeout"] == 2.25
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        stdout.write(b"invalid output")
        return subprocess.CompletedProcess(argv, 137 if failure == "crash" else 0)

    monkeypatch.setattr(push_gate_hook, "_trees", lambda _: {tip: tree})
    monkeypatch.setattr(push_gate_hook.subprocess, "run", child)
    data = f"HEAD {tip} refs/heads/topic {'0' * len(tip)}\n".encode()
    push_gate_hook.run(push_repo / ".git", data)
    (row,) = push_gate_log.read(push_gate_log.log_path(push_repo / ".git"))
    assert row["result"] == "gate_error"
    assert row["would_block"] is False
    assert row["reason"] == {"timeout": "check_timeout", "crash": "check_failed", "invalid": "invalid_output"}[failure]
