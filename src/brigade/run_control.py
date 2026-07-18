"""Live control socket for app-server Brigade runs."""

from __future__ import annotations

import hmac
import json
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CLIENT_TIMEOUT_SECONDS = 5.0
NO_ACTIVE_TURN = "no-active-turn"
_NO_ACTIVE_TURN_RETRY_SECONDS = 5.0
_NO_ACTIVE_TURN_RETRY_INTERVAL = 0.1
_LIVE_RUN_STATUSES = frozenset({"started", "dispatching"})
_TRANSPORT_SCHEMA = "brigade.run_control_transport.v1"
_LOOPBACK_HOST = "127.0.0.1"
_SUPPORTED_TRANSPORT_KINDS = frozenset({"unix", "loopback-tcp"})


class ControlError(RuntimeError):
    """Control socket setup, request, or operation failure."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ControlTransport:
    """Typed local transport descriptor for run control clients."""

    kind: str
    path: str | None = None
    port: int | None = None
    owner_token: str | None = None

    def to_metadata(self) -> dict[str, object]:
        payload: dict[str, object] = {"schema": _TRANSPORT_SCHEMA, "kind": self.kind}
        if self.kind == "unix":
            if not self.path:
                raise ControlError("unix transport missing path")
            payload["path"] = self.path
        elif self.kind == "loopback-tcp":
            if self.port is None or not self.owner_token:
                raise ControlError("loopback-tcp transport missing port or owner_token")
            payload["host"] = _LOOPBACK_HOST
            payload["port"] = self.port
            payload["owner_token"] = self.owner_token
        else:
            raise ControlError(f"unsupported control transport kind: {self.kind}")
        return payload

    @classmethod
    def from_metadata(cls, value: object) -> ControlTransport:
        if not isinstance(value, dict):
            raise ControlError("control_transport must be a JSON object")
        if value.get("schema") != _TRANSPORT_SCHEMA:
            raise ControlError("unsupported control_transport schema")
        kind = value.get("kind")
        if kind not in _SUPPORTED_TRANSPORT_KINDS:
            raise ControlError(f"unsupported control_transport kind: {kind!r}")
        if kind == "unix":
            path = value.get("path")
            if not isinstance(path, str) or not path:
                raise ControlError("unix control_transport requires path")
            return cls(kind="unix", path=path)
        host = value.get("host")
        if host not in (None, _LOOPBACK_HOST, "127.0.0.1"):
            raise ControlError("loopback-tcp control_transport must bind loopback only")
        port = value.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port <= 65535:
            raise ControlError("loopback-tcp control_transport requires valid port")
        owner_token = value.get("owner_token")
        if not isinstance(owner_token, str) or not owner_token:
            raise ControlError("loopback-tcp control_transport requires owner_token")
        return cls(kind="loopback-tcp", port=port, owner_token=owner_token)


@dataclass(frozen=True)
class ActiveTurn:
    worker: str
    thread: Any
    thread_id: str
    turn_id: str


class LiveTurnRegistry:
    """Thread-safe registry of worker names to their current app-server turn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, ActiveTurn] = {}

    def register(self, worker: str, thread: Any, turn_id: str) -> None:
        thread_id = getattr(thread, "thread_id", "")
        with self._lock:
            self._active[worker] = ActiveTurn(worker=worker, thread=thread, thread_id=thread_id, turn_id=turn_id)

    def unregister(self, worker: str, turn_id: str) -> None:
        with self._lock:
            active = self._active.get(worker)
            if active is not None and active.turn_id == turn_id:
                self._active.pop(worker, None)

    def steer(self, worker: str, text: str) -> dict[str, object]:
        if not text.strip():
            raise ControlError("steer text must not be empty")
        active = self._require_worker(worker)
        active.thread.steer(text, active.turn_id)
        return {
            "ok": True,
            "worker": active.worker,
            "thread_id": active.thread_id,
            "turn_id": active.turn_id,
        }

    def interrupt(self, worker: str | None = None) -> dict[str, object]:
        turns = self._turns(worker)
        if not turns:
            target = f" for worker {worker!r}" if worker else ""
            raise ControlError(f"no active turn{target}", code=NO_ACTIVE_TURN)
        interrupted: list[str] = []
        for active in turns:
            active.thread.interrupt(active.turn_id)
            interrupted.append(active.worker)
        return {"ok": True, "interrupted": len(interrupted), "workers": interrupted}

    def _require_worker(self, worker: str) -> ActiveTurn:
        with self._lock:
            active = self._active.get(worker)
        if active is None:
            raise ControlError(f"no active turn for worker {worker!r}", code=NO_ACTIVE_TURN)
        return active

    def _turns(self, worker: str | None) -> list[ActiveTurn]:
        with self._lock:
            if worker:
                active = self._active.get(worker)
                return [active] if active is not None else []
            return list(self._active.values())


