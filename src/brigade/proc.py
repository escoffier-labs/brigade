"""Run external tool CLIs and capture their results. No tool is imported in-process."""

from __future__ import annotations

import errno
import json
import ntpath
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

_STREAM_ENCODING = "utf-8"
_UNSUPPORTED_WINDOWS_SUFFIXES: dict[str, str] = {
    ".ps1": "PowerShell script",
    ".vbs": "VBScript",
    ".js": "JavaScript file",
    ".py": "Python script",
    ".jar": "Java archive",
    ".msi": "Windows installer",
}
_WINDOWS_BATCH_SUFFIXES = frozenset({".cmd", ".bat"})
_WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_TIMED_OUT_DRAIN_SECONDS = 0.5


class ProcessRegistry:
    """Own cancellable subprocess groups for one worker dispatch."""

    def __init__(self, *, terminate_grace: float = 0.5, kill_grace: float = 0.5) -> None:
        self._terminate_grace = terminate_grace
        self._kill_grace = kill_grace
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._canceled = False

    def register(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if not self._canceled:
                self._processes.add(process)
                return
        _terminate_processes(
            (process,),
            terminate_grace=self._terminate_grace,
            kill_grace=self._kill_grace,
        )

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel(self) -> None:
        with self._lock:
            self._canceled = True
            processes = tuple(self._processes)
        _terminate_processes(
            processes,
            terminate_grace=self._terminate_grace,
            kill_grace=self._kill_grace,
        )

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        _terminate_processes(
            (process,),
            terminate_grace=self._terminate_grace,
            kill_grace=self._kill_grace,
        )


def _signal_process_group(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except OSError:
        pass


def _terminate_windows_process_tree(process: subprocess.Popen[bytes], *, timeout: float) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=max(timeout, 0.1),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _wait_for_processes(processes: tuple[subprocess.Popen[bytes], ...], timeout: float) -> None:
    deadline = time.monotonic() + max(timeout, 0.0)
    while any(process.poll() is None for process in processes):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.01, remaining))


def _terminate_processes(
    processes: tuple[subprocess.Popen[bytes], ...],
    *,
    terminate_grace: float,
    kill_grace: float,
) -> None:
    if not processes:
        return
    if os.name == "nt":
        timeout = terminate_grace + kill_grace
        for process in processes:
            _terminate_windows_process_tree(process, timeout=timeout)
        _wait_for_processes(processes, kill_grace)
        return
    for process in processes:
        _signal_process_group(process, signal.SIGTERM)
    _wait_for_processes(processes, terminate_grace)
    for process in processes:
        _signal_process_group(process, signal.SIGKILL)
    _wait_for_processes(processes, kill_grace)


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    stdout_decode_error: str | None = None
    stderr_decode_error: str | None = None

    @property
    def decode_failed(self) -> bool:
        return self.stdout_decode_error is not None or self.stderr_decode_error is not None

    @property
    def decode_failure_detail(self) -> str:
        return self.stderr_decode_error or self.stdout_decode_error or "child output is not valid UTF-8"

    def json(self) -> Optional[object]:
        try:
            return json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError):
            return None


def _decode_stream(value: str | bytes | None, *, stream: str) -> tuple[str, str | None]:
    if value is None:
        return "", None
    if isinstance(value, str):
        return value, None
    try:
        return value.decode(_STREAM_ENCODING), None
    except UnicodeDecodeError as exc:
        replaced = value.decode(_STREAM_ENCODING, errors="replace")
        return replaced, f"child {stream} is not valid UTF-8 ({_STREAM_ENCODING}): {exc}"


def _decoded_output(stdout: str | bytes | None, stderr: str | bytes | None) -> tuple[str, str, str | None, str | None]:
    out_text, out_error = _decode_stream(stdout, stream="stdout")
    err_text, err_error = _decode_stream(stderr, stream="stderr")
    return out_text, err_text, out_error, err_error


