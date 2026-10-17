"""Claim loss must stop silent local workers without interpreter interrupts."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from brigade import aboyeur, agents, cli, fleet_client, fleet_session_bootstrap, fleet_t3_transport as t3, proc
from brigade.aboyeur import orchestrator
from tests._posix import requires_process_groups
from tests.test_fleet_claims import NODE_A, NODE_B, _plant_home_identity


@requires_process_groups
@pytest.mark.parametrize("transport", ["local", "t3"])
def test_claim_loss_stops_silent_worker_without_main_interrupt(tmp_path: Path, monkeypatch, transport):
    """The fallback kills only after the bound, so a queued interrupt cannot pass."""
    ws = tmp_path / "ws"
    (ws / ".brigade").mkdir(parents=True)
    home = tmp_path / "home"
    _plant_home_identity(home, NODE_A)
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://hub.example.test")
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "test-token")
    monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0.01)
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: {"state": "unconfigured", "models": []})
    monkeypatch.setattr(fleet_session_bootstrap, "ensure_prompt", lambda prompt, **kwargs: prompt)
    monkeypatch.setattr(fleet_client._thread, "interrupt_main", lambda: None)
    roster = ws / "roster.toml"
    roster.write_text(
        'orchestrator = "chef"\n[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.coder]\ncli = "codex"\nrole = "implement"\n'
    )
    pid_file = ws / "worker.pid"
    child = ws / "silent-worker"
    child.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    child.chmod(0o700)
    lost = threading.Event()
    finished = threading.Event()
    loss_time = []

    def post(_hub, _token, body, *, timeout):
        if body["action"] == "acquire":
            return 200, {"granted": True, "claim": {"target": body["target"], "owner_node": NODE_A}}
        if body["action"] == "release":
            return 200, {"released": True}
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not pid_file.exists():
            return 200, {"granted": True}
        loss_time.append(time.monotonic())
        lost.set()
        return 409, {"granted": False, "error": "held", "owner": {"owner_node": NODE_B, "expires_at": "soon"}}

    def kill_worker():
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def failsafe():
        if lost.wait(15) and not finished.wait(4):
            kill_worker()

    def run_agent(*args, process_registry=None, **kwargs):
        result = proc.run([str(child)], timeout=15, process_registry=process_registry)
        return agents.AgentResult(text=result.stdout, ok=result.code == 0, detail=result.stderr)

    def remote_transport(**kwargs):
        registry_kwargs = {key: value for key, value in kwargs.items() if key == "process_registry"}
        client = t3.T3FleetClient(executable=str(child), cwd=ws, **registry_kwargs)

        def dispatch(*args):
            client.wait(request_id="worker/req-test", timeout=15)
            raise AssertionError("a silent waiter cannot complete successfully")

        return dispatch

    monkeypatch.setattr(fleet_client, "_post_claim_blocking", post)
    monkeypatch.setattr(agents, "run_agent", run_agent)
    monkeypatch.setattr(
        orchestrator, "_worker_remote_transport", remote_transport if transport == "t3" else lambda **kw: None
    )
    watchdog = threading.Thread(target=failsafe, daemon=True)
    watchdog.start()
    try:
        rc = cli.main(
            [
                "run",
                "do something",
                "--roster",
                str(roster),
                "--cwd",
                str(ws),
                "--worker",
                "coder",
                "--codex-transport",
                "exec",
                "--no-code-graph",
                "--no-evidence",
                "--no-route",
            ]
        )
        ended_at = time.monotonic()
    finally:
        finished.set()
        kill_worker()
        watchdog.join(timeout=1)
    assert lost.is_set(), "the worker must start before ownership is revoked"
    assert ended_at - loss_time[0] < 3, "claim loss left the silent worker alive until the failsafe killed it"
    assert rc == 2
    receipt = json.loads(next((ws / ".brigade" / "runs").glob("*/run.json")).read_text())
    assert receipt["status"] == "failed"
    assert receipt["failure"]["kind"] == "fleet-claim-lost"


def test_claim_loss_before_dispatch_does_not_start_worker(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / ".brigade").mkdir(parents=True)
    roster = ws / "roster.toml"
    roster.write_text('orchestrator = "chef"\n[agents.chef]\ncli = "codex"\nrole = "plan"\n')
    dispatched = []

    @contextmanager
    def claim(*args, **kwargs):
        kwargs["on_claim_lost"]("held")
        yield fleet_client.ClaimDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "repo_claim", claim)
    monkeypatch.setattr(aboyeur, "run", lambda *args, **kwargs: dispatched.append(True) or 0)
    assert cli.main(["run", "do something", "--roster", str(roster), "--cwd", str(ws)]) == 2
    assert dispatched == []