def _af_unix_available() -> bool:
    return hasattr(socket, "AF_UNIX")


def _unix_path_limit() -> int:
    if sys.platform == "darwin":
        return 104
    return 107


def _unix_path_supported(path: Path) -> bool:
    if not _af_unix_available():
        return False
    encoded = str(path.expanduser()).encode("utf-8", errors="surrogateescape")
    return len(encoded) <= _unix_path_limit()


def plan_control_transport(unix_path: Path) -> str:
    """Return the transport kind to use for a preferred Unix socket path."""
    if _unix_path_supported(unix_path):
        return "unix"
    return "loopback-tcp"


class ControlServer:
    """Newline-delimited JSON control server over a local authenticated transport."""

    def __init__(self, path: Path, registry: LiveTurnRegistry) -> None:
        self.path = path.expanduser()
        self.registry = registry
        self.transport: ControlTransport | None = None
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_threads: list[threading.Thread] = []

    def start(self) -> ControlTransport:
        if self._thread is not None:
            assert self.transport is not None
            return self.transport
        kind = plan_control_transport(self.path)
        if kind == "unix":
            transport = self._start_unix()
        else:
            transport = self._start_loopback_tcp()
        self.transport = transport
        return transport

    def _start_unix(self) -> ControlTransport:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            sock.bind(str(self.path))
            bound = True
            sock.listen()
            sock.settimeout(0.1)
            transport = ControlTransport(kind="unix", path=str(self.path))
            thread = threading.Thread(target=self._serve, name="brigade-run-control", daemon=True)
            self._sock = sock
            self._thread = thread
            self.transport = transport
            thread.start()
            return transport
        except (OSError, RuntimeError) as exc:
            self._sock = None
            self._thread = None
            self.transport = None
            try:
                sock.close()
            except OSError:
                pass
            if bound:
                try:
                    self.path.unlink()
                except OSError:
                    pass
            raise ControlError(f"failed to start control socket: {exc}") from exc

    def _start_loopback_tcp(self) -> ControlTransport:
        owner_token = secrets.token_hex(16)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bound = False
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((_LOOPBACK_HOST, 0))
            bound = True
            port = sock.getsockname()[1]
            sock.listen()
            sock.settimeout(0.1)
            transport = ControlTransport(kind="loopback-tcp", port=port, owner_token=owner_token)
            thread = threading.Thread(target=self._serve, name="brigade-run-control", daemon=True)
            self._sock = sock
            self._thread = thread
            self.transport = transport
            thread.start()
            return transport
        except (OSError, RuntimeError) as exc:
            self._sock = None
            self._thread = None
            self.transport = None
            try:
                sock.close()
            except OSError:
                pass
            if bound:
                pass
            raise ControlError(f"failed to start control transport: {exc}") from exc

    def close(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            sock.close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        for client in list(self._client_threads):
            client.join(timeout=0.2)
        if self.transport is not None and self.transport.kind == "unix":
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.transport = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            self._client_threads.append(thread)
            thread.start()

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            try:
                fh = conn.makefile("rwb")
            except OSError:
                return
            with fh:
                for raw in fh:
                    try:
                        request = json.loads(raw.decode())
                        if not isinstance(request, dict):
                            raise ControlError("request must be a JSON object")
                        response = self._dispatch(request)
                    except json.JSONDecodeError as exc:
                        response = {"ok": False, "error": f"invalid JSON: {exc.msg}"}
                    except ControlError as exc:
                        response = {"ok": False, "error": str(exc)}
                        if exc.code is not None:
                            response["code"] = exc.code
                    except Exception as exc:  # noqa: BLE001 - control server boundary
                        response = {"ok": False, "error": str(exc)}
                    fh.write(json.dumps(response, sort_keys=True).encode() + b"\n")
                    fh.flush()

    def _dispatch(self, request: dict[str, object]) -> dict[str, object]:
        transport = self.transport
        if transport is not None and transport.kind == "loopback-tcp":
            token = request.get("owner_token")
            expected = transport.owner_token
            if not isinstance(token, str) or expected is None or not hmac.compare_digest(token, expected):
                raise ControlError("control request denied", code="auth-denied")
        op = request.get("op")
        if op == "steer":
            worker = request.get("worker")
            text = request.get("text")
            if not isinstance(worker, str) or not worker:
                raise ControlError("steer requires worker")
            if not isinstance(text, str):
                raise ControlError("steer requires text")
            return self.registry.steer(worker, text)
        if op == "interrupt":
            worker = request.get("worker")
            if worker is not None and (not isinstance(worker, str) or not worker):
                raise ControlError("interrupt worker must be a non-empty string")
            return self.registry.interrupt(worker)
        raise ControlError(f"unknown control op: {op}")


def _connect(transport: ControlTransport, *, timeout: float) -> socket.socket:
    if transport.kind == "unix":
        if not transport.path:
            raise ControlError("unix control transport is missing path")
        path = Path(transport.path).expanduser()
        if not path.exists():
            raise ControlError(f"control socket is not active: {path}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
        except OSError as exc:
            sock.close()
            raise ControlError(f"control socket request failed: {exc}") from exc
        return sock
    if transport.kind == "loopback-tcp":
        if transport.port is None:
            raise ControlError("loopback-tcp control transport is missing port")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((_LOOPBACK_HOST, transport.port))
        except OSError as exc:
            sock.close()
            raise ControlError(f"control transport request failed: {exc}") from exc
        return sock
    raise ControlError(f"unsupported control transport kind: {transport.kind}")


def send_request(
    transport: ControlTransport,
    payload: dict[str, object],
    *,
    timeout: float = _CLIENT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request = dict(payload)
    if transport.kind == "loopback-tcp":
        if not transport.owner_token:
            raise ControlError("loopback-tcp control transport is missing owner_token")
        request["owner_token"] = transport.owner_token
    sock = _connect(transport, timeout=timeout)
    try:
        with sock, sock.makefile("rwb") as fh:
            fh.write(json.dumps(request, sort_keys=True).encode() + b"\n")
            fh.flush()
            raw = fh.readline()
    except OSError as exc:
        raise ControlError(f"control transport request failed: {exc}") from exc
    if not raw:
        raise ControlError("control transport closed without a response")
    try:
        response = json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        raise ControlError(f"control transport returned invalid JSON: {exc}") from exc
    if not isinstance(response, dict):
        raise ControlError("control transport returned a non-object response")
    return response


def send_request_with_retry(
    run_dir: Path,
    transport: ControlTransport,
    payload: dict[str, object],
    *,
    retry_seconds: float = _NO_ACTIVE_TURN_RETRY_SECONDS,
    retry_interval: float = _NO_ACTIVE_TURN_RETRY_INTERVAL,
) -> dict[str, Any]:
    """Send a control request, retrying while the live run has no active turn.

    The control socket exists from the moment a run starts dispatching, before
    any worker turn has registered, and workers are briefly unregistered
    between turns. A steer or interrupt landing in that window would fail with
    "no active turn" even though a turn is about to start, so retry until the
    run leaves a live status or the window closes.
    """
    deadline = time.monotonic() + retry_seconds
    while True:
        response = send_request(transport, payload)
        if response.get("ok") is True or response.get("code") != NO_ACTIVE_TURN:
            return response
        if time.monotonic() >= deadline or not _run_is_live(run_dir):
            return response
        time.sleep(retry_interval)


def _run_is_live(run_dir: Path) -> bool:
    try:
        meta = json.loads((run_dir / "run.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(meta, dict) and meta.get("status") in _LIVE_RUN_STATUSES


def control_transport_from_run(run_dir: Path) -> ControlTransport:
    run_json = run_dir / "run.json"
    try:
        meta = json.loads(run_json.read_text())
    except FileNotFoundError as exc:
        raise ControlError(f"run.json not found in {run_dir}") from exc
    except json.JSONDecodeError as exc:
        raise ControlError(f"run.json is not valid JSON: {exc}") from exc
    if not isinstance(meta, dict):
        raise ControlError("run.json must contain a JSON object")
    transport_value = meta.get("control_transport")
    if transport_value is not None:
        return ControlTransport.from_metadata(transport_value)
    socket_value = meta.get("control_socket")
    if isinstance(socket_value, str) and socket_value:
        return ControlTransport(kind="unix", path=socket_value)
    if meta.get("codex_transport") != "app-server":
        raise ControlError("run was not started with app-server transport")
    raise ControlError("run does not record a control transport")


def control_socket_from_run(run_dir: Path) -> Path:
    transport = control_transport_from_run(run_dir)
    if transport.kind != "unix" or not transport.path:
        raise ControlError("run does not record a unix control socket")
    return Path(transport.path)


def print_control_response(response: dict[str, object], *, op: str) -> int:
    if response.get("ok") is not True:
        print(f"error: {response.get('error') or 'control request failed'}", file=sys.stderr)
        return 1
    if op == "steer":
        print(f"steer: {response.get('worker')} turn={response.get('turn_id')}")
    elif op == "interrupt":
        workers = response.get("workers")
        worker_text = ", ".join(str(worker) for worker in workers) if isinstance(workers, list) else ""
        print(f"interrupt: {response.get('interrupted', 0)}" + (f" ({worker_text})" if worker_text else ""))
    return 0
