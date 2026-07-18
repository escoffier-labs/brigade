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


def _control_transport_from_run_json(run_dir: Path) -> run_control.ControlTransport | None:
    # Polled while aboyeur.run rewrites run.json on another thread, so tolerate
    # a file that is missing or not yet parseable and let _wait_for retry.
    try:
        meta = json.loads((run_dir / "run.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    transport_value = meta.get("control_transport")
    if transport_value is not None:
        try:
            return run_control.ControlTransport.from_metadata(transport_value)
        except run_control.ControlError:
            return None
    socket_value = meta.get("control_socket")
    if isinstance(socket_value, str) and socket_value:
        return run_control.ControlTransport(kind="unix", path=socket_value)
    return None


def _control_transport_active(transport: run_control.ControlTransport) -> bool:
    if transport.kind == "unix":
        return bool(transport.path and Path(transport.path).exists())
    if transport.kind == "loopback-tcp" and transport.port is not None:
        probe = run_control.socket.socket(run_control.socket.AF_INET, run_control.socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect((run_control._LOOPBACK_HOST, transport.port))
        except OSError:
            return False
        else:
            probe.close()
            return True
    return False


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
    transport = server.start()
    try:
        steer = run_control.send_request(
            transport,
            {"op": "steer", "worker": "coder", "text": "please focus on tests"},
        )
        interrupt = run_control.send_request(
            transport,
            {"op": "interrupt", "worker": "coder"},
        )
    finally:
        server.close()

    assert transport.kind == "unix"
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

    control_transport = _wait_for(lambda: _control_transport_from_run_json(run_dir))
    _wait_for(lambda: _control_transport_active(control_transport))

    assert cli.main(["runs", "steer", str(run_dir), "cook", "please finish"]) == 0
    thread.join(timeout=5.0)

    assert result == {"rc": 0}
    assert "steer: cook" in capsys.readouterr().out
    run_meta = json.loads((run_dir / "run.json").read_text())
    assert run_meta["control_transport"]["kind"] == "unix"
    assert run_meta["control_socket"] == control_transport.path
    if control_transport.kind == "unix" and control_transport.path:
        assert not Path(control_transport.path).exists()
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

    control_transport = _wait_for(lambda: _control_transport_from_run_json(run_dir))
    _wait_for(lambda: _control_transport_active(control_transport))

    assert cli.main(["runs", "steer", str(run_dir), "cook", "please finish"]) == 0
    thread.join(timeout=5.0)

    assert result == {"rc": 0}
    assert "steer: cook" in capsys.readouterr().out
    worker_results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    assert worker_results[0]["text"] == "steered: please finish"


def test_cli_steer_does_not_retry_when_run_is_finished(tmp_path, capsys):
    registry = run_control.LiveTurnRegistry()
    socket_path = tmp_path / "run" / "control.sock"
    server = run_control.ControlServer(socket_path, registry)
    transport = server.start()
    _write_json(
        tmp_path / "run" / "run.json",
        {
            "status": "ok",
            "codex_transport": "app-server",
            "control_transport": transport.to_metadata(),
            "control_socket": str(socket_path),
        },
    )
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

    control_transport = _wait_for(lambda: _control_transport_from_run_json(run_dir))
    _wait_for(lambda: _control_transport_active(control_transport))
    assert turn_registered.wait(timeout=5.0)

    assert cli.main(["runs", "interrupt", str(run_dir), "cook"]) == 0
    thread.join(timeout=5.0)

    assert "rc" in result
    assert "interrupt: 1 (cook)" in capsys.readouterr().out
    assert not _control_transport_active(control_transport)
    worker_results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    entry = worker_results[0]
    assert entry["status"] == "interrupted"
    assert entry["ok"] is False
    assert isinstance(entry["thread_id"], str) and entry["thread_id"]


def test_plan_control_transport_selects_loopback_when_af_unix_missing(tmp_path, monkeypatch):
    monkeypatch.delattr(run_control.socket, "AF_UNIX", raising=False)
    assert run_control.plan_control_transport(tmp_path / "control.sock") == "loopback-tcp"


def test_plan_control_transport_selects_loopback_for_deep_unix_path(tmp_path, monkeypatch):
    monkeypatch.setattr(run_control, "_unix_path_limit", lambda: 10)
    deep = tmp_path / ("a" * 50) / "control.sock"
    assert run_control.plan_control_transport(deep) == "loopback-tcp"


def test_loopback_control_transport_round_trip_with_owner_token(tmp_path, monkeypatch):
    monkeypatch.setattr(run_control, "plan_control_transport", lambda _path: "loopback-tcp")
    registry = run_control.LiveTurnRegistry()
    turn = _FakeTurn()
    registry.register("coder", turn, "turn-1")
    server = run_control.ControlServer(tmp_path / "control.sock", registry)
    transport = server.start()
    try:
        assert transport.kind == "loopback-tcp"
        steer = run_control.send_request(
            transport,
            {"op": "steer", "worker": "coder", "text": "focus"},
        )
        sock = run_control.socket.socket(run_control.socket.AF_INET, run_control.socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((run_control._LOOPBACK_HOST, transport.port))
        with sock, sock.makefile("rwb") as fh:
            fh.write(
                json.dumps(
                    {"op": "steer", "owner_token": "wrong", "text": "nope", "worker": "coder"},
                    sort_keys=True,
                ).encode()
                + b"\n"
            )
            fh.flush()
            denied = json.loads(fh.readline().decode())
    finally:
        server.close()

    assert steer == {"ok": True, "worker": "coder", "thread_id": "thread-1", "turn_id": "turn-1"}
    assert denied == {"code": "auth-denied", "error": "control request denied", "ok": False}


def test_loopback_control_rejects_foreign_owner_token_without_leaking(tmp_path, monkeypatch):
    monkeypatch.setattr(run_control, "plan_control_transport", lambda _path: "loopback-tcp")
    registry_a = run_control.LiveTurnRegistry()
    registry_b = run_control.LiveTurnRegistry()
    registry_a.register("coder", _FakeTurn(), "turn-a")
    registry_b.register("coder", _FakeTurn(), "turn-b")
    server_a = run_control.ControlServer(tmp_path / "a.sock", registry_a)
    server_b = run_control.ControlServer(tmp_path / "b.sock", registry_b)
    transport_a = server_a.start()
    transport_b = server_b.start()
    try:
        assert transport_a.owner_token and transport_b.owner_token
        assert transport_a.owner_token != transport_b.owner_token
        sock = run_control.socket.socket(run_control.socket.AF_INET, run_control.socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((run_control._LOOPBACK_HOST, transport_b.port))
        with sock, sock.makefile("rwb") as fh:
            fh.write(
                json.dumps(
                    {
                        "op": "steer",
                        "owner_token": transport_a.owner_token,
                        "text": "foreign",
                        "worker": "coder",
                    },
                    sort_keys=True,
                ).encode()
                + b"\n"
            )
            fh.flush()
            denied = json.loads(fh.readline().decode())
    finally:
        server_a.close()
        server_b.close()

    assert denied == {"code": "auth-denied", "error": "control request denied", "ok": False}
    response_text = json.dumps(denied)
    assert transport_a.owner_token not in response_text
    assert transport_b.owner_token not in response_text


def test_control_transport_from_run_rejects_malformed_descriptor(tmp_path):
    run_dir = tmp_path / "run"
    _write_json(
        run_dir / "run.json",
        {
            "status": "started",
            "codex_transport": "app-server",
            "control_transport": {"schema": "brigade.run_control_transport.v1", "kind": "loopback-tcp"},
        },
    )
    with pytest.raises(run_control.ControlError, match="requires valid port"):
        run_control.control_transport_from_run(run_dir)


def test_control_transport_from_run_rejects_boolean_port(tmp_path):
    run_dir = tmp_path / "run"
    _write_json(
        run_dir / "run.json",
        {
            "status": "started",
            "codex_transport": "app-server",
            "control_transport": {
                "schema": "brigade.run_control_transport.v1",
                "kind": "loopback-tcp",
                "port": True,
                "owner_token": "secret-token",
            },
        },
    )
    with pytest.raises(run_control.ControlError, match="requires valid port"):
        run_control.control_transport_from_run(run_dir)


def test_loopback_transport_ready_before_serving_thread_accepts(tmp_path, monkeypatch):
    monkeypatch.setattr(run_control, "plan_control_transport", lambda _path: "loopback-tcp")
    registry = run_control.LiveTurnRegistry()
    turn = _FakeTurn()
    registry.register("coder", turn, "turn-1")
    server = run_control.ControlServer(tmp_path / "control.sock", registry)
    observed: dict[str, object] = {}
    real_start = threading.Thread.start

    def recording_start(thread_self):
        transport = server.transport
        observed["transport_ready"] = (
            transport is not None
            and transport.kind == "loopback-tcp"
            and transport.port is not None
            and bool(transport.owner_token)
        )
        real_start(thread_self)

    monkeypatch.setattr(threading.Thread, "start", recording_start)
    transport = server.start()
    try:
        assert observed["transport_ready"] is True
        sock = run_control.socket.socket(run_control.socket.AF_INET, run_control.socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((run_control._LOOPBACK_HOST, transport.port))
        with sock, sock.makefile("rwb") as fh:
            fh.write(json.dumps({"op": "steer", "text": "nope", "worker": "coder"}, sort_keys=True).encode() + b"\n")
            fh.flush()
            denied = json.loads(fh.readline().decode())
    finally:
        server.close()

    assert denied == {"code": "auth-denied", "error": "control request denied", "ok": False}
    assert turn.calls == []


def test_runs_watch_json_omits_control_transport_secrets(tmp_path, capsys, monkeypatch):
    from brigade import runs_cmd

    run_dir = tmp_path / "run"
    private_path = str(tmp_path / "private" / "control.sock")
    _write_json(
        run_dir / "run.json",
        {
            "task": "watch the run",
            "cwd": str(tmp_path),
            "status": "ok",
            "started_at": "2026-07-08T10:00:00Z",
            "finished_at": "2026-07-08T10:00:03Z",
            "duration_seconds": 3.0,
            "codex_transport": "app-server",
            "control_socket": private_path,
            "control_transport": {
                "schema": "brigade.run_control_transport.v1",
                "kind": "loopback-tcp",
                "host": "127.0.0.1",
                "port": 54321,
                "owner_token": "secret-token",
            },
        },
    )

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.0, json_output=True) == 0

    emitted = capsys.readouterr().out
    assert "secret-token" not in emitted
    assert private_path not in emitted
    assert "control_transport" not in emitted
    assert "control_socket" not in emitted
    run_records = [json.loads(line) for line in emitted.splitlines() if json.loads(line).get("type") == "run"]
    assert run_records
    assert "owner_token" not in json.dumps(run_records[0])


def test_deep_unix_path_linux_regression_uses_loopback_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(run_control, "_unix_path_limit", lambda: 10)
    deep = tmp_path / ("nested" * 20) / "control.sock"
    registry = run_control.LiveTurnRegistry()
    turn = _FakeTurn()
    registry.register("coder", turn, "turn-1")
    server = run_control.ControlServer(deep, registry)
    transport = server.start()
    try:
        assert transport.kind == "loopback-tcp"
        assert not deep.exists()
        response = run_control.send_request(transport, {"op": "interrupt", "worker": "coder"})
    finally:
        server.close()
    assert response == {"interrupted": 1, "ok": True, "workers": ["coder"]}
