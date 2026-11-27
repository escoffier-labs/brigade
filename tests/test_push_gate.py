"""Clone-local push gate installation, reporting and real Git execution."""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

import pytest

from brigade import doctor
from brigade.cli import main
from brigade.work_cmd import push_gate, push_gate_hook, verify_push

from tests._home import set_home
from tests.test_pre_push_hook import HELPER_TIMEOUT_SECONDS, _hook_shell
from tests.test_verify_push import (
    CHECK_ENV_NAMES,
    ENV_PREFIX_MESSAGE,
    IDENTITY_WRAPPERS,
    SENTINEL_ENV_VALUE,
    PREFIX,
    SELECTOR,
    _commit,
    _git,
    _line,
    _policy,
    _receipt,
    _single,
    _tree,
)
from tests.test_verify_push import push_repo as push_repo
from tests.test_verify_push import verdict_budget as verdict_budget


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
    assert _gate(push_repo, "install", "--mode", "off") == 0
    capsys.readouterr()
    status = _status(push_repo, capsys)
    assert status["wired"] and status["current"] and status["hook_version"] == 2
    assert not status["will_run"]
    assert _doctor(push_repo)[0] == "INFO"
    hook = Path(status["hook_path"])
    original = hook.read_bytes()
    assert _gate(push_repo, "install") == 0
    capsys.readouterr()
    assert hook.read_bytes() == original
    assert _gate(push_repo, "install", "--mode", "enforce") == 0
    assert "mode=enforce, wired" in capsys.readouterr().out
    assert _git(push_repo, "config", "--local", "--get", "brigade.pushGate") == "enforce"
    status = _status(push_repo, capsys)
    assert status["configured_mode"] == status["mode"] == "enforce"
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


@cache
def _gate_bash():
    return _hook_shell()


def _run_gate_hook(repo, hook, refs):
    result = subprocess.run(
        [_gate_bash(), Path(hook).as_posix()],
        cwd=repo,
        input=refs,
        text=True,
        capture_output=True,
        timeout=HELPER_TIMEOUT_SECONDS,
    )
    return _assert_subprocess_budget(repo, result)


def _bash_fixture(repo, monkeypatch, script):
    env_script = repo / ".git" / "gate-fixture.sh"
    env_script.write_bytes(script.encode("utf-8"))
    monkeypatch.setenv("BASH_ENV", env_script.as_posix())


def _log_rows(repo):
    log = repo / ".git" / "brigade" / "push-gate.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def _assert_subprocess_budget(repo, result):
    log = repo / ".git" / "brigade" / "push-gate.jsonl"
    timeout = next((row for row in _log_rows(repo) if row["reason"] == "check_timeout"), None) if log.exists() else None
    if timeout:
        message = f"Push gate check_timeout after {timeout['duration_ms']} ms (two-second evaluation budget)"
        if sys.platform == "win32":
            pytest.skip(f"Windows infrastructure timeout: {message}")
        pytest.fail(f"{message}: {result.stderr}")
    return result


def _push_with_verdict(repo):
    result = subprocess.run(
        ["git", "push", "origin", "main"], cwd=repo, capture_output=True, text=True, timeout=HELPER_TIMEOUT_SECONDS
    )
    return _assert_subprocess_budget(repo, result)


def _evaluate_hook(repo, tip, mode, monkeypatch, capsys):
    _git(repo, "config", "--local", "brigade.pushGate", mode)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(tip, "refs/heads/main") + "\n"))
    code = push_gate_hook.run(hook_v2=True)
    return code, capsys.readouterr().err


@pytest.mark.parametrize(
    ("has_pass", "check_exit", "status", "reason"),
    [(True, 0, "ok", "receipt_eligible"), (False, 1, "rejected", "receipt_ineligible")],
)
def test_hook_matches_push_check_with_newer_unrelated_run(
    push_repo, capsys, monkeypatch, has_pass, check_exit, status, reason
):
    tip = _policy(push_repo)
    tree = _tree(push_repo)
    if has_pass:
        _receipt(push_repo, tree)
    _receipt(push_repo, tree, second=2, argv=["git", "status"])
    code, result = _single(push_repo, capsys)

    _git(push_repo, "config", "brigade.pushGate", "warn")
    monkeypatch.chdir(push_repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(tip, "refs/heads/topic") + "\n"))
    assert push_gate_hook.run() == 0
    (row,) = _log_rows(push_repo)
    assert (code, result["status"], row["result"], row["reason"]) == (check_exit, status, status, reason)
    assert row["would_block"] is (not has_pass)
    if not has_pass:
        assert result["remedy"] in capsys.readouterr().err


@pytest.mark.parametrize("at_cap", [False, True], ids=["newer-failure", "scan-cap"])
def test_hook_matches_push_check_for_failure_or_scan_cap(push_repo, capsys, monkeypatch, at_cap):
    tip = _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    if at_cap:
        _receipt(push_repo, tree, second=2, argv=["git", "status"])
        monkeypatch.setattr(verify_push, "_RECEIPT_LIMIT", 1)
    else:
        _receipt(push_repo, tree, second=2, status="failed", command_status="failed")
    code, result = _single(push_repo, capsys)

    _git(push_repo, "config", "brigade.pushGate", "warn")
    monkeypatch.chdir(push_repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(tip, "refs/heads/topic") + "\n"))
    assert push_gate_hook.run() == 0
    (row,) = _log_rows(push_repo)
    output = capsys.readouterr()
    if at_cap:
        assert (code, result["status"], row["result"], row["reason"]) == (
            3,
            "indeterminate",
            "indeterminate",
            "verdict_unavailable",
        )
        assert row["would_block"] is False
        assert output.out == ""
        assert result["reason"] in output.err and result["remedy"] in output.err
        assert len(output.err.splitlines()) == 1
    else:
        assert (code, result["status"], row["result"], row["reason"]) == (
            1,
            "rejected",
            "rejected",
            "receipt_ineligible",
        )
        assert row["would_block"] is True
        assert "An earlier run had passed" in result["reason"]
        assert result["reason"] in output.err
        assert result["remedy"] in output.err


def _bare_remote(repo):
    remote = repo.with_name(f"{repo.name}-remote.git")
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "set-url", "origin", str(remote))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pre-push hook and restricted PATH fixture")
@pytest.mark.skipif(shutil.which("timeout") is None, reason="requires GNU coreutils timeout on PATH")
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
            timeout=HELPER_TIMEOUT_SECONDS,
        )

    with ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(push, i, _path_with_brigade()) for i in range(2)]
        for job in jobs:
            pushed = _assert_subprocess_budget(push_repo, job.result())
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
@pytest.mark.parametrize(
    "with_timeout",
    [
        pytest.param(True, marks=pytest.mark.skipif(shutil.which("timeout") is None, reason="requires GNU timeout")),
        False,
    ],
)
def test_global_template_preserves_history_guard(push_repo, capsys, monkeypatch, with_timeout):
    from brigade.work_cmd.push_gate import _guard_bytes, _snippet

    _policy(push_repo)
    _bare_remote(push_repo)
    shared = push_repo.with_name(f"{push_repo.name} shared hooks")
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

    tools_dir = push_repo.with_name(f"{push_repo.name} tools with spaces")
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
    if with_timeout:
        monkeypatch.setenv("PATH", str(tools_dir) + os.pathsep + _path_with_brigade())
    else:
        for command in ("git", "bash", "cat", "awk", "grep"):
            executable = shutil.which(command)
            assert executable is not None
            (tools_dir / command).symlink_to(executable)
        monkeypatch.setenv("PATH", str(tools_dir))
        assert shutil.which("timeout") is None
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
            timeout=HELPER_TIMEOUT_SECONDS,
        )

    baseline = run_hook()
    assert baseline.returncode == 1 and tip in baseline.stdout and "BLOCKED" in baseline.stderr
    current = _guard_bytes()
    hook.write_bytes(current)
    assert _gate(push_repo, "install") == 0
    assert "mode=warn, wired" in capsys.readouterr().out
    assert hook.read_bytes() == current and global_config.read_bytes() == global_before
    info = _status(push_repo, capsys)
    assert info["brigade_on_path"]
    assert info["timeout_on_path"] is info["will_run"] is with_timeout
    doctor_check = _doctor(push_repo)
    assert doctor_check[0] == ("INFO" if with_timeout else "WARN")
    blocked = run_hook()
    assert blocked.returncode == baseline.returncode and blocked.stdout == baseline.stdout
    assert "BLOCKED" in blocked.stderr
    if with_timeout:
        assert "missing_receipt" in blocked.stderr
        (row,) = _log_rows(push_repo)
        assert row["tip"] == tip
    else:
        assert "configured but will not run: timeout missing from PATH" in doctor_check[2]

    monkeypatch.setenv("TEST_GUARD_EXIT", "0")
    pushed = subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=push_repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=HELPER_TIMEOUT_SECONDS,
    )
    assert tip in pushed.stdout.splitlines()
    if not with_timeout:
        assert pushed.stderr.count("GNU coreutils timeout is missing from Git's PATH") == 1
    assert _gate(push_repo, "uninstall") == 0
    assert hook.read_bytes() == current and global_config.read_bytes() == global_before
    if with_timeout:
        assert len(_log_rows(push_repo)) == 2
    else:
        assert not (push_repo / ".git" / "brigade" / "push-gate.jsonl").exists()


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
    tip = _git(push_repo, "rev-parse", "HEAD")
    _policy(push_repo)
    _git(push_repo, "config", "brigade.pushGate", "warn")
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


