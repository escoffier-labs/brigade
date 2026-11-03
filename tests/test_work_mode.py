"""Yolo work mode and the independent safety-rails switch (#2233)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

import brigade
from brigade import cli, config, localio, work_mode
from brigade.claude_hooks import classify, install_cmd, runtime
from brigade.install import install_selection
from brigade.repos_cmd import adoption
from brigade.selection import Selection

SRC = Path(brigade.__file__).resolve().parents[1]
PRE_PUSH = Path(brigade.__file__).resolve().parent / "templates" / "hooks" / "pre-push"
# content-guard: allow api-key-assignment
LEAK_LINE = 'api_key = "AKIAIOSFODNN7EXAMPLE0123456789"\n'
ZERO40 = "0" * 40


def _wired(tmp_path: Path, *, harnesses: tuple[str, ...] = ("claude",), name: str = "repo") -> Path:
    target = tmp_path / name
    selection = Selection(depth="repo", harnesses=list(harnesses), owner=harnesses[0], includes=[])
    assert install_selection(target, selection) == 0
    return target


def _payload(target: Path, event: str, *, session_id: str = "s1", **extra):
    return {"session_id": session_id, "cwd": str(target), "hook_event_name": event, **extra}


def _bash(target: Path, event: str, command: str, **extra):
    return _payload(target, event, tool_name="Bash", tool_input={"command": command}, **extra)


def _write(target: Path):
    return _payload(
        target,
        "PostToolUse",
        tool_name="Write",
        tool_input={"file_path": str(target / "src.py"), "content": "x\n"},
    )


def _set_mode(target: Path, mode: str, capsys) -> None:
    assert cli.main(["work", "mode", "set", mode, "--target", str(target)]) == 0
    capsys.readouterr()


def _settings_events(target: Path) -> dict[str, list]:
    return json.loads((target / ".claude" / "settings.json").read_text())["hooks"]


def test_config_round_trips_mode_and_rails_and_omits_defaults(tmp_path: Path) -> None:
    target = tmp_path / "t"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    config.write_config(target, config.Config(version=1, selection=selection))
    assert "work" not in json.loads(config.config_path(target).read_text())
    config.write_config(target, config.Config(version=1, selection=selection, work_mode="yolo", safety_rails=False))
    assert json.loads(config.config_path(target).read_text())["work"] == {"mode": "yolo", "rails": "off"}
    loaded = config.load_config(target)
    assert loaded is not None and (loaded.work_mode, loaded.safety_rails) == ("yolo", False)


def test_invalid_settings_fail_safe_in_hook_readers(tmp_path: Path) -> None:
    target = tmp_path / "t"
    path = target / ".brigade" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "work": {"mode": "chaos", "rails": "maybe"}}))
    assert work_mode.resolve_mode(target) == "enforced"
    assert work_mode.rails_enabled(target) is True
    with pytest.raises(ValueError):
        config.load_config(target)


def test_yolo_session_runs_raw_checks_and_finishes_without_block(tmp_path: Path, monkeypatch, capsys) -> None:
    target = _wired(tmp_path)
    _set_mode(target, "yolo", capsys)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief: yolo")
    monkeypatch.setattr(
        "brigade.memory_hooks.recall_text_for_hook", lambda **_kwargs: "recall: astro notes card", raising=True
    )
    delivered: list[str] = []

    def deliver(harness, session_id, event, **_kwargs):
        delivered.append(event)
        return "fleet inbox: hello from the hub"

    monkeypatch.setattr("brigade.fleet_inbox_delivery.deliver", deliver)

    start = runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    context = start["hookSpecificOutput"]["additionalContext"]
    assert "work brief: yolo" in context and "recall: astro notes card" in context

    prompt = runtime.handle_payload("UserPromptSubmit", _payload(target, "UserPromptSubmit", prompt="astro"))
    prompt_context = prompt["hookSpecificOutput"]["additionalContext"]
    assert "recall: astro notes card" in prompt_context
    assert "fleet inbox: hello from the hub" in prompt_context

    assert runtime.handle_payload("PreToolUse", _bash(target, "PreToolUse", "python -m pytest -q")) is None
    runtime.handle_payload("PostToolUse", _write(target))
    failure = runtime.handle_payload("PostToolUseFailure", _bash(target, "PostToolUseFailure", "pytest -q"))
    assert failure is None
    assert runtime.handle_payload("Stop", _payload(target, "Stop", stop_hook_active=False)) is None
    assert "UserPromptSubmit" in delivered


def test_switching_back_to_enforced_restores_block_and_hooks(tmp_path: Path, monkeypatch, capsys) -> None:
    target = _wired(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief: test")
    monkeypatch.setattr(runtime, "_run_recall", lambda *_args, **_kwargs: "")
    enforced_settings = (target / ".claude" / "settings.json").read_text()

    _set_mode(target, "yolo", capsys)
    hooks = _settings_events(target)
    assert "Stop" not in hooks and "PostToolUseFailure" not in hooks
    assert {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionEnd"} <= set(hooks)
    status = install_cmd.status_payload(target)
    assert status["current"] is True and status["work_mode"] == "yolo"

    _set_mode(target, "enforced", capsys)
    assert (target / ".claude" / "settings.json").read_text() == enforced_settings
    assert "work_mode" not in install_cmd.status_payload(target)
    denied = runtime.handle_payload("PreToolUse", _bash(target, "PreToolUse", "python -m pytest -q"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    runtime.handle_payload("PostToolUse", _write(target))
    assert runtime.handle_payload("Stop", _payload(target, "Stop", stop_hook_active=False))["decision"] == "block"


def test_mode_switch_preserves_foreign_stop_handlers(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    settings_path = target / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    foreign = {"hooks": [{"type": "command", "command": "echo mine"}]}
    settings["hooks"]["Stop"].append(foreign)
    settings_path.write_text(json.dumps(settings))
    _set_mode(target, "yolo", capsys)
    assert _settings_events(target)["Stop"] == [foreign]
    _set_mode(target, "enforced", capsys)
    stop = _settings_events(target)["Stop"]
    assert foreign in stop and len(stop) == 2


def test_stale_enforcement_event_in_yolo_project_is_reported(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    # Switching only the setting, without the reinstall, leaves Stop installed.
    assert work_mode.set_mode(target, "yolo") is None
    status = install_cmd.status_payload(target)
    assert status["current"] is False and "Stop" in status["stale_events"]


def test_init_yolo_installs_without_enforcement_events(tmp_path: Path, capsys) -> None:
    target = tmp_path / "fresh"
    rc = cli.main(["init", "--target", str(target), "--depth", "repo", "--harnesses", "claude", "--yolo"])
    assert rc == 0
    capsys.readouterr()
    assert work_mode.resolve_mode(target) == "yolo"
    assert "Stop" not in _settings_events(target)
    # Re-running init without --yolo keeps the recorded mode.
    assert cli.main(["init", "--target", str(target), "--depth", "repo", "--harnesses", "claude"]) == 0
    assert work_mode.resolve_mode(target) == "yolo"


def test_yolo_brief_drops_verification_wording(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    assert cli.main(["work", "brief", "--target", str(target)]) == 0
    assert "Verify captured through Brigade" in capsys.readouterr().out
    _set_mode(target, "yolo", capsys)
    assert cli.main(["work", "brief", "--target", str(target)]) == 0
    text = capsys.readouterr().out
    assert "Verify captured" not in text and "Outcome recorded" not in text
    assert "work_mode: yolo" in text and "Memory Handoff" in text
    assert cli.main(["work", "brief", "--target", str(target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["work_mode"] == {"mode": "yolo", "rails": "on"}
    assert not any("Verify" in item for item in payload["session_close"]["items"])


def _unreceipted_session(repo: Path, session_id: str = "yolo-session") -> None:
    now = localio.utc_now()
    runtime.write_session_state(
        repo,
        session_id,
        {
            "session_id": session_id,
            "session_fingerprint": runtime._session_fingerprint(session_id),
            "target": str(repo.resolve()),
            "started_at": (now - timedelta(hours=1)).isoformat(),
            "last_write_at": (now - timedelta(minutes=30)).isoformat(),
            "briefed": True,
            "write_observed": True,
            "verify_denied_count": 0,
        },
    )


def test_adoption_reports_yolo_and_never_bypassed(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    repo = _wired(workspace / "repos", name="claude")
    (workspace / ".brigade").mkdir(parents=True)
    (workspace / ".brigade" / "repos.toml").write_text(
        '[[repo]]\nid = "claude"\nlabel = "claude repo"\npath = "repos/claude"\nenabled = true\nexpect_brigade = true\n'
    )
    _unreceipted_session(repo)
    payload, _ = adoption.adoption_payload(target=workspace, harnesses=["claude"])
    assert payload is not None and payload["rows"][0]["state"] == "bypassed"
    assert classify.classify(repo)["state"] == "dormant"

    _set_mode(repo, "yolo", capsys)
    payload, _ = adoption.adoption_payload(target=workspace, harnesses=["claude"])
    assert payload is not None
    row = payload["rows"][0]
    assert row["state"] == "yolo" and row["work"] == {"mode": "yolo", "rails": "on"}
    assert row["use"]["bypassed_session_count"] == 0
    assert [item["status"] for item in row["use"]["sessions"]] == ["yolo"]
    assert payload["denominators"]["bypassed_sessions"] == 0
    assert payload["monitor"]["alerts"] == []
    assert classify.classify(repo)["state"] == "yolo"


def test_doctor_and_status_show_mode_and_rails(tmp_path: Path, monkeypatch, capsys) -> None:
    target = _wired(tmp_path)
    monkeypatch.setattr(work_mode, "_config_is_tracked", lambda _target, _path=None: False)
    _set_mode(target, "yolo", capsys)
    assert cli.main(["work", "rails", "off", "--target", str(target)]) == 0
    capsys.readouterr()
    checks = {name: (status, detail) for status, name, detail in work_mode.doctor_checks(target)}
    assert checks["work mode"][0] == "INFO" and checks["work mode"][1].startswith("yolo")
    assert checks["safety rails"][0] == "WARN" and "secrets" in checks["safety rails"][1]
    assert cli.main(["status", "--target", str(target)]) == 0
    assert "work mode: yolo; safety rails: off" in capsys.readouterr().out
    cli.main(["status", "--target", str(target), "--json"])
    assert json.loads(capsys.readouterr().out)["work"] == {"mode": "yolo", "rails": "off"}


def test_rails_are_independent_of_mode_and_warn_once(tmp_path: Path, monkeypatch, capsys) -> None:
    target = _wired(tmp_path)
    monkeypatch.setattr(work_mode, "_config_is_tracked", lambda _target, _path=None: False)
    assert cli.main(["work", "rails", "check", "--target", str(target)]) == 0
    assert cli.main(["work", "rails", "off", "--target", str(target)]) == 0
    first = capsys.readouterr().err
    assert "secrets, credentials, private hosts, or private paths" in first
    assert cli.main(["work", "rails", "off", "--target", str(target)]) == 0
    assert "secrets" not in capsys.readouterr().err
    assert work_mode.resolve_mode(target) == "enforced"
    assert cli.main(["work", "rails", "check", "--target", str(target)]) == work_mode.RAILS_OFF_EXIT
    _set_mode(target, "yolo", capsys)
    assert work_mode.summary(target) == {"mode": "yolo", "rails": "off"}
    assert cli.main(["work", "rails", "on", "--target", str(target)]) == 0
    assert cli.main(["work", "rails", "check", "--target", str(target)]) == 0


def test_committed_rails_off_cannot_disable_scanning(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    assert work_mode.set_rails(target, "off") is None
    assert work_mode.rails_enabled(target) is False
    subprocess.run(["git", "add", "-f", ".brigade/config.json"], cwd=target, check=True)
    assert work_mode.rails_enabled(target) is True
    status, _, detail = work_mode.doctor_checks(target)[1]
    assert status == "WARN" and "git tracks .brigade/config.json" in detail


def test_committed_legacy_config_cannot_disable_scanning(tmp_path: Path) -> None:
    target = tmp_path / "repo"
    legacy = target / config.LEGACY_WORKSPACE_DIRNAMES[0]
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text(json.dumps({"work": {"rails": "off"}}), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    assert not (target / config.CONFIG_REL_PATH).exists()
    assert work_mode.rails_off_requested(target) is True
    assert work_mode.rails_enabled(target) is False
    subprocess.run(["git", "add", "-f", str(legacy / "config.json")], cwd=target, check=True)
    assert work_mode.rails_enabled(target) is True


@pytest.mark.parametrize("value", [[], {}, ["off"], 0, None])
def test_non_string_rails_values_fail_safe(tmp_path: Path, value, capsys) -> None:
    target = _wired(tmp_path)
    path = target / config.CONFIG_REL_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("work", {})["rails"] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        config.validate_safety_rails(value)
    assert work_mode.rails_off_requested(target) is False
    assert work_mode.rails_enabled(target) is True
    assert work_mode.summary(target)["rails"] == "on"
    cli.main(["work", "rails", "show", "--target", str(target)])
    capsys.readouterr()


def _git_env(home: Path, shim_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "BRIGADE_NO_UPDATE_CHECK": "1",
        "CONTENT_GUARD_EXTRA_POLICY": str(home / "no-private-policy.json"),
        "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(SRC),
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim for the checkout's brigade CLI")
def test_pre_push_honors_rails_switch_with_planted_secret(tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "brigade"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m brigade "$@"\n')
    shim.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env(tmp_path, shim_dir)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote))
    (repo / ".gitignore").write_text(".brigade/\n")
    (repo / "leak.txt").write_text(LEAK_LINE)
    git("add", ".gitignore", "leak.txt")
    git("commit", "-q", "-m", "plant test secret")
    tip = git("rev-parse", "HEAD")
    (repo / ".brigade").mkdir()
    selection = Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[])
    config.write_config(repo, config.Config(version=1, selection=selection))

    def push_hook() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(PRE_PUSH), "origin", str(remote)],
            cwd=repo,
            env=env,
            input=f"refs/heads/main {tip} refs/heads/main {ZERO40}\n",
            capture_output=True,
            text=True,
            check=False,
        )

    blocked = push_hook()
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    assert "BLOCKED" in blocked.stderr

    assert work_mode.set_rails(repo, "off") is None
    allowed = push_hook()
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "safety rails are off" in allowed.stderr
    assert "scanning" not in allowed.stdout

    assert work_mode.set_rails(repo, "on") is None
    restored = push_hook()
    assert restored.returncode == 1, restored.stdout + restored.stderr
    assert "BLOCKED" in restored.stderr
