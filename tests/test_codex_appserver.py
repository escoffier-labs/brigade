"""Tests for the codex app-server JSON-RPC client against the fake server."""

from __future__ import annotations

import io
import json
import os
import queue
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


class _BudgetStubServer:
    def __init__(self):
        self._output_limit_exceeded = False
        self._capture_budgets = {}
        self._orphan_budget = proc.ByteBudget()

    def capture_budget(self, thread_id=None):
        if thread_id is None:
            return self._orphan_budget
        return self._capture_budgets.setdefault(thread_id, proc.ByteBudget())

    def reset_capture(self, thread_id):
        self._capture_budgets[thread_id] = proc.ByteBudget()

    def _signal_output_limit(self):
        self._output_limit_exceeded = True
        for budget in self._capture_budgets.values():
            budget.overflowed = True

    def request(self, method, params, timeout=None):  # noqa: ARG002
        return {"turn": {"id": "turn-1"}}


def test_consume_caps_deltas_during_accumulation(monkeypatch):
    monkeypatch.setattr(proc, "MAX_CAPTURE_BYTES", 256)
    q: queue.Queue = queue.Queue()
    server = _BudgetStubServer()
    thread = codex_appserver.CodexThread(server, "thread-1", q)
    chunk = "D" * 50
    for _ in range(20):
        q.put(
            {
                "method": "item/agentMessage/delta",
                "params": {"itemId": "msg-1", "delta": chunk},
            }
        )
    q.put(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "completed", "items": []}},
        }
    )

    result = thread.run_turn("work", timeout=2.0)

    assert result.output_limit_exceeded is True
    assert result.ok is False
    assert result.completed_observed is False
    assert len(result.text.encode("utf-8")) <= 256
    assert server.capture_budget("thread-1").used <= 256
    assert server.capture_budget("thread-1").overflowed is True
    assert "combined output exceeded" in result.detail


def test_over_cap_after_turn_completed_observed_keeps_completion_signal(monkeypatch):
    """#1200: cap hit only when binding the final answer still records the observed completion."""
    monkeypatch.setattr(proc, "MAX_CAPTURE_BYTES", 64)
    q: queue.Queue = queue.Queue()
    server = _BudgetStubServer()
    thread = codex_appserver.CodexThread(server, "thread-1", q)
    q.put({"method": "item/agentMessage/delta", "params": {"itemId": "msg-1", "delta": "hi"}})
    q.put(
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": "F" * 4096}],
                }
            },
        }
    )

    result = thread.run_turn("work", timeout=2.0)

    assert result.output_limit_exceeded is True
    assert result.completed_observed is True


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_over_cap_completion_requires_completed_status(monkeypatch, status):
    """#1200 finding 2: salvage signal requires turn status == completed."""
    monkeypatch.setattr(proc, "MAX_CAPTURE_BYTES", 64)
    q: queue.Queue = queue.Queue()
    server = _BudgetStubServer()
    thread = codex_appserver.CodexThread(server, "thread-1", q)
    q.put({"method": "item/agentMessage/delta", "params": {"itemId": "msg-1", "delta": "hi"}})
    q.put(
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": "turn-1",
                    "status": status,
                    "items": [{"type": "agentMessage", "text": "F" * 4096}],
                }
            },
        }
    )

    result = thread.run_turn("work", timeout=2.0)

    assert result.output_limit_exceeded is True
    assert result.completed_observed is False
    assert result.ok is False


def _ingestion_server(lines: bytes):
    server = codex_appserver.AppServer(argv=FAKE)
    q: queue.Queue = queue.Queue()
    server._queues["t-cap"] = q
    server.reset_capture("t-cap")

    class FakeProc:
        stdout = io.BytesIO(lines)

    server._proc = FakeProc()
    return server, q