@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize(
    ("case", "result", "reason"),
    [
        ("missing", "missing_receipt", "No local verification receipt matches this tree."),
        ("unrelated", "rejected", "required check"),
        ("empty-pytest", "rejected", "tests"),
        ("scan-cap", "indeterminate", "receipt scan reached its limit"),
        ("deadline", "gate_error", "two-second time limit"),
        ("crash", "gate_error", "could not evaluate verification receipts"),
        ("ok", "ok", None),
        ("no-policy", "not_applicable", None),
    ],
)
def test_hook_mode_verdicts(push_repo, monkeypatch, capsys, mode, case, result, reason):
    tip = _git(push_repo, "rev-parse", "HEAD") if case == "no-policy" else _policy(push_repo)
    tree = _tree(push_repo)
    if case == "unrelated":
        _receipt(push_repo, tree, argv=["git", "status"])
    if case == "empty-pytest":
        _receipt(push_repo, tree, passed=0, status="failed", command_status="failed")
    if case == "scan-cap":
        root = push_repo / ".brigade" / "work" / "verify-runs"
        for i in range(verify_push._RECEIPT_LIMIT + 1):
            run = root / f"20260101-120000-work-verify-{i:06x}"
            run.mkdir(parents=True)
            (run / "receipt.json").write_text("{}", encoding="utf-8")
    if case == "deadline":

        def timed_out(self, *args, **kwargs):
            verify_push._remaining(self.deadline)
            monkeypatch.setattr(verify_push, "monotonic", lambda: self.deadline)
            verify_push._remaining(self.deadline)

        monkeypatch.setattr(verify_push._Receipts, "matching", timed_out)
    if case == "crash":

        def crash(*args, **kwargs):
            raise RuntimeError("fixture crash")

        monkeypatch.setattr(verify_push, "_check", crash)
    if case == "ok":
        _receipt(push_repo, tree)
    _git(push_repo, "config", "brigade.pushGate", mode)
    monkeypatch.chdir(push_repo)
    ref = "refs/heads/topic"
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(tip, ref) + "\n"))
    blocked = mode == "enforce" and reason is not None
    assert push_gate_hook.run(hook_v2=True) == (3 if blocked else 0)
    (row,) = _log_rows(push_repo)
    assert row["mode"] == mode and row["result"] == result
    if mode == "enforce":
        assert row["blocked"] is blocked
    if case in {"crash", "deadline", "scan-cap"} and mode == "warn":
        advisory = capsys.readouterr().err.splitlines()
        assert len(advisory) == 1 and reason in advisory[0] and " Run: " in advisory[0]
    if blocked:
        err = capsys.readouterr().err
        assert ref in err and reason in err
        if case == "crash":
            assert "brigade work verify push-check" in err
        elif case == "scan-cap":
            assert "brigade work verify push-gate prune" in err
        else:
            repair = next(line for line in err.splitlines() if line.startswith("brigade work verify run "))
            argv = shlex.split(repair)
            assert argv[:4] == ["brigade", "work", "verify", "run"]
            assert json.loads(argv[argv.index("--argv-json") + 1])[-1] == "tests/test_example.py"


@pytest.mark.parametrize(("mode", "block"), [("warn", False), ("enforce", True)])
def test_installed_gate_controls_real_push(push_repo, capsys, monkeypatch, mode, block):
    (push_repo / "focused.py").write_text("print('1 passed in 0.01s')\n", encoding="utf-8")
    base = _policy(push_repo)
    _bare_remote(push_repo)
    remote = _git(push_repo, "remote", "get-url", "origin")
    _git(push_repo, "push", "origin", "main")
    monkeypatch.setenv("PATH", _path_with_brigade())
    assert _gate(push_repo, "install", "--mode", mode) == 0
    capsys.readouterr()
    (push_repo / "src.py").write_text("value = 2\n", encoding="utf-8")
    tip = _commit(push_repo)

    pushed = _push_with_verdict(push_repo)
    assert (pushed.returncode != 0) is block
    assert "refs/heads/main" in pushed.stderr and "brigade work verify run" in pushed.stderr
    assert _git(Path(remote), "rev-parse", "refs/heads/main") == (base if block else tip)
    assert _gate(push_repo, "report", "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["blocked"] == int(block)
    if block:
        remedy = next(line for line in pushed.stderr.splitlines() if line.startswith("brigade work verify run "))
        subprocess.run(
            shlex.split(remedy),
            cwd=push_repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=HELPER_TIMEOUT_SECONDS,
        )
        verified = _push_with_verdict(push_repo)
        assert verified.returncode == 0, verified.stderr
        assert _git(Path(remote), "rev-parse", "refs/heads/main") == tip
        assert [(row["mode"], row["blocked"]) for row in _log_rows(push_repo)] == [
            ("enforce", True),
            ("enforce", False),
        ]


def test_enforce_names_bad_ref_and_blocks_whole_push(push_repo, monkeypatch, capsys):
    good = _policy(push_repo)
    _receipt(push_repo, _tree(push_repo))
    (push_repo / "src.py").write_text("value = 2\n", encoding="utf-8")
    bad = _commit(push_repo)
    _git(push_repo, "config", "brigade.pushGate", "enforce")
    monkeypatch.chdir(push_repo)
    refs = [_line(good, "refs/heads/good"), _line(bad, "refs/heads/bad")]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(refs) + "\n"))
    assert push_gate_hook.run(hook_v2=True) == 3
    output = capsys.readouterr().err
    assert "refs/heads/bad" in output and "refs/heads/good" not in output
    assert [(row["result"], row["blocked"]) for row in _log_rows(push_repo)] == [
        ("ok", True),
        ("missing_receipt", True),
    ]


@pytest.mark.parametrize(
    "verdict", ["ok", "not_applicable", "base_predates_policy", "missing_receipt", "rejected", "indeterminate"]
)
def test_all_verdicts_agree_between_push_check_and_enforce_hook(push_repo, monkeypatch, capsys, verdict):
    tip = (
        _git(push_repo, "rev-parse", "HEAD")
        if verdict in {"not_applicable", "base_predates_policy"}
        else _policy(push_repo)
    )
    if verdict == "base_predates_policy":
        _policy(push_repo)
        _git(push_repo, "switch", "--detach", tip)
    if verdict in {"ok", "rejected", "indeterminate"}:
        path = _receipt(push_repo, _tree(push_repo), passed=int(verdict != "rejected"))
        if verdict == "indeterminate":
            path.write_text("{", encoding="utf-8")

    _, result = _single(push_repo, capsys)
    assert result["status"] == verdict
    _git(push_repo, "config", "brigade.pushGate", "enforce")
    monkeypatch.chdir(push_repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(tip, "refs/heads/topic") + "\n"))
    blocked = verdict in {"missing_receipt", "rejected", "indeterminate"}
    assert push_gate_hook.run(hook_v2=True) == (3 if blocked else 0)
    (row,) = _log_rows(push_repo)
    assert (row["result"], row["blocked"]) == (result["status"], blocked)
    if blocked:
        assert result["reason"] in capsys.readouterr().err


