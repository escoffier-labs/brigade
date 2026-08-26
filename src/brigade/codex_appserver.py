"""JSON-RPC stdio client for `codex app-server`.

Every protocol-shape assumption for the experimental app-server API lives in
this module and nowhere else. Wire format: newline-delimited JSON-RPC 2.0
(verified against codex-cli 0.142.5). Approval requests from the server are
always auto-declined: brigade runs are headless and rely on approvalPolicy
"never" plus an explicit sandbox.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

from . import proc as proc_mod

_CLIENT_NAME = "brigade"
_CLIENT_VERSION = "0.0.0"
_REQUEST_TIMEOUT = 30.0
_INTERRUPT_GRACE = 5.0
_ORPHAN_LIMIT = 1000
_READ_CHUNK_BYTES = 4096
_DEAD = object()  # queue sentinel: server process is gone
_OUTPUT_LIMIT = object()  # queue sentinel: capture cap exceeded

# Chatty per-token notifications: consumed for salvage, never forwarded to on_event.
_DELTA_METHODS = frozenset(
    {
        "item/agentMessage/delta",
        "item/plan/delta",
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
        "command/exec/outputDelta",
        "process/outputDelta",
    }
)

# #1200: bounded metadata salvage for oversized turn/completed records. The
# record itself is never parsed or retained; only the turn id and status are.
_TURN_COMPLETED_MARKER = b'"turn/completed"'
_TURN_OBJECT_ID_RE = re.compile(rb'"id"\s*:\s*"([^"]+)"')
_THREAD_ID_RE = re.compile(rb'"threadId"\s*:\s*"([^"]+)"')
_STATUS_RE = re.compile(rb'"status"\s*:\s*"([^"]+)"')
_METADATA_WINDOW = 65536


def _turn_object_window(raw: bytes) -> bytes:
    start = raw.find(b'"turn"')
    if start < 0:
        return b""
    brace = raw.find(b"{", start)
    if brace < 0:
        return b""
    depth = 0
    end = brace
    while end < len(raw) and end - brace < _METADATA_WINDOW:
        char = raw[end : end + 1]
        if char == b"{":
            depth += 1
        elif char == b"}":
            depth -= 1
            if depth == 0:
                break
        end += 1
    return raw[brace : end + 1]


def _salvage_completion_metadata(raw: bytes) -> tuple[str | None, str | None]:
    """Extract (turn id, status) from an oversized turn/completed record."""

    if _TURN_COMPLETED_MARKER not in raw:
        return None, None
    window = _turn_object_window(raw)
    turn_match = _TURN_OBJECT_ID_RE.search(window)
    status_match = _STATUS_RE.search(window)
    turn_id = turn_match.group(1).decode("utf-8", "replace") if turn_match else None
    status = status_match.group(1).decode("utf-8", "replace") if status_match else None
    return turn_id, status


def _message_thread_id_from_raw(raw: bytes) -> str:
    match = _THREAD_ID_RE.search(raw)
    return match.group(1).decode("utf-8", "replace") if match else ""


def _byte_stream(stdout: Any) -> Any:
    """Prefer the binary buffer so text-mode iteration cannot hold an unbounded line."""

    buffer = getattr(stdout, "buffer", None)
    return stdout if buffer is None else buffer


def _read_stdout_chunk(stream: Any, n: int) -> bytes:
    """Read at most ``n`` bytes without blocking for a full chunk on a live pipe.

    ``BufferedReader.read(n)`` on a pipe waits until ``n`` bytes or EOF, so a
    short JSONL line would stall until the child closed stdout. ``read1``
    returns as soon as any data is available.
    """

    if n <= 0:
        return b""
    reader = getattr(stream, "read1", None)
    if not callable(reader):
        reader = stream.read
    try:
        chunk = reader(n)
    except (OSError, ValueError):
        return b""
    if not chunk:
        return b""
    if isinstance(chunk, str):
        return chunk.encode("utf-8")
    return bytes(chunk)


def _message_thread_id(msg: dict) -> str | None:
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    thread_id = params.get("threadId")
    if isinstance(thread_id, str):
        return thread_id
    thread = params.get("thread")
    if isinstance(thread, dict):
        ident = thread.get("id")
        if isinstance(ident, str):
            return ident
    return None


class AppServerError(RuntimeError):
    """Spawn, handshake, transport, or server-reported request failure."""


@dataclass(frozen=True)
class TurnResult:
    text: str
    ok: bool
    status: str  # complete | interrupted | failed
    thread_id: str
    detail: str = ""
    timed_out: bool = False
    output_limit_exceeded: bool = False
    # #1200: True only when a turn/completed notification for this turn id was
    # actually observed on the stream before the capture cap hit.
    completed_observed: bool = False


class AppServer:
    """One `codex app-server` child; thread-safe for concurrent CodexThread turns."""

    def __init__(
        self,
        argv: list[str] | None = None,
        cwd: Path | None = None,
        process_registry: proc_mod.ProcessRegistry | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._argv = argv or ["codex", "app-server"]
        self._cwd = cwd
        self._process_registry = process_registry or proc_mod.ProcessRegistry()
        self._env = env
        self._proc: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict] = {}  # id -> {"event": Event, "response": msg}
        self._queues: dict[str, queue.Queue] = {}
        self._orphans: deque[tuple[str, dict]] = deque(maxlen=_ORPHAN_LIMIT)
        self._dead = False
        self._capture_budgets: dict[str, proc_mod.ByteBudget] = {}
        self._orphan_budget = proc_mod.ByteBudget()
        self._output_limit_exceeded = False
        # #1200: (thread_id, turn_id) -> status for every turn/completed seen
        # during ingestion, including oversized records (metadata only).
        self._observed_completions: dict[tuple[str, str], str] = {}

    def __enter__(self) -> "AppServer":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def start(self) -> None:
        process_group_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            process_group_kwargs["start_new_session"] = True
        elif os.name == "nt":
            process_group_kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        try:
            child_env = None
            if self._env is not None:
                child_env = dict(os.environ)
                child_env.update(self._env)
            self._proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd=self._cwd,
                env=child_env,
                **process_group_kwargs,
            )
            self._process_registry.register(cast("subprocess.Popen[bytes]", self._proc))
        except OSError as exc:
            raise AppServerError(f"failed to spawn {self._argv[0]}: {exc}") from exc
        try:
            threading.Thread(target=self._read_loop, daemon=True).start()
            self.request(
                "initialize",
                {"clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION}},
            )
            self._send({"jsonrpc": "2.0", "method": "initialized"})
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            self._process_registry.terminate(cast("subprocess.Popen[bytes]", proc))
        finally:
            self._process_registry.unregister(cast("subprocess.Popen[bytes]", proc))

    def request(self, method: str, params: dict, timeout: float = _REQUEST_TIMEOUT) -> dict:
        with self._state_lock:
            if self._dead or self._output_limit_exceeded:
                raise AppServerError("app-server exited")
            self._next_id += 1
            req_id = self._next_id
            entry: dict = {"event": threading.Event(), "response": None}
            self._pending[req_id] = entry
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        if not entry["event"].wait(timeout):
            with self._state_lock:
                self._pending.pop(req_id, None)
            raise AppServerError(f"{method} timed out after {timeout}s")
        response = entry["response"]
        if response is None or "error" in response:
            error = (response or {}).get("error") or {"message": "app-server exited"}
            raise AppServerError(f"{method} failed: {error.get('message', error)}")
        return response.get("result") or {}

    def start_thread(self, *, cwd: Path | None, model: str | None = None, sandbox: str | None = None) -> "CodexThread":
        result = self.request("thread/start", self._thread_params(cwd, model, sandbox))
        return self._attach(result["thread"]["id"])

    def resume_thread(
        self, thread_id: str, *, cwd: Path | None, model: str | None = None, sandbox: str | None = None
    ) -> "CodexThread":
        params = self._thread_params(cwd, model, sandbox)
        params["threadId"] = thread_id
        result = self.request("thread/resume", params)
        return self._attach(result["thread"]["id"])

    def _thread_params(self, cwd: Path | None, model: str | None, sandbox: str | None) -> dict:
        # Omitted keys fall through to the user's codex config, matching exec behavior.
        params: dict = {"approvalPolicy": "never", "ephemeral": False}
        if cwd is not None:
            params["cwd"] = str(cwd)
        if model is not None:
            params["model"] = model
        if sandbox is not None:
            params["sandbox"] = sandbox
        return params

    def capture_budget(self, thread_id: str | None = None) -> proc_mod.ByteBudget:
        if thread_id is None:
            return self._orphan_budget
        return self._capture_budgets.setdefault(thread_id, proc_mod.ByteBudget())

    def observed_completion_status(self, thread_id: str, turn_id: str) -> str | None:
        """#1200: status of a turn/completed observed on the stream, if any."""

        return self._observed_completions.get((thread_id, turn_id))

    def _note_turn_completed(self, thread_id: Any, turn: Any) -> None:
        if not isinstance(turn, dict):
            return
        turn_id = turn.get("id")
        status = turn.get("status")
        if isinstance(thread_id, str) and isinstance(turn_id, str):
            self._observed_completions[(thread_id, turn_id)] = status if isinstance(status, str) else ""

    def reset_capture(self, thread_id: str) -> None:
        self._capture_budgets[thread_id] = proc_mod.ByteBudget()

    def _attach(self, thread_id: str) -> "CodexThread":
        q: queue.Queue = queue.Queue()
        with self._state_lock:
            self._queues[thread_id] = q
            self._capture_budgets.setdefault(thread_id, proc_mod.ByteBudget())
            dead = self._dead
            for orphan_id, msg in list(self._orphans):
                if orphan_id == thread_id:
                    q.put(msg)
            self._orphans = deque(((tid, m) for tid, m in self._orphans if tid != thread_id), maxlen=_ORPHAN_LIMIT)
        if dead:
            q.put(_DEAD)
        return CodexThread(self, thread_id, q)

    def _send(self, obj: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AppServerError("app-server not started")
        line = json.dumps(obj) + "\n"
        with self._write_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AppServerError("app-server exited") from exc

    def _signal_output_limit(self) -> None:
        """Stop accumulating, wake waiters, and terminate the child."""

        with self._state_lock:
            if self._output_limit_exceeded:
                pending: list[dict] = []
                queues: list[queue.Queue] = []
            else:
                self._output_limit_exceeded = True
                pending = list(self._pending.values())
                self._pending.clear()
                queues = list(self._queues.values())
        for entry in pending:
            entry["event"].set()
        for q in queues:
            q.put(_OUTPUT_LIMIT)
        proc = self._proc
        if proc is not None:
            try:
                self._process_registry.terminate(cast("subprocess.Popen[bytes]", proc))
            except Exception:
                pass

    def _note_overflow_metadata(self, raw: bytes) -> None:
        """#1200: preserve turn/completed metadata from an oversized record."""

        turn_id, status = _salvage_completion_metadata(raw)
        if turn_id is not None:
            self._observed_completions[(_message_thread_id_from_raw(raw), turn_id)] = status or ""

    def _trip_output_limit(self, line_bytes: int, thread_id: str | None = None) -> None:
        """Charge ``line_bytes`` against the cap (overflowing) and stop the child."""

        self.capture_budget(thread_id).accept(line_bytes)
        self._signal_output_limit()

    def _charge_record(self, line_bytes: int, thread_id: str | None) -> bool:
        """Account ``line_bytes`` against the cap. False means overflow."""

        if not self.capture_budget(thread_id).try_add(line_bytes):
            self._signal_output_limit()
            return False
        return True

    def _ingest_line(self, raw: bytes) -> bool:
        """Charge one JSONL record, then parse. False means the cap tripped.

        A record larger than the cap is charged and rejected before
        ``json.loads``. Malformed or non-object records are charged too, so
        they cannot bypass the cap by failing to parse.
        """

        if self._output_limit_exceeded:
            return False
        stripped = raw.strip()
        if not stripped:
            return True
        line_bytes = len(stripped)
        if line_bytes > proc_mod.MAX_CAPTURE_BYTES:
            # #1200: preserve turn/completed metadata without retaining text.
            self._note_overflow_metadata(stripped)
            self._trip_output_limit(line_bytes)
            return False
        try:
            msg = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return self._charge_record(line_bytes, None)
        if not isinstance(msg, dict):
            return self._charge_record(line_bytes, None)
        thread_id = _message_thread_id(msg)
        if not self._charge_record(line_bytes, thread_id):
            # #1200: a parsed record can still overflow the shared budget.
            if msg.get("method") == "turn/completed":
                self._note_turn_completed(thread_id, (msg.get("params") or {}).get("turn"))
            return False
        if msg.get("method") == "turn/completed":
            self._note_turn_completed(thread_id, (msg.get("params") or {}).get("turn"))
        if not self._charge_record(line_bytes, thread_id):
            return False
        if msg.get("id") is not None and "method" in msg:
            self._handle_server_request(msg)
        elif msg.get("id") is not None:
            self._handle_response(msg)
        elif "method" in msg:
            self._route_notification(msg)
        return True

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = _byte_stream(self._proc.stdout)
        leftover = bytearray()
        limit = proc_mod.MAX_CAPTURE_BYTES
        chunk_size = _READ_CHUNK_BYTES
        while not self._output_limit_exceeded:
            newline_at = leftover.find(b"\n")
            if newline_at < 0:
                if len(leftover) > limit:
                    self._note_overflow_metadata(bytes(leftover))
                    self._trip_output_limit(len(leftover))
                    leftover.clear()
                    break
                chunk = _read_stdout_chunk(stream, min(chunk_size, limit + 1 - len(leftover)))
                if not chunk:
                    if leftover and not self._ingest_line(bytes(leftover)):
                        leftover.clear()
                    break
                leftover.extend(chunk)
                continue
            raw = bytes(leftover[:newline_at])
            del leftover[: newline_at + 1]
            if not self._ingest_line(raw):
                leftover.clear()
                break
        overflowed = self._output_limit_exceeded
        with self._state_lock:
            self._dead = True
            pending = list(self._pending.values())
            self._pending.clear()
            queues = list(self._queues.values())
        for entry in pending:
            entry["event"].set()
        for q in queues:
            q.put(_OUTPUT_LIMIT if overflowed else _DEAD)

    def _handle_response(self, msg: dict) -> None:
        with self._state_lock:
            entry = self._pending.pop(msg["id"], None)
        if entry is not None:
            entry["response"] = msg
            entry["event"].set()

    def _handle_server_request(self, msg: dict) -> None:
        # Headless policy: decline every approval; refuse anything else.
        method = str(msg.get("method", ""))
        if "pproval" in method:
            reply: dict = {"jsonrpc": "2.0", "id": msg["id"], "result": {"decision": "decline"}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32000, "message": "brigade runs headless; request declined"},
            }
        try:
            self._send(reply)
        except AppServerError:
            return
        params = msg.get("params") or {}
        thread_id = params.get("threadId")
        if isinstance(thread_id, str):
            self._route_to_thread(
                thread_id,
                {"method": f"{method}#auto-declined", "params": params},
            )

    def _route_notification(self, msg: dict) -> None:
        params = msg.get("params") or {}
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            thread = params.get("thread")
            thread_id = thread.get("id") if isinstance(thread, dict) else None
        if isinstance(thread_id, str):
            self._route_to_thread(thread_id, msg)

    def _route_to_thread(self, thread_id: str, msg: dict) -> None:
        if self._output_limit_exceeded:
            return
        with self._state_lock:
            q = self._queues.get(thread_id)
            if q is None:
                self._orphans.append((thread_id, msg))
                return
        q.put(msg)


