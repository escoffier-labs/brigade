"""Fixed stdio MCP launcher for the validated Excalidraw helper."""

from __future__ import annotations

import json
import os
import select
import signal
import stat
import subprocess
import time
from typing import Any, Mapping, NoReturn

from .adapters import allowlisted_excalidraw_env
from .contracts import ERROR_MESSAGES, ObsidianError
from .runtime_config import (
    descriptor_execution_available,
    open_validated_executable_fd,
    validate_regular_executable,
)

CLIENT_NAME = "grokbot-obsidian-operator"
CLIENT_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"
DEADLINE_SECONDS = 45
MAX_OUTPUT_BYTES = 262_144
MAX_RPC_ID = 1_000_000
EXCALIDRAW_MCP_TOOLS = frozenset({"start_session", "create_diagram", "add_elements", "export_diagram"})


def _unavailable() -> NoReturn:
    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])


def _protocol() -> NoReturn:
    raise ObsidianError("protocol_error", ERROR_MESSAGES["protocol_error"])


def _timeout() -> NoReturn:
    raise ObsidianError("timeout", ERROR_MESSAGES["timeout"])


class StdioExcalidrawMcpClient:
    def __init__(self, *, executable: str, staging_dir: str, env: Mapping[str, str]):
        if not isinstance(staging_dir, str) or not staging_dir.startswith("/") or "\0" in staging_dir:
            _unavailable()
        fd = -1
        try:
            helper = validate_regular_executable(executable)
            if not descriptor_execution_available():
                _unavailable()
            fd = open_validated_executable_fd(helper)
            info = os.fstat(fd)
            current = os.lstat(helper)
            if (
                not stat.S_ISREG(info.st_mode)
                or (info.st_mode & 0o111) == 0
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
            ):
                _unavailable()
        except ObsidianError as exc:
            if fd != -1:
                os.close(fd)
            raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
        except OSError as exc:
            if fd != -1:
                os.close(fd)
            raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
        self._closed = False
        self._ready = False
        self._next_id = 1
        self._read_bytes = 0
        self._stdout_buf = b""
        self._deadline = time.monotonic() + DEADLINE_SECONDS
        self._proc: subprocess.Popen[bytes] | None = None
        self._pgid: int | None = None
        try:
            self._proc = subprocess.Popen(
                [f"/proc/self/fd/{fd}"],
                cwd=staging_dir,
                env=allowlisted_excalidraw_env(env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                close_fds=True,
                pass_fds=(fd,),
            )
        except OSError as exc:
            raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
        finally:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            self._pgid = os.getpgid(self._proc.pid)
        except OSError:
            self._pgid = None

    @property
    def pid(self) -> int | None:
        return None if self._proc is None else self._proc.pid

    def _remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if self._pgid is not None:
            try:
                os.killpg(self._pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        self._proc = None

    def close(self) -> None:
        self._closed = True
        self._ready = False
        self._kill()

    def _account(self, data: bytes) -> None:
        self._read_bytes += len(data)
        if self._read_bytes > MAX_OUTPUT_BYTES:
            self.close()
            _unavailable()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            ready, _, _ = select.select([proc.stderr], [], [], 0)
        except (OSError, ValueError):
            return
        if not ready:
            return
        try:
            extra = os.read(proc.stderr.fileno(), 4096)
        except OSError:
            return
        if extra:
            self._account(extra)

    def _read_line(self) -> str:
        proc = self._proc
        if proc is None or proc.stdout is None:
            _unavailable()
        while b"\n" not in self._stdout_buf:
            if self._remaining() <= 0:
                self.close()
                _timeout()
            self._drain_stderr()
            try:
                ready, _, _ = select.select([proc.stdout], [], [], min(0.05, self._remaining()))
            except (OSError, ValueError) as exc:
                raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
            if not ready:
                if proc.poll() is not None:
                    _unavailable()
                continue
            try:
                chunk = os.read(proc.stdout.fileno(), 4096)
            except OSError as exc:
                raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
            if not chunk:
                if proc.poll() is not None:
                    _unavailable()
                continue
            self._account(chunk)
            self._stdout_buf += chunk
        line, self._stdout_buf = self._stdout_buf.split(b"\n", 1)
        try:
            return line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc

    def _send(self, payload: Mapping[str, Any]) -> None:
        proc = self._proc
        if self._closed or proc is None or proc.stdin is None:
            _unavailable()
        if self._remaining() <= 0:
            self.close()
            _timeout()
        try:
            proc.stdin.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
            proc.stdin.flush()
        except OSError as exc:
            raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc

    def _rpc(self, method: str, params: Mapping[str, Any] | None = None, *, notification: bool = False) -> object:
        if notification:
            payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = dict(params)
            self._send(payload)
            return None
        if not 1 <= self._next_id <= MAX_RPC_ID:
            _unavailable()
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})})
        while True:
            try:
                raw = self._read_line()
            except ObsidianError:
                raise
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                _unavailable()
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if message.get("error") is not None:
                _unavailable()
            if "result" not in message:
                _protocol()
            return message["result"]

    def _ensure_session(self) -> None:
        if self._ready:
            return
        initialized = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        if not isinstance(initialized, dict) or not initialized.get("protocolVersion"):
            _protocol()
        self._rpc("notifications/initialized", notification=True)
        self._ready = True

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> object:
        if self._closed:
            _unavailable()
        if name not in EXCALIDRAW_MCP_TOOLS:
            _protocol()
        self._ensure_session()
        return self._rpc("tools/call", {"name": name, "arguments": dict(arguments or {})})


def create_excalidraw_stdio_client(
    *,
    executable: str,
    staging_dir: str,
    env: Mapping[str, str],
) -> StdioExcalidrawMcpClient:
    return StdioExcalidrawMcpClient(executable=executable, staging_dir=staging_dir, env=env)