@pytest.mark.parametrize("global_mode", [None, "enforce"])
def test_unset_hook_never_invokes_gate(push_repo, monkeypatch, global_mode):
    from brigade.work_cmd.push_gate import _minimal_hook

    tip = _policy(push_repo)
    if global_mode:
        config = push_repo / "fake-global-config"
        _git(push_repo, "config", "--file", str(config), "brigade.pushGate", global_mode)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    hook = push_repo / ".git" / "hooks" / "pre-push"
    hook.write_bytes(_minimal_hook())
    hook.chmod(0o755)
    _bash_fixture(
        push_repo,
        monkeypatch,
        "brigade() { touch gate-was-called; exit 1; }\ntimeout() { if [[ $3 == 1 ]]; then return 0; fi; brigade; }\n",
    )
    result = _run_gate_hook(push_repo, hook, _line(tip, "refs/heads/topic"))
    assert result.returncode == 0
    assert not (push_repo / "gate-was-called").exists()


@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize("failure", ["no-brigade", "no-timeout", "wrong-timeout", "process-crash", "old-cli"])
def test_installed_gate_startup_error(push_repo, capsys, monkeypatch, mode, failure):
    tip = _policy(push_repo)
    assert _gate(push_repo, "install", "--mode", mode) == 0
    capsys.readouterr()
    hook = push_repo / ".git" / "hooks" / "pre-push"
    unavailable = {"no-brigade": "brigade", "no-timeout": "timeout"}.get(failure, "")
    body = {
        "wrong-timeout": "return 2",
        "old-cli": "[[ $3 == 1 ]] && return 0; [[ ${*: -1} != --hook-v2 ]] || return 2",
    }.get(failure, "[[ $3 == 1 ]] && return 0; return 1")
    _bash_fixture(
        push_repo,
        monkeypatch,
        f'command() {{ if [[ $1 == -v && $2 == "{unavailable}" ]]; then return 1; fi; builtin command "$@"; }}\n'
        f"brigade() {{ return 1; }}\ntimeout() {{ {body}; }}\n",
    )
    for ref, commit, remote in [
        ("refs/heads/topic", tip, None),
        ("refs/tags/topic", tip, None),
        ("refs/heads/deleted", "0" * len(tip), tip),
    ]:
        result = _run_gate_hook(push_repo, hook, _line(commit, ref, remote))
        blocked = mode == "enforce" and ref == "refs/heads/topic"
        assert result.returncode == int(blocked), result.stderr
        if blocked:
            assert ref in result.stderr and "brigade work verify push-gate status" in result.stderr
        if mode == "warn" and failure == "no-brigade":
            assert result.stderr.count("brigade is missing from Git's PATH") == 1
        if failure == "wrong-timeout" and (mode == "warn" or blocked):
            assert "is unusable (the -k probe failed or timed out)" in result.stderr


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
@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_hook_watchdog_controls_push_and_preserves_guard_refs(push_repo, monkeypatch, mode):
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
    assert _gate(push_repo, "install", "--mode", mode) == 0
    tip = _commit(push_repo)
    pushed = subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=push_repo,
        capture_output=True,
        text=True,
        timeout=HELPER_TIMEOUT_SECONDS,
    )
    if mode == "warn":
        assert pushed.returncode == 0
        assert pushed.stderr.count("the push gate process timed out") == 1
        assert tip in pushed.stdout.splitlines()
    else:
        assert pushed.returncode != 0
        assert "refs/heads/main: the push gate process timed out" in pushed.stderr
        assert "brigade work verify push-gate status" in pushed.stderr
        assert tip not in pushed.stdout.splitlines()


def test_doctor_reports_latch_scan_cutoff(tmp_path, monkeypatch):
    from brigade.claude_hooks import latch_doctor, runtime

    monkeypatch.setattr(latch_doctor, "_SCAN_LIMIT", 2)
    for i in range(3):
        session = f"example-session-{i}"
        runtime.write_session_state(tmp_path, session, {"session_id": session})

    check = latch_doctor.check(tmp_path)
    assert check is not None and check[0] == "INFO"
    assert "directory scan cut short after 2 entries" in check[2]


@pytest.mark.parametrize(
    ("value", "mode", "invalid"),
    [
        ("Enforce", "enforce", False),
        ("ENFORCE", "enforce", False),
        ("enforce ", "enforce", False),
        (" \tWaRn\t ", "warn", False),
        ("enforced", "off", True),
        ("block", "off", True),
        ("", "off", False),
    ],
)
def test_config_values_apply_in_minimal_hook(push_repo, monkeypatch, capsys, value, mode, invalid):
    tip = _policy(push_repo)
    monkeypatch.setenv("PATH", _path_with_brigade())
    assert _gate(push_repo, "install") == 0
    capsys.readouterr()
    _git(push_repo, "config", "brigade.pushGate", value)
    info = _status(push_repo, capsys)
    assert (info["configured_mode"], info["mode"], info["invalid_mode"]) == (value, mode, invalid)
    assert info["enforce_effective"] is (mode == "enforce")
    assert _doctor(push_repo)[0] == ("WARN" if invalid else "INFO")
    result = _run_gate_hook(push_repo, info["hook_path"], _line(tip, "refs/heads/topic"))
    assert (result.returncode != 0) is (mode == "enforce")
    if invalid:
        assert result.stderr.splitlines() == [
            f"push gate: unrecognised brigade.pushGate value '{value}'; the gate is not running"
        ]
    elif mode == "off":
        assert result.stderr == ""
    else:
        assert "missing_receipt" in result.stderr


@pytest.mark.parametrize("conditional", [False, True])
def test_clone_local_includes_enforce(push_repo, monkeypatch, capsys, conditional):
    tip = _policy(push_repo)
    monkeypatch.setenv("PATH", _path_with_brigade())
    assert _gate(push_repo, "install", "--mode", "enforce") == 0
    capsys.readouterr()
    _git(push_repo, "config", "--unset", "brigade.pushGate")
    included = push_repo / ".git" / "extra-config"
    _git(push_repo, "config", "--file", str(included), "brigade.pushGate", "enforce")
    key = f"includeIf.gitdir:{push_repo.as_posix()}/.git.path" if conditional else "include.path"
    _git(push_repo, "config", "--local", key, str(included))
    info = _status(push_repo, capsys)
    assert info["enforce_effective"] and info["ignored_mode"] is None
    result = _run_gate_hook(push_repo, info["hook_path"], _line(tip, "refs/heads/topic"))
    assert result.returncode == 3 and "missing_receipt" in result.stderr


def test_install_reports_included_mode_that_overrides_direct_setting(push_repo, monkeypatch, capsys):
    monkeypatch.setenv("PATH", _path_with_brigade())
    assert _gate(push_repo, "install", "--mode", "off") == 0
    capsys.readouterr()
    included = push_repo / ".git" / "extra-config"
    _git(push_repo, "config", "--file", str(included), "brigade.pushGate", "warn")
    _git(push_repo, "config", "--local", "include.path", str(included))
    assert _gate(push_repo, "install", "--mode", "enforce") == 1
    error = capsys.readouterr().err
    assert "included Git config" in error and included.name in error
    assert _git(push_repo, "config", "--local", "--get", "brigade.pushGate") == "off"
    assert _status(push_repo, capsys)["mode"] == "warn"


