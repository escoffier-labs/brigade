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
from tests._home import set_home

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


def _set_repo_rails_off(config_file: Path) -> None:
    """Plant ``work.rails: "off"`` in a repository config, as earlier builds of #2233 wrote it."""
    data = json.loads(config_file.read_text(encoding="utf-8"))
    data.setdefault("work", {})["rails"] = "off"
    config_file.write_text(json.dumps(data), encoding="utf-8")


def _rails_off_config(target: Path) -> Path:
    selection = Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[])
    config.write_config(target, config.Config(version=1, selection=selection))
    path = target / config.CONFIG_REL_PATH
    _set_repo_rails_off(path)
    return path


def test_config_round_trips_mode_and_never_writes_rails(tmp_path: Path) -> None:
    target = tmp_path / "t"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    config.write_config(target, config.Config(version=1, selection=selection))
    assert json.loads(config.config_path(target).read_text())["work"] == {"mode": "strict"}
    config.write_config(target, config.Config(version=1, selection=selection, work_mode="off"))
    assert json.loads(config.config_path(target).read_text())["work"] == {"mode": "off"}
    loaded = config.load_config(target)
    assert loaded is not None and loaded.work_mode == "off"
    assert not hasattr(loaded, "safety_rails")


def test_invalid_settings_fail_safe_in_hook_readers(tmp_path: Path) -> None:
    target = tmp_path / "t"
    path = target / ".brigade" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "work": {"mode": "chaos", "rails": "maybe"}}))
    assert work_mode.resolve_mode(target) == "strict"
    assert work_mode.rails_enabled(target) is True
    with pytest.raises(ValueError):
        config.load_config(target)


