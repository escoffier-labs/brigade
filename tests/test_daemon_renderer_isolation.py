"""Generated service commands keep imports pinned to the rendering installation."""

from __future__ import annotations

import importlib
import json
import os
import plistlib
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from brigade import care_cmd, grokbot_ops, grokbot_pack_relay

RENDERERS = (
    "dot",
    "dot-watch",
    "t3",
    "t3-watch",
    "t3-intake",
    "ops",
    "pack-relay",
    "cerebro",
    "operations-relay",
    "wazuh",
    "obsidian",
    "backup",
    "n8n",
    "fleet",
)
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="systemd service renderers require POSIX")


def _render(name, target, python, monkeypatch):
    instance = {
        "bind": "127.0.0.1:8766",
        "allowed_hosts": ["mcp.example.test"],
        "allowed_origins": [],
        "bearer": {"kind": "env", "name": "TEST_SERVICE_BEARER"},
        "runtime_path": str(target / "runtime.json"),
        "ledger_path": str(target / "ledger/state.json"),
        "action_state_path": str(target / "actions"),
        "approval_dir": str(target / "approvals"),
        "owner_workspace": str(target / "owner"),
        "workdir": str(target / "workdir"),
        "upstream_url": "http://127.0.0.1:27123",
        "upstream_key": {"kind": "env", "name": "TEST_UPSTREAM_KEY"},
        "staging_dir": str(target / "staging"),
    }
    if name == "ops":
        return grokbot_ops.render_unit(
            {
                "schema": grokbot_ops.CONFIG_SCHEMA,
                "instance": "operator",
                **{key: instance[key] for key in ("bind", "allowed_hosts", "allowed_origins", "bearer")},
            },
            python=python,
            exec_root=target,
        )
    if name == "pack-relay":
        monkeypatch.setattr(grokbot_pack_relay, "load_relay_config", lambda _: instance)
        monkeypatch.setattr(grokbot_pack_relay, "_optional_instance", lambda *_: None)
        out = target / "units"
        out.mkdir()
        paths = grokbot_pack_relay.write_relay_units(target, out, python=python)
        return next(path.read_text() for path in paths if path.suffix == ".service")
    base = name.split("-", 1)[0]
    module = importlib.import_module(
        "brigade.dot.lifecycle"
        if base == "dot"
        else f"brigade.grokbot_{name.replace('-', '_') if name == 'operations-relay' else base}"
        + ("" if base in {"cerebro", "operations"} else ".lifecycle")
    )
    if base == "dot":
        monkeypatch.setattr(module, "require_local_platform", lambda: None)
        monkeypatch.setattr(module.grokbot_packs, "_load_instance_config", lambda *_: instance)
        # The Dot family passes its connector profile (#1943), so the stubs take keywords.
        monkeypatch.setattr(module, "_load_config", lambda _, **__: None)
    else:
        monkeypatch.setattr(module, "_load_validated_instance", lambda _: instance)
    if base in {"dot", "t3"}:
        monkeypatch.setattr(module, "load_runtime", lambda _, **__: SimpleNamespace())
    if base == "t3":
        monkeypatch.setattr(module, "_hardening", lambda _: "NoNewPrivileges=yes\n")
    method = "render_unit"
    if name.endswith("-watch"):
        method = "render_watch_units"
    elif name.endswith("-intake"):
        method = "render_intake_units"
    result = getattr(module, method)(target, python=python)
    return result[0] if isinstance(result, tuple) else result