@pytest.mark.parametrize("scope", ["global", "system", "worktree"])
def test_other_scopes_are_reported_and_do_not_activate(push_repo, monkeypatch, capsys, scope):
    tip = _policy(push_repo)
    assert _gate(push_repo, "install", "--mode", "off") == 0
    capsys.readouterr()
    _git(push_repo, "config", "--local", "--unset", "brigade.pushGate")
    if scope == "worktree":
        _git(push_repo, "config", "--local", "extensions.worktreeConfig", "true")
        _git(push_repo, "config", "--worktree", "brigade.pushGate", "enforce")
    else:
        file = push_repo / f"fixture-{scope}-config"
        _git(push_repo, "config", "--file", str(file), "brigade.pushGate", "enforce")
        if scope == "system":
            monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
        monkeypatch.setenv(f"GIT_CONFIG_{scope.upper()}", str(file))
    info = _status(push_repo, capsys)
    assert info["mode"] == "off" and info["ignored_mode"] == "enforce"
    severity, _, summary = _doctor(push_repo)
    assert severity == "WARN" and "Only clone-local config and its includes" in summary
    result = _run_gate_hook(push_repo, info["hook_path"], _line(tip, "refs/heads/topic"))
    assert result.returncode == 0 and result.stderr == ""


def test_old_hook_with_enforce_reports_ineffective_and_records_allow(push_repo, monkeypatch, capsys):
    from brigade.work_cmd.push_gate import ENFORCE_V1_NOTICE

    _policy(push_repo)
    _bare_remote(push_repo)
    monkeypatch.setenv("PATH", _path_with_brigade())
    hook = push_repo / ".git" / "hooks" / "pre-push"
    hook.write_text(
        "#!/usr/bin/env bash\n# BEGIN Brigade push gate v1\n"
        "brigade work verify push-gate run || true\n# END Brigade push gate v1\nexit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    assert _gate(push_repo, "install", "--mode", "enforce") == 1
    output = capsys.readouterr()
    notice = "Replace the version 1 hook block before enabling enforce mode."
    assert (output.out + output.err).count(notice) == 1
    assert ENFORCE_V1_NOTICE not in output.out + output.err
    assert _status(push_repo, capsys)["configured_mode"] is None
    _git(push_repo, "config", "--local", "brigade.pushGate", "enforce")
    info = _status(push_repo, capsys)
    assert info["hook_version"] == 1 and info["will_run"] and not info["enforce_effective"]
    severity, _, summary = _doctor(push_repo)
    assert severity == "FAIL" and ENFORCE_V1_NOTICE in summary
    pushed = subprocess.run(
        ["git", "push", "origin", "main"], cwd=push_repo, capture_output=True, text=True, timeout=HELPER_TIMEOUT_SECONDS
    )
    assert pushed.returncode == 0 and ENFORCE_V1_NOTICE in pushed.stderr
    assert "missing_receipt" not in pushed.stderr
    (row,) = _log_rows(push_repo)
    assert row["reason"] == "hook_too_old" and row["blocked"] is False
    _git(push_repo, "tag", "example-tag")
    _git(push_repo, "branch", "example-deletion")
    for refspec in ("refs/tags/example-tag", "example-deletion", ":example-deletion"):
        update = subprocess.run(
            ["git", "push", "origin", refspec],
            cwd=push_repo,
            capture_output=True,
            text=True,
            timeout=HELPER_TIMEOUT_SECONDS,
        )
        assert update.returncode == 0
        assert (ENFORCE_V1_NOTICE in update.stderr) is (refspec == "example-deletion")


@pytest.mark.parametrize("failure", ["wrong-timeout", "missing-timeout", "missing-brigade", "unwired"])
def test_install_enforce_does_not_write_mode_when_unavailable(push_repo, monkeypatch, capsys, failure):
    from brigade.work_cmd import push_gate

    assert _gate(push_repo, "install") == 0
    capsys.readouterr()
    real_which = push_gate.which
    if failure == "unwired":
        info = _status(push_repo, capsys)
        Path(info["hook_path"]).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    elif failure == "wrong-timeout":
        _bash_fixture(push_repo, monkeypatch, "timeout() { return 2; }\n")
        if sys.platform != "win32":
            wrong = push_repo / "timeout"
            wrong.write_bytes(b"#!/bin/sh\nexit 2\n")
            wrong.chmod(0o755)
            monkeypatch.setattr(push_gate, "which", lambda name: str(wrong) if name == "timeout" else real_which(name))
    elif failure == "missing-timeout":
        monkeypatch.setattr(push_gate, "_timeout_state", lambda: "missing")
    else:
        monkeypatch.setattr(push_gate, "which", lambda name: None if name == "brigade" else real_which(name))
    assert _gate(push_repo, "install", "--mode", "enforce") == 1
    output = capsys.readouterr()
    assert {"wrong-timeout": "timeout", "missing-timeout": "timeout", "missing-brigade": "brigade", "unwired": "Wire"}[
        failure
    ] in output.err
    info = _status(push_repo, capsys)
    assert info["mode"] == "warn" and not info["enforce_effective"]
    if failure == "wrong-timeout":
        assert info["timeout_state"] == "incompatible" and not info["timeout_on_path"]
        assert "does not support" in output.out


@pytest.mark.parametrize("hook_v2", [False, True])
def test_mode_read_failure_is_advisory(push_repo, monkeypatch, capsys, hook_v2):
    def fail(target):
        raise OSError("fixture Git config failure \x1b[31m\nsecond line")

    monkeypatch.setattr(push_gate_hook, "_mode", fail)
    assert push_gate_hook.run(hook_v2=hook_v2) == 0
    message, remedy = capsys.readouterr().err.splitlines()
    assert "did not run for this push because Git could not read the configuration" in message
    assert r"fixture Git config failure \x1b[31m\nsecond line" in message
    assert remedy == "Fix the Git configuration, then check with brigade work verify push-gate status."

    from brigade.work_cmd.push_gate import _minimal_hook

    hook = push_repo / ".git" / "hooks" / "pre-push"
    hook.write_bytes(_minimal_hook())
    hook.chmod(0o755)
    _bash_fixture(push_repo, monkeypatch, "git() { return 3; }\n")
    result = _run_gate_hook(push_repo, hook, "")
    assert result.returncode == 0 and result.stderr.count("did not run for this push") == 1


def test_retention_cap_warning_and_empty_report(push_repo, capsys):
    from brigade import config

    path = push_repo / config.CONFIG_REL_PATH
    path.parent.mkdir()
    path.write_text(
        '{"version": 1, "depth": "workspace", "harnesses": ["codex"], "verify_runs_keep": 129}', encoding="utf-8"
    )
    checks = doctor._check_publish_gate(push_repo)
    assert any(severity == "WARN" and "verify_runs_keep is 129" in message for severity, _, message in checks)
    assert _gate(push_repo, "report") == 0
    assert "blocked rows 0 | gate errors 0 | median n/a | p95 n/a" in capsys.readouterr().out


def test_manual_hook_v2_argument_is_registered(push_repo, monkeypatch, capsys):
    monkeypatch.chdir(push_repo)
    with pytest.raises(SystemExit) as result:
        main(["work", "verify", "push-gate", "run", "--hook-v2", "--help"])
    assert result.value.code == 0 and "--hook-v2" in capsys.readouterr().out


def test_git_deadline_warns_and_allows(push_repo, monkeypatch, capsys):
    from brigade import verify_manifest_git

    monkeypatch.setattr(verify_push, "_BUDGET_SECONDS", 2.0)

    tip = _policy(push_repo)
    real_run = subprocess.run
    budgets = []
    monkeypatch.setattr(push_gate_hook, "monotonic", lambda: 12.0)
    monkeypatch.setattr(verify_push, "monotonic", lambda: 12.0)
    monkeypatch.setattr(verify_manifest_git, "monotonic", lambda: 12.25)

    def timed_out_merge_base(args, **kwargs):
        if args[:2] == ["git", "merge-base"]:
            budgets.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return real_run(args, **kwargs)

    _git(push_repo, "config", "--local", "brigade.pushGate", "warn")
    monkeypatch.chdir(push_repo)
    monkeypatch.setattr(subprocess, "run", timed_out_merge_base)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(tip, "refs/heads/topic") + "\n"))
    assert push_gate_hook.run() == 0
    assert budgets == [1.75]
    assert "timed out" in capsys.readouterr().err.lower()
    (row,) = _log_rows(push_repo)
    assert row["reason"] == "check_timeout"


