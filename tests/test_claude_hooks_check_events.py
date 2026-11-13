"""Observed checks through the Claude hook entry points."""

import json
import os

import pytest

from brigade import observed_checks, work_mode
from brigade.claude_hooks import package, runtime
from tests._home import set_home
from tests.test_work_mode import _bash, _payload, _set_mode, _wired, _write


@pytest.fixture
def target(tmp_path, monkeypatch, capsys, request):
    if request.node.originalname in {
        "test_identity_survives_switch_write_and_epoch",
        "test_capture_status_identity_and_private_record",
        "test_export_and_managed_event",
        "test_unknown_model_switch_does_not_reuse_previous_identity",
    }:
        monkeypatch.setattr(observed_checks, "_capture_clock", lambda: 0.0)
    set_home(monkeypatch, tmp_path / "home")
    repo = _wired(tmp_path)
    _set_mode(repo, "observe", capsys)
    monkeypatch.setattr(runtime, "_run_brief", lambda *_a, **_k: "brief")
    return repo


def test_identity_survives_switch_write_and_epoch(target):
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", model="claude-sonnet-4-6"))
    assert runtime.read_session_state(target, "s1")["model"] == "claude-sonnet-4-6"
    runtime.handle_payload("PostModelSwitch", _payload(target, "PostModelSwitch", to_model="claude-opus-4-6"))
    runtime.handle_payload("PostToolUse", _write(target))
    state = runtime.read_session_state(target, "s1")
    assert (state["model"], state["model_source"]) == ("claude-opus-4-6", "hook-model-switch")
    assert runtime._normalize_state(target, "s1", state, task_epoch="next")["model"] == state["model"]


def test_capture_status_identity_and_private_record(target):
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", model="claude-sonnet-4-6"))
    cases = [
        ("PostToolUse", {}, "ok", None),
        ("PostToolUseFailure", {"error": "Exit code 1\nfailed", "agent_type": "Explore"}, "failed", 1),
        ("PostToolUseFailure", {"error": "Command timed out"}, "unknown", None),
        ("PostToolUseFailure", {"error": "Exit code 1", "is_interrupt": True}, "unknown", None),
    ]
    for event, extra, status, code in cases:
        runtime.handle_payload(event, _bash(target, event, "python -m pytest -q", duration_ms=1200, **extra))
        record = observed_checks.read_checks(target)[0][0]
        assert (record.status, record.exit_code, record.duration_ms) == (status, code, 1200)
        assert record.model == "claude-sonnet-4-6"
        assert record.agent_type == extra.get("agent_type", "main")
        assert "command" not in record.to_dict()
    text = observed_checks.digest_key_path(target).with_name("checks.jsonl").read_text()
    assert "s1" not in text and str(target) not in text and "python -m" not in text


@pytest.mark.parametrize("mode", ["strict", "off"])
def test_no_capture_in_other_modes(target, capsys, mode):
    _set_mode(target, mode, capsys)
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest"))
    assert observed_checks.read_checks(target)[0] == []


@pytest.mark.parametrize("command", ["ls -la", "brigade work verify run --command pytest"])
def test_ignore_non_raw_checks(target, command):
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", command))
    assert observed_checks.read_checks(target)[0] == []


def test_capture_exception_is_silent(target, monkeypatch, capsys):
    def broken(*_a, **_k):
        raise RuntimeError("capture broke")

    monkeypatch.setattr(observed_checks, "append_check", broken)
    capsys.readouterr()
    assert runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest")) is None
    assert capsys.readouterr() == ("", "")


def test_export_and_managed_event(target, tmp_path, monkeypatch):
    env = tmp_path / "env.sh"
    env.write_text("")
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env))
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    expected = f"export BRIGADE_AGENT_SESSION={runtime._session_fingerprint('s1')}\n"
    assert env.read_text() == expected
    assert "PostModelSwitch" in package.MANAGED_EVENTS
    hooks = json.loads((target / ".claude/settings.json").read_text())["hooks"]
    assert "PostModelSwitch" in hooks


