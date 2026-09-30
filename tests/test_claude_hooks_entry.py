"""brigade-hook direct entry point: flag parity, import weight, script text, upgrade detection."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import cli
from brigade.claude_hooks import entry, install_cmd, runtime
from brigade.claude_hooks.package import (
    COMMAND_PREFIX,
    HOOK_SCRIPT_NAME,
    MANAGED_EVENTS,
    MANAGED_MARKER_KEY,
    PACKAGE_ID,
    PACKAGE_REF,
    PACKAGE_VERSION,
    hook_script_text,
    is_managed_user_handler,
    managed_user_command,
)
from brigade.claude_hooks.paths import resolve_claude_home
from tests._home import home_env, set_home

REPO_ROOT = Path(__file__).resolve().parents[1]


def _argparse_failure(func, argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    with pytest.raises(SystemExit) as info:
        func(argv)
    return int(info.value.code), capsys.readouterr().err


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--event", "PreToolUse"],
        ["--package", PACKAGE_REF],
        ["--event", "Bogus", "--package", PACKAGE_REF],
        ["--event", "Stop", "--package", PACKAGE_REF, "--target"],
        ["--event", "Stop", "--package", PACKAGE_REF, "-t"],
    ],
)
def test_entry_argparse_errors_match_work_hook_run(flags: list[str], capsys: pytest.CaptureFixture[str]):
    cli_code, cli_err = _argparse_failure(cli.main, ["work", "hook-run", *flags], capsys)
    entry_code, entry_err = _argparse_failure(entry.main, flags, capsys)

    assert entry_code == cli_code == 2
    assert entry_err == cli_err


def test_entry_rejects_unknown_flag_with_exit_2(capsys: pytest.CaptureFixture[str]):
    code, err = _argparse_failure(entry.main, ["--event", "Stop", "--package", PACKAGE_REF, "--bad"], capsys)

    assert code == 2
    assert "unrecognized arguments: --bad" in err


def test_entry_accepts_every_managed_event(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "hook_run", lambda **kwargs: calls.append(kwargs) or 0)

    for event in MANAGED_EVENTS:
        assert entry.main(["--event", event, "--package", PACKAGE_REF]) == 0

    assert [call["event"] for call in calls] == list(MANAGED_EVENTS)


def test_entry_calls_hook_run_without_target_like_the_dispatcher(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "hook_run", lambda **kwargs: calls.append(kwargs) or 0)

    assert entry.main(["--event", "PreToolUse", "--package", PACKAGE_REF]) == 0

    assert calls == [{"event": "PreToolUse", "package": PACKAGE_REF}]


@pytest.mark.parametrize("flag", ["--target", "-t"])
def test_entry_passes_target_as_path(flag: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "hook_run", lambda **kwargs: calls.append(kwargs) or 0)

    assert entry.main(["--event", "Stop", "--package", PACKAGE_REF, flag, str(tmp_path)]) == 0

    assert calls == [{"event": "Stop", "package": PACKAGE_REF, "target": tmp_path}]


def _subprocess_env(home: Path) -> dict[str, str]:
    env = {"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": os.environ.get("PATH", ""), **home_env(home)}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def test_entry_import_does_not_load_brigade_cli(tmp_path: Path):
    code = (
        "import sys\n"
        "from brigade.claude_hooks.entry import main\n"
        "assert 'brigade.cli' not in sys.modules, 'entry import pulled brigade.cli'\n"
        "try:\n"
        "    main(['--event', 'Stop', '--package', 'brigade-claude-work-loop@0', "
        "'--target', '/nonexistent-brigade-hook'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "assert 'brigade.cli' not in sys.modules, 'entry run pulled brigade.cli'\n"
    )
    env = _subprocess_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", code], input="{}", capture_output=True, text=True, env=env, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr


def test_hook_runtime_import_skips_heavy_command_modules(tmp_path: Path):
    code = (
        "import sys\n"
        "import brigade.claude_hooks.entry, brigade.claude_hooks.runtime\n"
        "heavy = ('brigade.work_cmd', 'brigade.dogfood_cmd', 'brigade.aboyeur')\n"
        "loaded = [name for name in heavy if name in sys.modules]\n"
        "assert not loaded, f'hook import pulled {loaded}'\n"
    )
    env = _subprocess_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr


def test_brigade_hook_console_script_is_declared():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'brigade-hook = "brigade.claude_hooks.entry:main"' in text


def test_package_version_is_bumped_for_direct_entry():
    assert PACKAGE_VERSION == "1.3.1"
    assert PACKAGE_REF == f"{PACKAGE_ID}@1.3.1"


def test_unpinned_script_tries_brigade_hook_then_cli():
    text = hook_script_text()

    assert text.startswith("#!/usr/bin/env sh\n")
    assert "set -eu\n" in text
    assert "--target" not in text
    entry_probe = text.index('cli="$(command -v brigade')
    entry_exec = text.index(f'exec "${{cli%/*}}/brigade-hook" --event "$event" --package "{PACKAGE_REF}"')
    cli_exec = text.index(f'exec {COMMAND_PREFIX} --event "$event" --package "{PACKAGE_REF}"')
    assert entry_probe < entry_exec < cli_exec


def test_pinned_script_forwards_target():
    text = hook_script_text(pin=Path("/work/my repo"))
    target = shlex.quote(str(Path("/work/my repo")))

    assert f'exec "${{cli%/*}}/brigade-hook" --event "$event" --package "{PACKAGE_REF}" --target {target}' in text
    assert f'exec {COMMAND_PREFIX} --event "$event" --package "{PACKAGE_REF}" --target {target}' in text


def test_script_is_valid_posix_sh(tmp_path: Path):
    for pin in (None, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text(hook_script_text(pin=pin), encoding="utf-8")
        result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


def _run_script(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(script), *args], capture_output=True, text=True, env=env, check=False)


def _stub(path: Path, label: str) -> None:
    path.write_text(f'#!/bin/sh\necho "{label} $*"\n', encoding="utf-8")
    path.chmod(0o755)


def test_script_prefers_brigade_hook_then_cli(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = tmp_path / "hook.sh"
    script.write_text(hook_script_text(), encoding="utf-8")
    base_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "home")}

    _stub(bin_dir / "brigade", "cli")
    result = _run_script(script, base_env, "--event", "Stop")
    assert result.stdout.strip() == f"cli work hook-run --event Stop --package {PACKAGE_REF}"

    _stub(bin_dir / "brigade-hook", "entry")
    result = _run_script(script, base_env, "--event", "Stop")
    assert result.stdout.strip() == f"entry --event Stop --package {PACKAGE_REF}"

    assert _run_script(script, base_env).stdout == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH layout")
def test_script_ignores_brigade_hook_from_another_installation(tmp_path: Path):
    cli_dir = tmp_path / "cli-bin"
    other_dir = tmp_path / "other-bin"
    cli_dir.mkdir()
    other_dir.mkdir()
    script = tmp_path / "hook.sh"
    script.write_text(hook_script_text(), encoding="utf-8")
    _stub(cli_dir / "brigade", "cli")
    _stub(other_dir / "brigade-hook", "other-entry")
    env = {"PATH": f"{other_dir}:{cli_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    result = _run_script(script, env, "--event", "Stop")

    assert result.stdout.strip() == f"cli work hook-run --event Stop --package {PACKAGE_REF}"


def test_script_text_does_not_depend_on_install_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    set_home(monkeypatch, tmp_path / "a")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-a"))
    first = hook_script_text()
    set_home(monkeypatch, tmp_path / "b")
    monkeypatch.delenv("XDG_DATA_HOME")

    assert hook_script_text() == first
    assert str(tmp_path) not in first


def test_pinned_script_runs_entry_with_target(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = tmp_path / "hook.sh"
    script.write_text(hook_script_text(pin=tmp_path), encoding="utf-8")
    _stub(bin_dir / "brigade", "cli")
    _stub(bin_dir / "brigade-hook", "entry")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}

    result = _run_script(script, env, "--event", "Stop")

    assert result.stdout.strip() == f"entry --event Stop --package {PACKAGE_REF} --target {tmp_path}"


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fake-home"
    claude = home / ".claude-config"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    set_home(monkeypatch, str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    assert resolve_claude_home() == claude.resolve()
    return claude


def _downgrade_user_install(claude_home: Path, old_ref: str) -> None:
    settings = claude_home / "settings.json"
    payload = json.loads(settings.read_text())
    for groups in payload["hooks"].values():
        for group in groups:
            for handler in group.get("hooks", []):
                if handler.get(MANAGED_MARKER_KEY) == PACKAGE_REF:
                    handler[MANAGED_MARKER_KEY] = old_ref
    settings.write_text(json.dumps(payload, indent=2) + "\n")
    sidecar_path = claude_home / "brigade" / "claude-hooks.json"
    sidecar = json.loads(sidecar_path.read_text())
    sidecar["package_version"] = old_ref.split("@", 1)[1]
    sidecar_path.write_text(json.dumps(sidecar))
    (claude_home / "hooks" / HOOK_SCRIPT_NAME).write_text(
        f'#!/usr/bin/env sh\nset -eu\nexec brigade work hook-run --event "$1" --package "{old_ref}"\n',
        encoding="utf-8",
    )


def test_user_scope_detects_1_3_0_install_as_outdated_and_upgrades_in_place(claude_home: Path):
    assert install_cmd.hooks_install(target=Path("."), scope="user") == 0
    _downgrade_user_install(claude_home, f"{PACKAGE_ID}@1.3.0")

    status = install_cmd.status_payload(Path("."), scope="user")
    assert status["installed"] is True
    assert status["current"] is False
    assert status["script_current"] is False
    assert sorted(status["managed_events"]) == sorted(MANAGED_EVENTS)
    assert status["current_events"] == []

    assert install_cmd.hooks_install(target=Path("."), scope="user") == 0

    settings = json.loads((claude_home / "settings.json").read_text())
    markers = [
        handler[MANAGED_MARKER_KEY]
        for groups in settings["hooks"].values()
        for group in groups
        for handler in group["hooks"]
        if MANAGED_MARKER_KEY in handler
    ]
    assert markers and set(markers) == {PACKAGE_REF}
    total = sum(len(group["hooks"]) for groups in settings["hooks"].values() for group in groups)
    assert total == len(markers), "old-version handlers must be replaced, never duplicated as foreign"
    assert install_cmd.status_payload(Path("."), scope="user")["current"] is True
    assert "brigade-hook" in (claude_home / "hooks" / HOOK_SCRIPT_NAME).read_text()


def test_user_handler_from_older_package_version_is_still_managed(claude_home: Path):
    script = claude_home / "hooks" / HOOK_SCRIPT_NAME
    handler = {
        "type": "command",
        "command": managed_user_command("Stop", script),
        MANAGED_MARKER_KEY: f"{PACKAGE_ID}@1.3.0",
    }

    assert is_managed_user_handler(handler, script, "Stop") is True
    assert is_managed_user_handler({**handler, MANAGED_MARKER_KEY: "someone-else@1.3.0"}, script) is False
    assert is_managed_user_handler({**handler, MANAGED_MARKER_KEY: f"{PACKAGE_ID}@"}, script) is False
