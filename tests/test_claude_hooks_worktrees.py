"""Claude closeout follows writes into linked worktrees, not the session cwd."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from brigade import cli
from brigade.claude_hooks import runtime
from brigade.install import install_selection
from brigade.selection import Selection

SESSION = "linked-worktree-session"


def event(cwd: Path, name: str, **fields):
    return runtime.handle_payload(name, {"session_id": SESSION, "cwd": str(cwd), **fields})


@pytest.fixture
def worktrees(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(runtime.heartbeat, "on_event", lambda *args: None)
    monkeypatch.setattr(runtime.presence, "emit_presence", lambda *args: None)
    monkeypatch.setattr(runtime, "_run_recall", lambda *args: "")
    monkeypatch.setattr(runtime, "_run_brief", lambda *args: "work brief")
    first, second = tmp_path / "first", tmp_path / "second tree"
    first.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(first), *args], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test User")
    (first / "source.py").write_text("before\n")
    git("add", "source.py")
    git("commit", "-qm", "test baseline")
    git("worktree", "add", "-qb", "other", str(second))
    for target in (first, second):
        assert (
            install_selection(target, Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])) == 0
        )
    event(first, "SessionStart", source="startup")
    state = runtime.read_session_state(first, SESSION)
    assert state is not None
    monkeypatch.setenv(runtime.CLAUDE_SESSION_ENV, state["session_fingerprint"])
    return first, second


def replacement(target: Path) -> str:
    return runtime._verify_replacement(target, "<test>", runtime._session_fingerprint(SESSION))


def verify(cwd: Path, target: Path):
    args = ["work", "verify", "run", "--target", str(target), "--argv-json", json.dumps([sys.executable, "-c", "pass"])]
    fields = {"tool_name": "Bash", "tool_input": {"command": shlex.join(["brigade", *args])}}
    assert event(cwd, "PreToolUse", **fields) is None
    assert cli.main(args) == 0
    event(cwd, "PostToolUse", **fields)


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/before/after/' {file}",
        "git -C {other} commit -am changed",
        "cd {other} && python3 - <<'EOF'\nfrom pathlib import Path\nPath('source.py').write_text('after')\nEOF",
        "cd {other} && claude -p 'update source.py'",
        "printf after > {file}",
        "touch {file} 2>&1",
        "bash -lc {nested}",
    ],
)
def test_sibling_write_accepts_only_sibling_receipt(worktrees, command):
    first, second = worktrees
    command = command.format(
        other=shlex.quote(str(second)),
        file=shlex.quote(str(second / "source.py")),
        nested=shlex.quote(f"cd {shlex.quote(str(second))} && touch source.py"),
    )
    fields = {"tool_name": "Bash", "tool_input": {"command": command}}
    assert event(first, "PreToolUse", **fields) is None
    (second / "source.py").write_text("after\n")
    event(first, "PostToolUse", **fields)

    blocked = event(first, "Stop", stop_hook_active=False)
    assert blocked is not None and blocked.get("decision") == "block"
    assert replacement(second) in blocked["reason"]
    assert replacement(first) not in blocked["reason"]
    first_state = runtime.read_session_state(first, SESSION)
    second_state = runtime.read_session_state(second, SESSION)
    assert first_state is not None and first_state["write_observed"] is False
    assert second_state is not None and second_state["write_observed"] is True

    verify(first, first)
    assert event(first, "Stop", stop_hook_active=False)["decision"] == "block"
    verify(first, second)
    stopped = event(first, "Stop", stop_hook_active=False)
    assert stopped is None or "decision" not in stopped


@pytest.mark.parametrize("change_cwd", [False, True])
def test_writes_in_both_worktrees_require_both_receipts(worktrees, change_cwd):
    first, second = worktrees
    command = (
        f"touch source.py && cd {shlex.quote(str(second))} && touch source.py"
        if change_cwd
        else f"touch source.py {shlex.quote(str(second / 'source.py'))}"
    )
    fields = {"tool_name": "Bash", "tool_input": {"command": command}}
    event(first, "PreToolUse", **fields)
    for target in (first, second):
        (target / "source.py").write_text("after\n")
    event(first, "PostToolUse", **fields)
    blocked = event(first, "Stop", stop_hook_active=False)
    assert blocked is not None and blocked.get("decision") == "block"
    assert replacement(first) in blocked["reason"]
    assert replacement(second) in blocked["reason"]
    verify(first, second)
    stopped = event(first, "Stop", stop_hook_active=False)
    assert stopped is not None and stopped.get("decision") == "block"
    assert replacement(first) in stopped["reason"]
    assert replacement(second) not in stopped["reason"]
    verify(first, first)
    stopped = event(first, "Stop", stop_hook_active=False)
    assert stopped is None or "decision" not in stopped


def test_metadata_in_cwd_does_not_hide_sibling_write(worktrees):
    first, second = worktrees
    args = ["work", "verify", "run", "--target", str(first), "--argv-json", json.dumps([sys.executable, "-c", "pass"])]
    command = shlex.join(["brigade", *args]) + f" && touch {shlex.quote(str(second / 'source.py'))}"
    fields = {"tool_name": "Bash", "tool_input": {"command": command}}
    event(first, "PreToolUse", **fields)
    assert cli.main(args) == 0
    (second / "source.py").write_text("after\n")
    event(first, "PostToolUse", **fields)
    stopped = event(first, "Stop", stop_hook_active=False)
    assert stopped is not None and stopped.get("decision") == "block"
    assert replacement(second) in stopped["reason"]
    assert replacement(first) not in stopped["reason"]
    verify(first, second)
    stopped = event(first, "Stop", stop_hook_active=False)
    assert stopped is None or "decision" not in stopped


def test_sibling_receipt_does_not_cover_later_cwd_write(worktrees):
    first, second = worktrees
    fields = {"tool_name": "Write", "tool_input": {"file_path": str(second / "source.py")}}
    event(first, "PreToolUse", **fields)
    (second / "source.py").write_text("after\n")
    event(first, "PostToolUse", **fields)
    verify(first, second)
    # A read of B beside an explicit write in A does not move A's obligation.
    command = f"cat {shlex.quote(str(second / 'source.py'))} && touch source.py"
    fields = {"tool_name": "Bash", "tool_input": {"command": command}}
    event(first, "PreToolUse", **fields)
    (first / "source.py").write_text("later\n")
    event(first, "PostToolUse", **fields)
    stopped = event(first, "Stop", stop_hook_active=False)
    assert stopped is not None and stopped.get("decision") == "block"
    assert replacement(first) in stopped["reason"]
    assert replacement(second) not in stopped["reason"]


@pytest.mark.parametrize("command", ['touch "$OUTPUT_FILE"', "sed -i 's/before/after/'"])
def test_unresolved_write_destination_retains_cwd_gate(worktrees, command):
    first, _ = worktrees
    fields = {"tool_name": "Bash", "tool_input": {"command": command}}
    event(first, "PreToolUse", **fields)
    event(first, "PostToolUse", **fields)
    stopped = event(first, "Stop", stop_hook_active=False)
    assert stopped is not None and stopped.get("decision") == "block"
    assert replacement(first) in stopped["reason"]


@pytest.mark.parametrize("redirect", ["2>&1", ">&2", "2>/dev/null", "> {outside}"])
def test_readonly_command_keeps_worktree_gate_disarmed(worktrees, tmp_path, redirect):
    first, _ = worktrees
    redirect = redirect.format(outside=shlex.quote(str(tmp_path / "outside.log")))
    fields = {"tool_name": "Bash", "tool_input": {"command": f"echo result {redirect}"}}
    event(first, "PreToolUse", **fields)
    event(first, "PostToolUse", **fields)
    assert event(first, "Stop", stop_hook_active=False) is None


@pytest.mark.parametrize(
    "command",
    [
        "touch -r {reference} {file}",
        "touch -cr {reference} {file}",
        "truncate -r {reference} {file}",
    ],
)
def test_option_values_are_read_not_written(worktrees, command):
    first, second = worktrees
    command = command.format(
        reference=shlex.quote(str(second / "source.py")), file=shlex.quote(str(first / "source.py"))
    )
    fields = {"tool_name": "Bash", "tool_input": {"command": command}}
    event(first, "PreToolUse", **fields)
    (first / "source.py").write_text("after\n")
    event(first, "PostToolUse", **fields)
    stopped = event(first, "Stop", stop_hook_active=False)
    assert stopped is not None and stopped.get("decision") == "block"
    assert replacement(first) in stopped["reason"]
    assert replacement(second) not in stopped["reason"]
    second_state = runtime.read_session_state(second, SESSION)
    assert second_state is None or second_state.get("write_observed") is not True


def test_separated_long_option_value_keeps_the_cwd_gate(worktrees):
    first, second = worktrees
    command = f"touch --reference {shlex.quote(str(second / 'source.py'))} source.py"
    fields = {"tool_name": "Bash", "tool_input": {"command": command}}
    event(first, "PreToolUse", **fields)
    event(first, "PostToolUse", **fields)
    stopped = event(first, "Stop", stop_hook_active=False)
    assert stopped is not None and stopped.get("decision") == "block"
    assert replacement(first) in stopped["reason"]


@pytest.mark.parametrize(
    "command, expected",
    [
        ("cd {other} && claude -p 'update source.py'", ["cd", "{path}", "&&", "claude", "-p", "update source.py"]),
        ('printf after > "{path}\\source.py"', ["printf", "after", ">", "{path}\\source.py"]),
        ("bash -lc {nested}", ["bash", "-lc", "cd {other} && touch source.py"]),
    ],
)
def test_bash_windows_paths_follow_shell_quoting_on_every_host(monkeypatch, command, expected):
    # Change only the runtime module's platform binding, not pathlib or pytest's
    # process-wide os.name. This exercises the Windows lexer branch on Linux.
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt"))
    path = str(PureWindowsPath("D:/example/second tree"))
    fields = {"path": path, "other": shlex.quote(path)}
    fields["nested"] = shlex.quote("cd {other} && touch source.py".format(**fields))
    assert runtime._shell_path_tokens(command.format(**fields)) == [token.format(**fields) for token in expected]
