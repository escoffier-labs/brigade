"""Fleet owner boundaries execute installed Python rather than ambient packages."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import fleet_action_adapters as adapters, fleet_action_runner as runner
from brigade.fleet_action_auth import ActionError
from tests._home import set_home
from tests.test_fleet_action_runner import broker_fixture

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Fleet action jobs require POSIX")


def test_schedule_resolves_relative_tool_before_entering_trusted_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executable = tmp_path / "tools" / "systemd-run"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    manifest = {"executables": {"systemd-run": "tools/systemd-run"}}

    assert runner._schedule(tmp_path / "manifest.json", manifest, "aa_fixture", 1, timeout=5)


@pytest.mark.parametrize(
    ("configured", "relative_path"), [("systemd-run", False), ("systemd-run", True), ("./systemd-run", False)]
)
def test_schedule_preserves_executable_lookup_before_entering_trusted_cwd(
    tmp_path, monkeypatch, configured, relative_path
):
    monkeypatch.chdir(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    local = tmp_path / "systemd-run"
    on_path = tools / "systemd-run"
    for executable, code in (
        (local, 0 if configured.startswith("./") else 7),
        (on_path, 7 if configured.startswith("./") else 0),
    ):
        executable.write_text(f"#!/bin/sh\nexit {code}\n")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", "tools" if relative_path else str(tools))
    manifest = {"executables": {"systemd-run": configured}}

    assert runner._schedule(tmp_path / "manifest.json", manifest, "aa_fixture", 1, timeout=5)


def test_schedule_missing_path_name_does_not_fall_back_to_caller_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executable = tmp_path / "systemd-run"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "absent"))
    manifest = {"executables": {"systemd-run": "systemd-run"}}

    with pytest.raises(FileNotFoundError) as exc_info:
        runner._schedule(tmp_path / "manifest.json", manifest, "aa_fixture", 1, timeout=5)

    assert exc_info.value.filename == "systemd-run"


def test_inbox_dispatch_preserves_relative_credential_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home" / ".brigade"))
    monkeypatch.setenv("BRIGADE_FLEET_MESSAGE_TOKEN_FILE", "message-token")
    for key in ("BRIGADE_FLEET_TOKEN", "BRIGADE_FLEET_NODE_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    token = tmp_path / "message-token"
    token.write_text("x" * 32)
    token.chmod(0o600)
    prompt = tmp_path / "prompt"
    prompt.write_text("Inspect the fixture failure.")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            payload = json.dumps({"message": {"id": 42, "client_id": "aa_fixture"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", f"http://127.0.0.1:{server.server_port}")
        action = {"action_id": "aa_fixture", "request": {"target": {"family": "dot", "principal": "agent:fixture"}}}
        try:
            result = adapters.dispatch(action, {}, tmp_path / "unused-brigade", prompt)
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert result["status"] == "accepted"
    assert len(received) == 1
    assert received[0]["body"] == prompt.read_text()


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
        # Observe cwd at CLI dispatch, after the unchanged bootstrap runs.
        child[3] = (
            "import runpy,os;_run=runpy.run_module;"
            "runpy.run_module=lambda *a,**k:(print('bootstrap-cwd:'+os.getcwd(),flush=True),_run(*a,**k))[1];"
            + child[3]
        )
        startup_cwd = next(value.split("=", 1)[1] for value in argv if value.startswith("--working-directory="))
        result = run([*child, "--help"], cwd=startup_cwd, capture_output=True, text=True, timeout=20)
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
        assert child.stdout.splitlines()[0] == "bootstrap-cwd:" + str(workspace)
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