def _exec_argv(unit):
    lines = [line.removeprefix("ExecStart=") for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert len(lines) == 1
    return shlex.split(lines[0].replace("%%", "%").replace("$$", "$"))


@pytest.mark.parametrize("renderer", RENDERERS)
@pytest.mark.parametrize("relative", [False, True])
def test_service_argv_preserves_override_target_and_safe_workdir(renderer, relative, tmp_path, monkeypatch):
    target = tmp_path / "target with 'quotes'"
    target.mkdir()
    python = "/opt/example python/bin/python"
    monkeypatch.chdir(tmp_path)
    unit = _render(renderer, Path(target.name) if relative else target, python, monkeypatch)
    argv = _exec_argv(unit)
    assert argv[:3] == [python, "-I", "-c"]
    assert "\n" not in argv[3]
    assert Path(argv[4]).is_absolute() and Path(argv[4]).name == "__init__.py"
    assert argv[5] == (
        "brigade.dot"
        if renderer == "dot-watch"
        else "brigade.grokbot_t3"
        if renderer in {"t3-watch", "t3-intake"}
        else "brigade"
    )
    assert argv[argv.index("--target") + 1] == str(target)
    cwd_lines = [
        line.removeprefix("WorkingDirectory=") for line in unit.splitlines() if line.startswith("WorkingDirectory=")
    ]
    assert len(cwd_lines) == 1
    cwd_args = shlex.split(cwd_lines[0])
    assert len(cwd_args) == 1
    cwd = Path(cwd_args[0])
    assert cwd.is_absolute() and cwd.is_dir() and cwd != target


@pytest.mark.parametrize("renderer", RENDERERS)
@pytest.mark.parametrize("shadow", ["brigade.py", "brigade/__init__.py"])
@pytest.mark.parametrize("origin", ["cwd", "pythonpath"])
def test_generated_service_command_rejects_cwd_and_pythonpath_shadows(renderer, shadow, origin, tmp_path, monkeypatch):
    target = tmp_path / "target with 'quotes'"
    target.mkdir()
    unit = _render(renderer, target, sys.executable, monkeypatch)
    pythonpath = tmp_path / "pythonpath"
    pythonpath.mkdir()
    planted = (target if origin == "cwd" else pythonpath) / shadow
    planted.parent.mkdir(exist_ok=True)
    marker = tmp_path / "shadow-executed"
    planted.write_text(f"from pathlib import Path; Path({str(marker)!r}).write_text('executed'); raise SystemExit(0)\n")
    env = {**os.environ, "PYTHONPATH": str(pythonpath), "BRIGADE_NO_UPDATE_CHECK": "1"}
    result = subprocess.run(
        [*_exec_argv(unit), "--help"], cwd=target, env=env, capture_output=True, text=True, timeout=30
    )
    assert not marker.exists(), result.stderr
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower(), result.stdout


@pytest.mark.parametrize("backend", ["systemd", "launchd", "crontab"])
def test_care_launchers_pin_imports_and_keep_explicit_workspace(backend, tmp_path):
    workspace = tmp_path / "workspace with 'quotes'"
    workspace.mkdir()
    entry = care_cmd.CARE_ENTRIES[0]
    if backend == "systemd":
        units = care_cmd._systemd_unit_bodies(workspace=workspace, entries=(entry,))
        service = next(value for name, value in units.items() if name.endswith(".service"))
        argv = _exec_argv(service)
        cwd = shlex.split(
            next(line.split("=", 1)[1] for line in service.splitlines() if line.startswith("WorkingDirectory="))
        )[0]
        assert care_cmd._workspace_from_care_body(service) == workspace
    elif backend == "launchd":
        plists = care_cmd._launchd_plists(workspace=workspace, entries=(entry,))
        payload = plistlib.loads(next(iter(plists.values())))
        argv = payload["ProgramArguments"]
        cwd = payload["WorkingDirectory"]
    else:
        command = care_cmd._cron_command(entry, workspace=workspace)
        tokens = shlex.split(command)
        assert tokens[0] == "cd" and tokens[2] == "&&"
        cwd, argv = tokens[1], tokens[3:]
        assert care_cmd._workspace_from_care_body(command) == workspace
    assert argv[:3] == [sys.executable, "-I", "-c"]
    assert argv[argv.index("--target") + 1] == str(workspace)
    assert argv[argv.index("--approved") + 1] == str(workspace / entry.runbook_rel)
    assert Path(cwd).is_absolute() and Path(cwd).is_dir() and Path(cwd) != workspace
    marker = tmp_path / "care-shadow-executed"
    (workspace / "brigade.py").write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()\n")
    runbook = workspace / entry.runbook_rel
    runbook.parent.mkdir(parents=True)
    runbook.write_text(
        json.dumps(
            {
                "id": "scheduler-target",
                "allowed_commands": ["printf"],
                "steps": [{"id": "target", "run": "printf scheduler-target"}],
            }
        )
    )
    env = {**os.environ, "PYTHONPATH": str(workspace), "BRIGADE_NO_UPDATE_CHECK": "1"}
    if backend == "crontab":
        result = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True, timeout=30)
    else:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    assert not marker.exists(), result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    receipts = list((workspace / ".brigade/runbooks/runs").glob("*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "completed"
    assert receipt["runbook_id"] == "scheduler-target"
    assert receipt["target"] == str(workspace)
    assert receipt["runbook_path"] == str(runbook)


@pytest.mark.parametrize("backend", ["systemd", "crontab"])
def test_care_scheduler_escapes_expansions_and_recovers_workspace(backend, tmp_path):
    workspace = tmp_path / "workspace%pct$literal"
    workspace.mkdir()
    entry = care_cmd.CARE_ENTRIES[0]
    if backend == "systemd":
        units = care_cmd._systemd_unit_bodies(workspace=workspace, entries=(entry,))
        body = next(text for name, text in units.items() if name.endswith(".service"))
        command = next(line for line in body.splitlines() if line.startswith("ExecStart="))
        assert "$$literal" in command
        argv = _exec_argv(body)
        assert argv[argv.index("--target") + 1] == str(workspace)
    else:
        body = care_cmd._cron_command(entry, workspace=workspace)
    assert care_cmd._workspace_from_care_body(body) == workspace
