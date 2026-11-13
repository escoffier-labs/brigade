"""Unsigned observations of checks, kept separately from verification receipts."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import tempfile
import stat
import json
import os
import re
import shlex
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from . import dirfd, guard, localio, work_mode
from .agent_identity import MODEL_SOURCES, AgentIdentity, map_identity
from .claude_hooks.envelope import redact_paths
from .guard.types import ScanOptions
from .inbox_lock import InboxLockTimeout, held_file_lock_descriptor as held_file_lock

CHECKS_FILE = "checks.jsonl"
CHECK_SCHEMA = "brigade.observed_check.v1"
STATUSES = ("ok", "failed", "unknown")
DIGEST_UNAVAILABLE = "0" * 16
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024
MAX_RECORDS = 10000
MAX_STRING_CHARS = 4096
ROTATE_BYTES = 4 * 1024 * 1024
COMMAND_MAX_CHARS = 1000
LOCK_SECONDS = 2.0
_CAPTURE_LOCK_SECONDS: ContextVar[float] = ContextVar("observed_lock_seconds", default=LOCK_SECONDS)
REDACTED = "[REDACTED]"
_SENSITIVE_OPTION = re.compile(r"key|token|secret|pass|pwd|auth|cred|bearer", re.I)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
CHECK_KINDS = ("tests", "lint", "types", "build", "checks")


@dataclass(frozen=True)
class ObservedCheck:
    schema: str
    ts: str
    harness: str
    model: str
    model_source: str
    agent_type: str
    session_fingerprint: str
    tree_fingerprint: str | None
    tool: str
    command_digest: str
    status: str
    exit_code: int | None
    duration_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ObservedCheck | None:
        if not isinstance(data, Mapping) or set(data) != {field.name for field in fields(cls)}:
            return None
        nullable = {"tree_fingerprint", "exit_code", "duration_ms"}
        for key, value in data.items():
            if isinstance(value, str) and len(value) > MAX_STRING_CHARS:
                return None
            if key not in nullable and (not isinstance(value, str) or not value.strip()):
                return None
        if data["tree_fingerprint"] is not None and (
            not isinstance(data["tree_fingerprint"], str) or not data["tree_fingerprint"].strip()
        ):
            return None
        for key in ("exit_code", "duration_ms"):
            if data[key] is not None and type(data[key]) is not int:
                return None
        if data["duration_ms"] is not None and data["duration_ms"] < 0:
            return None
        if (
            data["schema"] != CHECK_SCHEMA
            or data["status"] not in STATUSES
            or data["model_source"] not in MODEL_SOURCES
        ):
            return None
        if not re.fullmatch(r"[0-9a-f]{16}", data["command_digest"]) or data["tool"] not in _TOOL_LABELS:
            return None
        data = dict(data)
        data.update(map_identity(data["harness"], data["model"], data["agent_type"]))
        if not _valid_fingerprint(data["session_fingerprint"], 16):
            return None
        if data["tree_fingerprint"] is not None and not _valid_fingerprint(data["tree_fingerprint"], 40):
            return None
        try:
            if datetime.fromisoformat(data["ts"].replace("Z", "+00:00")).tzinfo is None:
                return None
        except ValueError:
            return None
        return cls(**data)


def _rt() -> Any:
    from .claude_hooks import runtime

    return runtime


# Closed vocabularies prevent even safe-looking positional secrets from becoming labels.
_BARE_TOOLS = frozenset(
    {
        "other",
        "pytest",
        "py.test",
        "nose",
        "nose2",
        "tox",
        "nox",
        "vitest",
        "jest",
        "mocha",
        "rspec",
        "phpunit",
        "ctest",
        "unittest",
        "mypy",
        "pyright",
        "eslint",
        "tsc",
        "pre-commit",
        "make",
        "go",
        "cargo",
        "ruff",
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "scripts/verify",
        "scripts/verify-focused",
    }
)
_TOOL_TARGETS = {
    "make": {"test", "check", "verify"},
    "go": {"test", "vet"},
    "cargo": {"test", "clippy"},
    "ruff": {"check", "format"},
    **dict.fromkeys(
        ("npm", "pnpm", "yarn", "bun"),
        {
            "test",
            "check",
            "build",
            "test:unit",
            "test:integration",
            "check:types",
            "check:lint",
        },
    ),
}
_SAFE_TARGET = re.compile(r"[A-Za-z0-9._:/-]{1,32}\Z")
_TOOL_LABELS = _BARE_TOOLS | frozenset(
    name + (":" if name in {"npm", "pnpm", "yarn", "bun"} else " ") + target
    for name, targets in _TOOL_TARGETS.items()
    for target in targets
)


def _tool_label(name: str, target: str | None, separator: str = " ") -> str:
    if target and _SAFE_TARGET.fullmatch(target) and target in _TOOL_TARGETS[name]:
        return name + separator + target
    return name


def _normalize_tokens(tokens: list[str]) -> str:
    rt = _rt()
    tokens = rt._strip_env(tokens)
    while tokens and tokens[0] in rt._SHELL_CONTROL_PREFIXES:
        tokens = rt._strip_env(tokens[1:])
    if not tokens:
        return "other"
    name = Path(tokens[0]).name
    module = rt._python_module_invocation(tokens)
    if module:
        return _normalize_tokens([module[0], *module[1]])
    if name in {"uv", "poetry", "npm", "pnpm", "yarn", "bun"}:
        tail, ambiguous = rt._strip_runner_global_options(name, tokens[1:])
        if ambiguous:
            return "other"
        if name in {"uv", "poetry"}:
            if tail[:1] != ["run"]:
                return "other"
            tail, ambiguous = rt._strip_runner_run_options(name, tail[1:])
            return "other" if ambiguous else _normalize_tokens(tail)
        script = rt._package_runner_script(tail)
        if name in {"pnpm", "yarn", "bun"} and tail and tail[0] in _TOOL_TARGETS[name]:
            script = tail[0]
        return _tool_label(name, script, ":")
    if name == "npx":
        tail, ambiguous = rt._strip_npx_options(tokens[1:])
        return "other" if ambiguous else _normalize_tokens(tail)
    for suffix in ("scripts/verify", "scripts/verify-focused"):
        if tokens[0].replace("\\", "/").endswith(suffix):
            return suffix
    if name == "make":
        tail, ambiguous = rt._strip_make_global_options(tokens[1:])
        return _tool_label("make", tail[0] if tail and not ambiguous else None)
    if name in {"go", "cargo", "ruff"} and len(tokens) > 1:
        return _tool_label(name, tokens[1])
    return name if name in _BARE_TOOLS else "other"


def normalize_tool(command: str) -> str:
    if not isinstance(command, str):
        return "other"
    rt = _rt()
    try:
        segment = rt._shell_tokens(rt._first_verifier_command(command))
        label = _normalize_tokens(segment)
        return label if label in _TOOL_LABELS else "other"
    except (ValueError, RecursionError):
        return "other"


_KIND_BY_TOOL = {
    **dict.fromkeys(
        (
            "pytest",
            "py.test",
            "vitest",
            "jest",
            "mocha",
            "rspec",
            "phpunit",
            "ctest",
            "tox",
            "nox",
            "unittest",
            "nose2",
            "go test",
            "cargo test",
        ),
        "tests",
    ),
    **dict.fromkeys(("eslint", "cargo clippy", "go vet", "pre-commit"), "lint"),
    **dict.fromkeys(("mypy", "pyright", "tsc"), "types"),
}


def check_kind(tool: str) -> str:
    if tool.startswith("ruff "):
        return "lint"
    runner, _, script = tool.partition(":")
    if runner in {"npm", "pnpm", "yarn", "bun"}:
        if script.startswith("test"):
            return "tests"
        if script.startswith("build"):
            return "build"
    return _KIND_BY_TOOL.get(tool, "checks")


def _command_secrets(command: str) -> str:
    """Mask shell credential positions before content and path redaction.

    Invalid shell syntax loses the entire command through a conservative regex
    fallback. Markers never depend on the secret's contents or length.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return re.sub(r"[\s\S]+", lambda _: REDACTED, command)
    hide_next = False
    result = []
    for token in tokens:
        if hide_next:
            result.append(REDACTED)
            hide_next = False
            continue
        if _ASSIGNMENT.match(token):
            result.append(token.split("=", 1)[0] + "=" + REDACTED)
            continue
        if token.startswith(("-H", "-u", "-p")) and len(token) > 2:
            result.append(token[:2] + REDACTED)
            continue
        option, equals, _ = token.partition("=")
        if (option.startswith("-") and _SENSITIVE_OPTION.search(option)) or option in {
            "-p",
            "-u",
            "-H",
            "--header",
            "--user",
        }:
            result.append(option + "=" + REDACTED if equals else option)
            hide_next = not equals
            continue
        result.append(re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/]*@", lambda m: m[1] + REDACTED + "@", token))
    return shlex.join(result)