def test_stray_backup_pruning_preserves_gate_verdict(push_repo, monkeypatch, capsys):
    tip = _policy(push_repo)
    good = _receipt(push_repo, _tree(push_repo))
    for index in range(130):
        backup = good.parent.with_name(f"backup-{index}")
        backup.mkdir()
        (backup / "receipt.json").write_text("{", encoding="utf-8")
    assert _gate(push_repo, "prune") == 0
    output = capsys.readouterr()
    assert "Pruned 0 local verify run(s). Local receipts remaining: 1." in output.out
    assert output.err.count("mv -- ") == 130
    assert good.is_file() and len(list(good.parent.parent.glob("backup-*"))) == 130
    code, err = _evaluate_hook(push_repo, tip, "enforce", monkeypatch, capsys)
    assert code == 0 and err.count("ignoring malformed receipt") == 1


@pytest.mark.parametrize("store_kind", ["file", "symlink"])
def test_prune_and_gate_reject_non_directory_store(push_repo, capsys, store_kind):
    _policy(push_repo)
    store = push_repo / ".brigade" / "work" / "verify-runs"
    store.parent.mkdir(parents=True)
    real = push_repo / "existing-store"
    real.mkdir()
    token = real / "keep"
    token.touch()
    if store_kind == "symlink":
        store.symlink_to(real, target_is_directory=True)
    else:
        store.write_text("preserve me", encoding="utf-8")
    assert _gate(push_repo, "prune") == 1
    message = capsys.readouterr().err
    assert "receipt store" in message and repr(str(store)) in message
    assert token.is_file() and os.path.lexists(store)
    assert _single(push_repo, capsys)[1]["status"] == "indeterminate"


def test_prune_honors_grace_period_and_start_time_order(push_repo, capsys):
    from time import time

    _policy(push_repo)
    tree = _tree(push_repo)
    older = _receipt(push_repo, tree, started_at="2026-01-01T12:00:01.100000Z")
    newer = _receipt(push_repo, tree, second=2, started_at="2026-01-01T12:00:01.900000Z")
    destination = newer.parent.with_name("20260101-120001-work-verify-000000")
    newer.parent.rename(destination)
    config = push_repo / ".brigade" / "config.json"
    config.write_text(
        '{"version": 1, "depth": "workspace", "harnesses": ["codex"], "verify_runs_keep": 1}', encoding="utf-8"
    )
    assert _gate(push_repo, "prune") == 0
    assert "Pruned 0" in capsys.readouterr().out and older.is_file()
    past = time() - 120
    os.utime(older, (past, past))
    os.utime(older.parent, (past, past))
    assert _gate(push_repo, "prune") == 0
    assert "Pruned 1" in capsys.readouterr().out
    assert not older.exists() and (destination / "receipt.json").exists()


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_hanging_timeout_probe_is_bounded_in_real_push(push_repo, monkeypatch, capsys, mode):
    _policy(push_repo)
    _bare_remote(push_repo)
    monkeypatch.setenv("PATH", _path_with_brigade())
    assert _gate(push_repo, "install", "--mode", mode) == 0
    capsys.readouterr()
    _bash_fixture(push_repo, monkeypatch, "timeout() { exec sleep 30; }\n")
    result = subprocess.run(
        ["git", "push", "origin", "main"], cwd=push_repo, capture_output=True, text=True, timeout=HELPER_TIMEOUT_SECONDS
    )
    assert (result.returncode != 0) is (mode == "enforce")
    assert result.stderr.count("is unusable (the -k probe failed or timed out)") == 1


@pytest.mark.parametrize("missing", ["brigade", "timeout"])
def test_enforce_refusal_does_not_create_minimal_hook(push_repo, monkeypatch, capsys, missing):
    from brigade.work_cmd import push_gate

    real_which = push_gate.which
    if missing == "timeout":
        monkeypatch.setattr(push_gate, "_timeout_state", lambda: "missing")
    else:
        monkeypatch.setattr(push_gate, "which", lambda name: None if name == missing else real_which(name))
    git_config = push_repo / ".git" / "config"
    before = git_config.read_bytes()
    assert _gate(push_repo, "install", "--mode", "enforce") == 1
    assert missing in capsys.readouterr().err
    assert git_config.read_bytes() == before
    assert not (push_repo / ".git" / "hooks" / "pre-push").exists()


def test_unknown_mode_escapes_terminal_controls(push_repo, capsys):
    tip = _policy(push_repo)
    assert _gate(push_repo, "install", "--mode", "warn") == 0
    capsys.readouterr()
    _git(push_repo, "config", "--local", "brigade.pushGate", "block\x1b[31m\x07")
    hook = push_repo / ".git" / "hooks" / "pre-push"
    result = _run_gate_hook(push_repo, hook, _line(tip, "refs/heads/topic"))
    assert result.returncode == 0 and "unrecognised" in result.stderr
    assert "\x1b" not in result.stderr and "\x07" not in result.stderr
    assert len(result.stderr.splitlines()) == 1


def test_status_reports_git_config_include_cycle(push_repo, capsys):
    extra = push_repo / ".git" / "fixture-extra-config"
    _git(push_repo, "config", "--file", str(extra), "include.path", str(extra))
    _git(push_repo, "config", "--local", "include.path", str(extra))
    assert _gate(push_repo, "status") == 1
    message = capsys.readouterr().err
    assert "include depth" in message and "a Git worktree is required" not in message


def test_prune_reports_invalid_writer_receipt_before_removing_valid_runs(push_repo, capsys):
    from time import time

    _policy(push_repo)
    tree = _tree(push_repo)
    older = _receipt(push_repo, tree)
    invalid = _receipt(push_repo, tree, second=2)
    invalid.write_text("{", encoding="utf-8")
    _receipt(push_repo, tree, second=3)
    past = time() - 120
    os.utime(older, (past, past))
    os.utime(older.parent, (past, past))
    (push_repo / ".brigade" / "config.json").write_text(
        '{"version": 1, "depth": "workspace", "harnesses": ["codex"], "verify_runs_keep": 1}', encoding="utf-8"
    )
    assert _gate(push_repo, "prune") == 1
    error = capsys.readouterr().err
    assert repr(str(invalid)) in error and "mv -- " in error
    assert older.is_file() and invalid.is_file()


def test_cap_warns_once_for_stray_receipts_across_refs(push_repo, monkeypatch, capsys):
    base = _policy(push_repo)
    good = _receipt(push_repo, _tree(push_repo))
    _receipt(push_repo, _tree(push_repo), second=2)
    stranger = good.parent.with_name("backup-old")
    stranger.mkdir()
    (stranger / "receipt.json").write_text("{", encoding="utf-8")
    tip = _commit(push_repo, "second tip")
    _git(push_repo, "config", "--local", "brigade.pushGate", "enforce")
    monkeypatch.chdir(push_repo)
    monkeypatch.setattr(verify_push, "_RECEIPT_LIMIT", 1)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(_line(base, "refs/heads/base") + "\n" + _line(tip, "refs/heads/topic"))
    )
    assert push_gate_hook.run(hook_v2=True) == 3
    warning = capsys.readouterr().err
    assert warning.count("ignoring malformed receipt") == 1
    assert warning.count("local receipt scan reached its limit") == 2


def test_native_windows_timeout_status_probes_git_bash(tmp_path, monkeypatch):
    from brigade.work_cmd import push_gate

    git = tmp_path / "Git" / "cmd" / "git.exe"
    bash = git.parents[1] / "bin" / "bash.exe"
    for path in (git, bash):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(push_gate.sys, "platform", "win32")
    monkeypatch.setattr(push_gate, "which", lambda name: str(git) if name == "git" else None)

    def run(command, *, capture_output, timeout, check):
        assert command[:2] == [str(bash), "-c"]
        assert command[-4:] == ["-k", "1", "1", "true"]
        assert timeout == 2 and capture_output and not check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(push_gate.subprocess, "run", run)
    assert push_gate._timeout_state() == "usable"


