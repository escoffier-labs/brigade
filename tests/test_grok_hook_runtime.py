"""Runtime coverage for the Grok work-loop hook."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


HOOK = Path(__file__).parents[1] / "src" / "brigade" / "templates" / "grok" / "hooks" / "brigade-work.sh"


def _write_config(target: Path, harnesses: list[str]) -> None:
    config = target / ".brigade" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"version": 1, "depth": "repo", "harnesses": harnesses, "owner": "this-repo", "includes": []})
    )


def _without_jq_path(tmp_path: Path) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for command in ("cat", "date", "dirname", "find", "grep", "mkdir", "rm", "sleep", "touch", "tr"):
        source = shutil.which(command)
        assert source is not None
        destination = bin_dir / command
        if not destination.exists():
            destination.symlink_to(source)
    python = bin_dir / "python3"
    if not python.exists():
        python.symlink_to(sys.executable)
    return str(bin_dir)


def _run_hook(tmp_path: Path, payload: dict[str, object], **env: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "BRIGADE_BIN": str(tmp_path / "missing-brigade"),
            "GROK_BRIGADE_STATE_DIR": str(tmp_path / "state"),
            "PATH": _without_jq_path(tmp_path),
        }
    )
    environment.update(env)
    return subprocess.run(
        ["/usr/bin/bash", str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def _pre_tool_payload(target: Path, command: str, *, event: str = "pre_tool_use") -> dict[str, object]:
    return {
        "hookEventName": event,
        "sessionId": "session-1",
        "workspaceRoot": str(target),
        "toolName": "run_terminal_command",
        "toolInput": {"command": command},
    }


def test_hook_parses_json_without_jq_and_uses_bash32_safe_event_lowering(tmp_path: Path):
    target = tmp_path / "repo"
    _write_config(target, ["grok"])

    result = _run_hook(tmp_path, _pre_tool_payload(target, "pytest -q", event="PRE_TOOL_USE"))

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "deny"
    assert "jq" not in HOOK.read_text()
    assert "${event,,}" not in HOOK.read_text()


def test_fallback_stops_at_canonical_home_and_requires_grok_config(tmp_path: Path):
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    home_link = tmp_path / "home-link"
    home_link.symlink_to(real_home, target_is_directory=True)
    parent = real_home / "parent"
    _write_config(parent, ["GROK"])
    target = parent / "repo"
    _write_config(target, ["claude"])
    nested = target / "src"
    nested.mkdir()

    result = _run_hook(
        tmp_path,
        _pre_tool_payload(nested, "pytest -q"),
        HOME=str(home_link),
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"decision": "allow"}


def test_fallback_recognizes_grok_config_and_blocks_raw_verification(tmp_path: Path):
    target = tmp_path / "repo"
    _write_config(target, [" GROK "])

    result = _run_hook(tmp_path, _pre_tool_payload(target, "pytest -q"))

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "deny"


def test_template_blocks_raw_focused_verify_without_matching_similar_names(tmp_path: Path):
    target = tmp_path / "repo"
    _write_config(target, ["grok"])

    focused = _run_hook(tmp_path, _pre_tool_payload(target, "./scripts/verify-focused"))
    similar = _run_hook(tmp_path, _pre_tool_payload(target, "./scripts/verify-focused-extra"))

    assert "scripts/(verify-focused|verify)" in HOOK.read_text()
    assert json.loads(focused.stdout)["decision"] == "deny"
    assert json.loads(similar.stdout) == {"decision": "allow"}


def test_direct_brigade_verify_is_allowed_but_substring_bypasses_are_denied(tmp_path: Path):
    target = tmp_path / "repo"
    _write_config(target, ["grok"])

    direct = _run_hook(
        tmp_path,
        _pre_tool_payload(target, 'brigade work verify run --target . --command "pytest -q" --capture brigade-work'),
    )
    bypass = _run_hook(
        tmp_path,
        _pre_tool_payload(target, 'echo "brigade work verify run"; pytest -q'),
    )
    brief_bypass = _run_hook(
        tmp_path,
        _pre_tool_payload(target, 'echo "brigade work brief"; pytest -q'),
    )

    assert json.loads(direct.stdout) == {"decision": "allow"}
    assert json.loads(bypass.stdout)["decision"] == "deny"
    assert json.loads(brief_bypass.stdout)["decision"] == "deny"


def test_session_start_uses_portable_bounded_brief_runner(tmp_path: Path):
    target = tmp_path / "repo"
    _write_config(target, ["grok"])
    brigade = tmp_path / "brigade"
    brigade.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "work" ] && [ "$2" = "resolve-target" ]; then exit 1; fi\n'
        'if [ "$1" = "work" ] && [ "$2" = "brief" ]; then sleep 5; fi\n'
    )
    brigade.chmod(0o755)
    payload = {
        "hookEventName": "session_start",
        "sessionId": "slow-brief",
        "workspaceRoot": str(target),
    }

    result = _run_hook(
        tmp_path,
        payload,
        BRIGADE_BIN=str(brigade),
        BRIGADE_GROK_BRIEF_TIMEOUT_SECONDS="1",
    )

    assert result.returncode == 0
    failed = tmp_path / "state" / "slow-brief" / "brief.failed"
    deadline = time.monotonic() + 3
    while not failed.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert failed.is_file()
    assert 'timeout "$brief_timeout_seconds"' not in HOOK.read_text()