def redact_command(command: str) -> str:
    if not isinstance(command, str):
        return REDACTED
    try:
        if len(command) > 64 * 1024:
            return REDACTED
        return redact_paths(
            guard.redact_text(_command_secrets(command), options=ScanOptions(honor_allow_comments=False))
        )[:COMMAND_MAX_CHARS]
    except (ValueError, UnicodeError, RecursionError):
        return REDACTED


@contextmanager
def capture_budget():
    """Hook capture gives up promptly when another process owns storage."""
    token = _CAPTURE_LOCK_SECONDS.set(0.01)
    try:
        yield
    finally:
        _CAPTURE_LOCK_SECONDS.reset(token)


class _StateUnavailable(OSError):
    pass


def _key_path(target: Path) -> Path:
    root = work_mode.rails_record_path()
    if root is None:
        raise _StateUnavailable("other")
    name = hashlib.sha256(work_mode.repository_key(target).encode("utf-8")).hexdigest()
    path = root.parent / "observed" / name / "digest.key"
    real = Path(os.path.realpath(target))
    tree = next((d for d in (real, *real.parents) if os.path.lexists(d / ".git")), real)
    if Path(os.path.realpath(path)).is_relative_to(tree):
        raise _StateUnavailable("state directory inside the project's nearest work tree")
    return path