@pytest.mark.parametrize("variant", ["minimal", "full", "exit-first", "never-run", "suffix", "harmless-prefix"])
def test_enforcement_status_requires_exact_hook(push_repo, monkeypatch, capsys, variant):
    from brigade.work_cmd.push_gate import _guard_bytes, _minimal_hook

    monkeypatch.setenv("PATH", _path_with_brigade())
    hook = push_repo / ".git" / "hooks" / "pre-push"
    shebang, body = _minimal_hook().split(b"\n", 1)
    contents = {
        "minimal": _minimal_hook(),
        "full": _guard_bytes(),
        "exit-first": shebang + b"\nexit 0\n" + body,
        "never-run": shebang + b"\nif false; then\n" + body + b"fi\n",
        "suffix": _minimal_hook() + b":\n",
        "harmless-prefix": shebang + b"\n:\n" + body,
    }[variant]
    hook.write_bytes(contents)
    hook.chmod(0o755)

    assert _gate(push_repo, "install", "--mode", "enforce") == 0
    notice = "Block present, hook not verified. Enforcement cannot be confirmed because the hook has other content."
    verified = variant in {"minimal", "full"}
    assert (notice in capsys.readouterr().out) is not verified
    info = _status(push_repo, capsys)
    assert info["current"] and info["wired"]
    assert info["hook_verified"] is info["enforce_effective"] is verified
    assert _gate(push_repo, "status") == 0
    assert (notice in capsys.readouterr().out) is not verified
    assert hook.read_bytes() == contents


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_newer_timeout_with_empty_collection_in_hook(push_repo, monkeypatch, capsys, mode):
    tip = _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    _receipt(push_repo, tree, second=2, status="failed", command_status="timed_out", passed=0)
    code, err = _evaluate_hook(push_repo, tip, mode, monkeypatch, capsys)
    assert (code != 0) is (mode == "enforce")
    assert "refs/heads/main (rejected)" in err and "timed out. An earlier run had passed." in err
    assert "brigade work verify run" in err


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_mode_read_error_allows_real_push(push_repo, monkeypatch, capsys, mode):
    _policy(push_repo)
    _bare_remote(push_repo)
    monkeypatch.setenv("PATH", _path_with_brigade())
    assert _gate(push_repo, "install", "--mode", mode) == 0
    capsys.readouterr()
    _bash_fixture(
        push_repo,
        monkeypatch,
        "git() {\n"
        '  if [[ "$1" == config ]]; then\n'
        "    printf 'fixture Git configuration error \\033[31m\\n' >&2\n"
        "    return 3\n"
        "  fi\n"
        '  command git "$@"\n'
        "}\n",
    )
    push = subprocess.run(
        ["git", "push", "origin", "main"], cwd=push_repo, capture_output=True, text=True, timeout=HELPER_TIMEOUT_SECONDS
    )
    assert push.returncode == 0, push.stderr
    assert push.stderr.count("did not run for this push because Git could not read the configuration") == 1
    assert "fixture" in push.stderr and "configuration" in push.stderr and "\x1b" not in push.stderr
    assert "Fix the Git configuration, then check with brigade work verify push-gate status." in push.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="Windows disallows ESC and newline in directory names")
def test_enforce_remedy_moves_exact_directory_with_control_characters(push_repo, monkeypatch, capsys):
    tip = _policy(push_repo)
    receipt = _receipt(push_repo, _tree(push_repo))
    stray = receipt.parent.with_name("bad \x1b[31m\n' receipt")
    receipt.parent.rename(stray)
    _git(push_repo, "config", "--local", "brigade.pushGate", "enforce")
    monkeypatch.chdir(push_repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(tip, "refs/heads/topic") + "\n"))
    assert push_gate_hook.run(hook_v2=True) == 3
    output = capsys.readouterr().err
    assert "\x1b" not in output and r"\x1b" in output and r"\n" in output
    remedy = next(line for line in output.splitlines() if line.startswith("mv -- "))
    scratch = push_repo.parent / "moved"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    subprocess.run([_gate_bash(), "-c", remedy], cwd=push_repo, check=True, timeout=HELPER_TIMEOUT_SECONDS)
    assert not stray.exists()
    (destination,) = scratch.iterdir()
    assert (destination / stray.name / "receipt.json").is_file()
    assert _single(push_repo, capsys)[1]["status"] == "missing_receipt"


def test_hook_escapes_ref_plan_guidance_and_preserves_remedy(push_repo, monkeypatch, capsys):
    tip = _policy(push_repo, coverage=True)
    tree = _tree(push_repo)
    filename = "tests/test_\x1b\x9b\x7f\u202e.py"
    argv = [*PREFIX, SELECTOR, filename]
    plan = {
        "schema_version": 1,
        "snapshot_tree": tree,
        "base_commit": tip,
        "mode": "selective",
        "missing_files": [filename],
        "required_files": [SELECTOR, filename],
        "run_argv": argv,
    }
    _receipt(push_repo, tree, plan=plan)
    _git(push_repo, "config", "--local", "brigade.pushGate", "enforce")
    monkeypatch.chdir(push_repo)
    ref = "refs/heads/topic\u202e"
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(tip, ref) + "\n"))
    assert push_gate_hook.run(hook_v2=True) == 3
    output = capsys.readouterr().err
    assert r"refs/heads/topic\u202e" in output
    assert r"test_\x1b\x9b\x7f\u202e.py" in output
    assert all(char not in output for char in "\x1b\x9b\x7f\u202e")
    remedy = next(line for line in output.splitlines() if line.startswith("brigade work verify run "))
    assert json.loads(shlex.split(remedy)[-1]) == argv


def test_status_surfaces_git_config_command_error(push_repo, monkeypatch, capsys):
    from brigade.work_cmd import push_gate

    git = push_gate._git

    def fail_config(target, *args):
        if args[0] == "config":
            return subprocess.CompletedProcess(args, 1, "", "fixture Git error \x1b[31m\nextra detail")
        return git(target, *args)

    monkeypatch.setattr(push_gate, "_git", fail_config)
    assert _gate(push_repo, "status") == 1
    assert capsys.readouterr().err == (
        r"push gate: Git could not read the configuration: fixture Git error \x1b[31m\nextra detail" + "\n"
    )


def test_bash_argument_rendering_preserves_control_bytes(push_repo):
    from brigade.work_cmd.push_gate_display import shell_arg

    raw = "folder \x1b\n' \x9b\u202e"
    quoted = shell_arg(raw)
    assert all(char not in quoted for char in "\x1b\n\x9b\u202e")
    result = subprocess.run(
        [_gate_bash(), "-c", f"printf '%s' {quoted}"],
        cwd=push_repo,
        capture_output=True,
        check=True,
        timeout=HELPER_TIMEOUT_SECONDS,
    )
    assert result.stdout.decode("utf-8") == raw


def test_report_escapes_observation_labels(push_repo, capsys):
    from brigade.work_cmd import push_gate_log

    log = push_gate_log.log_path(push_repo / ".git")
    push_gate_log.append(
        log,
        [
            {
                "schema": push_gate_log.SCHEMA,
                "time": "2026-01-01T00:00:00+00:00",
                "result": "bad\x1b\u202e",
                "would_block": False,
                "duration_ms": 1,
            }
        ],
    )
    assert _gate(push_repo, "report") == 0
    output = capsys.readouterr().out
    assert r"bad\x1b\u202e=1" in output and "\x1b" not in output and "\u202e" not in output


@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize(
    ("later_plan", "indeterminate"),
    [
        ("contradictory", True),
        ("malformed", True),
        ("missing-result", True),
        ("unrelated", False),
        ("legacy", False),
    ],
)
def test_planned_commands_in_hook(push_repo, monkeypatch, capsys, mode, later_plan, indeterminate):
    tip = _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    required = shlex.join([*PREFIX, SELECTOR])
    later = _receipt(push_repo, tree, second=2, argv=["git", "status"])
    if later_plan != "legacy":
        receipt = json.loads(later.read_text(encoding="utf-8"))
        receipt["planned_commands"] = {
            "contradictory": [required],
            "malformed": required,
            "missing-result": ["git status", required],
            "unrelated": ["git 'status'"],
        }[later_plan]
        later.write_text(json.dumps(receipt), encoding="utf-8")

    code, err = _evaluate_hook(push_repo, tip, mode, monkeypatch, capsys)
    blocked = mode == "enforce" and indeterminate
    assert (code != 0) is blocked
    (row,) = _log_rows(push_repo)
    assert row["result"] == ("indeterminate" if indeterminate else "ok")
    if indeterminate:
        assert "refs/heads/main (indeterminate)" in err
        assert "brigade work verify run" in err or "mv --" in err


