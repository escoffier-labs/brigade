"""Descriptor-bound I/O for Claude session state, locks and timeout sidecars.

All child operations use the existing cross-platform no-follow primitives.
The directory chain is retained and revalidated, including across a locked
read/modify/write transaction. No pathname fallback is permitted.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .. import dirfd, inbox_lock

_COMPONENTS = (".brigade", "work", "claude-hooks", "sessions")
_ACTIVE: ContextVar[SessionDirectory | None] = ContextVar("claude_session_directory", default=None)
_WINDOWS_REPLACE_ATTEMPTS = 5
_WINDOWS_REPLACE_BACKOFF_SECONDS = 0.01


class SessionIntegrityError(OSError):
    """Session I/O cannot prove path integrity; never interpret this as absence."""


def integrity_envelope(event: str, *, stop_hook_active: bool = False) -> dict[str, Any]:
    reason = "Brigade session state integrity check failed. Restore the session directory and run brigade doctor."
    if event == "Stop" and not stop_hook_active:
        return {"decision": "block", "reason": reason}
    if event == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    return {"continue": False, "stopReason": reason}


def _raise_integrity_error(error: OSError) -> None:
    """Translate only no-follow path/type refusals, preserving ordinary I/O errors."""
    if (
        isinstance(error, (NotADirectoryError, IsADirectoryError))
        or error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}
        or dirfd.is_reparse_point_refusal(error)
        # The native backend's handle metadata check has no errno for these.
        or (os.name == "nt" and str(error) in {"path component is not a directory", "path component is a directory"})
    ):
        raise SessionIntegrityError(f"Claude hook session path refused: {error}") from error


def _windows_replace_contention(error: OSError) -> bool:
    return os.name == "nt" and (
        isinstance(error, PermissionError)
        or getattr(error, "winerror", None) in {5, 32, 33}
        # nt_dirfd preserves STATUS_SHARING_VIOLATION as an untyped OSError.
        or str(error).endswith("NTSTATUS=0xC0000043")
    )


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _plain_directory(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and not getattr(info, "st_reparse_tag", 0)
        and not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )


class SessionDirectory:
    """An open target root and no-follow chain down to its session directory."""

    def __init__(self, target: Path, *, create: bool, requested: Path | None = None) -> None:
        self.target = target
        self.requested = requested or target
        self.pid = os.getpid()
        self.chain: list[int] = []
        self.lock: tuple[str, int] | None = None
        try:
            self.chain.append(dirfd.open_directory_nofollow(target, writable=create))
            for name in _COMPONENTS:
                parent = self.chain[-1]
                try:
                    child = dirfd.open_child_directory(parent, name, writable=create)
                except FileNotFoundError:
                    if not create:
                        raise
                    self.validate()
                    try:
                        dirfd.mkdir_child(parent, name, 0o700 if name == "sessions" else 0o777)
                    except FileExistsError:
                        pass
                    child = dirfd.open_child_directory(parent, name, writable=create)
                self.chain.append(child)
            self.validate()
        except BaseException as exc:
            self.close()
            if isinstance(exc, OSError):
                _raise_integrity_error(exc)
            raise

    @property
    def fd(self) -> int:
        return self.chain[-1]

    def held_for(self, requested: Path, resolved: Path) -> bool:
        """Match every name of the held target without trusting a re-resolved path alone."""
        names = {self.requested, self.target}
        # A replaced target path resolves elsewhere; route it here so validate() refuses it.
        return requested in names or resolved in names or resolved == self.target.resolve()

    def close(self) -> None:
        for fd in reversed(self.chain):
            os.close(fd)
        self.chain.clear()

    def validate(self) -> None:
        """Refuse renamed, linked or replaced components without following them."""
        try:
            self._validate_chain()
        except FileNotFoundError as exc:
            raise SessionIntegrityError("Claude hook held session directory disappeared") from exc
        except OSError as exc:
            _raise_integrity_error(exc)
            raise

    def _validate_chain(self) -> None:
        root_info = os.lstat(self.target)
        if not _plain_directory(root_info) or _identity(root_info) != _identity(os.fstat(self.chain[0])):
            raise SessionIntegrityError("Claude hook target directory changed during session I/O")
        for index, name in enumerate(_COMPONENTS[: len(self.chain) - 1]):
            named = dirfd.stat_child(self.chain[index], name, allow_reparse=True)
            held = os.fstat(self.chain[index + 1])
            if not _plain_directory(named) or _identity(named) != _identity(held):
                raise SessionIntegrityError("Claude hook session directory changed during session I/O")
        if self.lock is not None:
            self.validate_file(*self.lock)

    def validate_file(self, name: str, fd: int) -> os.stat_result:
        """Require the named file to remain a single-link regular held inode."""
        try:
            held = os.fstat(fd)
            named = dirfd.stat_child(self.fd, name, allow_reparse=True)
        except FileNotFoundError as exc:
            raise SessionIntegrityError("Claude hook held session file disappeared") from exc
        except OSError as exc:
            _raise_integrity_error(exc)
            raise
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_nlink != 1
            or not stat.S_ISREG(named.st_mode)
            or bool(getattr(named, "st_reparse_tag", False))
            or getattr(named, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or _identity(named) != _identity(held)
        ):
            raise SessionIntegrityError("Claude hook session file is not a single-link regular held file")
        return held

    @contextlib.contextmanager
    def open_file(self, name: str, flags: int) -> Iterator[int]:
        dirfd.validate_component(name)
        self.validate()
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        reading = not flags & (os.O_WRONLY | os.O_RDWR)
        try:
            fd = dirfd.open_child_file(self.fd, name, flags, 0o600)
        except FileNotFoundError:
            self.validate()
            raise
        except OSError as exc:
            _raise_integrity_error(exc)
            raise
        try:
            if reading:
                held = os.fstat(fd)
                # Atomic replacement can unlink a held reader before fstat.
                # Zero links leaves no outside name; trust this read-only fd
                # just as when replacement follows the initial check.
                if not stat.S_ISREG(held.st_mode) or held.st_nlink not in {0, 1}:
                    raise SessionIntegrityError(
                        "Claude hook session file is not a single-link or unlinked regular file"
                    )
            else:
                self.validate_file(name, fd)
            self.validate()
            yield fd
        finally:
            os.close(fd)

    def read_bytes(self, name: str, max_bytes: int | None = None) -> bytes:
        with self.open_file(name, os.O_RDONLY) as fd:
            chunks: list[bytes] = []
            remaining = max_bytes
            while remaining is None or remaining > 0:
                chunk = os.read(fd, 65536 if remaining is None else min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
            # Atomic publication may change the name and unlink this inode.
            # Its contents remain trusted after the initial held-fd check.
            self.validate()
            return b"".join(chunks)

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        temporary = f".{dirfd.validate_component(name)}.{uuid.uuid4().hex}.tmp"
        created = False
        published = False
        try:
            self.validate()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
            try:
                fd = dirfd.open_child_file(self.fd, temporary, flags, 0o600)
            except OSError as exc:
                _raise_integrity_error(exc)
                raise
            created = True
            try:
                self.validate_file(temporary, fd)
                self.validate()
                _write_all(fd, data)
                os.fsync(fd)
                for attempt in range(_WINDOWS_REPLACE_ATTEMPTS):
                    self.validate_file(temporary, fd)
                    self.validate()
                    try:
                        dirfd.replace_children(self.fd, temporary, name)
                    except OSError as exc:
                        _raise_integrity_error(exc)
                        if not _windows_replace_contention(exc):
                            raise
                        if attempt + 1 == _WINDOWS_REPLACE_ATTEMPTS:
                            raise OSError(errno.EBUSY, "Windows session publication remained busy") from exc
                        time.sleep(_WINDOWS_REPLACE_BACKOFF_SECONDS)
                    else:
                        published = True
                        break
                self.validate_file(name, fd)
            finally:
                os.close(fd)
            dirfd.fsync_directory(self.fd)
        finally:
            if created and not published:
                # Cleanup stays relative even when a parent was renamed.
                primary_error = sys.exc_info()[1]
                try:
                    dirfd.unlink_child(self.fd, temporary)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    if isinstance(primary_error, SessionIntegrityError):
                        raise primary_error from exc
                    _raise_integrity_error(exc)
                    raise

    @contextlib.contextmanager
    def held_lock(self, name: str, *, deadline_seconds: float) -> Iterator[None]:
        if self.lock is not None:
            raise RuntimeError("nested Claude session locks are not supported")
        self.validate()
        try:
            if os.name == "nt":
                fd = dirfd.open_child_lock_file(self.fd, name)
            else:
                fd = dirfd.open_child_file(
                    self.fd, name, dirfd.file_flags(os.O_RDWR | os.O_CREAT | os.O_NONBLOCK), 0o600
                )
        except OSError as exc:
            _raise_integrity_error(exc)
            raise
        acquired = False
        try:
            self.validate_file(name, fd)
            deadline = time.monotonic() + deadline_seconds
            if os.name == "nt":
                inbox_lock._msvcrt_lock_with_deadline(fd, name, deadline, deadline_seconds)
            else:
                inbox_lock._flock_with_deadline(fd, name, deadline, deadline_seconds)
            acquired = True
            # Reuse the existing fork cleanup so children cannot prolong locks.
            with inbox_lock._HELD_FILE_LOCK_FDS_GUARD:
                inbox_lock._HELD_FILE_LOCK_FDS.add(fd)
            self.lock = (name, fd)
            token = _ACTIVE.set(self)
            try:
                self.validate()
                yield
            finally:
                _ACTIVE.reset(token)
                self.lock = None
                with inbox_lock._HELD_FILE_LOCK_FDS_GUARD:
                    inbox_lock._HELD_FILE_LOCK_FDS.discard(fd)
        finally:
            if acquired:
                with contextlib.suppress(OSError):
                    if os.name == "nt" and inbox_lock.msvcrt is not None:
                        inbox_lock.msvcrt.locking(fd, inbox_lock.msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                    elif inbox_lock.fcntl is not None:
                        inbox_lock.fcntl.flock(fd, inbox_lock.fcntl.LOCK_UN)
            os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short Claude hook session write")
        remaining = remaining[written:]


def requested_target(target: Path) -> Path:
    """Absolute target path as named by the caller, without following links."""
    return Path(os.path.abspath(target.expanduser()))


@contextlib.contextmanager
def session_directory(target: Path, *, create: bool = False) -> Iterator[SessionDirectory]:
    requested = requested_target(target)
    target = requested.resolve()
    active = _ACTIVE.get()
    if active is not None and active.pid == os.getpid() and active.held_for(requested, target):
        active.validate()
        yield active
        return
    anchor = SessionDirectory(target, create=create, requested=requested)
    try:
        yield anchor
    finally:
        anchor.close()


def _read_json(anchor: SessionDirectory, name: str) -> dict[str, Any] | None:
    try:
        value = json.loads(anchor.read_bytes(name))
    except (FileNotFoundError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_json(target: Path, name: str) -> dict[str, Any] | None:
    try:
        with session_directory(target) as anchor:
            return _read_json(anchor, name)
    except FileNotFoundError:
        return None


def write_json(target: Path, name: str, payload: dict[str, Any]) -> None:
    with session_directory(target, create=True) as anchor:
        anchor.write_json(name, payload)


def _sidecar_target(path: Path) -> Path:
    if tuple(path.parent.parts[-4:]) != _COMPONENTS:
        raise SessionIntegrityError("Claude hook sidecar layout is not a sessions directory")
    return path.parents[4]


def read_sidecar(path: Path, max_bytes: int) -> bytes:
    try:
        with session_directory(_sidecar_target(path)) as anchor:
            return anchor.read_bytes(path.name, max_bytes)
    except FileNotFoundError:
        return b""


def append_sidecar(path: Path, payload: bytes, *, max_bytes: int | None) -> bool:
    try:
        with session_directory(_sidecar_target(path), create=True) as anchor:
            with anchor.open_file(path.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND) as fd:
                if max_bytes is not None and os.fstat(fd).st_size >= max_bytes:
                    return False
                anchor.validate()
                anchor.validate_file(path.name, fd)
                _write_all(fd, payload)
        return True
    except SessionIntegrityError:
        raise
    except OSError:
        return False


def iter_states(
    target: Path, *, modified_since: datetime | None = None, limit: int | None = None
) -> Iterator[dict[str, Any]]:
    try:
        with session_directory(target) as anchor:
            threshold = modified_since.timestamp() if modified_since is not None else None
            candidates: list[tuple[float, str]] = []
            # Both backends enumerate the held directory. Unsupported native
            # enumeration raises and is diagnosed below, never reopened by path.
            for name in dirfd.child_names(anchor.fd, sys.maxsize):
                if not name.endswith(".json"):
                    continue
                try:
                    info = dirfd.stat_child(anchor.fd, name, allow_reparse=True)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and (threshold is None or info.st_mtime >= threshold):
                    candidates.append((info.st_mtime, name))
            anchor.validate()
            candidates.sort(reverse=True)
            if limit is not None:
                candidates = candidates[: max(limit, 0)]
            for _, name in candidates:
                value = _read_json(anchor, name)
                if value is not None:
                    yield value
    except SessionIntegrityError:
        raise
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"brigade: Claude hook session listing refused: {exc}", file=sys.stderr)
