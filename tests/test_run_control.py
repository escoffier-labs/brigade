"""Tests for live run steering and interruption."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from brigade import aboyeur, agents, cli, codex_appserver, run_control
from brigade.roster import Agent, Roster

FAKE = [sys.executable, str(Path(__file__).parent / "fake_appserver.py")]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _wait_for(predicate, *, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


class _FakeTurn:
    thread_id = "thread-1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def steer(self, text: str, turn_id: str) -> None:
        self.calls.append(("steer", turn_id, text))

    def interrupt(self, turn_id: str) -> None:
        self.calls.append(("interrupt", turn_id, ""))


def test_control_socket_round_trip_with_fake_registry(tmp_path):
    registry = run_control.LiveTurnRegistry()
    turn = _FakeTurn()
    registry.register("coder", turn, "turn-1")
    server = run_control.ControlServer(tmp_path / "control.sock", registry)
    server.start()
    try:
        steer = run_control.send_request(
            tmp_path / "control.sock",
            {"op": "steer", "worker": "coder", "text": "please focus on tests"},
        )
        interrupt = run_control.send_request(
            tmp_path / "control.sock",
            {"op": "interrupt", "worker": "coder"},
        )
    finally:
        server.close()

    assert steer == {"ok": True, "worker": "coder", "thread_id": "thread-1", "turn_id": "turn-1"}
    assert interrupt == {"ok": True, "interrupted": 1, "workers": ["coder"]}
    assert turn.calls == [
        ("steer", "turn-1", "please focus on tests"),
        ("interrupt", "turn-1", ""),
    ]
    assert not (tmp_path / "control.sock").exists()


def test_run_turn_reports_turn_start_callback():
    turns: list[str] = []
    with codex_appserver.AppServer(argv=FAKE) as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("say hi", timeout=10.0, on_turn_start=turns.append)

    assert result.ok
    assert turns == ["turn-t-1"]


def test_runs_steer_refuses_non_app_server_run(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_json(run_dir / "run.json", {"status": "started", "codex_transport": "exec"})

    rc = cli.main(["runs", "steer", str(run_dir), "coder", "keep going"])

    assert rc == 2
    assert "was not started with app-server transport" in capsys.readouterr().err


def test_runs_interrupt_refuses_missing_socket(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_json(
        run_dir / "run.json",
        {
            "status": "started",
            "codex_transport": "app-server",
            "control_socket": str(run_dir / "control.sock"),
        },
    )

    rc = cli.main(["runs", "interrupt", str(run_dir)])

    assert rc == 2
    assert "control socket is not active" in capsys.readouterr().err


def test_run_records_control_socket_and_cli_steers_live_worker(tmp_path, monkeypatch, capsys):
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan and synthesize"),
            "cook": Agent("cook", "codex", "write code"),
        },
        timeout_seconds=10.0,
        codex_transport="app-server",
    )
    run_dir = tmp_path / "run"
    real_app_server = codex_appserver.AppServer
    monkeypatch.setattr(
        aboyeur.codex_appserver,
        "AppServer",
        lambda cwd=None: real_app_server(argv=FAKE, cwd=cwd),
    )

    def fake_run_agent(cli_ref, prompt, **kwargs):
        if "Return exactly one JSON object" in prompt:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "cook", "task": "HANG until steered"}]}),
                ok=True,
            )
        assert "steered: please finish" in prompt
        return agents.AgentResult(text="final synthesis", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    result: dict[str, int] = {}

    thread = threading.Thread(
        target=lambda: result.update(rc=aboyeur.run("do it", roster, cwd=tmp_path, output_dir=run_dir)),
        daemon=True,
    )
    thread.start()

    control_socket = _wait_for(
        lambda: (
            Path(json.loads((run_dir / "run.json").read_text()).get("control_socket", ""))
            if (run_dir / "run.json").is_file() and json.loads((run_dir / "run.json").read_text()).get("control_socket")
            else None
        )
    )
    _wait_for(lambda: control_socket.exists())

    assert cli.main(["runs", "steer", str(run_dir), "cook", "please finish"]) == 0
    thread.join(timeout=5.0)

    assert result == {"rc": 0}
    assert "steer: cook" in capsys.readouterr().out
    run_meta = json.loads((run_dir / "run.json").read_text())
    assert run_meta["control_socket"] == str(control_socket)
    assert not control_socket.exists()
    worker_results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    assert worker_results[0]["text"] == "steered: please finish"


def test_cli_interrupt_leaves_live_worker_resumable(tmp_path, monkeypatch, capsys):
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan and synthesize"),
            "cook": Agent("cook", "codex", "write code"),
        },
        timeout_seconds=10.0,
        codex_transport="app-server",
    )
    run_dir = tmp_path / "run"
    real_app_server = codex_appserver.AppServer
    monkeypatch.setattr(
        aboyeur.codex_appserver,
        "AppServer",
        lambda cwd=None: real_app_server(argv=FAKE, cwd=cwd),
    )

    def fake_run_agent(cli_ref, prompt, **kwargs):
        if "Return exactly one JSON object" in prompt:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "cook", "task": "HANG until interrupted"}]}),
                ok=True,
            )
        return agents.AgentResult(text="final synthesis", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    result: dict[str, int] = {}

    thread = threading.Thread(
        target=lambda: result.update(rc=aboyeur.run("do it", roster, cwd=tmp_path, output_dir=run_dir)),
        daemon=True,
    )
    thread.start()

    control_socket = _wait_for(
        lambda: (
            Path(json.loads((run_dir / "run.json").read_text()).get("control_socket", ""))
            if (run_dir / "run.json").is_file() and json.loads((run_dir / "run.json").read_text()).get("control_socket")
            else None
        )
    )
    _wait_for(lambda: control_socket.exists())

    assert cli.main(["runs", "interrupt", str(run_dir), "cook"]) == 0
    thread.join(timeout=5.0)

    assert "rc" in result
    assert "interrupt: 1 (cook)" in capsys.readouterr().out
    assert not control_socket.exists()
    worker_results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    entry = worker_results[0]
    assert entry["status"] == "interrupted"
    assert entry["ok"] is False
    assert isinstance(entry["thread_id"], str) and entry["thread_id"]
