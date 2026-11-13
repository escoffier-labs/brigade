"""Regressions for optional capture review findings."""

import json
import os
import subprocess
import time
from unittest.mock import Mock

import pytest

from brigade import observed_checks, work_mode
from brigade.claude_hooks import check_events, runtime
from tests.test_claude_hooks_check_events import target as _target

from tests.test_work_mode import _bash, _payload, _set_mode, _write

target = _target


def test_capture_never_spawns(target, monkeypatch):
    spawns = [Mock(side_effect=AssertionError("process spawned")) for _ in range(3)]
    capture = check_events._on_event

    def no_processes(*args):
        with monkeypatch.context() as patch:
            for name, mock in zip(("Popen", "run", "call"), spawns, strict=True):
                patch.setattr(subprocess, name, mock)
            capture(*args)

    monkeypatch.setattr(check_events, "_on_event", no_processes)
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest"))
    for mock in spawns:
        mock.assert_not_called()
    assert observed_checks.read_checks(target)[0][0].tree_fingerprint is None


def test_git_capture_is_small_and_has_no_tree(target):
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    from tests.test_claude_hooks_fingerprint import _git

    (target / "tracked.txt").write_text("tracked")
    _git(target, "add", ".")
    _git(target, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    (target / "untracked.txt").write_text("untracked\n" * 1000)
    started = time.monotonic()
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest"))
    assert time.monotonic() - started < 0.5
    assert observed_checks.read_checks(target)[0][0].tree_fingerprint is None


@pytest.mark.parametrize("slow", [False, True])
def test_existing_write_handling_precedes_capture(target, monkeypatch, slow):
    seen = []

    def capture(*args):
        seen.append(runtime.read_session_state(target, "s1").get("write_observed"))
        if slow:
            time.sleep(0.12)
        raise RuntimeError("optional capture failed")

    monkeypatch.setattr(check_events, "_on_event", capture)
    assert runtime.handle_payload("PostToolUse", _write(target)) is None
    assert seen == [True]
    assert runtime.read_session_state(target, "s1")["write_observed"] is True


@pytest.mark.parametrize("mode", ["observe", "strict", "off"])
@pytest.mark.parametrize("event,field", [("SessionStart", "model"), ("PostModelSwitch", "to_model")])
def test_identity_and_env_writes_follow_mode(target, tmp_path, monkeypatch, capsys, mode, event, field):
    monkeypatch.setattr(observed_checks, "_capture_clock", lambda: 0.0)
    _set_mode(target, mode, capsys)
    env = tmp_path / "env.sh"
    env.write_text("")
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env))
    runtime.handle_payload(event, _payload(target, event, **{field: "claude-sonnet-4-6"}))
    assert ("model" in runtime.read_session_state(target, "s1")) == (mode != "off")
    assert bool(env.read_text()) == (mode != "off" and event == "SessionStart")


@pytest.mark.parametrize("mode", ["off", "strict", "observe"])
def test_doctor_warning_only_observe(target, monkeypatch, capsys, mode):
    _set_mode(target, mode, capsys)
    monkeypatch.setattr(observed_checks, "recording_unavailable_reason", lambda _: "other")
    rows = work_mode.doctor_checks(target)
    assert any(name == "observed checks" for _, name, _ in rows) == (mode == "observe")


def test_duration_over_one_day_is_null(target):
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest", duration_ms=86400001))
    assert observed_checks.read_checks(target)[0][0].duration_ms is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow and FIFO open flags")
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_env_swap_cannot_follow_or_block(target, tmp_path, monkeypatch, kind):
    env = tmp_path / "env.sh"
    env.write_text("")
    victim = tmp_path / "victim"
    victim.write_text("original")
    original_open = os.open
    flags_seen = []

    def swap(path, flags, *args, **kwargs):
        if os.fspath(path) == str(env):
            flags_seen.append(flags)
            env.unlink()
            if kind == "symlink":
                env.symlink_to(victim)
            else:
                os.mkfifo(env)
            # Refuse an unsafe open rather than hang the regression suite.
            assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, swap})
    monkeypatch.setattr(os, "open", swap)
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env))
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    assert flags_seen
    assert victim.read_text() == "original"


