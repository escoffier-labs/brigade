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

# #1200 round 4: completion is recorded only for records json.loads parsed
# successfully; an oversized raw record (one the reader cannot hand to
# json.loads within MAX_CAPTURE_BYTES) never signals completion. The map of
# observed completions is bounded so interleaved turns cannot grow it forever.
_MAX_OBSERVED_COMPLETIONS = 256


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
    output_bytes: int = 0
    output_cap_bytes: int = 0


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
        self._stream_budget = proc_mod.ByteBudget(proc_mod.MAX_STREAM_BYTES)
        self._output_limit_exceeded = False
        # #1200: (thread id, turn id) -> status for every parsed turn/completed
        # whose result has not been built yet. Keyed per turn so interleaved
        # turns cannot erase each other's completions.
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

        with self._state_lock:
            return self._observed_completions.get((thread_id, turn_id))

    def consume_observed_completion(self, thread_id: str, turn_id: str) -> str | None:
        """#1200: atomically read-and-remove exactly this turn's entry."""

        with self._state_lock:
            return self._observed_completions.pop((thread_id, turn_id), None)

    def _note_turn_completed(self, thread_id: Any, turn: Any) -> None:
        if not isinstance(turn, dict):
            return
        turn_id = turn.get("id")
        status = turn.get("status")
        if isinstance(thread_id, str) and isinstance(turn_id, str):
            with self._state_lock:
                key = (thread_id, turn_id)
                self._observed_completions.pop(key, None)
                self._observed_completions[key] = status if isinstance(status, str) else ""
                while len(self._observed_completions) > _MAX_OBSERVED_COMPLETIONS:
                    del self._observed_completions[next(iter(self._observed_completions))]

    def reset_capture(self, thread_id: str) -> None:
        self._capture_budgets[thread_id] = proc_mod.ByteBudget()
        with self._state_lock:
            stale = [key for key in self._observed_completions if key[0] == thread_id]
            for key in stale:
                del self._observed_completions[key]

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

    def _trip_output_limit(self, line_bytes: int) -> None:
        """Stop the child after its untrusted transport stream reaches the ceiling."""

        self._stream_budget.observe(line_bytes)
        self._signal_output_limit()

    def _ingest_line(self, raw: bytes) -> bool:
        """Parse one JSONL record under the independent transport ceiling.

        A record larger than the ceiling is rejected before ``json.loads`` so it
        is never fully buffered (#1108), and never signals completion (#1200).
        A record that merely tips the accumulated ceiling is still parsed first,
        so a final ``turn/completed`` is recorded before the child is stopped.
        """

        if self._output_limit_exceeded:
            return False
        stripped = raw.strip()
        if not stripped:
            return True
        line_bytes = len(stripped)
        if line_bytes > proc_mod.MAX_STREAM_BYTES:
            self._trip_output_limit(line_bytes)
            return False
        within_ceiling = self._stream_budget.observe(line_bytes)
        try:
            msg = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            msg = None
        if not isinstance(msg, dict):
            if not within_ceiling:
                self._signal_output_limit()
                return False
            return True
        thread_id = _message_thread_id(msg)
        if msg.get("method") == "turn/completed":
            # #1200: record the completion before publishing the limit signal so
            # a turn waiting on the limit never wakes to find its entry absent.
            self._note_turn_completed(thread_id, (msg.get("params") or {}).get("turn"))
        if not within_ceiling:
            self._signal_output_limit()
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
        limit = proc_mod.MAX_STREAM_BYTES
        chunk_size = _READ_CHUNK_BYTES
        while not self._output_limit_exceeded:
            newline_at = leftover.find(b"\n")
            if newline_at < 0:
                if len(leftover) > limit:
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
                payload_bytes = len(json.dumps(msg).encode("utf-8"))
                if self._orphan_budget.try_add(payload_bytes):
                    self._orphans.append((thread_id, msg))
                return
        q.put(msg)