def test_over_cap_completed_record_ingested_via_stream_preserves_completion(monkeypatch):
    """#1200 finding 1: the real ingestion path records an over-cap turn/completed."""
    monkeypatch.setattr(proc, "MAX_CAPTURE_BYTES", 512)
    delta = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "item/agentMessage/delta",
            "params": {"threadId": "t-cap", "itemId": "m", "delta": "ok"},
        }
    )
    oversized_turn = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": "t-cap",
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": "Z" * 2048}],
                },
            },
        }
    )
    server, q = _ingestion_server((delta + "\n" + oversized_turn + "\n").encode("utf-8"))

    server._read_loop()

    assert server._output_limit_exceeded is True
    assert server.observed_completion_status("t-cap", "turn-1") == "completed"

    monkeypatch.setattr(server, "request", lambda *args, **kwargs: {"turn": {"id": "turn-1"}})
    thread = codex_appserver.CodexThread(server, "t-cap", q)
    result = thread.run_turn("work", timeout=2.0)
    assert result.completed_observed is True
    assert result.ok is False
    assert result.output_limit_exceeded is True


def test_ingested_failed_status_over_cap_record_does_not_signal_completion(monkeypatch):
    """#1200: metadata recorded at ingestion still respects status for the salvage gate."""
    monkeypatch.setattr(proc, "MAX_CAPTURE_BYTES", 512)
    oversized_turn = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": "t-cap",
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "boom"},
                    "items": [{"type": "agentMessage", "text": "Z" * 2048}],
                },
            },
        }
    )
    server, _q = _ingestion_server((oversized_turn + "\n").encode("utf-8"))

    server._read_loop()

    assert server.observed_completion_status("t-cap", "turn-1") == "failed"


def test_read_loop_stops_queueing_after_line_cap(monkeypatch):
    monkeypatch.setattr(proc, "MAX_CAPTURE_BYTES", 512)
    captured = {"queued": 0, "peak": 0}

    class RecordingQueue:
        def __init__(self):
            self._items = []

        def put(self, msg):
            if msg is codex_appserver._OUTPUT_LIMIT or msg is codex_appserver._DEAD:
                return
            size = len(json.dumps(msg).encode("utf-8"))
            captured["queued"] += size
            captured["peak"] = max(captured["peak"], captured["queued"])

    server = codex_appserver.AppServer(argv=FAKE)
    server._queues["t-flood"] = RecordingQueue()
    server.reset_capture("t-flood")
    flood_line = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "item/agentMessage/delta",
            "params": {"threadId": "t-flood", "itemId": "m", "delta": "X" * 200},
        }
    )
    lines = "\n".join([flood_line] * 20) + "\n"

    class FakeProc:
        stdout = io.BytesIO(lines.encode("utf-8"))

    server._proc = FakeProc()
    server._read_loop()

    assert server._output_limit_exceeded is True
    assert captured["peak"] <= 512
    assert server.capture_budget("t-flood").overflowed is True


class _VirtualRecordStream:
    """A single virtual JSONL record that is never fully materialized."""

    def __init__(self, prefix: bytes, fill_size: int, suffix: bytes, fill: bytes = b"X"):
        if len(fill) != 1:
            raise ValueError("fill must be one byte")
        self._prefix = prefix
        self._fill_size = fill_size
        self._suffix = suffix
        self._fill = fill
        self._offset = 0
        self.bytes_served = 0
        self.max_request = 0
        self.total = len(prefix) + fill_size + len(suffix)

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            raise AssertionError("unbounded stdout read would buffer the whole record")
        self.max_request = max(self.max_request, n)
        if self._offset >= self.total or n == 0:
            return b""
        end = min(self._offset + n, self.total)
        chunk = self._slice(self._offset, end)
        self._offset = end
        self.bytes_served += len(chunk)
        return chunk

    def _slice(self, start: int, end: int) -> bytes:
        prefix_end = len(self._prefix)
        fill_end = prefix_end + self._fill_size
        parts = []
        cur = start
        if cur < prefix_end:
            take = min(end, prefix_end)
            parts.append(self._prefix[cur:take])
            cur = take
        if cur < end and cur < fill_end:
            count = min(end, fill_end) - cur
            parts.append(self._fill * count)
            cur += count
        if cur < end:
            suffix_start = cur - fill_end
            suffix_end = end - fill_end
            parts.append(self._suffix[suffix_start:suffix_end])
        return b"".join(parts)


class _RecordingRegistry:
    def __init__(self) -> None:
        self.terminated: list[object] = []

    def register(self, process):  # noqa: ARG002
        return None

    def terminate(self, process):
        self.terminated.append(process)

    def unregister(self, process):  # noqa: ARG002
        return None


