"""Tests for live run steering and interruption."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

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


def _control_socket_from_run_json(run_dir: Path) -> Path | None:
    # Polled while aboyeur.run rewrites run.json on another thread, so tolerate
    # a file that is missing or not yet parseable and let _wait_for retry.
    try:
        meta = json.loads((run_dir / "run.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    socket_value = meta.get("control_socket")
    return Path(socket_value) if socket_value else None


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


def test_control_server_start_cleans_up_after_thread_start_failure(tmp_path, monkeypatch):
    path = tmp_path / "control.sock"
    server = run_control.ControlServer(path, run_control.LiveTurnRegistry())
    startup_error = RuntimeError("forced thread start failure")

    def fail_start(_thread):
        raise startup_error

    monkeypatch.setattr(run_control.threading.Thread, "start", fail_start)

    with pytest.raises(run_control.ControlError, match="forced thread start failure") as exc_info:
        server.start()

    assert exc_info.value.__cause__ is startup_error
    assert not path.exists()
    assert server._sock is None
    assert server._thread is None


def test_control_server_start_does_not_unlink_socket_owned_by_bind_race(tmp_path, monkeypatch):
    path = tmp_path / "control.sock"
    server = run_control.ControlServer(path, run_control.LiveTurnRegistry())
    real_socket = run_control.socket.socket
    competing_socket = real_socket(run_control.socket.AF_UNIX, run_control.socket.SOCK_STREAM)
    bind_error = OSError("address already in use")

    class BindRaceSocket:
        def bind(self, socket_path):
            competing_socket.bind(socket_path)
            competing_socket.listen()
            raise bind_error

        def close(self):
            pass

    monkeypatch.setattr(run_control.socket, "socket", lambda *_args: BindRaceSocket())

    try:
        with pytest.raises(run_control.ControlError, match="address already in use") as exc_info:
            server.start()

        assert exc_info.value.__cause__ is bind_error
        assert path.exists()
    finally:
        competing_socket.close()
        path.unlink(missing_ok=True)


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

    control_socket = _wait_for(lambda: _control_socket_from_run_json(run_dir))
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


def test_cli_steer_retries_until_worker_turn_registers(tmp_path, monkeypatch, capsys):
    # The control socket exists as soon as the run starts dispatching, before
    # any worker turn has registered. Delay registration so the steer request
    # is guaranteed to arrive first; the CLI must retry instead of failing
    # with "no active turn" (the race CI runners hit on unmodified diffs).
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
    real_register = run_control.LiveTurnRegistry.register

    def slow_register(self, worker, thread, turn_id):
        time.sleep(0.5)
        real_register(self, worker, thread, turn_id)

    monkeypatch.setattr(run_control.LiveTurnRegistry, "register", slow_register)

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

    control_socket = _wait_for(lambda: _control_socket_from_run_json(run_dir))
    _wait_for(lambda: control_socket.exists())

    assert cli.main(["runs", "steer", str(run_dir), "cook", "please finish"]) == 0
    thread.join(timeout=5.0)

    assert result == {"rc": 0}
    assert "steer: cook" in capsys.readouterr().out
    worker_results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    assert worker_results[0]["text"] == "steered: please finish"


def test_cli_steer_does_not_retry_when_run_is_finished(tmp_path, capsys):
    registry = run_control.LiveTurnRegistry()
    socket_path = tmp_path / "run" / "control.sock"
    _write_json(
        tmp_path / "run" / "run.json",
        {
            "status": "ok",
            "codex_transport": "app-server",
            "control_socket": str(socket_path),
        },
    )
    server = run_control.ControlServer(socket_path, registry)
    server.start()
    try:
        rc = cli.main(["runs", "steer", str(tmp_path / "run"), "cook", "keep going"])
    finally:
        server.close()

    assert rc == 1
    assert "no active turn for worker 'cook'" in capsys.readouterr().err


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
    turn_registered = threading.Event()
    real_register = run_control.LiveTurnRegistry.register

    def register_and_signal(registry, worker, turn, turn_id):
        real_register(registry, worker, turn, turn_id)
        if worker == "cook":
            turn_registered.set()

    monkeypatch.setattr(run_control.LiveTurnRegistry, "register", register_and_signal)

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

    control_socket = _wait_for(lambda: _control_socket_from_run_json(run_dir))
    _wait_for(lambda: control_socket.exists())
    assert turn_registered.wait(timeout=5.0)

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
