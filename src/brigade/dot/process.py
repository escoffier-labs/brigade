"""Bounded subprocess capture and credential-safe results ported from the bridge."""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Mapping

from .. import proc
from .contracts import DotError, encoded_size, require
from .runtime_config import BinPaths
from .safe_state import redact_credentials

MAX_RESULT_BYTES = 65536
MAX_CHILD_STDIN = 65536
ENV_PASSTHROUGH = (
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "SSH_AUTH_SOCK",
    "DBUS_SESSION_BUS_ADDRESS",
    "T3_FLEET_CONFIG",
    "T3_FLEET_STATE",
)


def child_path(runtime_bin: BinPaths) -> str:
    """Configured binary directories in field order, followed by system paths."""
    directories = [
        str(Path(value).parent)
        for field in fields(runtime_bin)
        if (value := getattr(runtime_bin, field.name)) is not None
    ]
    return os.pathsep.join(dict.fromkeys([*directories, *_system_directories()]))


def _system_directories() -> list[str]:
    if sys.platform == "win32":
        root = Path(os.environ.get("SystemRoot", "C:/Windows"))
        return [str(root / "System32"), str(root)]
    return ["/usr/bin", "/bin"]


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: bytes
    timed_out: bool = False
    overflow: bool = False


def child_environment(path: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal explicit child environment; PATH is fixed, nothing else is inherited."""
    keys = ENV_PASSTHROUGH + (("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if sys.platform == "win32" else ())
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env["PATH"] = path
    env.setdefault("LANG", "C.UTF-8")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["NO_COLOR"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


class CommandRunner:
    """Fixed argv, shell=False, cwd=/, stderr discarded, bounded stdout and time.

    The child runs in its own session so a timeout or overflow kills the whole
    process group it started (for example an ssh transport), never anything else.
    A non-zero exit is returned, not raised: the fleet CLI reports its closed
    error envelope on stdout with a non-zero status.
    """

    def run(
        self,
        argv: list[str],
        *,
        stdin: bytes = b"",
        timeout: float,
        max_output: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        require(
            isinstance(argv, list) and bool(argv) and all(isinstance(item, str) and "\x00" not in item for item in argv)
        )
        require(isinstance(stdin, bytes) and len(stdin) <= MAX_CHILD_STDIN)
        require(isinstance(timeout, (int, float)) and 0 < timeout <= 600)
        if sys.platform == "win32":
            return _run_windows(argv, stdin=stdin, timeout=timeout, max_output=max_output, env=env)
        try:
            child = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                bufsize=0,
                cwd="/",
                env=child_environment("/usr/bin:/bin") if env is None else env,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise DotError("command_unavailable") from None
        assert child.stdin is not None and child.stdout is not None
        output = bytearray()
        timed_out = overflow = False
        try:
            remaining = memoryview(stdin)
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                os.set_blocking(child.stdout.fileno(), False)
                selector.register(child.stdout, selectors.EVENT_READ)
                if remaining:
                    os.set_blocking(child.stdin.fileno(), False)
                    selector.register(child.stdin, selectors.EVENT_WRITE)
                else:
                    child.stdin.close()
                while selector.get_map() and not overflow:
                    seconds = deadline - time.monotonic()
                    if seconds <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(seconds):
                        if key.fileobj is child.stdin:
                            try:
                                count = os.write(child.stdin.fileno(), remaining[:65536])
                            except BrokenPipeError:
                                count = len(remaining)
                            remaining = remaining[count:]
                            if not remaining:
                                selector.unregister(child.stdin)
                                child.stdin.close()
                        else:
                            chunk = os.read(child.stdout.fileno(), 65536)
                            if not chunk:
                                selector.unregister(child.stdout)
                            else:
                                output.extend(chunk)
                                if len(output) > max_output:
                                    overflow = True
                                    break
            if not timed_out and not overflow:
                try:
                    child.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
        except Exception:
            raise DotError("command_unavailable") from None
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            child.stdin.close()
            child.stdout.close()
        return CommandResult(child.returncode, bytes(output[:max_output]), timed_out, overflow)


def _run_windows(
    argv: list[str], *, stdin: bytes, timeout: float, max_output: int, env: Mapping[str, str] | None
) -> CommandResult:
    """Drain Windows pipes on threads under the existing owned process-tree job.

    A suspended launch is bound before any child code executes. Closing the job
    also reaps descendants after an exited leader, without targeting a reused PID.
    """
    job = proc._create_windows_child_job()
    require(job is not None, "command_unavailable")
    assert job is not None
    try:
        child = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            bufsize=0,
            cwd=Path(sys.executable).anchor,
            env=child_environment(os.pathsep.join(_system_directories())) if env is None else env,
            **proc._process_group_kwargs(suspend=True),
        )
    except BaseException as error:
        # The job is owned here until a child is bound to it; never leak it.
        job.close()
        if isinstance(error, (OSError, ValueError, TypeError)):
            raise DotError("command_unavailable") from None
        raise
    # On binding failure this helper kills the suspended child and closes its job.
    if proc._bind_suspended_windows_child(job, child, argv) is not None:
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()
        raise DotError("command_unavailable")
    assert child.stdin is not None and child.stdout is not None
    child_stdin, child_stdout = child.stdin, child.stdout
    output = bytearray()
    done, overflow, failed = threading.Event(), threading.Event(), threading.Event()

    def read() -> None:
        try:
            while chunk := child_stdout.read(65536):
                remaining = max_output - len(output)
                output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    break
        except OSError:
            failed.set()
        finally:
            done.set()

    def write() -> None:
        try:
            remaining = memoryview(stdin)
            while remaining:
                count = child_stdin.write(remaining[:65536])
                if not count:
                    break
                remaining = remaining[count:]
        except (BrokenPipeError, OSError):
            pass
        finally:
            child_stdin.close()

    threads: list[threading.Thread] = []
    timed_out = cleanup_failed = False
    deadline = time.monotonic() + timeout
    try:
        for action in (read, write):
            thread = threading.Thread(target=action, daemon=True)
            thread.start()
            threads.append(thread)
        while not overflow.is_set() and not failed.is_set():
            if done.is_set() and child.poll() is not None:
                break
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                timed_out = True
                break
            time.sleep(min(0.01, remaining_time))
    except Exception:
        raise DotError("command_unavailable") from None
    finally:
        # Terminate through the held job even when the direct child has exited.
        try:
            job.terminate()
            child.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            cleanup_failed = True
        finally:
            # Kill-on-close is also the fallback if termination or waiting failed.
            try:
                job.close()
            finally:
                try:
                    for thread in threads:
                        thread.join(timeout=5)
                finally:
                    try:
                        child_stdin.close()
                    finally:
                        child_stdout.close()
    require(
        not cleanup_failed and not failed.is_set() and not any(thread.is_alive() for thread in threads),
        "command_unavailable",
    )
    return CommandResult(child.returncode, bytes(output), timed_out, overflow.is_set())


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        return "[depth-limit]"
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, dict):
        return {str(key): _scrub(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    return str(value)


def _clamp(value: Any, max_string: int, max_items: int, depth: int = 0) -> tuple[Any, bool]:
    """Returns (value, truncated)."""
    if isinstance(value, str):
        if len(value) > max_string:
            return value[:max_string] + f"...[truncated {len(value) - max_string} chars]", True
        return value, False
    if isinstance(value, dict):
        truncated = len(value) > max_items
        result: dict[str, Any] = {}
        for key in list(value)[:max_items]:
            result[key], cut = _clamp(value[key], max_string, max_items, depth + 1)
            truncated = truncated or cut
        return result, truncated
    if isinstance(value, list):
        items: list[Any] = []
        truncated = len(value) > max_items
        for item in value[:max_items]:
            item, cut = _clamp(item, max_string, max_items, depth + 1)
            items.append(item)
            truncated = truncated or cut
        if len(value) > max_items:
            items.append(f"...[{len(value) - max_items} more items]")
        return items, truncated
    return value, False


def bound_result(value: Any) -> Any:
    """Credential-redact every string, then shrink until it fits MAX_RESULT_BYTES."""
    value = _scrub(value)
    for limits in (None, (16384, 200), (4096, 64), (1024, 24), (256, 8)):
        if limits is None:
            candidate, truncated = value, False
        else:
            candidate, truncated = _clamp(value, *limits)
        if truncated and isinstance(candidate, dict):
            candidate = {**candidate, "truncated": True}
        if encoded_size(candidate) <= MAX_RESULT_BYTES:
            return candidate
    return {"error": "output_too_large"}


def parse_json_bytes(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