def recording_unavailable_reason(target: Path) -> str | None:
    """Return a fixed, path-free cause using the writer's trust checks."""
    try:
        path = _key_path(target)
        fd = _state_directory(path.parent)
        os.close(fd)
    except _StateUnavailable as exc:
        return str(exc)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return "other"
    return None


def digest_key_path(target: Path) -> Path | None:
    """Locate a workspace's private state beside the per-user rails record."""
    try:
        return _key_path(target)
    except (OSError, ValueError):
        return None


def _state_directory(path: Path, *, create: bool = False) -> int:
    """Trust one private directory for keys and records, outside every work tree."""
    path = path.absolute()
    owned = (path.parent.parent, path.parent)
    if create:
        owned[0].parent.mkdir(parents=True, exist_ok=True)
        for directory in (*owned, path):
            directory.mkdir(mode=0o700, exist_ok=True)
    for directory in (*reversed(path.parents), path):
        if directory.is_symlink():
            raise _StateUnavailable("config directory reached through a symbolic link")
    if os.name != "nt":
        user = os.geteuid()
        for directory, writable in ((owned[0], 0o002), (owned[1], 0o022), (path, 0o077)):
            try:
                metadata = os.lstat(directory)
            except FileNotFoundError:
                continue
            if metadata.st_uid != user or metadata.st_mode & writable:
                raise _StateUnavailable("a parent directory writable by other users")
    fd = dirfd.open_directory_chain(path)
    try:
        if os.name != "nt":
            metadata = os.fstat(fd)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise _StateUnavailable("a parent directory writable by other users")
        if create:
            # A home directory kept in Git must not pick these files up.
            try:
                ignore = dirfd.open_child_file(fd, ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError:
                pass
            else:
                try:
                    os.write(ignore, b"*\n")
                finally:
                    os.close(ignore)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _digest_key(target: Path) -> bytes:
    path = digest_key_path(target)
    if path is None:
        raise OSError("observed state unavailable")
    parent = _state_directory(path.parent, create=True)
    try:
        with held_file_lock(_lock_path(path.with_name(CHECKS_FILE)), deadline_seconds=_CAPTURE_LOCK_SECONDS.get()):
            # A crash between link and unlink leaves a second name on the key.
            for stale in path.parent.glob(".key-*"):
                stale.unlink(missing_ok=True)
            if not path.exists():
                fd, temporary = tempfile.mkstemp(prefix=".key-", dir=path.parent)
                try:
                    try:
                        if os.write(fd, secrets.token_bytes(32)) != 32:
                            raise OSError("incomplete workspace key")
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        pass
                finally:
                    os.unlink(temporary)
            fd = _open_regular(parent, path.name, os.O_RDONLY)
            try:
                metadata = os.fstat(fd)
                if os.name != "nt" and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600):
                    raise OSError("observed state unavailable")
                key = os.read(fd, 33)
                if len(key) != 32:
                    raise OSError("invalid workspace key")
                return key
            finally:
                os.close(fd)
    finally:
        os.close(parent)