@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize(
    ("argv", "older_pass", "status", "verdict"),
    [
        *(([*wrapper, *PREFIX, SELECTOR], True, "failed", "rejected") for wrapper in IDENTITY_WRAPPERS),
        (["bash", "-c", shlex.join([*PREFIX, SELECTOR])], True, "failed", "indeterminate"),
        (["bash", "-c", shlex.join([*PREFIX, SELECTOR])], False, "completed", "rejected"),
        (["env", *PREFIX, SELECTOR], False, "completed", "ok"),
        (["ruff", "check", "."], True, "failed", "ok"),
        (["python", "-m", "mypy", "src"], True, "failed", "ok"),
    ],
)
def test_required_check_identity_controls_hook(push_repo, monkeypatch, capsys, mode, argv, older_pass, status, verdict):
    tip = _policy(push_repo)
    tree = _tree(push_repo)
    if older_pass:
        _receipt(push_repo, tree)
    path = _receipt(push_repo, tree, second=2, argv=argv, status=status, passed=int(status == "completed"))
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["planned_commands"] = [shlex.join(argv)]
    path.write_text(json.dumps(receipt), encoding="utf-8")

    code, err = _evaluate_hook(push_repo, tip, mode, monkeypatch, capsys)
    blocked = mode == "enforce" and verdict != "ok"
    assert (code != 0) is blocked, err
    (row,) = _log_rows(push_repo)
    assert row["result"] == verdict
    if verdict != "ok":
        assert f"refs/heads/main ({verdict})" in err


_CUSTOM_PREFIX = b'#!/usr/bin/env bash\nSTDIN_REFS="$(cat)"\n# custom prefix\n'
_CUSTOM_SUFFIX = b"# custom suffix\nexit 0"
_V1_BLOCK = b"# BEGIN Brigade push gate v1\nbrigade work verify push-gate run || true\n# END Brigade push gate v1\n"


@pytest.fixture
def custom_hook(push_repo, monkeypatch):
    set_home(monkeypatch, push_repo)
    global_config = push_repo / "global config"
    global_config.write_bytes(b"[user]\n\tname = Test\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    hook = push_repo / ".git" / "hooks" / "pre-push"
    hook.write_bytes(_CUSTOM_PREFIX + _V1_BLOCK + _CUSTOM_SUFFIX)
    hook.chmod(0o750)
    return hook


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_block_only_upgrades_custom_hook(push_repo, capsys, custom_hook, newline):
    def endings(data):
        return data.replace(b"\n", newline)

    original = endings(_CUSTOM_PREFIX + _V1_BLOCK + _CUSTOM_SUFFIX)
    custom_hook.write_bytes(original)
    initial_mode = stat.S_IMODE(custom_hook.stat().st_mode)
    initial_config = _git(push_repo, "config", "--list", "--show-origin")

    assert _gate(push_repo, "install", "--block-only", "--dry-run") == 0
    preview = capsys.readouterr().out
    assert push_gate._snippet().decode("ascii") not in preview
    assert preview.count("--- pre-push (before)") == 1
    assert "-# BEGIN Brigade push gate v1" in preview
    assert "+# BEGIN Brigade push gate v2" in preview
    assert custom_hook.read_bytes() == original
    assert not list(custom_hook.parent.glob("pre-push.*.bak"))

    assert _gate(push_repo, "install", "--block-only") == 0
    output = capsys.readouterr().out
    (backup,) = custom_hook.parent.glob("pre-push.*.bak")
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == initial_mode
    assert shlex.split(output.split("cp -p -- ", 1)[1]) == [backup.as_posix(), custom_hook.resolve().as_posix()]
    upgraded = endings(_CUSTOM_PREFIX + push_gate._snippet() + _CUSTOM_SUFFIX)
    assert custom_hook.read_bytes() == upgraded
    assert stat.S_IMODE(custom_hook.stat().st_mode) == initial_mode
    info = _status(push_repo, capsys)
    assert info["hook_version"] == 2 and info["current"] and not info["hook_verified"]
    assert "Block present, hook not verified" in push_gate.describe(info)

    assert _gate(push_repo, "install", "--block-only") == 0
    assert "already current" in capsys.readouterr().out
    assert custom_hook.read_bytes() == upgraded
    assert list(custom_hook.parent.glob("pre-push.*.bak")) == [backup]
    assert _git(push_repo, "config", "--list", "--show-origin") == initial_config


@pytest.mark.parametrize(
    ("block", "error"),
    [
        (_V1_BLOCK * 2, "multiple Brigade"),
        (_V1_BLOCK.split(b"# END")[0], "damaged Brigade"),
        (_V1_BLOCK.replace(b"END Brigade push gate v1", b"END Brigade push gate v2"), "damaged Brigade"),
        (b"", "--confirm-insert"),
    ],
)
def test_block_only_refuses_unsafe_boundaries(push_repo, capsys, custom_hook, block, error):
    original = _CUSTOM_PREFIX + block + _CUSTOM_SUFFIX
    custom_hook.write_bytes(original)
    config = _git(push_repo, "config", "--list", "--show-origin")
    assert _gate(push_repo, "install", "--block-only") == 1
    assert error in capsys.readouterr().err
    assert custom_hook.read_bytes() == original
    assert not list(custom_hook.parent.glob("pre-push.*.bak"))
    assert _git(push_repo, "config", "--list", "--show-origin") == config


def test_block_only_refuses_read_only_hook(push_repo, capsys, custom_hook):
    original = custom_hook.read_bytes()
    mode = stat.S_IMODE(custom_hook.stat().st_mode)
    custom_hook.chmod(stat.S_IREAD)
    try:
        assert _gate(push_repo, "install", "--block-only") == 1
        assert "not writable" in capsys.readouterr().err
        assert custom_hook.read_bytes() == original
        assert not list(custom_hook.parent.glob("pre-push.*.bak"))
    finally:
        custom_hook.chmod(mode)


def test_block_only_refuses_external_symlink(push_repo, capsys, custom_hook):
    original = custom_hook.read_bytes()
    external = push_repo / "external hook"
    custom_hook.replace(external)
    try:
        custom_hook.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert _gate(push_repo, "install", "--block-only") == 1
    assert "outside the hooks directory" in capsys.readouterr().err
    assert custom_hook.is_symlink()
    assert external.read_bytes() == original
    assert not list(custom_hook.parent.glob("pre-push.*.bak"))


@pytest.mark.parametrize(
    ("shebang", "newline"),
    [(b"#!/usr/bin/env bash", b"\n"), (b"#!/usr/bin/env bash", b"\r\n"), (b"#!/usr/bin/env -S bash", b"\n")],
)
def test_block_only_inserts_after_capturing_refs_with_confirmation(push_repo, capsys, custom_hook, shebang, newline):
    original = (shebang + b"\n" + _CUSTOM_PREFIX.split(b"\n", 1)[1] + _CUSTOM_SUFFIX).replace(b"\n", newline)
    custom_hook.write_bytes(original)
    assert _gate(push_repo, "install", "--block-only", "--dry-run") == 0
    assert "+# BEGIN Brigade push gate v2" in capsys.readouterr().out
    assert custom_hook.read_bytes() == original
    assert _gate(push_repo, "install", "--block-only", "--confirm-insert") == 0
    capture, suffix = original.split(b"# custom prefix")
    assert (
        custom_hook.read_bytes() == capture + push_gate._snippet().replace(b"\n", newline) + b"# custom prefix" + suffix
    )


@pytest.mark.parametrize(
    ("raw", "options", "error"),
    [
        (b"#!/bin/sh\n" + _V1_BLOCK, [], "Bash interpreter line"),
        (b"#!/bin/sh\n", ["--confirm-insert"], "Bash interpreter line"),
        (
            _CUSTOM_PREFIX + b"brigade work verify push-gate run\n",
            ["--confirm-insert"],
            "around the existing gate invocation",
        ),
        (b"#!/bin/bash\n", ["--confirm-insert"], "needs one STDIN_REFS"),
        (_CUSTOM_PREFIX + b'STDIN_REFS="$(cat)"\n', ["--confirm-insert"], "needs one STDIN_REFS"),
        (_CUSTOM_PREFIX + b" " + _V1_BLOCK, [], "line 4"),
        (_CUSTOM_PREFIX + _V1_BLOCK.replace(b"v1\n", b"v1 \n", 1), [], "no indentation or trailing spaces"),
    ],
)
def test_block_only_refuses_unsafe_hook_contents(push_repo, capsys, custom_hook, raw, options, error):
    custom_hook.write_bytes(raw)
    assert _gate(push_repo, "install", "--block-only", *options) == 1
    assert error in capsys.readouterr().err
    assert custom_hook.read_bytes() == raw
    assert not list(custom_hook.parent.glob("pre-push.*.bak"))


@pytest.mark.parametrize("kind", ["missing", "dangling", "directory"])
def test_block_only_reports_unusable_hook_path(push_repo, capsys, custom_hook, kind):
    custom_hook.unlink()
    if kind == "dangling":
        try:
            custom_hook.symlink_to(custom_hook.with_name("absent"))
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    elif kind == "directory":
        custom_hook.mkdir()
    assert _gate(push_repo, "install", "--block-only") == 1
    expected = "regular file" if kind == "directory" else "no effective pre-push hook to update"
    assert expected in capsys.readouterr().err.lower()
    assert not list(custom_hook.parent.glob("pre-push.*.bak"))


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (["--block-only", "--mode", "warn"], "cannot be used with --mode"),
        (["--dry-run"], "require --block-only"),
    ],
)
def test_block_only_reports_conflicting_options(push_repo, capsys, custom_hook, options, error):
    raw = custom_hook.read_bytes()
    config = _git(push_repo, "config", "--list", "--show-origin")
    assert _gate(push_repo, "install", *options) == 2
    assert error in capsys.readouterr().err
    assert custom_hook.read_bytes() == raw
    assert _git(push_repo, "config", "--list", "--show-origin") == config


