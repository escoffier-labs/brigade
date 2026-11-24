"""Clone-local push gate installation, reporting and real Git execution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import shlex
from concurrent.futures import ThreadPoolExecutor
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


def _doctor(target):
    return next(check for check in doctor._check_publish_gate(target) if check[1] == "verify push gate")


def test_install_mode_and_uninstall(push_repo, capsys):
    assert _status(push_repo, capsys)["mode"] == "off"
    assert _doctor(push_repo)[0] == "INFO"
    assert _gate(push_repo, "install", "--mode", "enforce") == 2
    assert "does not exist yet" in capsys.readouterr().err
    assert _gate(push_repo, "install", "--mode", "off") == 0
    capsys.readouterr()
    status = _status(push_repo, capsys)
    assert status["wired"] and status["current"] and status["hook_version"] == 1
    assert not status["will_run"]
    assert _doctor(push_repo)[0] == "INFO"
    hook = Path(status["hook_path"])
    original = hook.read_bytes()
    assert _gate(push_repo, "install") == 0
    capsys.readouterr()
    assert hook.read_bytes() == original
    _git(push_repo, "config", "brigade.pushGate", "enforce")
    status = _status(push_repo, capsys)
    assert status["configured_mode"] == "enforce" and status["mode"] == "warn"
    assert _gate(push_repo, "uninstall") == 0
    capsys.readouterr()
    assert not hook.exists()
    assert _status(push_repo, capsys)["mode"] == "off"


def test_foreign_hook_is_preserved(push_repo, capsys):
    hook = push_repo / ".git" / "hooks" / "pre-push"
    hook.write_bytes(b"#!/bin/sh\nexit 3\n")
    assert _gate(push_repo, "install") == 0
    assert "mode=warn, not wired" in capsys.readouterr().out.splitlines()[0]
    assert _doctor(push_repo)[0] == "WARN"
    assert not _status(push_repo, capsys)["wired"]
    assert _gate(push_repo, "uninstall") == 0
    assert hook.read_bytes() == b"#!/bin/sh\nexit 3\n"


def test_modified_minimal_hook_is_preserved(push_repo, capsys):
    assert _gate(push_repo, "install") == 0
    capsys.readouterr()
    hook = Path(_status(push_repo, capsys)["hook_path"])
    modified = hook.read_bytes() + b"# local change\n"
    hook.write_bytes(modified)
    assert _gate(push_repo, "uninstall") == 0
    assert hook.read_bytes() == modified


def test_relative_tracked_hook_is_untouched_from_linked_worktree(push_repo, capsys):
    from brigade.work_cmd.push_gate import _guard_bytes

    hook = push_repo / "hooks" / "pre-push"
    hook.parent.mkdir()
    original = _guard_bytes()
    hook.write_bytes(original)
    hook.chmod(0o755)
    _commit(push_repo)
    _git(push_repo, "config", "core.hooksPath", "hooks")
    linked = push_repo.parent / "linked path"
    _git(push_repo, "worktree", "add", "-b", "feature", str(linked))
    before = _git(push_repo, "config", "--local", "--list")
    assert _gate(linked, "install") == 0
    capsys.readouterr()
    assert _status(linked, capsys)["wired"]
    assert _status(push_repo, capsys)["wired"]
    assert _git(push_repo, "config", "--local", "--list") == before + "\nbrigade.pushgate=warn"
    assert _git(push_repo, "status", "--short") == _git(linked, "status", "--short") == ""
    assert (linked / "hooks" / "pre-push").read_bytes() == original
    assert _gate(linked, "uninstall") == 0
    assert _git(push_repo, "config", "--local", "--list") == before


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


def _path_with_brigade():
    return str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]


def _log_rows(repo):
    log = repo / ".git" / "brigade" / "push-gate.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def _bare_remote(repo):
    remote = repo.with_name(f"{repo.name}-remote.git")
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "set-url", "origin", str(remote))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pre-push hook and restricted PATH fixture")
def test_concurrent_push_from_worktrees_and_missing_brigade(push_repo, capsys, monkeypatch):
    _policy(push_repo)
    _bare_remote(push_repo)
    links = [push_repo.parent / f"linked path {i}" for i in range(2)]
    for i, linked in enumerate(links):
        _git(push_repo, "worktree", "add", "-b", f"feature{i}", str(linked))
    assert _gate(links[0], "install") == 0
    capsys.readouterr()
    tips = [_commit(linked) for linked in links]
    git = shutil.which("git")
    assert git is not None

    def push(index, bin_path):
        return subprocess.run(
            [git, "push", "origin", f"feature{index}"],
            cwd=links[index],
            env={**os.environ, "PATH": bin_path},
            capture_output=True,
            text=True,
            check=True,
        )

    with ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(push, i, _path_with_brigade()) for i in range(2)]
        for job in jobs:
            pushed = job.result()
            assert "missing_receipt" in pushed.stderr
            assert "brigade work verify run" in pushed.stderr
    rows = sorted(_log_rows(push_repo), key=lambda row: row["ref"])
    assert [(row["ref"], row["tip"], row["tree"], row["worktree"]) for row in rows] == [
        (f"refs/heads/feature{i}", tip, _tree(link), str(link))
        for i, (tip, link) in enumerate(zip(tips, links, strict=True))
    ]
    assert all(row["would_block"] for row in rows)

    _commit(links[0])
    isolated = push_repo.parent / "isolated-bin"
    isolated.mkdir()
    for command in ("git", "bash", "cat"):
        executable = shutil.which(command)
        assert executable is not None
        (isolated / command).symlink_to(executable)
    monkeypatch.setenv("PATH", str(isolated))
    push(0, str(isolated))
    assert sorted(_log_rows(push_repo), key=lambda row: row["ref"]) == rows
    info = _status(links[0], capsys)
    assert info["wired"] and not info["brigade_on_path"] and not info["will_run"]
    assert _doctor(links[0])[0] == "WARN"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX content guard shell hook fixture")
def test_global_template_preserves_history_guard(push_repo, capsys, monkeypatch):
    from brigade.work_cmd.push_gate import _guard_bytes, _snippet

    _policy(push_repo)
    _bare_remote(push_repo)
    shared = push_repo.parent / "shared hooks"
    shared.mkdir()
    global_config = push_repo.parent / "global config"
    _git(push_repo, "config", "--file", str(global_config), "core.hooksPath", str(shared))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    global_before = global_config.read_bytes()
    hook = shared / "pre-push"
    old = _guard_bytes().replace(_snippet(), b"")
    hook.write_bytes(old)
    hook.chmod(0o755)
    assert _gate(push_repo, "install") == 0
    notice = capsys.readouterr().out
    assert "mode=warn, not wired" in notice.splitlines()[0]
    assert _snippet().decode("ascii") in notice and str(hook) in notice
    assert hook.read_bytes() == old and _doctor(push_repo)[0] == "WARN"

    tools_dir = push_repo.parent / "tools with spaces"
    tools_dir.mkdir()
    fake = tools_dir / "brigade"
    real = shlex.quote(str(Path(sys.executable).parent / "brigade"))
    fake.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        '  guard) cat; exit "${TEST_GUARD_EXIT:-0}";;\n'
        "  scrub) exit 0;;\n"
        '  work) if [ "$2" = rails ]; then exit 0; fi;;\n'
        f'esac\nexec {real} "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools_dir) + os.pathsep + _path_with_brigade())
    tip = _commit(push_repo)
    monkeypatch.setenv("TEST_GUARD_EXIT", "1")
    line = f"refs/heads/main {tip} refs/heads/main {'0' * len(tip)}\n"

    def run_hook():
        return subprocess.run(
            [str(hook), "origin", _git(push_repo, "remote", "get-url", "origin")],
            cwd=push_repo,
            input=line,
            capture_output=True,
            text=True,
            check=False,
        )

    baseline = run_hook()
    assert baseline.returncode == 1 and tip in baseline.stdout and "BLOCKED" in baseline.stderr
    current = _guard_bytes()
    hook.write_bytes(current)
    assert _gate(push_repo, "install") == 0
    assert "mode=warn, wired" in capsys.readouterr().out
    assert hook.read_bytes() == current and global_config.read_bytes() == global_before
    assert _status(push_repo, capsys)["will_run"] and _doctor(push_repo)[0] == "INFO"
    blocked = run_hook()
    assert blocked.returncode == baseline.returncode and blocked.stdout == baseline.stdout
    assert "BLOCKED" in blocked.stderr and "missing_receipt" in blocked.stderr
    (row,) = _log_rows(push_repo)
    assert row["tip"] == tip

    monkeypatch.setenv("TEST_GUARD_EXIT", "0")
    _git(push_repo, "push", "origin", "main")
    assert len(_log_rows(push_repo)) == 2
    assert _gate(push_repo, "uninstall") == 0
    assert hook.read_bytes() == current and global_config.read_bytes() == global_before
    assert len(_log_rows(push_repo)) == 2


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


@pytest.mark.parametrize("failure", ["timeout", "crash"])
def test_evaluator_failure_is_a_gate_error(push_repo, monkeypatch, failure):
    import io

    from brigade.work_cmd import push_gate_hook, verify_push

    tip = _git(push_repo, "rev-parse", "HEAD")
    _policy(push_repo)
    monkeypatch.chdir(push_repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"HEAD {tip} refs/heads/topic {'0' * len(tip)}\n"))
    git = verify_push.verify_manifest_git._git

    def child(*argv, **kwargs):
        if "merge-base" in argv:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 1)
            raise RuntimeError("fixture crash")
        return git(*argv, **kwargs)

    monkeypatch.setattr(verify_push.verify_manifest_git, "_git", child)
    assert push_gate_hook.run() == 0
    (row,) = _log_rows(push_repo)
    assert row["result"] == "gate_error" and row["would_block"] is False
    assert row["reason"] == {"timeout": "check_timeout", "crash": "check_failed"}[failure]


def test_log_byte_bound_keeps_newest_records(push_repo, monkeypatch):
    from brigade.work_cmd import push_gate_log

    path = push_gate_log.log_path(push_repo / ".git")
    monkeypatch.setattr(push_gate_log, "_BYTE_LIMIT", 256)
    push_gate_log.append(path, [{"index": i, "padding": "a" * 64} for i in range(10)])
    assert path.stat().st_size <= 256
    assert [json.loads(line)["index"] for line in path.read_bytes().splitlines()] == [8, 9]


@pytest.mark.parametrize("layout", ["sidecar-only", "old-state", "seventeenth"])
def test_doctor_reads_latch_beyond_recent_state_files(tmp_path, layout):
    import time

    from brigade.claude_hooks import latch_doctor, runtime, session_state

    session = "example-latched-session"
    state = runtime._state_path(tmp_path, session)
    if layout != "sidecar-only":
        runtime.write_session_state(tmp_path, session, {"session_id": session})
        if layout == "old-state":
            old = time.time() - 48 * 3600
            os.utime(state, (old, old))
    for _ in range(runtime.HOOK_TIMEOUT_LATCH_AFTER):
        session_state._append_timeout_marker(tmp_path, session)
    if layout == "sidecar-only":
        assert not state.exists()
    if layout == "seventeenth":
        older = time.time() - 60
        journal = state.with_suffix(".json.timeouts")
        os.utime(state, (older, older))
        os.utime(journal, (older, older))
        for i in range(16):
            newer = f"example-newer-{i}"
            runtime.write_session_state(tmp_path, newer, {"session_id": newer})

    assert session_state._read_hook_latch(tmp_path, session)[0]
    check = latch_doctor.check(tmp_path)
    assert check is not None and check[0] == "WARN"
    assert "1 recent session(s)" in check[2] and "64 most recent" in check[2]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX content guard shell hook fixture")
@pytest.mark.skipif(shutil.which("timeout") is None, reason="requires GNU coreutils timeout on PATH")
def test_hook_timeout_allows_push_and_preserves_guard_refs(push_repo, monkeypatch):
    from time import monotonic

    from brigade.work_cmd.push_gate import _guard_bytes

    _policy(push_repo)
    _bare_remote(push_repo)
    hook = push_repo / ".git" / "hooks" / "pre-push"
    hook.write_bytes(_guard_bytes())
    hook.chmod(0o755)
    tools_dir = push_repo.parent / f"{push_repo.name}-bin"
    tools_dir.mkdir()
    fake = tools_dir / "brigade"
    fake.write_text(
        '#!/bin/sh\ncase "$1 $2 $3" in\n  "work verify push-gate") exec sleep 30;;\n  guard*) cat;;\nesac\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools_dir) + os.pathsep + _path_with_brigade())
    assert _gate(push_repo, "install") == 0
    tip = _commit(push_repo)
    start = monotonic()
    pushed = subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=push_repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=8,
    )
    assert monotonic() - start < 8
    assert pushed.stderr.count("pre-push: push gate timed out; push continues") == 1
    assert tip in pushed.stdout.splitlines()


def test_doctor_reports_latch_scan_cutoff(tmp_path, monkeypatch):
    from brigade.claude_hooks import latch_doctor, runtime

    monkeypatch.setattr(latch_doctor, "_SCAN_LIMIT", 2)
    for i in range(3):
        session = f"example-session-{i}"
        runtime.write_session_state(tmp_path, session, {"session_id": session})

    check = latch_doctor.check(tmp_path)
    assert check is not None and check[0] == "INFO"
    assert "directory scan cut short after 2 entries" in check[2]
