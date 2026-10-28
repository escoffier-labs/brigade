"""Brigade metadata commands must not hide explicit source writes in Bash."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import cli
from brigade.claude_hooks import runtime
from brigade.install import install_selection
from brigade.selection import Selection


SESSION = "mixed-bash-session"


def event(target: Path, name: str, **fields):
    return runtime.handle_payload(name, {"session_id": SESSION, "cwd": str(target), **fields})


@pytest.fixture
def target(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "repo"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert install_selection(target, Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])) == 0
    monkeypatch.setattr(runtime.heartbeat, "on_event", lambda *args: None)
    monkeypatch.setattr(runtime.presence, "emit_presence", lambda *args: None)
    monkeypatch.setattr(runtime, "_run_recall", lambda *args: "")
    monkeypatch.setattr(runtime, "_run_brief", lambda *args: "work brief")
    event(target, "SessionStart", source="startup")
    state = runtime.read_session_state(target, SESSION)
    assert state is not None
    monkeypatch.setenv(runtime.CLAUDE_SESSION_ENV, state["session_fingerprint"])
    return target


@pytest.mark.parametrize(
    "command",
    [
        "brigade work verify run --command true --capture taste && touch src.py",
        "brigade work verify run --command true --capture taste; printf changed > src.py",
        "brigade work verify run --command true --capture taste > src.py",
        "brigade work verify run --command true --capture taste | tee src.py",
        "brigade work verify run --command true --capture taste | tee src.py 2>&1",
        "brigade work verify run --command true --capture taste && printf note >> .claude/memory-handoffs/note.md && touch src.py",
        "brigade run --help && touch src.py",
        "touch src.py && brigade run --help",
    ],
)
def test_metadata_command_does_not_hide_explicit_source_write(target, command):
    assert event(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}) is None
    (target / "src.py").write_text("changed\n")
    event(target, "PostToolUse", tool_name="Bash", tool_input={"command": command})

    # Stop must see this session's first write, even with no earlier Write event.
    stopped = event(target, "Stop", stop_hook_active=False)
    assert stopped is not None and stopped.get("decision") == "block"
    state = runtime.read_session_state(target, SESSION)
    assert state is not None and state["write_observed"] is True
    assert "pending_bash_fingerprint" not in state
    if "--capture taste" in command:
        assert state["exercised_artifact_id"] == "taste"


@pytest.mark.skipif(os.name == "nt", reason="Drives a POSIX shell compound command with touch")
def test_real_verification_then_source_write_requires_a_new_receipt(target):
    verify = [
        "work",
        "verify",
        "run",
        "--target",
        str(target),
        "--argv-json",
        json.dumps([sys.executable, "-c", "pass"]),
    ]
    command = shlex.join([sys.executable, "-m", "brigade", *verify, "--capture", "brigade-work"])
    command += " && touch src.py"
    assert event(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}) is None
    completed = subprocess.run(command, shell=True, cwd=target, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (target / "src.py").is_file()
    receipts = list((target / ".brigade/work/verify-runs").glob("*/receipt.json"))
    assert len(receipts) == 1
    first_receipt = json.loads(receipts[0].read_text())
    assert first_receipt["status"] == "completed"
    event(target, "PostToolUse", tool_name="Bash", tool_input={"command": command})

    stopped = event(target, "Stop", stop_hook_active=False)
    assert stopped is not None and stopped.get("decision") == "block"

    # A fresh real receipt after the source write satisfies closeout.
    assert cli.main([*verify, "--capture", "brigade-work"]) == 0
    stopped = event(target, "Stop", stop_hook_active=False)
    assert stopped is not None and "decision" not in stopped


@pytest.mark.parametrize(
    "command",
    [
        "brigade work verify run --command true > .brigade/result.log 2>&1",
        "brigade work verify run --command true | tee .brigade/result.log",
        "brigade work verify run --command true | tee .brigade/result.log 2>&1",
        "brigade work verify run --command true | tee > .brigade/result.log",
        "brigade work verify run --command true | tee 2> .brigade/result.log",
        "brigade run --help && touch .brigade/result.log",
        "brigade run --help && printf result",
        f"brigade work verify run --command true > {os.devnull} 2>&1",
    ],
)
def test_metadata_and_internal_logs_do_not_record_source_writes(target, command):
    assert event(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}) is None
    (target / ".brigade/result.log").write_text("result\n")
    event(target, "PostToolUse", tool_name="Bash", tool_input={"command": command})
    state = runtime.read_session_state(target, SESSION)
    assert state is not None and state["write_observed"] is False
    assert "pending_bash_fingerprint" not in state
    assert event(target, "Stop", stop_hook_active=False) is None


@pytest.mark.parametrize("redirect", ["", f" > {os.devnull} 2>&1"])
def test_verification_followed_by_handoff_keeps_source_verification_boundary(target, redirect):
    (target / "src.py").write_text("changed\n")
    event(target, "PostToolUse", tool_name="Write", tool_input={"file_path": str(target / "src.py")})
    before = runtime.read_session_state(target, SESSION)
    assert before is not None
    command = f"brigade work verify run --command true{redirect} && printf note >> .claude/memory-handoffs/note.md"
    assert event(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}) is None
    assert (
        cli.main(
            [
                "work",
                "verify",
                "run",
                "--target",
                str(target),
                "--argv-json",
                json.dumps([sys.executable, "-c", "pass"]),
                "--capture",
                "brigade-work",
            ]
        )
        == 0
    )
    (target / ".claude/memory-handoffs/note.md").write_text("note\n")
    event(target, "PostToolUse", tool_name="Bash", tool_input={"command": command})
    state = runtime.read_session_state(target, SESSION)
    assert state is not None
    assert state["last_verification_write_at"] == before["last_verification_write_at"]
    assert state["last_write_at"] > before["last_write_at"]
    assert event(target, "Stop", stop_hook_active=False) is None