@pytest.mark.parametrize("mode", ["observe", "strict"])
def test_unwired_session_cd_is_discovered(target, tmp_path, capsys, mode):
    _set_mode(target, mode, capsys)
    unwired = tmp_path / "unwired"
    unwired.mkdir()
    command = f"cd {target.as_posix()} && pytest"
    pre = runtime.handle_payload("PreToolUse", _bash(unwired, "PreToolUse", command))
    assert (pre is None) == (mode == "observe")
    if mode == "strict":
        assert pre["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert runtime.handle_payload("PostToolUse", _bash(unwired, "PostToolUse", command)) is None
        assert len(observed_checks.read_checks(target)[0]) == 1


def test_outcome_maps_edited_identity(target):
    from brigade import outcome_cmd
    from tests.test_outcome_reused_receipt_dedup import _write_receipt

    directory = _write_receipt(target, "edited")
    path = directory / "receipt.json"
    receipt = json.loads(path.read_text())
    receipt["agent_identity"] = dict.fromkeys(
        ("harness", "model", "agent_type", "model_source", "harness_source"), "private text"
    )
    path.write_text(json.dumps(receipt))
    assert outcome_cmd.capture(target=target, artifact_id="brigade-work", run_id="edited") == 0
    record = json.loads((target / "memory/outcome/records.jsonl").read_text().splitlines()[-1])
    assert "private text" not in json.dumps(record["context"]["agent_identity"])


@pytest.mark.parametrize("step", ["identity", "key", "lock", "append"])
def test_overall_deadline_stops_between_steps(target, monkeypatch, capsys, step):
    now = [0.0]
    monkeypatch.setattr(observed_checks, "_capture_clock", lambda: now[0], raising=False)
    if step == "identity":
        original = check_events.claude_identity
        module, name = check_events, "claude_identity"
    elif step == "key":
        original = observed_checks._digest_key
        module, name = observed_checks, "_digest_key"
    elif step == "lock":
        original = observed_checks._state_directory
        module, name = observed_checks, "_state_directory"
    else:
        original = observed_checks._open_regular
        module, name = observed_checks, "_open_regular"

    def expire(*args, **kwargs):
        result = original(*args, **kwargs)
        if step != "append" or args[1] == observed_checks.CHECKS_FILE:
            now[0] = 1.0
        return result

    with monkeypatch.context() as patch:
        patch.setattr(module, name, expire)
        capsys.readouterr()
        assert runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest")) is None
        assert capsys.readouterr() == ("", "")
    assert observed_checks.read_checks(target)[0] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory ownership")
def test_foreign_owner_has_distinct_reason(target, tmp_path, monkeypatch):
    root = tmp_path / "config"
    (root / "brigade").mkdir(parents=True)
    monkeypatch.setattr("brigade.component_paths.config_root", lambda: str(root))
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(root).st_uid + 1)
    assert observed_checks.recording_unavailable_reason(target) == "a parent directory owned by another user"


@pytest.mark.parametrize("slow", [False, True])
def test_pending_command_cleared_before_capture(target, monkeypatch, slow):
    runtime.handle_payload("PreToolUse", _bash(target, "PreToolUse", "pytest"))
    assert "pending_bash_fingerprint" in runtime.read_session_state(target, "s1")
    seen = []

    def capture(*args):
        seen.append("pending_bash_fingerprint" in runtime.read_session_state(target, "s1"))
        if slow:
            time.sleep(0.12)
        raise RuntimeError("optional capture failed")

    monkeypatch.setattr(check_events, "_on_event", capture)
    assert runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest")) is None
    assert seen == [False]


@pytest.mark.parametrize("elapsed", [0.2, 1.1])
@pytest.mark.parametrize("event,field", [("SessionStart", "model"), ("PostModelSwitch", "to_model")])
def test_session_identity_has_one_second_budget(target, tmp_path, monkeypatch, elapsed, event, field):
    now = [0.0]
    monkeypatch.setattr(observed_checks, "_capture_clock", lambda: now[0])
    env = tmp_path / "env"
    env.write_text("")
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env))
    original = check_events._on_event

    def delayed(*args):
        now[0] = elapsed
        return original(*args)

    monkeypatch.setattr(check_events, "_on_event", delayed)
    runtime.handle_payload(event, _payload(target, event, **{field: "claude-sonnet-4-6"}))
    assert bool(env.read_text()) == (elapsed < 1.0 and event == "SessionStart")
    assert ("model" in runtime.read_session_state(target, "s1")) == (elapsed < 1.0)
