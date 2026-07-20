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
_DEAD = object()  # queue sentinel: server process is gone

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


class AppServer:
    """One `codex app-server` child; thread-safe for concurrent CodexThread turns."""

    def __init__(
        self,
        argv: list[str] | None = None,
        cwd: Path | None = None,
        process_registry: proc_mod.ProcessRegistry | None = None,
    ) -> None:
        self._argv = argv or ["codex", "app-server"]
        self._cwd = cwd
        self._process_registry = process_registry or proc_mod.ProcessRegistry()
        self._proc: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict] = {}  # id -> {"event": Event, "response": msg}
        self._queues: dict[str, queue.Queue] = {}
        self._orphans: deque[tuple[str, dict]] = deque(maxlen=_ORPHAN_LIMIT)
        self._dead = False

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
            self._proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd=self._cwd,
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
            if self._dead:
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

    def _attach(self, thread_id: str) -> "CodexThread":
        q: queue.Queue = queue.Queue()
        with self._state_lock:
            self._queues[thread_id] = q
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

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") is not None and "method" in msg:
                self._handle_server_request(msg)
            elif msg.get("id") is not None:
                self._handle_response(msg)
            elif "method" in msg:
                self._route_notification(msg)
        with self._state_lock:
            self._dead = True
            pending = list(self._pending.values())
            self._pending.clear()
            queues = list(self._queues.values())
        for entry in pending:
            entry["event"].set()
        for q in queues:
            q.put(_DEAD)

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
        if completed is not None:
            return self._finish(completed, deltas, completed_texts)

        # Timed out: interrupt, then drain briefly for the interrupted turn/completed.
        try:
            self.interrupt(turn_id)
        except AppServerError:
            pass
        completed = self._consume(time.monotonic() + _INTERRUPT_GRACE, turn_id, deltas, completed_texts, on_event)
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = self._queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if msg is _DEAD:
                return _DEAD
            method = msg.get("method", "")
            params = msg.get("params") or {}
            if method in _DELTA_METHODS:
                item_id = params.get("itemId")
                delta = params.get("delta")
                if method == "item/agentMessage/delta" and isinstance(item_id, str) and isinstance(delta, str):
                    deltas.setdefault(item_id, []).append(delta)
                continue
            if on_event is not None:
                try:
                    on_event(msg)
                except Exception:  # noqa: BLE001 - observer must never kill the turn
                    pass
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    completed_texts.append(item["text"])
            elif method == "turn/completed" and (params.get("turn") or {}).get("id") == turn_id:
                return msg

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
