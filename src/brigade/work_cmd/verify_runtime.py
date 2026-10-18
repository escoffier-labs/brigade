"""Private cancellation controller, child ownership, and console runtime for verification."""

from __future__ import annotations

import errno
import io
import os
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .. import proc
from . import verify_reaper

_VERIFY_CANCELED_RC = 130
_VERIFY_INTERRUPTED_COMMAND_STATUS = "interrupted"
_VERIFY_CANCELED_RECEIPT_STATUS = "canceled"


class _VerifySignalCancel(KeyboardInterrupt):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"verification canceled by {signal.Signals(signum).name}")


class _VerifyCancellation:
    def __init__(self) -> None:
        self.received: int | None = None
        self.raised = False
        self.deferred = 0
        self.interruption: dict[str, str] | None = None
        self.owned_receipt: tuple[Path, Path, dict[str, Any], Any] | None = None

    def raise_if_pending(self) -> None:
        if self.received is not None and not self.raised and not self.deferred:
            self.raised = True
            if self.received == signal.SIGINT:
                raise KeyboardInterrupt
            raise _VerifySignalCancel(self.received)


_ACTIVE_VERIFY_CANCEL: ContextVar[_VerifyCancellation | None] = ContextVar("verify_cancellation", default=None)


@contextmanager
def _verify_cancellation_signals():
    state = _VerifyCancellation()
    token = _ACTIVE_VERIFY_CANCEL.set(state)
    previous = {}

    def cancel(signum, _frame):
        # An outer run owns the installed handler, while a nested run owns its
        # own context. Threads never inherit either run's cancellation latch.
        active = _ACTIVE_VERIFY_CANCEL.get()
        if active is not None:
            if active.received is None:
                active.received = signum
            active.raise_if_pending()

    try:
        if os.name == "posix" and threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                disposition = signal.getsignal(signum)
                if (signum == signal.SIGINT and disposition is signal.default_int_handler) or (
                    signum != signal.SIGINT and disposition == signal.SIG_DFL
                ):
                    previous[signum] = disposition
                    signal.signal(signum, cancel)
        yield state
    finally:
        # Restoration must not deliver a new exception outside verify_run.
        state.raised = True
        for signum, disposition in previous.items():
            signal.signal(signum, disposition)
        _ACTIVE_VERIFY_CANCEL.reset(token)


@contextmanager
def _defer_verify_signal():
    state = _ACTIVE_VERIFY_CANCEL.get()
    released = False
    if state is not None:
        state.deferred += 1

    def release():
        nonlocal released
        if not released:
            released = True
            if state is not None:
                state.deferred -= 1

    try:
        yield release
    finally:
        release()


def _verify_signal_received() -> bool:
    state = _ACTIVE_VERIFY_CANCEL.get()
    return state is not None and state.received is not None


def _verify_interruption_record() -> dict[str, str]:
    state = _ACTIVE_VERIFY_CANCEL.get()
    if state is not None and state.interruption is not None:
        return state.interruption
    if state is not None and state.received is not None and state.received != signal.SIGINT:
        name = signal.Signals(state.received).name
        return {"kind": "signal", "signal": name, "detail": f"verification canceled by {name}"}
    return {"kind": "keyboard-interrupt", "detail": "verification canceled by user"}


def _sink_verify_console(stream) -> None:
    # Redirect the underlying descriptor too: a failed buffered write can be
    # retried during interpreter shutdown, which otherwise changes rc to 120.
    # This permanently sinks the descriptor for the entire process, including
    # programmatic callers. Native Windows console behavior remains unverified.
    with open(os.devnull, "w") as sink:
        try:
            os.dup2(sink.fileno(), stream.fileno())
        except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
            if stream is sys.stdout:
                sys.stdout = open(os.devnull, "w")
            if stream is sys.stderr:
                sys.stderr = open(os.devnull, "w")


def _print_verify_console(*args, **kwargs) -> None:
    stream = kwargs.get("file", sys.stdout)
    try:
        print(*args, **{**kwargs, "flush": True})
    except OSError as exc:
        if exc.errno not in (errno.EIO, errno.EPIPE):
            raise
        _sink_verify_console(stream)


@contextmanager
def _verify_console_output():
    # Storage errors from capture/indexing propagate. Only their informational
    # output is buffered and sent through the console failure boundary.
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            yield
    finally:
        _print_verify_console(stdout.getvalue(), end="", file=sys.stdout)
        _print_verify_console(stderr.getvalue(), end="", file=sys.stderr)


def _verify_child_popen_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": proc._WINDOWS_NEW_PROCESS_GROUP}


def _decode_verify_child_output(stdout: str | bytes | None, stderr: str | bytes | None) -> tuple[str, str]:
    stdout_text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else (stdout or "")
    stderr_text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")
    return stdout_text, stderr_text