def _tracking_loads(parsed_sizes: list[int]):
    real_loads = json.loads

    def tracking_loads(s, *args, **kwargs):
        if isinstance(s, (bytes, bytearray)):
            parsed_sizes.append(len(s))
        elif isinstance(s, str):
            parsed_sizes.append(len(s.encode("utf-8")))
        else:
            parsed_sizes.append(0)
        return real_loads(s, *args, **kwargs)

    return tracking_loads


def test_read_loop_oversized_valid_record_trips_cap_without_full_buffer(monkeypatch):
    cap = 512
    monkeypatch.setattr(proc, "MAX_CAPTURE_BYTES", cap)
    parsed_sizes: list[int] = []
    monkeypatch.setattr(codex_appserver.json, "loads", _tracking_loads(parsed_sizes))

    prefix = (
        b'{"jsonrpc":"2.0","method":"item/completed",'
        b'"params":{"threadId":"t-oversize","item":{"type":"agentMessage","text":"'
    )
    suffix = b'"}}}\n'
    fill_size = cap + 4096
    stream = _VirtualRecordStream(prefix, fill_size, suffix)
    assert stream.total > cap

    registry = _RecordingRegistry()
    server = codex_appserver.AppServer(argv=FAKE, process_registry=registry)

    class FakeProc:
        stdout = stream

    server._proc = FakeProc()
    server._read_loop()

    assert server._output_limit_exceeded is True
    assert stream.bytes_served <= cap + 1
    assert stream.max_request <= cap + 1
    assert stream.bytes_served < stream.total
    assert all(size <= cap for size in parsed_sizes)
    assert registry.terminated
    assert server.capture_budget(None).overflowed is True
    assert server.capture_budget(None).used == cap


def test_read_loop_oversized_malformed_record_trips_cap_and_charges_budget(monkeypatch):
    cap = 512
    monkeypatch.setattr(proc, "MAX_CAPTURE_BYTES", cap)
    parsed_sizes: list[int] = []
    monkeypatch.setattr(codex_appserver.json, "loads", _tracking_loads(parsed_sizes))

    fill_size = cap + 4096
    stream = _VirtualRecordStream(b'{"not-json ', fill_size, b"\n", fill=b"A")
    assert stream.total > cap

    registry = _RecordingRegistry()
    server = codex_appserver.AppServer(argv=FAKE, process_registry=registry)

    class FakeProc:
        stdout = stream

    server._proc = FakeProc()
    server._read_loop()

    assert server._output_limit_exceeded is True
    assert server.capture_budget(None).overflowed is True
    assert server.capture_budget(None).used == cap
    assert stream.bytes_served <= cap + 1
    assert stream.bytes_served < stream.total
    assert parsed_sizes == []
    assert registry.terminated


def test_flood_turn_signals_output_limit_under_small_cap(monkeypatch):
    monkeypatch.setattr(proc, "MAX_CAPTURE_BYTES", 4096)
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("FLOOD now", timeout=10.0)
    assert result.output_limit_exceeded is True
    assert result.ok is False
    assert len(result.text.encode("utf-8")) <= 4096
    assert "combined output exceeded" in result.detail


def test_appserver_env_reaches_child_without_seat_env(monkeypatch):
    """Run-scoped identity can ride AppServer process env (not per-seat env)."""
    from brigade import receipt_schema

    captured = {}

    class StubProcess:
        pid = 4242

    class StubRegistry:
        def register(self, process):
            pass

        def terminate(self, process):
            pass

        def unregister(self, process):
            pass

    class StubThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return StubProcess()

    server = codex_appserver.AppServer(
        argv=["codex", "app-server"],
        process_registry=StubRegistry(),
        env={receipt_schema.BRIGADE_RUN_ID_ENV: "orch-appserver-1"},
    )
    monkeypatch.setattr(codex_appserver.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(codex_appserver.threading, "Thread", StubThread)
    monkeypatch.setattr(server, "request", lambda *args, **kwargs: {})
    monkeypatch.setattr(server, "_send", lambda *args, **kwargs: None)

    server.start()
    server.close()

    assert captured["env"][receipt_schema.BRIGADE_RUN_ID_ENV] == "orch-appserver-1"