class CodexThread:
    def __init__(self, server: AppServer, thread_id: str, q: queue.Queue) -> None:
        self._server = server
        self.thread_id = thread_id
        self._queue = q

    def steer(self, text: str, turn_id: str) -> None:
        self._server.request(
            "turn/steer",
            {"threadId": self.thread_id, "expectedTurnId": turn_id, "input": [{"type": "text", "text": text}]},
        )

    def interrupt(self, turn_id: str) -> None:
        self._server.request(
            "turn/interrupt",
            {"threadId": self.thread_id, "turnId": turn_id},
            timeout=_INTERRUPT_GRACE,
        )

    def run_turn(
        self,
        prompt: str,
        *,
        timeout: float,
        on_event: Callable[[dict], None] | None = None,
        on_turn_start: Callable[[str], None] | None = None,
        effort: str | None = None,
    ) -> TurnResult:
        params: dict = {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt}]}
        if effort is not None:
            params["effort"] = effort
        reset_capture = getattr(self._server, "reset_capture", None)
        if reset_capture is not None:
            reset_capture(self.thread_id)
        try:
            result = self._server.request(
                "turn/start",
                params,
            )
        except AppServerError as exc:
            return TurnResult(text="", ok=False, status="failed", thread_id=self.thread_id, detail=str(exc)[:200])
        turn_id = result["turn"]["id"]
        if on_turn_start is not None:
            try:
                on_turn_start(turn_id)
            except Exception:  # noqa: BLE001 - observer must never kill the turn
                pass
        deltas: dict[str, list[str]] = {}
        completed_texts: list[str] = []
        deadline = time.monotonic() + timeout

        completed = self._consume(deadline, turn_id, deltas, completed_texts, on_event)
        if completed is _OUTPUT_LIMIT or self._server_output_limited():
            return self._output_limit_result(deltas, completed_texts, turn_id)
        if completed is not None:
            return self._finish(completed, deltas, completed_texts)

        # Timed out: interrupt, then drain briefly for the interrupted turn/completed.
        try:
            self.interrupt(turn_id)
        except AppServerError:
            pass
        completed = self._consume(time.monotonic() + _INTERRUPT_GRACE, turn_id, deltas, completed_texts, on_event)
        if completed is _OUTPUT_LIMIT or self._server_output_limited():
            return self._output_limit_result(deltas, completed_texts, turn_id)
        salvaged = self._salvage(deltas, completed_texts)
        detail = f"timeout after {timeout}s; interrupted"
        if completed is _DEAD:
            detail = "app-server exited"
        elif completed is not None:
            turn = completed["params"]["turn"]
            if turn.get("status") == "failed":
                detail = ((turn.get("error") or {}).get("message") or detail)[:200]
        return TurnResult(
            text=salvaged,
            ok=False,
            status="interrupted",
            thread_id=self.thread_id,
            detail=detail,
            timed_out=True,
        )

    def _consume(
        self,
        deadline: float,
        turn_id: str,
        deltas: dict[str, list[str]],
        completed_texts: list[str],
        on_event: Callable[[dict], None] | None,
    ):
        """Pump notifications until turn/completed, server death, or deadline.

        Returns the turn/completed message, the _DEAD sentinel, or None on deadline.
        """
        while True:
            if self._server_output_limited():
                return _OUTPUT_LIMIT
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = self._queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if msg is _OUTPUT_LIMIT:
                return _OUTPUT_LIMIT
            if msg is _DEAD:
                return _DEAD
            method = msg.get("method", "")
            params = msg.get("params") or {}
            if method in _DELTA_METHODS:
                item_id = params.get("itemId")
                delta = params.get("delta")
                if method == "item/agentMessage/delta" and isinstance(item_id, str) and isinstance(delta, str):
                    if not self._retain_turn_text(delta, deltas, item_id):
                        return _OUTPUT_LIMIT
                continue
            if on_event is not None:
                try:
                    on_event(msg)
                except Exception:  # noqa: BLE001 - observer must never kill the turn
                    pass
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    if not self._retain_completed_text(item["text"], completed_texts):
                        return _OUTPUT_LIMIT
            elif method == "turn/completed" and (params.get("turn") or {}).get("id") == turn_id:
                return msg

    def _server_output_limited(self) -> bool:
        if getattr(self._server, "_output_limit_exceeded", False):
            return True
        budget = getattr(self._server, "capture_budget", None)
        if budget is None:
            return False
        return bool(budget(self.thread_id).overflowed)

    def _retain_turn_text(self, delta: str, deltas: dict[str, list[str]], item_id: str) -> bool:
        """Append a delta under the shared capture cap. False means overflow."""

        raw = delta.encode("utf-8")
        taken = self._server.capture_budget(self.thread_id).accept(len(raw))
        if taken <= 0:
            self._server._signal_output_limit()
            return False
        if taken < len(raw):
            deltas.setdefault(item_id, []).append(proc_mod.bound_text(delta, taken))
            self._server._signal_output_limit()
            return False
        deltas.setdefault(item_id, []).append(delta)
        return True

    def _retain_completed_text(self, text: str, completed_texts: list[str]) -> bool:
        raw = text.encode("utf-8")
        taken = self._server.capture_budget(self.thread_id).accept(len(raw))
        if taken <= 0:
            self._server._signal_output_limit()
            return False
        if taken < len(raw):
            completed_texts.append(proc_mod.bound_text(text, taken))
            self._server._signal_output_limit()
            return False
        completed_texts.append(text)
        return True

    def _completion_observed(self, turn_id: str) -> bool:
        """#1200: salvage signal requires an observed turn/completed with status completed."""

        getter = getattr(self._server, "observed_completion_status", None)
        if not callable(getter):
            return False
        return getter(self.thread_id, turn_id) == "completed"

    def _output_limit_result(self, deltas: dict, completed_texts: list[str], turn_id: str = "") -> TurnResult:
        return TurnResult(
            text=proc_mod.bound_text(self._salvage(deltas, completed_texts)),
            ok=False,
            status="failed",
            thread_id=self.thread_id,
            detail=f"combined output exceeded {proc_mod.MAX_CAPTURE_BYTES} byte limit"[:200],
            output_limit_exceeded=True,
            completed_observed=bool(turn_id) and self._completion_observed(turn_id),
        )

    def _finish(self, completed, deltas: dict, completed_texts: list[str]) -> TurnResult:
        if completed is _DEAD:
            return TurnResult(
                text=self._salvage(deltas, completed_texts),
                ok=False,
                status="failed",
                thread_id=self.thread_id,
                detail="app-server exited",
            )
        turn = completed["params"]["turn"]
        status = turn.get("status")
        agent_texts = [
            item.get("text", "")
            for item in turn.get("items", [])
            if isinstance(item, dict) and item.get("type") == "agentMessage"
        ]
        text = (agent_texts[-1] if agent_texts else "") or self._salvage(deltas, completed_texts)
        if len(text.encode("utf-8")) > proc_mod.MAX_CAPTURE_BYTES:
            return TurnResult(
                text=proc_mod.bound_text(text),
                ok=False,
                status="failed",
                thread_id=self.thread_id,
                detail=f"combined output exceeded {proc_mod.MAX_CAPTURE_BYTES} byte limit"[:200],
                output_limit_exceeded=True,
                # #1200: only a genuinely completed turn is a salvage signal.
                completed_observed=status == "completed",
            )
        if status == "completed":
            return TurnResult(
                text=text,
                ok=bool(text),
                status="complete",
                thread_id=self.thread_id,
                detail="" if text else "empty output",
            )
        if status == "interrupted":
            return TurnResult(
                text=text, ok=False, status="interrupted", thread_id=self.thread_id, detail="turn interrupted"
            )
        detail = ((turn.get("error") or {}).get("message") or f"turn status: {status}")[:200]
        return TurnResult(text=text, ok=False, status="failed", thread_id=self.thread_id, detail=detail)

    def _salvage(self, deltas: dict[str, list[str]], completed_texts: list[str]) -> str:
        if completed_texts:
            return completed_texts[-1]
        if deltas:
            last_item = list(deltas)[-1]
            return "".join(deltas[last_item])
        return ""