def command_digest(command: str, target: Path) -> str:
    """Group redacted commands within one workspace using a private HMAC key.

    All-zero hex means unavailable and must never be used as a grouping key.
    Unavailable private state disables recording, without an unkeyed fallback.
    """
    try:
        return hmac.new(_digest_key(target), redact_command(command).encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    except (OSError, ValueError, UnicodeError):
        return DIGEST_UNAVAILABLE


def _valid_fingerprint(value: Any, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value) is not None


def _session_digest(target: Path, session_id: str) -> str:
    try:
        return hmac.new(_digest_key(target), b"observed-session\0" + session_id.encode(), hashlib.sha256).hexdigest()[
            :16
        ]
    except (OSError, ValueError, UnicodeError):
        return DIGEST_UNAVAILABLE


def build_check(
    *,
    target: Path,
    command: str,
    status: str,
    identity: AgentIdentity,
    session_fingerprint: str,
    exit_code: int | None = None,
    duration_ms: int | None = None,
) -> ObservedCheck:
    tree = localio.tree_fingerprint(target)
    if tree is not None and not _valid_fingerprint(tree, 40):
        raise ValueError("invalid observed-check fingerprint")
    return ObservedCheck(
        schema=CHECK_SCHEMA,
        ts=localio.utc_now_iso(),
        **map_identity(identity.harness, identity.model, identity.agent_type),
        model_source=identity.model_source,
        session_fingerprint=_session_digest(target, session_fingerprint),
        tree_fingerprint=tree,
        tool=normalize_tool(command),
        command_digest=command_digest(command, target),
        status=status if status in STATUSES else "unknown",
        exit_code=exit_code,
        duration_ms=duration_ms,
    )


def _open_regular(parent: int, name: str, flags: int) -> int:
    flags |= (
        getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    # Reject devices before opening too, to avoid device-specific side effects.
    try:
        metadata = dirfd.stat_child(parent, name)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("record must be a single-link regular file")
    fd = dirfd.open_child_file(parent, name, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("record must be a single-link regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> bool:
    """Hold one OS lock across descriptor size check, rotation and append."""
    try:
        line = (json.dumps(dict(payload), ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        if len(line) > MAX_LINE_BYTES:
            return False
        parent = _state_directory(path.parent, create=True)
        try:
            with held_file_lock(_lock_path(path), deadline_seconds=_CAPTURE_LOCK_SECONDS.get()) as held:
                held.verify()
                fd = _open_regular(parent, path.name, os.O_RDWR | os.O_CREAT | os.O_APPEND)
                try:
                    if os.name != "nt":
                        os.fchmod(fd, 0o600)
                    if os.fstat(fd).st_size >= ROTATE_BYTES:
                        os.close(fd)
                        fd = -1
                        try:
                            dirfd.replace_children(parent, path.name, path.name + ".1", replace=True)
                        except OSError:
                            # Another open handle can block the rename; keep the record, rotate next time.
                            pass
                        fd = _open_regular(parent, path.name, os.O_RDWR | os.O_CREAT | os.O_APPEND)
                    if os.lseek(fd, 0, os.SEEK_END):
                        os.lseek(fd, -1, os.SEEK_END)
                        if os.read(fd, 1) != b"\n" and os.write(fd, b"\n") != 1:
                            return False
                    return os.write(fd, line) == len(line)
                finally:
                    if fd != -1:
                        os.close(fd)
        finally:
            os.close(parent)
    except (OSError, ValueError, TypeError, RecursionError):
        return False


def _decode(fd: int, byte_cap: int, line_cap: int, record_cap: int) -> tuple[list[dict[str, Any]], int]:
    from collections import deque

    records: deque[dict[str, Any]] = deque(maxlen=record_cap)
    skipped = 0
    if os.fstat(fd).st_size > byte_cap:
        return [], 1
    with os.fdopen(os.dup(fd), "rb") as stream:
        remaining = byte_cap
        while remaining > 0:
            line = stream.readline(min(line_cap, remaining) + 1)
            if not line:
                break
            remaining -= len(line)
            if len(line) > line_cap or remaining < 0:
                skipped += 1
                while line and not line.endswith(b"\n") and remaining > 0:
                    line = stream.readline(min(line_cap, remaining) + 1)
                    remaining -= len(line)
                continue
            try:
                if not line.endswith(b"\n"):
                    raise ValueError("partial or oversized line")
                record = json.loads(line)
                if not isinstance(record, dict) or "command" in record:
                    raise ValueError("expected object without legacy command text")
                if any(isinstance(v, str) and len(v) > MAX_STRING_CHARS for v in record.values()):
                    raise ValueError("oversized string field")
                if "schema" in record and ObservedCheck.from_dict(record) is None:
                    raise ValueError("invalid check schema")
                records.append(record)
            except (ValueError, UnicodeError, RecursionError):
                skipped += 1
    return list(reversed(records)), skipped


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Open both generations under one lock, then decode with fixed bounds.

    Lock timeout permits a bounded unlocked snapshot and counts one skip.
    Uses the same POSIX/Windows no-follow backend as the writer.
    """
    records: list[dict[str, Any]] = []
    skipped = 0
    try:
        parent = _state_directory(path.parent)
        with ExitStack() as streams:
            streams.callback(os.close, parent)
            with ExitStack() as locking:
                try:
                    locking.enter_context(
                        held_file_lock(_lock_path(path), deadline_seconds=_CAPTURE_LOCK_SECONDS.get())
                    )
                except (InboxLockTimeout, OSError):
                    skipped += 1
                opened = []
                for name in (path.name, path.name + ".1"):
                    try:
                        fd = _open_regular(parent, name, os.O_RDONLY)
                        streams.callback(os.close, fd)
                        opened.append(fd)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        skipped += 1
                for fd in opened:
                    batch, lost = _decode(fd, MAX_FILE_BYTES, MAX_LINE_BYTES, MAX_RECORDS - len(records))
                    records.extend(batch)
                    skipped += lost
                streams.close()
    except FileNotFoundError:
        pass
    except OSError:
        skipped += 1
    return records, skipped


def append_check(target: Path, check: ObservedCheck) -> bool:
    # Validate the closed label vocabulary even for directly constructed records.
    payload = check.to_dict()
    payload["tool"] = check.tool if check.tool in _TOOL_LABELS else "other"
    payload.update(map_identity(check.harness, check.model, check.agent_type))
    if ObservedCheck.from_dict(payload) is None:
        return False
    try:
        _digest_key(target)
        key_path = digest_key_path(target)
        return key_path is not None and append_jsonl(key_path.with_name(CHECKS_FILE), payload)
    except (OSError, ValueError):
        return False


def read_checks(target: Path) -> tuple[list[ObservedCheck], int]:
    path = digest_key_path(target)
    if path is None:
        return [], 0
    records, skipped = read_jsonl(path.with_name(CHECKS_FILE))
    checks = []
    for record in records:
        check = ObservedCheck.from_dict(record)
        if check is None:
            skipped += 1
        else:
            checks.append(check)
    return checks, skipped