class CodexThread:
    def __init__(self, server: AppServer, thread_id: str, q: queue.Queue) -> None:
        self._server = server
        self.thread_id = thread_id
        self._queue = q
        # #1144: once the retention budget is spent, later agent text rolls
        # through one shared bounded tail for the whole turn. Per-item buffers
        # would let N item ids each retain a full cap's worth of bytes, which
        # is the memory exhaustion #1108 closed.
        self._tail_chunks: deque[str] = deque()
        self._tail_bytes = 0

    def _reset_tail(self) -> None:
        self._tail_chunks.clear()
        self._tail_bytes = 0

    def _append_tail(self, text: str) -> None:
        """Roll ``text`` into the shared tail, evicting oldest chunks. O(1) amortized."""

        if not text:
            return
        limit = proc_mod.MAX_CAPTURE_BYTES // 2
        self._tail_chunks.append(text)
        self._tail_bytes += len(text.encode("utf-8"))
        while self._tail_bytes > limit and len(self._tail_chunks) > 1:
            self._tail_bytes -= len(self._tail_chunks.popleft().encode("utf-8"))
        if self._tail_bytes > limit:
            only = self._tail_chunks.pop()
            trimmed = proc_mod.bound_text_tail(only, limit)
            self._tail_chunks.append(trimmed)
            self._tail_bytes = len(trimmed.encode("utf-8"))

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
        self._reset_tail()
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
        return bool(getattr(self._server, "_output_limit_exceeded", False))

    def _retain_turn_text(self, delta: str, deltas: dict[str, list[str]], item_id: str) -> bool:
        """Append a delta under the shared retention cap without stopping the turn."""

        raw = delta.encode("utf-8")
        taken = self._server.capture_budget(self.thread_id).accept(len(raw))
        if taken < len(raw):
            # Budget exhausted mid-delta. Retain the prefix the budget was
            # charged for, then roll the remainder through the shared tail; the
            # final answer lives at the end of the stream and must survive.
            chunks = deltas.setdefault(item_id, [])
            if taken > 0:
                chunks.append(raw[:taken].decode("utf-8", errors="ignore"))
                self._append_tail(raw[taken:].decode("utf-8", errors="ignore"))
            else:
                self._append_tail(delta)
            return True
        deltas.setdefault(item_id, []).append(delta)
        return True

    def _retain_completed_text(self, text: str, completed_texts: list[str]) -> bool:
        raw = text.encode("utf-8")
        taken = self._server.capture_budget(self.thread_id).accept(len(raw))
        if taken < len(raw):
            self._append_tail(text)
            return True
        completed_texts.append(text)
        return True

    def _output_limit_result(self, deltas: dict, completed_texts: list[str], turn_id: str = "") -> TurnResult:
        # #1200: one atomic read-and-remove of this exact turn's entry, so a
        # sibling completion arriving between lookup and consume is never lost.
        consume = getattr(self._server, "consume_observed_completion", None)
        observed = None
        if turn_id and callable(consume):
            observed = consume(self.thread_id, turn_id)
        stream_budget = getattr(self._server, "_stream_budget", None)
        stream_observed = (
            stream_budget.observed
            if stream_budget is not None
            else self._server.capture_budget(self.thread_id).observed
        )
        return TurnResult(
            text=proc_mod.bound_text_ends(self._salvage(deltas, completed_texts)),
            ok=False,
            status="failed",
            thread_id=self.thread_id,
            detail=f"combined output exceeded {proc_mod.MAX_STREAM_BYTES} stream byte limit"[:200],
            output_limit_exceeded=True,
            completed_observed=observed == "completed",
            # This path is reached because the *stream* ceiling tripped, so the
            # receipt must report the stream volume against the stream ceiling.
            # Reporting the per-thread retention budget here made the run record
            # describe a limit that was not the one that fired.
            output_bytes=stream_observed,
            output_cap_bytes=proc_mod.MAX_STREAM_BYTES,
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
        budget = self._server.capture_budget(self.thread_id)
        truncated = budget.overflowed or len(text.encode("utf-8")) > proc_mod.MAX_CAPTURE_BYTES
        if len(text.encode("utf-8")) > proc_mod.MAX_CAPTURE_BYTES:
            text = proc_mod.bound_text_ends(text)
        truncation_detail = (
            f"combined output exceeded {proc_mod.MAX_CAPTURE_BYTES} byte capture limit; output truncated"
        )
        if status == "completed":
            # #1144: a turn that genuinely completed stays ok even when its text
            # was truncated. Truncation is a capture artifact, not a failed turn.
            return TurnResult(
                text=text,
                ok=bool(text),
                status="complete",
                thread_id=self.thread_id,
                detail=(truncation_detail if truncated else "")[:200],
                output_limit_exceeded=truncated,
                completed_observed=True,
                output_bytes=budget.observed,
                output_cap_bytes=proc_mod.MAX_CAPTURE_BYTES,
            )
        if status == "interrupted":
            return TurnResult(
                text=text,
                ok=False,
                status="interrupted",
                thread_id=self.thread_id,
                detail="turn interrupted",
                output_limit_exceeded=truncated,
                output_bytes=budget.observed,
                output_cap_bytes=proc_mod.MAX_CAPTURE_BYTES,
            )
        detail = ((turn.get("error") or {}).get("message") or f"turn status: {status}")[:200]
        return TurnResult(
            text=text,
            ok=False,
            status="failed",
            thread_id=self.thread_id,
            detail=detail,
            output_limit_exceeded=truncated,
            output_bytes=budget.observed,
            output_cap_bytes=proc_mod.MAX_CAPTURE_BYTES,
        )

    def _salvage(self, deltas: dict[str, list[str]], completed_texts: list[str]) -> str:
        head = ""
        if completed_texts:
            head = completed_texts[-1]
        elif deltas:
            last_item = list(deltas)[-1]
            head = "".join(deltas[last_item])
        if not self._tail_chunks:
            return head
        # #1144: head + tail, so a truncated turn still carries its final text.
        tail = "".join(self._tail_chunks)
        if not head:
            return tail
        return proc_mod.bound_text(head, proc_mod.MAX_CAPTURE_BYTES // 2) + proc_mod.TRUNCATION_MARKER + tail