def test_child_target_and_relative_target(target, tmp_path, monkeypatch, capsys):
    child = _wired(target, name="child")
    _set_mode(target, "strict", capsys)
    _set_mode(child, "observe", capsys)
    command = "cd ./child && pytest"
    assert runtime.handle_payload("PreToolUse", _bash(target, "PreToolUse", command)) is None
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", command))
    assert len(observed_checks.read_checks(child)[0]) == 1
    assert observed_checks.read_checks(target)[0] == []
    monkeypatch.chdir(tmp_path)
    assert observed_checks.read_checks(child.relative_to(tmp_path)) == observed_checks.read_checks(child)


def test_doctor_reports_storage_cause_without_path(target, monkeypatch):
    monkeypatch.setattr("brigade.component_paths.config_root", lambda: str(target / "config"))
    assert (
        observed_checks.recording_unavailable_reason(target) == "state directory inside the project's nearest work tree"
    )
    rows = work_mode.doctor_checks(target)
    assert any(
        name == "observed checks" and "unavailable" in detail and str(target) not in detail for _, name, detail in rows
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_storage_parent_cause(target, monkeypatch, tmp_path):
    root = tmp_path / "config"
    (root / "brigade").mkdir(parents=True)
    (root / "brigade").chmod(0o777)
    monkeypatch.setattr("brigade.component_paths.config_root", lambda: str(root))
    assert observed_checks.recording_unavailable_reason(target) == "a parent directory writable by other users"


def test_record_with_secret_passes_guard(target):
    from brigade import guard

    # content-guard: allow bare-token-prefix
    command = f"GH_TOKEN=ghp_{'A' * 36} pytest"
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", command))
    record = observed_checks.read_checks(target)[0][0]
    assert record.tool == "pytest"
    assert guard.scan_text(observed_checks.digest_key_path(target).with_name("checks.jsonl").read_text()).findings == []


@pytest.mark.parametrize(
    "raw", ["[]", "{", "[" * 1000 + "]" * 1000, json.dumps({"tool_input": {"command": "x" * 2000000}})]
)
def test_hostile_stdin_fails_open_without_record(target, capsys, raw):
    assert runtime.hook_run(event="PostToolUse", package=package.PACKAGE_REF, target=target, stdin_text=raw) == 0
    assert observed_checks.read_checks(target)[0] == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("tool_input", [None, [], "pytest", {"command": []}, {"command": "pytest " * 20000}])
def test_wrong_tool_input_has_no_record(target, tool_input):
    runtime.handle_payload("PostToolUse", _payload(target, "PostToolUse", tool_name="Bash", tool_input=tool_input))
    assert observed_checks.read_checks(target)[0] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_storage_symlink_cause(target, monkeypatch, tmp_path):
    root = tmp_path / "actual"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr("brigade.component_paths.config_root", lambda: str(link))
    assert observed_checks.recording_unavailable_reason(target) == "config directory reached through a symbolic link"


def test_recording_lock_budget(target):
    import time

    from brigade.inbox_lock import held_file_lock_descriptor as held_file_lock

    observed_checks.command_digest("pytest", target)
    path = observed_checks.digest_key_path(target).with_name("checks.jsonl.lock")
    with held_file_lock(path, deadline_seconds=1):
        started = time.monotonic()
        runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest"))
        elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert observed_checks.read_checks(target)[0] == []


def test_unknown_model_switch_does_not_reuse_previous_identity(target):
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", model="claude-sonnet-4-6"))
    runtime.handle_payload("PostModelSwitch", _payload(target, "PostModelSwitch", to_model="unrecognized model"))
    runtime.handle_payload("PostToolUse", _write(target))
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest"))
    record = observed_checks.read_checks(target)[0][0]
    assert record.model == "unknown"
    assert record.model_source == "hook-model-switch"