def _result_from_output(
    *,
    code: int,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> Result:
    out_text, err_text, stdout_error, stderr_error = _decoded_output(stdout, stderr)
    for stream_error in (stdout_error, stderr_error):
        if stream_error is not None:
            if err_text and not err_text.endswith("\n"):
                err_text += "\n"
            err_text += stream_error
    return Result(
        code=code,
        stdout=out_text,
        stderr=err_text,
        stdout_decode_error=stdout_error,
        stderr_decode_error=stderr_error,
    )


@dataclass(frozen=True)
class ExecutableIdentity:
    """Resolved adapter executable identity safe for public diagnostics."""

    command: str
    path: str | None
    kind: str
    runnable: bool
    detail: str


def _native_executable_remediation(command: str) -> str:
    return f"add the native {command} executable directory to PATH instead of the shim"


def _looks_like_windows_pe_executable(path: Path) -> bool:
    """Return True when path begins with a Windows PE executable signature."""

    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"MZ":
                return False
            handle.seek(0x3C)
            pe_offset_bytes = handle.read(4)
            if len(pe_offset_bytes) != 4:
                return False
            pe_offset = int.from_bytes(pe_offset_bytes, "little")
            handle.seek(pe_offset)
            return handle.read(4) == b"PE\0\0"
    except OSError:
        return False


def _windows_executable_kind(path: Path, *, raw_path: str, command: str) -> tuple[str, bool, str]:
    suffix = ntpath.splitext(raw_path)[1].lower()
    basename = ntpath.splitext(ntpath.basename(raw_path))[0] or path.name
    if suffix == ".exe":
        return "exe", True, f"{basename} resolves to a supported Windows exe executable"
    if suffix in _WINDOWS_BATCH_SUFFIXES:
        shim_kind = suffix[1:]
        return (
            shim_kind,
            False,
            (
                f"{basename} resolves to an unsupported Windows {shim_kind} shim; "
                f"{_native_executable_remediation(command)}"
            ),
        )
    if suffix in _UNSUPPORTED_WINDOWS_SUFFIXES:
        shim = _UNSUPPORTED_WINDOWS_SUFFIXES[suffix]
        return (
            f"unsupported{suffix}",
            False,
            (f"{basename} resolves to an unsupported Windows {shim}; {_native_executable_remediation(command)}"),
        )
    if suffix:
        return (
            "unsupported",
            False,
            (
                f"{basename} resolves to an unsupported Windows executable kind ({suffix}); "
                f"add the native {command} executable directory to PATH"
            ),
        )
    if _looks_like_windows_pe_executable(path):
        return "native", True, f"{basename} resolves to a supported Windows native executable"
    return (
        "npm-shim",
        False,
        (f"{basename} resolves to an unsupported Windows npm shim; {_native_executable_remediation(command)}"),
    )


def _posix_executable_kind(command: str) -> tuple[str, bool, str]:
    return "native", True, f"{command} is available on PATH"


def which(cmd: str, path: str | None = None) -> Optional[str]:
    return shutil.which(cmd, path=path)


def resolve_executable(command: str, path: str | None = None) -> ExecutableIdentity:
    """Resolve a command name once for detection and dispatch.

    Public diagnostics intentionally omit user-specific absolute paths.
    """

    resolved_path = which(command) if path is None else which(command, path=path)
    if resolved_path is None:
        return ExecutableIdentity(
            command=command,
            path=None,
            kind="missing",
            runnable=False,
            detail=f"{command} is not on PATH",
        )

    resolved = Path(resolved_path)
    if sys.platform == "win32":
        kind, runnable, detail = _windows_executable_kind(resolved, raw_path=resolved_path, command=command)
    else:
        kind, runnable, detail = _posix_executable_kind(command)

    return ExecutableIdentity(
        command=command,
        path=resolved_path,
        kind=kind,
        runnable=runnable,
        detail=detail,
    )


def _launch_failure(argv: List[str], exc: OSError) -> tuple[int, str]:
    command = ntpath.basename(argv[0]) if argv else "command"
    if isinstance(exc, FileNotFoundError):
        return 127, f"command not found: {command}"
    if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
        return 126, f"command permission denied: {command}"
    if exc.errno == errno.ENOEXEC:
        return 126, f"command has invalid executable format: {command}"
    return 126, f"command launch failed: {command}"


def run(
    args: List[str],
    timeout: float = 30.0,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
    stdin: bytes | None = None,
    process_registry: ProcessRegistry | None = None,
) -> Result:
    try:
        if process_registry is not None:
            process_group_kwargs: dict[str, Any] = {}
            if os.name == "posix":
                process_group_kwargs["start_new_session"] = True
            elif os.name == "nt":
                process_group_kwargs["creationflags"] = _WINDOWS_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                env=env,
                cwd=cwd,
                shell=False,
                **process_group_kwargs,
            )
            process_registry.register(process)
            try:
                process_stdout, process_stderr = process.communicate(input=stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                process_registry.terminate(process)
                try:
                    process_stdout, process_stderr = process.communicate(timeout=_TIMED_OUT_DRAIN_SECONDS)
                except subprocess.TimeoutExpired as drain_exc:
                    process_stdout, process_stderr = drain_exc.output, drain_exc.stderr
                    for stream_name in ("stdout", "stderr"):
                        stream = getattr(process, stream_name, None)
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass
                result = _result_from_output(code=124, stdout=process_stdout, stderr=process_stderr)
                timeout_detail = f"timeout after {timeout}s"
                result_stderr = result.stderr
                if result_stderr and not result_stderr.endswith("\n"):
                    result_stderr += "\n"
                return Result(
                    code=124,
                    stdout=result.stdout,
                    stderr=result_stderr + timeout_detail,
                    stdout_decode_error=result.stdout_decode_error,
                    stderr_decode_error=result.stderr_decode_error,
                )
            except BaseException:
                process_registry.terminate(process)
                raise
            finally:
                process_registry.unregister(process)
            return _result_from_output(
                code=process.returncode,
                stdout=process_stdout,
                stderr=process_stderr,
            )
        if stdin is None:
            cp = subprocess.run(
                args,
                capture_output=True,
                text=False,
                timeout=timeout,
                env=env,
                cwd=cwd,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
            )
        else:
            cp = subprocess.run(
                args,
                capture_output=True,
                text=False,
                timeout=timeout,
                env=env,
                cwd=cwd,
                check=False,
                shell=False,
                input=stdin,
            )
        return _result_from_output(code=cp.returncode, stdout=cp.stdout, stderr=cp.stderr)
    except OSError as exc:
        code, message = _launch_failure(args, exc)
        return Result(code=code, stdout="", stderr=message)
    except subprocess.TimeoutExpired as exc:
        result = _result_from_output(code=124, stdout=exc.stdout, stderr=exc.stderr)
        timeout_detail = f"timeout after {timeout}s"
        stderr = result.stderr
        if stderr and not stderr.endswith("\n"):
            stderr += "\n"
        return Result(
            code=124,
            stdout=result.stdout,
            stderr=stderr + timeout_detail,
            stdout_decode_error=result.stdout_decode_error,
            stderr_decode_error=result.stderr_decode_error,
        )
