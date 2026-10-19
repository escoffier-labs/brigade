"""Claim loss must stop silent local workers without interpreter interrupts."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from brigade import aboyeur, agents, cli, fleet_client, fleet_session_bootstrap, fleet_t3_transport as t3, proc
from brigade.claim_loss_cancellation import CLAIM_LOSS_TERMINATE_GRACE_SECONDS
from tests.test_fleet_t3_transport import FakePolicyClient, REVISION, REPO, WINDOWS_NODE, _document
from tests._posix import requires_process_groups
from tests.test_fleet_claims import NODE_A, NODE_B, _plant_home_identity


def _mock_claim_loss(monkeypatch, pid_file, lost, loss_time, refusal):
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
        if refusal == "quota":
            return 409, {
                "renewed": False,
                "reason": "claim-cardinality-exceeded",
                "error": "active claim quota exceeded",
                "owner": {},
            }
        return 409, {"granted": False, "error": "held", "owner": {"owner_node": NODE_B, "expires_at": "soon"}}

    renew = fleet_client.renew_claim

    def observed_renewal(*args, **kwargs):
        outcome = renew(*args, **kwargs)
        # Only the heartbeat's consumed decision may start the cancellation clock.
        # Abandoned HTTP threads can finish later without revoking ownership.
        if outcome.reason == refusal:
            loss_time.append(time.monotonic())
            lost.set()
        return outcome

    monkeypatch.setattr(fleet_client, "renew_claim", observed_renewal)
    monkeypatch.setattr(fleet_client, "_post_claim_blocking", post)


@requires_process_groups
@pytest.mark.parametrize("transport", ["local", "t3"])
@pytest.mark.parametrize("refusal", ["held", "quota"])
def test_claim_loss_stops_silent_worker_without_main_interrupt(tmp_path: Path, monkeypatch, transport, refusal):
    _exercise_blocked_child(tmp_path, monkeypatch, transport, refusal=refusal)


@pytest.mark.skipif(os.name != "nt", reason="native Windows process-tree cancellation")
def test_native_windows_claim_loss_stops_silent_worker(tmp_path, monkeypatch):
    _exercise_blocked_child(tmp_path, monkeypatch, "local")


def _exercise_blocked_child(tmp_path: Path, monkeypatch, transport, *, refusal="held"):
    """The fallback kills only after the bound, so a queued interrupt cannot pass."""
    ws = tmp_path / "ws"
    (ws / ".brigade").mkdir(parents=True)
    home = tmp_path / "home"
    _plant_home_identity(home, NODE_A)
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://hub.example.test")
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "test-token")
    # Keep event delivery and spooling outside this cancellation check.
    monkeypatch.setattr(fleet_client, "report_event", lambda event, **kwargs: False)
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
        "import json, os, signal, sys, time\n"
        "from pathlib import Path\n"
        "if len(sys.argv) > 1 and sys.argv[1] != 'wait':\n"
        "    print(json.dumps({'schema': 't3-fleet.result.v1', 'hosts': [{'state': 'eligible'}], "
        "'request_id': 'worker/req-test', 'result': {'state': 'running'}})); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    child.chmod(0o700)
    lost = threading.Event()
    finished = threading.Event()
    loss_time = []

    def kill_worker():
        if pid_file.exists():
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", pid_file.read_text(), "/T", "/F"],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                else:
                    os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def failsafe():
        if lost.wait(15) and not finished.wait(CLAIM_LOSS_TERMINATE_GRACE_SECONDS + 3):
            kill_worker()

    def run_agent(*args, process_registry=None, **kwargs):
        result = proc.run([sys.executable, str(child)], timeout=15, process_registry=process_registry)
        return agents.AgentResult(text=result.stdout, ok=result.code == 0, detail=result.stderr)

    build = t3.build_source_transport
    policy = FakePolicyClient()

    def source_transport(**kwargs):
        if transport == "local":
            return None
        kwargs.update(
            snapshot={
                "state": "authoritative",
                "fleet_policy": {
                    "active": True,
                    "version": 1,
                    "digest": "sha256:" + "a" * 64,
                },
            },
            policy_client=policy,
            document=_document(),
            node_id=WINDOWS_NODE,
            client=t3.T3FleetClient(executable=str(child), cwd=ws),
        )
        return build(**kwargs)

    _mock_claim_loss(monkeypatch, pid_file, lost, loss_time, refusal)
    monkeypatch.setattr(agents, "run_agent", run_agent)
    monkeypatch.setattr(t3, "build_source_transport", source_transport)
    monkeypatch.setattr(t3, "immutable_source_revision", lambda cwd, **kwargs: REVISION)
    monkeypatch.setattr(fleet_session_bootstrap, "_repo_identity", lambda *args: REPO)
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
    assert ended_at - loss_time[0] < CLAIM_LOSS_TERMINATE_GRACE_SECONDS + 2, (
        "claim loss left the silent worker alive until the failsafe killed it"
    )
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
    receipt = json.loads(next((ws / ".brigade" / "runs").glob("*/run.json")).read_text())
    assert receipt["failure"]["kind"] == "fleet-claim-lost"
