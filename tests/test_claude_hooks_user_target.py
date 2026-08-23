"""User-scope targeting, home refusal, and hook timeout latch (issue #1051)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from brigade import cli
from brigade.claude_hooks import envelope
from brigade.claude_hooks.install_cmd import hooks_install, status_payload
from brigade.claude_hooks.package import PACKAGE_REF, is_managed_handler, managed_command
from brigade.claude_hooks import runtime
from brigade.install import install_selection
from brigade.selection import Selection


def _wired_claude(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, selection) == 0
    return target


def _payload(target: Path, event: str, *, session_id: str = "session-1", **extra):
    return {
        "session_id": session_id,
        "cwd": str(target),
        "hook_event_name": event,
        **extra,
    }


def test_effective_target_skips_home_even_when_wired(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    selection = Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(home, selection) == 0
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home.resolve()))

    assert runtime.effective_hook_target(_payload(home, "PreToolUse")) is None
    monkeypatch.setattr(runtime, "_run_brief", lambda target: (_ for _ in ()).throw(AssertionError(target)))
    assert runtime.handle_payload("SessionStart", _payload(home, "SessionStart")) is None
    assert (
        runtime.handle_payload(
            "PreToolUse",
            _payload(home, "PreToolUse", tool_name="Bash", tool_input={"command": "pytest"}),
        )
        is None
    )


def test_hook_run_home_cwd_is_silent_no_op(tmp_path: Path, monkeypatch, capsys):
    home = tmp_path / "home"
    selection = Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(home, selection) == 0
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home.resolve()))

    capsys.readouterr()
    assert (
        runtime.hook_run(
            event="PreToolUse",
            package=PACKAGE_REF,
            stdin_text=json.dumps(_payload(home, "PreToolUse", tool_name="Bash")),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == envelope.empty_envelope("PreToolUse")
    assert not envelope.log_path(home).exists()


def test_hook_run_pin_restricts_to_wired_workspace(tmp_path: Path, monkeypatch, capsys):
    workspace = _wired_claude(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    calls: list[Path] = []

    def fake_brief(repo: Path) -> str:
        calls.append(repo)
        return "work brief: pinned"

    monkeypatch.setattr(runtime, "_run_brief", fake_brief)
    capsys.readouterr()
    assert (
        runtime.hook_run(
            event="SessionStart",
            package=PACKAGE_REF,
            target=workspace,
            stdin_text=json.dumps(_payload(other, "SessionStart")),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == envelope.empty_envelope("SessionStart")
    assert calls == []

    result = runtime.handle_payload(
        "SessionStart",
        _payload(workspace, "SessionStart"),
        pin=workspace,
    )
    assert result is not None
    assert "work brief: pinned" in result["hookSpecificOutput"]["additionalContext"]
    assert calls == [workspace.resolve()]


def test_hook_run_session_start_hints_init_for_unwired_git_repo(tmp_path: Path, capsys):
    repo = tmp_path / "fresh-repo"
    (repo / ".git").mkdir(parents=True)
    capsys.readouterr()
    assert (
        runtime.hook_run(
            event="SessionStart",
            package=PACKAGE_REF,
            stdin_text=json.dumps(_payload(repo, "SessionStart")),
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    context = result["hookSpecificOutput"]["additionalContext"]
    assert f"brigade init --target {repo.resolve()}" in context
    assert "--harnesses claude" in context


def test_hook_run_other_events_stay_silent_for_unwired_git_repo(tmp_path: Path, capsys):
    repo = tmp_path / "fresh-repo"
    (repo / ".git").mkdir(parents=True)
    capsys.readouterr()
    assert (
        runtime.hook_run(
            event="PreToolUse",
            package=PACKAGE_REF,
            stdin_text=json.dumps(_payload(repo, "PreToolUse", tool_name="Bash")),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == envelope.empty_envelope("PreToolUse")


def test_hook_run_unwired_path_times_out_when_target_resolution_hangs(tmp_path: Path, monkeypatch, capsys):
    """Unpinned unwired events must use the timed worker (issue #1051).

    ``_resolve_log_target`` returning None used to call ``handle_payload`` in
    the parent, so ``effective_hook_target`` path resolution or the SessionStart
    ``.git`` check could block Claude Code indefinitely.
    """
    repo = tmp_path / "unwired-git"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(envelope, "HOOK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(runtime, "_resolve_log_target", lambda *_args, **_kwargs: None)

    def hang_target(_payload: dict, *, pin=None) -> Path | None:
        time.sleep(5)
        return None

    monkeypatch.setattr(runtime, "effective_hook_target", hang_target)
    capsys.readouterr()
    started = time.monotonic()
    assert (
        runtime.hook_run(
            event="SessionStart",
            package=PACKAGE_REF,
            stdin_text=json.dumps(_payload(repo, "SessionStart")),
        )
        == 0
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert json.loads(capsys.readouterr().out) == envelope.degraded_envelope("SessionStart")


def test_hook_run_unwired_path_times_out_when_git_stat_hangs(tmp_path: Path, monkeypatch, capsys):
    """A stalled ``.git`` exists() check on an unwired SessionStart must not block."""
    repo = tmp_path / "unwired-git"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(envelope, "HOOK_TIMEOUT_SECONDS", 0.1)
    original_exists = Path.exists

    def hanging_exists(self: Path) -> bool:
        if self.name == ".git":
            time.sleep(5)
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", hanging_exists)
    capsys.readouterr()
    started = time.monotonic()
    assert (
        runtime.hook_run(
            event="SessionStart",
            package=PACKAGE_REF,
            stdin_text=json.dumps(_payload(repo, "SessionStart")),
        )
        == 0
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert json.loads(capsys.readouterr().out) == envelope.degraded_envelope("SessionStart")


def test_hook_run_latches_after_repeated_timeouts(tmp_path: Path, monkeypatch, capsys):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(envelope, "HOOK_TIMEOUT_SECONDS", 0.01)

    def hang(_event: str, _payload: dict, *, pin=None) -> dict | None:
        import time

        time.sleep(1)
        return None

    monkeypatch.setattr(runtime, "handle_payload", hang)
    payload = json.dumps(_payload(target, "PreToolUse"))
    capsys.readouterr()

    assert runtime.hook_run(event="PreToolUse", package=PACKAGE_REF, stdin_text=payload) == 0
    assert json.loads(capsys.readouterr().out) == envelope.degraded_envelope("PreToolUse")

    assert runtime.hook_run(event="PreToolUse", package=PACKAGE_REF, stdin_text=payload) == 0
    assert json.loads(capsys.readouterr().out) == envelope.latched_envelope("PreToolUse")

    assert runtime.hook_run(event="PreToolUse", package=PACKAGE_REF, stdin_text=payload) == 0
    assert json.loads(capsys.readouterr().out) == envelope.empty_envelope("PreToolUse")
    log = envelope.log_path(target).read_text(encoding="utf-8")
    assert "timed out" in log
    assert "latched after repeated timeouts" in log
    assert log.count("latched after repeated timeouts") == 1


def test_project_install_refuses_home_directory(tmp_path: Path, monkeypatch, capsys):
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home.resolve()))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))

    assert hooks_install(target=home, scope="project") == 2
    assert "user settings" in capsys.readouterr().err
    assert not (home / ".brigade" / "claude-hooks.json").exists()

    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}\n")
    assert hooks_install(target=home, scope="project") == 2
    assert "user settings" in capsys.readouterr().err
    assert not (home / ".brigade" / "claude-hooks.json").exists()


def test_user_scope_pin_writes_target_into_script(tmp_path: Path, monkeypatch):
    home = tmp_path / "fake-home"
    claude = home / ".claude-config"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    workspace = _wired_claude(tmp_path)

    assert hooks_install(target=workspace, scope="user") == 0
    script = (claude / "hooks" / "brigade-claude-work-loop.sh").read_text()
    assert f"--target {workspace.resolve()}" in script
    sidecar = json.loads((claude / "brigade" / "claude-hooks.json").read_text())
    assert sidecar["pinned_target"] == str(workspace.resolve())
    status = status_payload(Path("."), scope="user")
    assert status["current"] is True
    assert status["pinned_target"] == str(workspace.resolve())


def test_user_scope_refuses_home_pin(tmp_path: Path, monkeypatch, capsys):
    home = tmp_path / "fake-home"
    claude = home / ".claude-config"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home.resolve()))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))

    assert hooks_install(target=home, scope="user") == 2
    assert "home directory" in capsys.readouterr().err
    assert not (claude / "hooks" / "brigade-claude-work-loop.sh").exists()


def test_status_flags_duplicate_managed_handlers(tmp_path: Path):
    target = _wired_claude(tmp_path)
    assert hooks_install(target=target) == 0
    settings = target / ".claude" / "settings.json"
    payload = json.loads(settings.read_text())
    bash_group = next(group for group in payload["hooks"]["PreToolUse"] if group.get("matcher") == "Bash")
    payload["hooks"]["PreToolUse"].append(json.loads(json.dumps(bash_group)))
    settings.write_text(json.dumps(payload, indent=2) + "\n")

    status = status_payload(target)
    assert status["duplicate_handler_count"] >= 1
    assert status["current"] is False


def test_status_flags_scope_widened_when_project_settings_are_user_settings(tmp_path: Path, monkeypatch):
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home.resolve()))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}\n")

    status = status_payload(home, scope="project")
    assert status["scope_widened"] is True
    assert status["current"] is False


def test_is_managed_handler_accepts_optional_target_flag():
    command = f"{managed_command('PreToolUse')} --target /tmp/wired-workspace"
    assert is_managed_handler({"type": "command", "command": command}, "PreToolUse")
    assert is_managed_handler({"type": "command", "command": managed_command("PreToolUse")}, "PreToolUse")


def test_cli_hook_run_accepts_target(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, str, Path | None]] = []

    def fake_hook_run(*, event: str, package: str, target=None) -> int:
        calls.append((event, package, target))
        return 0

    monkeypatch.setattr(runtime, "hook_run", fake_hook_run)
    workspace = tmp_path / "wired"
    workspace.mkdir()
    assert (
        cli.main(
            [
                "work",
                "hook-run",
                "--event",
                "PreToolUse",
                "--package",
                PACKAGE_REF,
                "--target",
                str(workspace),
            ]
        )
        == 0
    )
    assert calls == [("PreToolUse", PACKAGE_REF, workspace)]