def _terminate_verify_child(process: subprocess.Popen[bytes]) -> None:
    try:
        b = verify_reaper._collect_descendants(process.pid)
    except KeyboardInterrupt:
        b = set()
    try:
        proc._terminate_processes((process,), terminate_grace=0.5, kill_grace=0.5)
    except KeyboardInterrupt:
        try:
            proc._terminate_processes((process,), terminate_grace=0.0, kill_grace=0.0)
        except KeyboardInterrupt:
            pass
    try:
        after = verify_reaper._collect_descendants(process.pid)
    except KeyboardInterrupt:
        after = set()
    survivors = tuple(verify_reaper._survivor_mocks(b | after))
    if survivors:
        try:
            proc._terminate_processes(survivors, terminate_grace=0.5, kill_grace=0.5)  # type: ignore
        except KeyboardInterrupt:
            try:
                proc._terminate_processes(survivors, terminate_grace=0.0, kill_grace=0.0)  # type: ignore
            except KeyboardInterrupt:
                pass


def _run_verify_child_process(
    execution_argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> tuple[str, int | None, str, str]:
    """Run one verify child in its own process group; return (status, exit_code, stdout, stderr)."""
    popen_kwargs = _verify_child_popen_kwargs()
    with _defer_verify_signal() as release:
        try:
            process = subprocess.Popen(
                execution_argv,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                shell=False,
                **popen_kwargs,
            )
        except OSError as exc:
            release()
            state = _ACTIVE_VERIFY_CANCEL.get()
            if state is not None:
                state.raise_if_pending()
            return "failed", 127, "", str(exc)
        try:
            try:
                release()
                state = _ACTIVE_VERIFY_CANCEL.get()
                if state is not None:
                    state.raise_if_pending()
                stdout, stderr = process.communicate(timeout=timeout)
                stdout_text, stderr_text = _decode_verify_child_output(stdout, stderr)
                if process.returncode == 0:
                    return "completed", 0, stdout_text, stderr_text
                return "failed", process.returncode, stdout_text, stderr_text
            except KeyboardInterrupt:
                state = _ACTIVE_VERIFY_CANCEL.get()
                if state is not None:
                    state.interruption = _verify_interruption_record()
                with _defer_verify_signal():
                    if state is not None:
                        state.raised = True
                    _terminate_verify_child(process)
                    stdout_text, stderr_text = "", ""
                    try:
                        stdout, stderr = process.communicate(timeout=proc._TIMED_OUT_DRAIN_SECONDS)
                        stdout_text, stderr_text = _decode_verify_child_output(stdout, stderr)
                    except KeyboardInterrupt:
                        _terminate_verify_child(process)
                    except subprocess.TimeoutExpired as exc:
                        stdout_text, stderr_text = _decode_verify_child_output(exc.output, exc.stderr)
                return _VERIFY_INTERRUPTED_COMMAND_STATUS, None, stdout_text, stderr_text
            except subprocess.TimeoutExpired as exc:
                with _defer_verify_signal():
                    try:
                        _terminate_verify_child(process)
                        stdout_text, stderr_text = _decode_verify_child_output(exc.stdout, exc.stderr)
                        try:
                            process.communicate(timeout=proc._TIMED_OUT_DRAIN_SECONDS)
                        except subprocess.TimeoutExpired:
                            pass
                    except KeyboardInterrupt:
                        _terminate_verify_child(process)
                        stdout_text, stderr_text = _decode_verify_child_output(exc.stdout, exc.stderr)
                        return _VERIFY_INTERRUPTED_COMMAND_STATUS, None, stdout_text, stderr_text
                    state = _ACTIVE_VERIFY_CANCEL.get()
                    if state is not None and state.received is not None:
                        state.raised = True
                if _verify_signal_received():
                    return _VERIFY_INTERRUPTED_COMMAND_STATUS, None, stdout_text, stderr_text
                return "timed_out", None, stdout_text, stderr_text

        except KeyboardInterrupt as exc:
            # A first TERM can interrupt a sibling timeout/SIGINT handler before
            # its deferral begins. Preserve the original SIGINT in that case.
            state = _ACTIVE_VERIFY_CANCEL.get()
            if state is not None:
                state.raised = True
                if isinstance(exc.__context__, KeyboardInterrupt) and not isinstance(
                    exc.__context__, _VerifySignalCancel
                ):
                    state.interruption = {"kind": "keyboard-interrupt", "detail": "verification canceled by user"}
                else:
                    state.interruption = _verify_interruption_record()
            return _VERIFY_INTERRUPTED_COMMAND_STATUS, None, "", ""
        finally:
            # Clean up after exceptions interrupt a sibling timeout/SIGINT body.
            # Do not rely on that body reaching its own teardown.
            # The first signal can still arrive before this deferral is entered.
            # This scoped controller does not mask every Python bytecode window.
            with _defer_verify_signal():
                if process.poll() is None:
                    _terminate_verify_child(process)
                state = _ACTIVE_VERIFY_CANCEL.get()
                if state is not None and state.received is not None:
                    state.raised = True