@pytest.mark.parametrize("mode", ["observe", "off", "yolo"])
def test_yolo_session_runs_raw_checks_and_finishes_without_block(tmp_path: Path, monkeypatch, capsys, mode) -> None:
    target = _wired(tmp_path)
    _set_mode(target, mode, capsys)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief: yolo")
    monkeypatch.setattr(
        "brigade.memory_hooks.recall_for_hook", lambda **_kwargs: ("recall: astro notes card", []), raising=True
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

    _set_mode(target, "off", capsys)
    # Yolo keeps every managed event; the runtime skips checks per target.
    assert (target / ".claude" / "settings.json").read_text() == enforced_settings
    assert set(install_cmd.MANAGED_EVENTS) <= set(_settings_events(target))
    status = install_cmd.status_payload(target)
    assert status["current"] is True and status["work_mode"] == "off"

    _set_mode(target, "strict", capsys)
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
    _set_mode(target, "off", capsys)
    stop = _settings_events(target)["Stop"]
    assert foreign in stop and len(stop) == 2
    _set_mode(target, "strict", capsys)
    stop = _settings_events(target)["Stop"]
    assert foreign in stop and len(stop) == 2


def test_yolo_install_missing_stop_is_reported_and_healed(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    _set_mode(target, "off", capsys)
    # An install written by a build that dropped Stop in yolo mode is incomplete.
    settings_path = target / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    del settings["hooks"]["Stop"]
    settings_path.write_text(json.dumps(settings))
    status = install_cmd.status_payload(target)
    assert status["current"] is False and "Stop" in status["missing_events"]
    _set_mode(target, "off", capsys)
    assert "Stop" in _settings_events(target)
    assert install_cmd.status_payload(target)["current"] is True


def test_yolo_workspace_session_is_blocked_for_enforced_child_repo(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = _wired(tmp_path, name="workspace")
    _set_mode(workspace, "off", capsys)
    child = workspace / "child"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(child, selection) == 0
    capsys.readouterr()
    assert "Stop" in _settings_events(workspace)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief: test")
    monkeypatch.setattr(runtime, "_run_recall", lambda *_args, **_kwargs: "")

    runtime.handle_payload("SessionStart", _payload(workspace, "SessionStart", session_id="mixed"))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            workspace,
            "PostToolUse",
            session_id="mixed",
            tool_name="Write",
            tool_input={"file_path": str(child / "src.py"), "content": "x\n"},
        ),
    )
    blocked = runtime.handle_payload("Stop", _payload(workspace, "Stop", session_id="mixed", stop_hook_active=False))
    assert blocked is not None and blocked["decision"] == "block"
    assert str(child.resolve()) in blocked["reason"]


def test_yolo_only_session_finishes_with_stop_installed(tmp_path: Path, monkeypatch, capsys) -> None:
    target = _wired(tmp_path)
    _set_mode(target, "off", capsys)
    assert "Stop" in _settings_events(target)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief: test")
    monkeypatch.setattr(runtime, "_run_recall", lambda *_args, **_kwargs: "")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    runtime.handle_payload("PostToolUse", _write(target))
    assert runtime.handle_payload("Stop", _payload(target, "Stop", stop_hook_active=False)) is None


def test_init_yolo_installs_every_managed_event(tmp_path: Path, capsys) -> None:
    target = tmp_path / "fresh"
    rc = cli.main(["init", "--target", str(target), "--depth", "repo", "--harnesses", "claude", "--yolo"])
    assert rc == 0
    capsys.readouterr()
    assert work_mode.resolve_mode(target) == "off"
    assert set(install_cmd.MANAGED_EVENTS) <= set(_settings_events(target))
    # Re-running init without --yolo keeps the recorded mode.
    assert cli.main(["init", "--target", str(target), "--depth", "repo", "--harnesses", "claude"]) == 0
    assert work_mode.resolve_mode(target) == "off"


def test_init_engineer_switches_a_yolo_install_back(tmp_path: Path, capsys) -> None:
    target = tmp_path / "fresh"
    base = ["init", "--target", str(target), "--depth", "repo", "--harnesses", "claude"]
    assert cli.main([*base, "--yolo"]) == 0
    assert cli.main([*base, "--engineer"]) == 0
    capsys.readouterr()
    assert work_mode.resolve_mode(target) == "strict"
    # The default is explicitly stored.
    assert json.loads((target / config.CONFIG_REL_PATH).read_text(encoding="utf-8"))["work"] == {"mode": "strict"}
    with pytest.raises(SystemExit):
        cli.main([*base, "--yolo", "--engineer"])


def test_legacy_enforced_name_reads_and_sets_as_engineer(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    path = target / config.CONFIG_REL_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    data["work"] = {"mode": "enforced"}
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = config.load_config(target)
    assert loaded is not None and loaded.work_mode == "strict"
    assert work_mode.resolve_mode(target) == "strict"
    assert not any(row[1] == "work.mode" for row in work_mode.doctor_checks(target))
    _set_mode(target, "off", capsys)
    _set_mode(target, "enforced", capsys)
    assert work_mode.resolve_mode(target) == "strict"
    assert json.loads(path.read_text(encoding="utf-8"))["work"] == {"mode": "strict"}
    denied = runtime.handle_payload("PreToolUse", _bash(target, "PreToolUse", "python -m pytest -q"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_mode_set_rejects_unknown_mode_without_naming_the_alias(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    path = target / config.CONFIG_REL_PATH
    before = path.read_text(encoding="utf-8")
    assert cli.main(["work", "mode", "set", "enforce", "--target", str(target)]) == 2
    err = capsys.readouterr().err
    assert "mode must be one of: strict, observe, off" in err
    assert "enforced" not in err
    assert path.read_text(encoding="utf-8") == before


def test_yolo_brief_drops_verification_wording(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    assert cli.main(["work", "brief", "--target", str(target)]) == 0
    assert "Verify captured through Brigade" in capsys.readouterr().out
    _set_mode(target, "off", capsys)
    assert cli.main(["work", "brief", "--target", str(target)]) == 0
    text = capsys.readouterr().out
    assert "Verify captured" not in text and "Outcome recorded" not in text
    assert "work_mode: off" in text and "Memory Handoff" in text
    assert cli.main(["work", "brief", "--target", str(target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["work_mode"] == {"mode": "off", "rails": "on"}
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


@pytest.mark.parametrize("mode", ["observe", "off"])
def test_adoption_reports_unenforced_modes_and_never_bypassed(tmp_path: Path, capsys, mode) -> None:
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

    _set_mode(repo, mode, capsys)
    payload, _ = adoption.adoption_payload(target=workspace, harnesses=["claude"])
    assert payload is not None
    row = payload["rows"][0]
    assert row["state"] == mode and row["work"] == {"mode": mode, "rails": "on"}
    assert row["use"]["bypassed_session_count"] == 0
    assert [item["status"] for item in row["use"]["sessions"]] == [mode]
    assert payload["denominators"]["bypassed_sessions"] == 0
    assert payload["monitor"]["alerts"] == []
    assert classify.classify(repo)["state"] == mode


def test_doctor_and_status_show_mode_and_rails(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    _set_mode(target, "off", capsys)
    before = (target / config.CONFIG_REL_PATH).read_text(encoding="utf-8")
    assert cli.main(["work", "rails", "off", "--target", str(target)]) == 0
    capsys.readouterr()
    assert (target / config.CONFIG_REL_PATH).read_text(encoding="utf-8") == before
    checks = {name: (status, detail) for status, name, detail in work_mode.doctor_checks(target)}
    assert checks["work mode"][0] == "INFO" and checks["work mode"][1].startswith("off")
    assert checks["safety rails"][0] == "WARN" and "secrets" in checks["safety rails"][1]
    assert cli.main(["status", "--target", str(target)]) == 0
    assert "work mode: off; safety rails: off" in capsys.readouterr().out
    cli.main(["status", "--target", str(target), "--json"])
    assert json.loads(capsys.readouterr().out)["work"] == {"mode": "off", "rails": "off"}


def test_rails_are_independent_of_mode_and_warn_once(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    assert cli.main(["work", "rails", "check", "--target", str(target)]) == 0
    assert cli.main(["work", "rails", "off", "--target", str(target)]) == 0
    first = capsys.readouterr().err
    assert "secrets, credentials, private hosts, or private paths" in first
    assert cli.main(["work", "rails", "off", "--target", str(target)]) == 0
    assert "secrets" not in capsys.readouterr().err
    assert work_mode.resolve_mode(target) == "strict"
    assert cli.main(["work", "rails", "check", "--target", str(target)]) == work_mode.RAILS_OFF_EXIT
    _set_mode(target, "off", capsys)
    assert work_mode.summary(target) == {"mode": "off", "rails": "off"}
    assert cli.main(["work", "rails", "on", "--target", str(target)]) == 0
    assert cli.main(["work", "rails", "check", "--target", str(target)]) == 0


def test_committed_rails_off_cannot_disable_scanning(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    _set_repo_rails_off(target / config.CONFIG_REL_PATH)
    assert work_mode.rails_enabled(target) is True
    subprocess.run(["git", "add", "-f", ".brigade/config.json"], cwd=target, check=True)
    assert work_mode.rails_enabled(target) is True
    rows = {name: (status, detail) for status, name, detail in work_mode.doctor_checks(target)}
    assert rows["work.rails"][0] == "WARN" and "ignored" in rows["work.rails"][1]
    assert rows["safety rails"][1].startswith("on:")
    assert cli.main(["work", "rails", "show", "--target", str(target)]) == 0
    shown = capsys.readouterr()
    assert "safety rails: on" in shown.out and "is ignored" in shown.err


def test_rails_off_never_writes_the_repository(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    before = (target / config.CONFIG_REL_PATH).read_text(encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["work", "rails", "off", "--target", str(target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    record = work_mode.rails_record_path()
    assert record is not None and payload["rails_record"] == str(record)
    assert not work_mode._inside(record, target)
    assert (target / config.CONFIG_REL_PATH).read_text(encoding="utf-8") == before
    assert json.loads(record.read_text(encoding="utf-8"))["rails_off"].keys() == {work_mode.repository_key(target)}


def test_committed_legacy_config_cannot_disable_scanning(tmp_path: Path) -> None:
    target = tmp_path / "repo"
    legacy = target / config.LEGACY_WORKSPACE_DIRNAMES[0]
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text(json.dumps({"version": 1, "work": {"rails": "off"}}), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    assert not (target / config.CONFIG_REL_PATH).exists()
    assert work_mode.rails_enabled(target) is True
    assert work_mode.repo_rails_setting(target) == legacy / "config.json"
    subprocess.run(["git", "add", "-f", str(legacy / "config.json")], cwd=target, check=True)
    assert work_mode.rails_enabled(target) is True


@pytest.mark.parametrize("value", [[], {}, ["off"], 0, None])
def test_non_string_rails_values_fail_safe(tmp_path: Path, value, capsys) -> None:
    target = _wired(tmp_path)
    path = target / config.CONFIG_REL_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("work", {})["rails"] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    assert work_mode.rails_enabled(target) is True
    assert work_mode.summary(target)["rails"] == "on"
    cli.main(["work", "rails", "show", "--target", str(target)])
    capsys.readouterr()


def test_invalid_work_mode_keeps_enforcement_in_hooks(tmp_path: Path) -> None:
    target = _wired(tmp_path)
    path = target / config.CONFIG_REL_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("work", {})["mode"] = "typo"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = config.load_config(target)
    assert loaded is not None and loaded.work_mode == "strict"
    assert work_mode.resolve_mode(target) == "strict"
    assert any(row[0] == "WARN" and row[1] == "work.mode" for row in work_mode.doctor_checks(target))


def test_yolo_sessions_are_never_bypassed_even_with_stale_wiring(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    repo = _wired(workspace / "repos", name="claude")
    (workspace / ".brigade").mkdir(parents=True)
    (workspace / ".brigade" / "repos.toml").write_text(
        '[[repo]]\nid = "claude"\nlabel = "claude repo"\npath = "repos/claude"\nenabled = true\nexpect_brigade = true\n'
    )
    _unreceipted_session(repo)
    _set_mode(repo, "off", capsys)
    settings_path = repo / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                hook["timeout"] = int(hook.get("timeout", 10)) + 1
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    payload, _ = adoption.adoption_payload(target=workspace, harnesses=["claude"])
    assert payload is not None
    row = payload["rows"][0]
    assert row["state"] == "stale"
    assert row["use"]["bypassed_session_count"] == 0
    assert payload["denominators"]["bypassed_sessions"] == 0


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
def test_pre_push_honors_rails_switch_with_planted_secret(tmp_path: Path, monkeypatch) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "brigade"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m brigade "$@"\n')
    shim.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env(tmp_path, shim_dir)
    # The hook's `brigade` and this process must read the same per-user rails record.
    set_home(monkeypatch, env["HOME"])
    monkeypatch.setenv("XDG_CONFIG_HOME", env["XDG_CONFIG_HOME"])

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

    # A rails-off value inside the repository is ignored: the push still blocks.
    _set_repo_rails_off(repo / config.CONFIG_REL_PATH)
    still_blocked = push_hook()
    assert still_blocked.returncode == 1, still_blocked.stdout + still_blocked.stderr
    assert "BLOCKED" in still_blocked.stderr

    assert work_mode.set_rails(repo, "off") is None
    allowed = push_hook()
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "safety rails are off" in allowed.stderr
    assert "scanning" not in allowed.stdout

    assert work_mode.set_rails(repo, "on") is None
    restored = push_hook()
    assert restored.returncode == 1, restored.stdout + restored.stderr
    assert "BLOCKED" in restored.stderr


def test_session_integrity_scan_runs_in_yolo_mode(tmp_path: Path, monkeypatch, capsys) -> None:
    target = _wired(tmp_path)
    _set_mode(target, "off", capsys)

    def refuse(_target: Path) -> bool:
        raise classify.SessionIntegrityError("planted")

    monkeypatch.setattr(classify, "_recent_write_without_receipt", refuse)
    result = classify.classify(target)
    assert result is not None and result["state"] == "error"
    assert any("session directory integrity refused" in issue for issue in result["issues"])


def test_init_yolo_prints_yolo_agent_workflow(tmp_path: Path, capsys) -> None:
    target = tmp_path / "fresh"
    args = ["init", "--target", str(target), "--depth", "repo", "--harnesses", "claude"]
    assert cli.main([*args, "--yolo"]) == 0
    yolo_out = capsys.readouterr().out
    assert "off work mode" in yolo_out
    assert "THROUGH brigade" not in yolo_out and "outcome capture" not in yolo_out
    other = tmp_path / "enforced"
    assert cli.main(["init", "--target", str(other), "--depth", "repo", "--harnesses", "claude"]) == 0
    assert "verify THROUGH brigade" in capsys.readouterr().out


def test_unsupported_config_version_cannot_turn_rails_off(tmp_path: Path) -> None:
    target = _wired(tmp_path)
    path = target / config.CONFIG_REL_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update({"version": 99, "work": {"mode": "off", "rails": "off"}})
    path.write_text(json.dumps(data), encoding="utf-8")
    assert work_mode.rails_enabled(target) is True
    assert work_mode.resolve_mode(target) == "strict"


def test_config_write_failure_is_reported_and_keeps_old_config(tmp_path: Path, monkeypatch) -> None:
    target = _wired(tmp_path)
    path = target / config.CONFIG_REL_PATH
    before = path.read_text(encoding="utf-8")

    def boom(_src, _dst) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", boom)
    error = work_mode.set_mode(target, "off")
    assert error is not None and "disk full" in error
    assert path.read_text(encoding="utf-8") == before
    assert sorted(item.name for item in path.parent.iterdir() if item.name.endswith(".tmp")) == []


def test_mode_set_warns_when_managed_hooks_lack_sidecar(tmp_path: Path, capsys) -> None:
    target = _wired(tmp_path)
    (target / install_cmd.SIDECAR_REL_PATH).unlink()
    assert cli.main(["work", "mode", "set", "off", "--target", str(target)]) == 0
    assert "managed Claude hooks exist without" in capsys.readouterr().err


def test_doctor_does_not_claim_checks_active_without_pre_push_hook(tmp_path: Path) -> None:
    target = tmp_path / "bare"
    (target / ".brigade").mkdir(parents=True)
    config.write_config(
        target,
        config.Config(version=1, selection=Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[])),
    )
    rows = {name: detail for _, name, detail in work_mode.doctor_checks(target)}
    assert "none was found" in rows["safety rails"] and "are active" not in rows["safety rails"]


def _symlink(link: Path, target: Path | str, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")


def _git_repo_with_rails_off_settings(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "shared-settings").mkdir(parents=True)
    shared = repo / "shared-settings" / "config.json"
    _rails_off_config(repo).replace(shared)
    (repo / ".brigade").rmdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def test_tracked_brigade_symlink_cannot_disable_rails(tmp_path: Path) -> None:
    repo = _git_repo_with_rails_off_settings(tmp_path)
    _symlink(repo / ".brigade", "shared-settings", directory=True)
    subprocess.run(["git", "add", ".brigade", "shared-settings/config.json"], cwd=repo, check=True)
    assert work_mode.rails_enabled(repo) is True


def test_symlinked_config_file_cannot_disable_rails(tmp_path: Path) -> None:
    repo = _git_repo_with_rails_off_settings(tmp_path)
    (repo / ".brigade").mkdir()
    # Path() gives native separators: Windows does not resolve "/" in a relative link.
    _symlink(repo / config.CONFIG_REL_PATH, Path("..", "shared-settings", "config.json"))
    assert work_mode.rails_enabled(repo) is True


def test_config_resolving_outside_target_cannot_disable_rails(tmp_path: Path) -> None:
    repo = _git_repo_with_rails_off_settings(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "shared-settings" / "config.json").replace(outside / "config.json")
    _symlink(repo / ".brigade", outside, directory=True)
    assert work_mode.rails_enabled(repo) is True


def test_plain_untracked_repo_config_no_longer_turns_rails_off(tmp_path: Path) -> None:
    repo = _git_repo_with_rails_off_settings(tmp_path)
    (repo / ".brigade").mkdir()
    (repo / "shared-settings" / "config.json").replace(repo / config.CONFIG_REL_PATH)
    assert work_mode.rails_enabled(repo) is True
    assert work_mode.set_rails(repo, "off") is None
    assert work_mode.rails_enabled(repo) is False


def _git_c(cwd: Path, *args: str) -> None:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "protocol.file.allow=always"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True)


def _rails_off_settings_repo(path: Path) -> Path:
    """A standalone repository whose committed config.json sets rails off."""
    path.mkdir(parents=True)
    _rails_off_config(path).replace(path / "config.json")
    (path / ".brigade").rmdir()
    _git_c(path, "init", "-q")
    _git_c(path, "add", "config.json")
    _git_c(path, "commit", "-q", "-m", "settings")
    return path


def test_committed_brigade_submodule_cannot_disable_rails(tmp_path: Path) -> None:
    settings = _rails_off_settings_repo(tmp_path / "settings-upstream")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_c(repo, "init", "-q")
    _git_c(repo, "submodule", "add", "-q", str(settings), ".brigade")
    _git_c(repo, "commit", "-q", "-m", "add settings submodule")
    clone = tmp_path / "clone"
    _git_c(tmp_path, "clone", "-q", "--recurse-submodules", str(repo), str(clone))
    assert work_mode.rails_enabled(clone) is True


def test_gitlink_ancestor_cannot_disable_rails_without_nested_git(tmp_path: Path) -> None:
    settings = _rails_off_settings_repo(tmp_path / "settings-upstream")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=settings, check=True, capture_output=True, text=True)
    repo = tmp_path / "repo"
    (repo / ".brigade").mkdir(parents=True)
    _git_c(repo, "init", "-q")
    _git_c(repo, "update-index", "--add", "--cacheinfo", f"160000,{head.stdout.strip()},.brigade")
    (settings / "config.json").replace(repo / config.CONFIG_REL_PATH)
    assert work_mode.rails_enabled(repo) is True


def test_nested_independent_repo_config_cannot_disable_rails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_c(repo, "init", "-q")
    # The nested repository's own committed config.json is .brigade/config.json here.
    _rails_off_settings_repo(repo / ".brigade")
    assert work_mode.rails_enabled(repo) is True


def test_committed_nested_bare_repository_cannot_disable_rails(tmp_path: Path) -> None:
    """Codex review 4457092224: committed files that Git discovers as a nested bare repository."""
    repo = tmp_path / "repo"
    nested = repo / "nested"
    for sub in ("objects", "refs"):
        (nested / sub).mkdir(parents=True)
        (nested / sub / ".keep").write_text("", encoding="utf-8")
    (nested / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (nested / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n", encoding="utf-8")
    (nested / ".brigade").mkdir()
    _rails_off_config(nested)
    _git_c(repo, "init", "-q")
    _git_c(repo, "add", "-f", ".")
    _git_c(repo, "commit", "-q", "-m", "nested bare layout")
    clone = tmp_path / "clone"
    _git_c(tmp_path, "clone", "-q", str(repo), str(clone))
    assert work_mode.rails_enabled(clone / "nested") is True
    assert work_mode.rails_enabled(clone) is True


def test_user_record_turns_rails_off_for_exact_canonical_path(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    repo = _wired(parent, name="repo")
    sibling = _wired(parent, name="sibling")
    child = repo / "src"
    child.mkdir()
    assert work_mode.set_rails(repo, "off") is None
    assert work_mode.rails_enabled(repo) is False
    for other in (sibling, parent, child, tmp_path / "parent" / "repo-2"):
        assert work_mode.rails_enabled(other) is True, other
    alias = tmp_path / "alias"
    _symlink(alias, repo, directory=True)
    assert work_mode.repository_key(alias) == work_mode.repository_key(repo)
    assert work_mode.rails_enabled(alias) is False
    assert work_mode.set_rails(alias, "on") is None
    assert work_mode.rails_enabled(repo) is True
    record = work_mode.rails_record_path()
    assert record is not None and json.loads(record.read_text(encoding="utf-8"))["rails_off"] == {}


def test_symlinked_target_writes_the_real_repository_record(tmp_path: Path) -> None:
    repo = _wired(tmp_path)
    alias = tmp_path / "alias"
    _symlink(alias, repo, directory=True)
    assert work_mode.set_rails(alias, "off") is None
    assert work_mode.rails_enabled(repo) is False


def test_repository_key_folds_case_only_on_case_insensitive_platforms(tmp_path: Path, monkeypatch) -> None:
    upper, lower = tmp_path / "Repo", tmp_path / "repo"
    monkeypatch.setattr(work_mode, "_case_insensitive_paths", lambda: True)
    assert work_mode.repository_key(upper) == work_mode.repository_key(lower)
    monkeypatch.setattr(work_mode, "_case_insensitive_paths", lambda: False)
    assert work_mode.repository_key(upper) != work_mode.repository_key(lower)


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"version": 2, "rails_off": {}}', '{"version": 1, "rails_off": []}']
)
def test_unreadable_user_record_keeps_rails_on(tmp_path: Path, content: str) -> None:
    repo = _wired(tmp_path)
    assert work_mode.set_rails(repo, "off") is None
    record = work_mode.rails_record_path()
    assert record is not None
    if content.startswith('{"version": 2'):
        content = json.dumps({"version": 2, "rails_off": {work_mode.repository_key(repo): {}}})
    record.write_text(content, encoding="utf-8")
    assert work_mode.rails_enabled(repo) is True


def test_user_record_inside_the_target_is_ignored(tmp_path: Path, monkeypatch) -> None:
    repo = _wired(tmp_path)
    # The config root is platform-specific (XDG on Linux, LOCALAPPDATA on Windows),
    # so point it at the repository directly.
    monkeypatch.setattr(work_mode.component_paths, "config_root", lambda: repo / "config-home")
    record = work_mode.rails_record_path()
    assert record is not None
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"version": 1, "rails_off": {work_mode.repository_key(repo): {}}}), encoding="utf-8")
    assert work_mode.rails_enabled(repo) is True
    assert work_mode.set_rails(repo, "off") is not None


def test_rails_on_clears_an_entry_whose_value_is_null(tmp_path: Path) -> None:
    repo = _wired(tmp_path)
    assert work_mode.set_rails(repo, "off") is None
    record = work_mode.rails_record_path()
    assert record is not None
    record.write_text(json.dumps({"version": 1, "rails_off": {work_mode.repository_key(repo): None}}), encoding="utf-8")
    assert work_mode.rails_enabled(repo) is False
    assert work_mode.set_rails(repo, "on") is None
    assert work_mode.rails_enabled(repo) is True


def test_user_record_inside_an_enclosing_checkout_is_ignored(tmp_path: Path, monkeypatch) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
    repo = _wired(outer / "nested")
    settings = outer / "settings"
    settings.mkdir()
    config_home = tmp_path / "config-home"
    _symlink(config_home, settings, directory=True)
    monkeypatch.setattr(work_mode.component_paths, "config_root", lambda: config_home)
    record = work_mode.rails_record_path()
    assert record is not None
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"version": 1, "rails_off": {work_mode.repository_key(repo): {}}}), encoding="utf-8")
    assert work_mode.rails_enabled(repo) is True
    assert work_mode.set_rails(repo, "off") is not None


def test_rails_off_for_a_subdirectory_warns_about_the_hook_root(tmp_path: Path, capsys) -> None:
    repo = _wired(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    sub = repo / "pkg"
    (sub / ".brigade").mkdir(parents=True)
    selection = Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[])
    config.write_config(sub, config.Config(version=1, selection=selection))
    assert cli.main(["work", "rails", "off", "--target", str(sub)]) == 0
    assert "the pre-push hook checks the repository root" in capsys.readouterr().err
    assert work_mode.rails_enabled(repo) is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("strict", "strict"),
        ("observe", "observe"),
        ("off", "off"),
        ("engineer", "strict"),
        ("enforced", "strict"),
        ("yolo", "off"),
    ],
)
def test_mode_aliases_resolve_to_canonical(tmp_path, value, expected):
    target = _wired(tmp_path)
    path = config.config_path(target)
    data = json.loads(path.read_text())
    data["work"] = {"mode": value}
    path.write_text(json.dumps(data))
    assert work_mode.resolve_mode(target) == expected
    assert config.load_config(target).work_mode == expected


def _drop_mode_key(target):
    path = config.config_path(target)
    data = json.loads(path.read_text())
    data.pop("work", None)
    path.write_text(json.dumps(data))


def test_pin_writes_current_mode_once_and_skips_tracked_or_unreadable_config(tmp_path):
    target = _wired(tmp_path)
    _drop_mode_key(target)
    assert work_mode.pin_explicit_mode(target) == "strict"
    assert json.loads(config.config_path(target).read_text())["work"] == {"mode": "strict"}
    assert work_mode.pin_explicit_mode(target) is None
    _drop_mode_key(target)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "add", "-f", ".brigade/config.json"], check=True)
    before = config.config_path(target).read_bytes()
    assert work_mode.pin_explicit_mode(target) is None
    assert config.config_path(target).read_bytes() == before
    assert ("INFO", "work.mode") in {(s, n) for s, n, _ in work_mode.doctor_checks(target)}


@pytest.mark.parametrize(
    "content", ["{", '{"version":99,"work":{"mode":"off"}}', '{"version":1,"work":{"mode":"chaos"}}']
)
def test_unreadable_config_resolves_to_strict(tmp_path, content):
    target = _wired(tmp_path)
    config.config_path(target).write_text(content)
    assert work_mode.resolve_mode(target) == "strict"
    assert work_mode.is_strict(target)
    assert work_mode.pin_explicit_mode(target) is None
    assert config.config_path(target).read_text() == content


def test_brief_pins_an_unpinned_untracked_config(tmp_path, capsys):
    target = _wired(tmp_path)
    _drop_mode_key(target)
    assert cli.main(["work", "brief", "--target", str(target), "--json"]) == 0
    assert json.loads(config.config_path(target).read_text())["work"] == {"mode": "strict"}


@pytest.mark.parametrize("mode", ["strict", "observe", "off", "engineer", "enforced", "yolo"])
def test_every_mode_leaves_rails_record_untouched(tmp_path, monkeypatch, capsys, mode):
    record = tmp_path / "user-config" / "work-rails.json"
    monkeypatch.setattr(work_mode, "rails_record_path", lambda: record)
    target = _wired(tmp_path)
    guard = target / ".brigade" / "guard-policy.json"
    guard.write_text("{}")
    assert work_mode.set_rails(target, "off") is None
    before = record.read_bytes()
    _set_mode(target, mode, capsys)
    assert record.read_bytes() == before
    assert guard.read_text() == "{}"
    assert not work_mode.rails_enabled(target)


@pytest.mark.parametrize(
    ("flag", "stored"),
    [("--strict", "strict"), ("--engineer", "strict"), ("--observe", "observe"), ("--off", "off"), ("--yolo", "off")],
)
def test_init_mode_flags_write_canonical_mode(tmp_path, capsys, flag, stored):
    target = tmp_path / "fresh"
    assert cli.main(["init", "--target", str(target), "--depth", "repo", "--harnesses", "claude", flag]) == 0
    out = capsys.readouterr().out
    assert json.loads(config.config_path(target).read_text())["work"] == {"mode": stored}
    if stored == "observe":
        assert "Agent: observe work mode" in out and "brigade work mode set strict" in out


def test_observe_brief_states_checks_are_recorded(tmp_path, capsys):
    target = _wired(tmp_path)
    _set_mode(target, "observe", capsys)
    assert cli.main(["work", "brief", "--target", str(target)]) == 0
    assert "work_mode: observe (checks are recorded, not enforced" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["strict", "engineer", "enforced"])
def test_strict_behavior_unchanged_under_each_alias(tmp_path, monkeypatch, capsys, mode):
    target = _wired(tmp_path)
    _set_mode(target, mode, capsys)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief")
    monkeypatch.setattr(runtime, "_run_recall", lambda *args, **kwargs: "")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    denied = runtime.handle_payload("PreToolUse", _bash(target, "PreToolUse", "python -m pytest -q"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    runtime.handle_payload("PostToolUse", _write(target))
    assert runtime.handle_payload("Stop", _payload(target, "Stop", stop_hook_active=False))["decision"] == "block"


def test_outcome_loop_findings_run_only_in_strict(tmp_path, monkeypatch, capsys):
    from brigade import doctor

    calls = []
    monkeypatch.setattr("brigade.outcome_cmd.health", lambda target: calls.append(target) or {"issues": []})
    for mode in ("observe", "off", "strict"):
        target = _wired(tmp_path, name=mode)
        _set_mode(target, mode, capsys)
        doctor._check_outcome_loop(doctor.build_context(target))
    assert [path.name for path in calls] == ["strict"]
