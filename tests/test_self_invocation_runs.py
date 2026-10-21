"""Execute run, dispatch, and dashboard children beside hostile Python paths."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import brigade
from brigade import cli
from brigade.center_cmd.dashboard import data
from brigade.cli import run as run_cli
from brigade.work_cmd import dispatch_apply as apply
from tests.test_run_detach import _write_roster
from tests.test_work_dispatch import _tasks


@pytest.fixture(params=["cwd-module", "cwd-package", "pythonpath"])
def hostile_workspace(tmp_path, monkeypatch, request):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "shadow-executed"
    shadow_root = workspace
    monkeypatch.delenv("PYTHONPATH", raising=False)
    if request.param == "pythonpath":
        shadow_root = tmp_path / "environment"
        shadow_root.mkdir()
        monkeypatch.setenv("PYTHONPATH", str(shadow_root))
    if request.param == "cwd-module":
        shadow = shadow_root / "brigade.py"
    else:
        shadow = shadow_root / "brigade" / "__init__.py"
        shadow.parent.mkdir()
    shadow.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise SystemExit(0)\n")
    monkeypatch.chdir(workspace)
    return workspace, marker


def test_detached_run_executes_trusted_cli(hostile_workspace, monkeypatch, capsys):
    workspace, marker = hostile_workspace
    _write_roster(workspace)
    launches = []
    popen = subprocess.Popen

    def launch(argv, **kwargs):
        # Help exercises the real child parser without dispatching a model seat.
        # Keep this help child inside the Windows CI Job Object. Production
        # detached runs still require breakaway and refuse a restricted job.
        if os.name == "nt":
            kwargs["creationflags"] &= ~subprocess.CREATE_BREAKAWAY_FROM_JOB
        launches.append((argv, kwargs))
        return popen([*argv, "--help"], **kwargs)

    monkeypatch.setattr(run_cli, "Popen", launch)
    result = cli.main(["run", "task", "--cwd", str(workspace), "--detach", "--worker", "coder"])
    captured = capsys.readouterr()
    assert not marker.exists(), "detached run executed workspace-controlled Python"
    assert result == 2, captured
    run_dir = next((workspace / ".brigade/runs").iterdir())
    assert "usage: brigade run" in (run_dir / "detached.log").read_text()
    ((argv, kwargs),) = launches
    assert argv[argv.index("--cwd") + 1] == str(workspace)
    assert kwargs["cwd"] == Path(brigade.__file__).resolve().parent


def test_dispatch_bootstrap_and_exec_use_trusted_cli(hostile_workspace, tmp_path, monkeypatch):
    workspace, marker = hostile_workspace
    _write_roster(workspace)
    _tasks(workspace, [["example.py"]])
    run_dir = tmp_path / "run"
    intent_path = tmp_path / "dispatch/intents/example.json"
    intent_path.parent.mkdir(parents=True)
    row = {
        "intent_id": "example",
        "task_id": "task-0",
        "claim_id": "dispatch-example",
        "seat": "coder",
        "worktree": str(workspace),
        "run_id": "run",
        "run_dir": str(run_dir),
        "state": "workspace_ready",
    }
    session = {"roster_path": str(workspace / ".brigade/roster.toml")}
    child_argv = apply._child_argv
    launches = []
    popen = subprocess.Popen

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        return popen(argv, **kwargs)

    monkeypatch.setattr(apply, "_child_argv", lambda row, session, prompt: child_argv(row, session, "--help"))
    # Preserve the real Popen class for lazily imported Windows asyncio code.
    child_subprocess = SimpleNamespace(**vars(subprocess))
    child_subprocess.Popen = launch
    monkeypatch.setattr(apply, "subprocess", child_subprocess)
    engine = apply.Apply(workspace)
    engine.advance(intent_path, row, session)
    child = engine.children["example"]
    try:
        assert child.wait(timeout=20) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    assert not marker.exists(), "dispatch bootstrap or exec executed workspace-controlled Python"
    receipt = json.loads((run_dir / "dispatch-launch.json").read_text())
    assert receipt["pid"] == child.pid
    assert "usage: brigade run" in (run_dir / "dispatch-child.log").read_text()
    ((argv, kwargs),) = launches
    assert argv[argv.index("--cwd") + 1] == str(workspace)
    assert kwargs["cwd"] == Path(brigade.__file__).resolve().parent


def test_dispatch_exec_argv_ignores_ambient_python_paths(hostile_workspace, tmp_path):
    workspace, marker = hostile_workspace
    row = {"seat": "coder", "worktree": str(workspace), "run_dir": str(tmp_path / "run")}
    session = {"roster_path": str(workspace / "roster.toml")}
    argv = apply._child_argv(row, session, "--help")
    result = subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=20)
    assert not marker.exists(), "dispatch exec argv executed workspace-controlled Python"
    assert result.returncode == 0, result.stderr
    assert "usage: brigade run" in result.stdout


@pytest.mark.parametrize("scope", ["target", "cwd"])
def test_dashboard_reads_explicit_workspace_without_shadow_import(hostile_workspace, scope):
    workspace, marker = hostile_workspace
    if scope == "target":
        _tasks(workspace, [["example.py"]])
        result = data.run_json(Path("."), ["work", "tasks"])
    else:
        run_dir = workspace / ".brigade/runs/example"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({"status": "ok", "cwd": str(workspace)}))
        result = data.run_json_cwd(Path("."), ["runs", "list"])
    assert not marker.exists(), "dashboard executed workspace-controlled Python"
    assert "error" not in result, result
    if scope == "target":
        assert result["tasks"][0]["id"] == "task-0"
    else:
        assert result["runs"][0]["run_id"] == "example"