def test_block_only_upgraded_custom_hook_runs_in_real_push(push_repo, capsys, monkeypatch, custom_hook):
    _policy(push_repo)
    _bare_remote(push_repo)
    monkeypatch.setenv("PATH", _path_with_brigade())
    custom_hook.write_bytes(
        _CUSTOM_PREFIX + b"echo custom-before >&2\n" + _V1_BLOCK + b"echo custom-after >&2\nexit 0\n"
    )
    _git(push_repo, "config", "--local", "brigade.pushGate", "warn")
    assert _gate(push_repo, "install", "--block-only") == 0
    capsys.readouterr()
    result = _push_with_verdict(push_repo)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("custom-before") == result.stderr.count("custom-after") == 1
    assert result.stderr.count("refs/heads/main (missing_receipt)") == 1
    (entry,) = _log_rows(push_repo)
    assert entry["result"] == "missing_receipt"


@pytest.mark.parametrize(
    ("wrapper", "selectors", "status", "older_pass", "expected"),
    [
        (["env"], ["a", "b"], "completed", False, "ok"),
        (["./python"], ["a", "b"], "completed", False, "ok"),
        (["env"], ["a"], "completed", False, "rejected"),
        (["env"], ["a", "b"], "failed", True, "rejected"),
        ([], ["a", "b"], "completed", False, "ok"),
        ([], ["a"], "completed", False, "rejected"),
        ([], ["a", "b"], "failed", True, "rejected"),
        (["env"], ["a", "b"], "completed", True, "ok"),
        (["env", "PYTEST_ADDOPTS=-k example"], ["a", "b"], "completed", True, "ok"),
    ],
)
def test_normalised_coverage_controls_hook(
    push_repo, monkeypatch, capsys, wrapper, selectors, status, older_pass, expected
):
    from tests.test_verify_plan_coverage import A, B, _setup

    _git(push_repo, "remote", "remove", "origin")
    tip = _setup(push_repo)
    tree = _tree(push_repo)
    if older_pass:
        old = _receipt(push_repo, tree, argv=[*PREFIX, A, B], deselected=0)
        assert json.loads(old.read_bytes())["gating"]["eligible"]
    selected = [path for name, path in (("a", A), ("b", B)) if name in selectors]
    prefix = [*wrapper, *PREFIX] if wrapper != ["./python"] else ["./python", *PREFIX[1:]]
    path = _receipt(
        push_repo,
        tree,
        second=2,
        status=status,
        argv=[*prefix, *selected],
        passed=int(status == "completed"),
        deselected=0,
    )
    receipt = json.loads(path.read_bytes())
    if wrapper and status == "completed":
        assert receipt["gating"]["reason"] == "plan_files_missing"
    code, err = _evaluate_hook(push_repo, tip, "enforce", monkeypatch, capsys)
    assert (code == 0) is (expected == "ok"), err
    (row,) = _log_rows(push_repo)
    assert row["result"] == expected
    if status == "completed" and selectors == ["a"]:
        assert f"missed required test files {B}." in err


@pytest.mark.parametrize("name", CHECK_ENV_NAMES)
@pytest.mark.parametrize("coverage", [False, True])
def test_wrapped_environment_allowlist_in_hook(push_repo, monkeypatch, capsys, name, coverage):
    from tests.test_verify_plan_coverage import A, B, _setup

    if coverage:
        _git(push_repo, "remote", "remove", "origin")
        tip = _setup(push_repo)
    else:
        tip = _policy(push_repo)
    selectors = [A, B] if coverage else [SELECTOR]
    argv = ["env", f"{name}=example", *PREFIX, *selectors]
    tree = _tree(push_repo)
    allowed = name == "PY"
    if not allowed:
        _receipt(push_repo, tree, argv=[*PREFIX, *selectors], deselected=0)
    path = _receipt(
        push_repo,
        tree,
        second=2,
        argv=argv,
        status="completed" if allowed else "failed",
        passed=int(allowed),
        deselected=0,
    )
    receipt = json.loads(path.read_bytes())
    receipt["planned_commands"] = [shlex.join(argv)]
    path.write_text(json.dumps(receipt), encoding="utf-8")
    code, err = _evaluate_hook(push_repo, tip, "enforce", monkeypatch, capsys)
    assert (code == 0) is allowed, err
    (row,) = _log_rows(push_repo)
    assert row["result"] == ("ok" if allowed else "indeterminate")


@pytest.mark.parametrize(
    ("mode", "form", "status"),
    [
        ("warn", "wrapper", "completed"),
        ("warn", "leading", "failed"),
        ("enforce", "wrapper", "failed"),
        ("enforce", "leading", "completed"),
    ],
)
def test_environment_prefix_diagnostic_in_real_push(push_repo, monkeypatch, capsys, mode, form, status):
    _policy(push_repo)
    _bare_remote(push_repo)
    monkeypatch.setenv("PATH", _path_with_brigade())
    assert _gate(push_repo, "install", "--mode", mode) == 0
    capsys.readouterr()
    assignment = f"PATH={SENTINEL_ENV_VALUE}"
    argv = ["env", assignment, *PREFIX, SELECTOR] if form == "wrapper" else [*PREFIX, SELECTOR]
    path = _receipt(push_repo, _tree(push_repo), argv=argv, status=status)
    receipt = json.loads(path.read_bytes())
    if form == "leading":
        receipt["commands"][0]["env"] = ["PATH"]
    receipt["planned_commands"] = [shlex.join(argv) if form == "wrapper" else assignment + " " + shlex.join(argv)]
    path.write_text(json.dumps(receipt), encoding="utf-8")

    pushed = _push_with_verdict(push_repo)
    assert (pushed.returncode != 0) is (mode == "enforce"), pushed.stderr
    assert ENV_PREFIX_MESSAGE in pushed.stderr
    assert "brigade work verify run" in pushed.stderr and " --no-reuse" in pushed.stderr
    assert SENTINEL_ENV_VALUE not in pushed.stdout + pushed.stderr
    log = push_repo / ".git" / "brigade" / "push-gate.jsonl"
    assert SENTINEL_ENV_VALUE not in log.read_text(encoding="utf-8")
    (row,) = _log_rows(push_repo)
    assert row["result"] == ("rejected" if status == "completed" else "indeterminate")
