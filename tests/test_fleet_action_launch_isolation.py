"""Fleet owner boundaries execute installed Python rather than ambient packages."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import fleet_action_adapters as adapters, fleet_action_runner as runner
from brigade.fleet_action_auth import ActionError
from tests.test_fleet_action_runner import broker_fixture

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Fleet action jobs require POSIX")


@pytest.fixture(params=["cwd-module", "cwd-package", "pythonpath"])
def ambient_package(tmp_path, monkeypatch, request):
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    marker = tmp_path / "shadow-executed"
    root = workspace
    monkeypatch.delenv("PYTHONPATH", raising=False)
    if request.param == "pythonpath":
        root = tmp_path / "environment"
        root.mkdir()
        monkeypatch.setenv("PYTHONPATH", str(root))
    shadow = root / ("brigade.py" if request.param == "cwd-module" else "brigade/__init__.py")
    shadow.parent.mkdir(exist_ok=True)
    shadow.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(0)\n")
    monkeypatch.chdir(workspace)
    return workspace, marker


def test_tick_scheduled_command_runs_installed_cli(ambient_package, tmp_path, monkeypatch):
    workspace, marker = ambient_package
    conn, manifest, _, action = broker_fixture(tmp_path, monkeypatch)
    launches = []
    run = subprocess.run

    def submit(argv, **kwargs):
        if argv[-1] == "show-environment":
            return SimpleNamespace(returncode=0)
        # Replay the emitted command with help instead of creating a systemd job
        # or consuming the action's irreversible execution fence.
        assert "--user" in argv and "--property=Type=exec" in argv
        child = argv[argv.index("--") + 1 :]
        result = run([*child, "--help"], cwd=workspace, capture_output=True, text=True, timeout=20)
        launches.append((argv, kwargs, result))
        return SimpleNamespace(returncode=result.returncode)

    child_subprocess = SimpleNamespace(**vars(subprocess))
    child_subprocess.run = submit
    monkeypatch.setattr(runner, "subprocess", child_subprocess)
    try:
        result = runner.tick(manifest, budget_seconds=15, max_actions=1)
        assert not marker.exists(), "scheduled action executed ambient Python"
        assert result["scheduled"] == 1
        ((argv, kwargs, child),) = launches
        assert child.returncode == 0, child.stderr
        assert "usage: brigade fleet actions execute" in child.stdout
        assert argv[argv.index("--manifest") + 1] == str(manifest.resolve())
        assert action["action_id"] in argv
        assert "--working-directory=" + str(Path(runner.__file__).resolve().parent) in argv
        assert kwargs["cwd"] == Path(runner.__file__).resolve().parent
        assert kwargs["timeout"] > 0 and kwargs["shell"] is False
    finally:
        conn.close()


@pytest.mark.parametrize("family", ["dot", "grokbot"])
def test_inbox_dispatch_runs_installed_helper_before_transport(ambient_package, tmp_path, monkeypatch, family):
    _, marker = ambient_package
    # A missing body is rejected by the real helper before credentials or network
    # access. Ambient code instead exits zero without producing its closed error.
    prompt = tmp_path / "missing-prompt"
    action = {"action_id": "aa_fixture", "request": {"target": {"family": family, "principal": "agent:fixture"}}}
    popen = subprocess.Popen
    launches = []

    def launch(argv, **kwargs):
        process = popen(argv, **kwargs)
        launches.append(kwargs)
        return process

    monkeypatch.setattr(adapters.subprocess, "Popen", launch)
    payloads = []

    def decode(raw):
        payload = json.loads(raw)
        payloads.append(payload)
        return payload

    monkeypatch.setattr(adapters, "json", SimpleNamespace(loads=decode))
    with pytest.raises(ActionError, match="^transport-acceptance-unknown$"):
        adapters.dispatch(action, {}, tmp_path / "unused-brigade", prompt)
    assert not marker.exists(), "inbox dispatch executed ambient Python"
    assert payloads == [{"error": "transport-acceptance-unknown"}]
    assert len(launches) == 1
    assert launches[0]["cwd"] == Path(adapters.__file__).resolve().parent
