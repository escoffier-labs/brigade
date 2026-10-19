"""Private filesystem and SQLite guards ported from the bridge."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from typing import Any, Iterator, TypeVar
from uuid import UUID

from .. import dirfd
from .contracts import DotError, require

MAX_TASK_BYTES = 6144

_Database = TypeVar("_Database", bound="PrivateDatabase")


def identity(value: Any) -> str:
    require(
        isinstance(value, str) and 0 < len(value) <= 256 and all(32 < ord(c) < 127 for c in value),
        "authentication_required",
    )
    return value


def job_uuid(value: Any) -> str:
    try:
        require(isinstance(value, str) and str(UUID(value)) == value)
    except (ValueError, TypeError, AttributeError):
        raise DotError("invalid_request") from None
    return value


def task_value(value: Any) -> str:
    try:
        require(isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= MAX_TASK_BYTES)
    except UnicodeError:
        raise DotError("invalid_request") from None
    return value


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        require(key not in result, "sender_unavailable")
        result[key] = value
    return result


def redact_review(text: str) -> str:
    """Conservative output filtering, not a guarantee of source-data secrecy.

    Never return metadata/config; additionally remove URL/path/token-shaped review
    content. Root still must approve sharing the pinned source's review text.
    """
    text = re.sub(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>]+", "[redacted-url]", text)
    text = re.sub(r"(?i)\bbearer\s+\S+", "[redacted-credential]", text)
    text = re.sub(r"(?<![\w.])(?:/[A-Za-z0-9_.~-][^\s<>]*|[A-Za-z]:\\[^\s<>]+)", "[redacted-path]", text)
    text = re.sub(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9])", "[redacted-token]", text)
    return text


CREDENTIAL_PATTERNS = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        "[redacted-private-key]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "[redacted-credential]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[redacted-jwt]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "[redacted-credential]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[redacted-credential]"),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"), "[redacted-credential]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted-credential]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted-credential]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[redacted-credential]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\b"
            r"(\s*[:=]\s*)([\"']?)[^\s\"',;]{6,}"
        ),
        r"\1\2\3[redacted-credential]",
    ),
)


def redact_credentials(text: str) -> str:
    for pattern, replacement in CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def require_local_platform() -> None:
    """The local bridge store requires flock and descriptor-pinned SQLite."""
    require(sys.platform != "win32", "unsupported_on_windows")


def private_file(info: os.stat_result) -> None:
    require_local_platform()
    require(os.name == "posix", "unsafe_state")
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1,
        "unsafe_state",
    )


def private_directory(directory: Path, *, create: bool = False) -> int:
    require_local_platform()
    require(os.name == "posix", "unsafe_state")
    current = None
    try:
        path = Path(directory)
        require(path.is_absolute() and ".." not in path.parts and len(path.parts) > 1, "unsafe_state")
        current = os.open("/", dirfd.directory_flags())
        for index, component in enumerate(path.parts[1:]):
            last = index == len(path.parts) - 2
            if last and create:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                else:
                    os.fsync(current)
            child = os.open(component, dirfd.directory_flags(), dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            if last:
                require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, "unsafe_state")
            else:
                require(
                    info.st_uid in (0, os.geteuid())
                    and (not info.st_mode & 0o022 or (info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX))),
                    "unsafe_state",
                )
        descriptor, current = current, None
        return descriptor
    except Exception:
        raise DotError("unsafe_state") from None
    finally:
        if current is not None:
            os.close(current)


def trusted_directory(directory: Path, *, allow_group_write: bool = False, code: str = "unsafe_state") -> int:
    """Open an existing directory through O_NOFOLLOW components.

    Same ancestor rule as private_directory. The final directory must be owned by
    this user and not world-writable. Group write is accepted only when asked and
    only for this user's own primary group (the canonical cards dir is 0775).
    """
    require(os.name == "posix", code)
    current = None
    try:
        path = Path(directory)
        require(path.is_absolute() and ".." not in path.parts and len(path.parts) > 1)
        current = os.open("/", dirfd.directory_flags())
        for index, component in enumerate(path.parts[1:]):
            last = index == len(path.parts) - 2
            child = os.open(component, dirfd.directory_flags(), dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            if last:
                require(
                    info.st_uid == os.geteuid()
                    and not info.st_mode & 0o002
                    and (not info.st_mode & 0o020 or (allow_group_write and info.st_gid == os.getegid()))
                )
            else:
                require(
                    info.st_uid in (0, os.geteuid())
                    and (not info.st_mode & 0o022 or (info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)))
                )
        descriptor, current = current, None
        return descriptor
    except Exception:
        raise DotError(code) from None
    finally:
        if current is not None:
            os.close(current)


class PrivateDatabase:
    """Pinned private local directory, flock and FULL SQLite transactions.

    Ancestors must be root/current-user owned and not group/world writable,
    except a root-owned sticky directory such as /tmp. Every component is opened
    with O_NOFOLLOW. SQLite accesses the pinned directory through /proc/self/fd.
    Cooperating processes never hold a database transaction across sender IO.
    The local UID remains trusted; this does not defend against a hostile owner.
    """

    DATABASE = "jobs.sqlite3"
    LOCK = "store.lock"

    def __init__(self, directory: Path) -> None:
        require_local_platform()
        self.fd: int | None = None
        self.directory = Path(directory)
        try:
            self.fd = private_directory(directory, create=True)
            with self.transaction() as connection:
                self.initialize(connection)
        except Exception:
            self.close()
            raise DotError("unsafe_state") from None

    def initialize(self, connection: sqlite3.Connection) -> None:
        raise NotImplementedError

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self: _Database) -> _Database:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()

    def file(self, name: str) -> int:
        require(os.name == "posix", "unsafe_state")
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
        try:
            private_file(os.fstat(descriptor))
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def check_files(self) -> None:
        require(os.name == "posix", "unsafe_state")
        assert self.fd is not None
        private = os.fstat(self.fd)
        require(private.st_uid == os.geteuid() and stat.S_IMODE(private.st_mode) == 0o700, "unsafe_state")
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                private_file(os.stat(self.DATABASE + suffix, dir_fd=self.fd, follow_symlinks=False))
            except FileNotFoundError:
                pass

    @contextmanager
    def lock(self, name: str, *, nonblocking: bool = False) -> Iterator[None]:
        require(os.name == "posix", "unsafe_state")
        try:
            import fcntl
        except ImportError:
            raise DotError("unsafe_state") from None

        descriptor = None
        try:
            descriptor = self.file(name)
            fcntl.flock(descriptor, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            yield
        except DotError:
            raise
        except BlockingIOError:
            raise DotError("sender_busy") from None
        except OSError as error:
            logging.getLogger("dot-state").warning(
                "state_unavailable at=lock error=%s errno=%s", type(error).__name__, error.errno
            )
            raise DotError("state_unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            with self.lock(self.LOCK):
                self.check_files()
                descriptor = self.file(self.DATABASE)
                os.close(descriptor)
                connection = sqlite3.connect(f"/proc/self/fd/{self.fd}/{self.DATABASE}", timeout=5)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
                self.check_files()
                # Persist initial DB/directory creation before any sender attempt.
                assert self.fd is not None
                os.fsync(self.fd)
        except DotError:
            raise
        except (OSError, sqlite3.Error) as error:
            # Type, errno and SQLite error name only; never paths, SQL or parameters.
            logging.getLogger("dot-state").warning(
                "state_unavailable error=%s errno=%s sqlite=%s",
                type(error).__name__,
                getattr(error, "errno", None),
                getattr(error, "sqlite_errorname", None),
            )
            raise DotError("state_unavailable") from None
        finally:
            if connection is not None:
                connection.close()
