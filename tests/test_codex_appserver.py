"""Tests for the codex app-server JSON-RPC client against the fake server."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from brigade import codex_appserver, proc

FAKE = [sys.executable, str(Path(__file__).parent / "fake_appserver.py")]


def _server():
    return codex_appserver.AppServer(argv=FAKE)


def test_happy_turn_returns_complete():
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("say hi", timeout=10.0)
    assert result.ok
    assert result.status == "complete"
    assert result.text == "result for: say hi"
    assert result.thread_id == "t-1"


def test_events_are_streamed_without_deltas():
    events: list[dict] = []
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        thread.run_turn("NOISE say hi", timeout=10.0, on_event=events.append)
    methods = [e["method"] for e in events]
    assert "turn/started" in methods
    assert "item/started" in methods
    assert "item/completed" in methods
    assert "turn/completed" in methods
    assert "totally/unknown" in methods  # unknown methods logged, not fatal
    assert not any("delta" in m for m in methods)


def test_timeout_interrupts_and_salvages_partial_text():
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("HANG forever", timeout=0.5)
    assert not result.ok
    assert result.status == "interrupted"
    assert result.text == "partial answer"
    assert "timeout" in result.detail
    assert result.timed_out is True


@pytest.mark.parametrize(
    "grace_result",
    [
        codex_appserver._DEAD,
        {
            "params": {
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "provider stopped during interrupt grace"},
                }
            }
        },
    ],
)
def test_timeout_cause_survives_terminal_event_during_interrupt_grace(monkeypatch, grace_result):
    class StubServer:
        def request(self, method, params, timeout=None):  # noqa: ARG002
            return {"turn": {"id": "turn-1"}}

    thread = codex_appserver.CodexThread(StubServer(), "thread-1", None)
    results = iter((None, grace_result))
    monkeypatch.setattr(thread, "_consume", lambda *args, **kwargs: next(results))
    monkeypatch.setattr(thread, "interrupt", lambda *args, **kwargs: None)

    result = thread.run_turn("work", timeout=0)

    assert result.status == "interrupted"
    assert result.timed_out is True


def test_approval_requests_are_auto_declined():
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("APPROVAL needed", timeout=10.0)
    assert result.ok
    assert result.text == "approval:decline"


def test_server_death_mid_turn_fails_not_hangs():
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("DIE now", timeout=10.0)
    assert not result.ok
    assert result.status == "failed"
    assert "app-server exited" in result.detail


def test_resume_thread_reuses_id():
    with _server() as server:
        thread = server.resume_thread("t-99", cwd=Path("/tmp"))
        result = thread.run_turn("continue", timeout=10.0)
    assert result.ok
    assert result.thread_id == "t-99"


def test_spawn_failure_raises():
    with pytest.raises(codex_appserver.AppServerError):
        codex_appserver.AppServer(argv=["/nonexistent-binary-xyz"]).start()


def test_start_closes_spawned_process_when_initialize_fails(monkeypatch):
    events = []

    class StubProcess:
        pid = 4242

    class StubRegistry:
        def register(self, process):
            events.append(("register", process.pid))

        def terminate(self, process):
            events.append(("terminate", process.pid))

        def unregister(self, process):
            events.append(("unregister", process.pid))

    class StubThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            events.append("reader-start")

    server = codex_appserver.AppServer(argv=["codex", "app-server"], process_registry=StubRegistry())
    monkeypatch.setattr(codex_appserver.subprocess, "Popen", lambda *args, **kwargs: StubProcess())
    monkeypatch.setattr(codex_appserver.threading, "Thread", StubThread)
    monkeypatch.setattr(
        server,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(codex_appserver.AppServerError("initialize failed")),
    )

    with pytest.raises(codex_appserver.AppServerError, match="initialize failed"):
        server.start()

    assert events == [
        ("register", 4242),
        "reader-start",
        ("terminate", 4242),
        ("unregister", 4242),
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_close_terminates_appserver_descendant_process_group(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    server_script = tmp_path / "server_with_child.py"
    server_script.write_text(
        "\n".join(
            [
                "import json, subprocess, sys, time",
                f"child_pid_path = {str(child_pid_path)!r}",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                "open(child_pid_path, 'w').write(str(child.pid))",
                "for line in sys.stdin:",
                "    request = json.loads(line)",
                "    if request.get('method') == 'initialize':",
                "        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {}}), flush=True)",
                "    time.sleep(0.01)",
            ]
        )
    )
    registry = proc.ProcessRegistry(terminate_grace=0.1, kill_grace=0.1)
    server = codex_appserver.AppServer(
        argv=[sys.executable, str(server_script)],
        process_registry=registry,
    )
    server.start()
    assert server._proc is not None
    process_group = server._proc.pid
    deadline = time.monotonic() + 5
    while not child_pid_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_path.is_file()

    server.close()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        os.killpg(process_group, signal.SIGKILL)
        pytest.fail("app-server descendant process group survived close")


def test_windows_appserver_uses_group_and_registry_tree_termination(monkeypatch):
    events = []
    captured = {}

    class StubProcess:
        pid = 4242

    class StubRegistry:
        def register(self, process):
            events.append(("register", process.pid))

        def terminate(self, process):
            events.append(("terminate", process.pid))

        def unregister(self, process):
            events.append(("unregister", process.pid))

    class StubThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return StubProcess()

    registry = StubRegistry()
    server = codex_appserver.AppServer(argv=["codex", "app-server"], process_registry=registry)
    monkeypatch.setattr(codex_appserver.os, "name", "nt")
    monkeypatch.setattr(codex_appserver.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(codex_appserver.threading, "Thread", StubThread)
    monkeypatch.setattr(server, "request", lambda *args, **kwargs: {})
    monkeypatch.setattr(server, "_send", lambda *args, **kwargs: None)

    server.start()
    server.close()

    assert captured["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert "start_new_session" not in captured
    assert events == [("register", 4242), ("terminate", 4242), ("unregister", 4242)]
