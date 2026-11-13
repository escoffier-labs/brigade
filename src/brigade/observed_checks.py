"""Unsigned observations of checks, kept separately from verification receipts."""

from __future__ import annotations

import json
import os
import re
import shlex
from contextlib import ExitStack
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from . import guard, localio
from .agent_identity import MODEL_SOURCES, AgentIdentity
from .claude_hooks.envelope import redact_paths
from .inbox_lock import InboxLockTimeout, held_file_lock

OBSERVED_DIR_REL = Path(".brigade/work/observed")
CHECKS_FILE = "checks.jsonl"
CHECK_SCHEMA = "brigade.observed_check.v1"
STATUSES = ("ok", "failed", "unknown")
ROTATE_BYTES = 4 * 1024 * 1024
COMMAND_MAX_CHARS = 1000
LOCK_SECONDS = 2.0
REDACTED = "[REDACTED]"
_SENSITIVE_OPTION = re.compile(r"key|token|secret|pass|pwd|auth|cred|bearer", re.I)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SAFE_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,79}\Z")
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
    command: str
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
        if len(data["command"]) > COMMAND_MAX_CHARS:
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
        return str(module[0])
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
        return f"{name}:{script}" if script else "other"
    if name == "npx":
        tail, ambiguous = rt._strip_npx_options(tokens[1:])
        return "other" if ambiguous else _normalize_tokens(tail)
    for suffix in ("scripts/verify", "scripts/verify-focused"):
        if tokens[0].replace("\\", "/").endswith(suffix):
            return suffix
    if name == "make":
        tail, ambiguous = rt._strip_make_global_options(tokens[1:])
        return f"make {tail[0]}" if tail and not ambiguous else "make"
    if name in {"go", "cargo", "ruff"} and len(tokens) > 1:
        return f"{name} {tokens[1]}"
    return name


def normalize_tool(command: str) -> str:
    if not isinstance(command, str):
        return "other"
    rt = _rt()
    try:
        segment = rt._shell_tokens(rt._first_verifier_command(command))
        if not rt._segment_is_verifier(segment):
            return "other"
        return _normalize_tokens(segment)
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
        if token.startswith(("-H", "-u")) and len(token) > 2:
            result.append(token[:2] + REDACTED)
            continue
        result.append(re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/]*@", lambda m: m[1] + REDACTED + "@", token))
    return shlex.join(result)


def redact_command(command: str) -> str:
    if not isinstance(command, str):
        return REDACTED
    return redact_paths(guard.redact_text(_command_secrets(command)))[:COMMAND_MAX_CHARS]


def _safe_identity(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTITY.fullmatch(value):
        return "unknown"
    redacted = redact_command(value)
    return redacted if _SAFE_IDENTITY.fullmatch(redacted) else "unknown"


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
    return ObservedCheck(
        schema=CHECK_SCHEMA,
        ts=localio.utc_now_iso(),
        harness=identity.harness,
        model=_safe_identity(identity.model),
        model_source=identity.model_source,
        agent_type=_safe_identity(identity.agent_type),
        session_fingerprint=session_fingerprint,
        tree_fingerprint=localio.tree_fingerprint(target),
        tool=normalize_tool(command),
        command=redact_command(command),
        status=status if status in STATUSES else "unknown",
        exit_code=exit_code,
        duration_ms=duration_ms,
    )


def _rotate(path: Path) -> None:
    """Rotate under the caller's OS lock, keeping only one older generation."""
    try:
        if path.stat().st_size >= ROTATE_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> bool:
    """Serialize size check, best-effort rotation and append on local filesystems.

    Cooperating processes use flock / msvcrt with a two-second acquisition
    bound. Timeout writes nothing. A short write can lose its own record, but
    the next append separates it with a newline. No fsync durability or network
    filesystem lock/append guarantees are provided. Only two generations remain.
    """
    try:
        line = (json.dumps(dict(payload), ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with held_file_lock(_lock_path(path), deadline_seconds=LOCK_SECONDS):
            if os.name != "nt":
                path.parent.chmod(0o700)
                try:
                    path.chmod(0o600)
                except FileNotFoundError:
                    pass
            try:
                _rotate(path)
            except OSError:
                pass
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
            try:
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                if os.lseek(fd, 0, os.SEEK_END):
                    os.lseek(fd, -1, os.SEEK_END)
                    if os.read(fd, 1) != b"\n" and os.write(fd, b"\n") != 1:
                        return False
                return os.write(fd, line) == len(line)
            finally:
                os.close(fd)
    except (OSError, ValueError, TypeError, RecursionError):
        return False


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Open both retained generations under the writer lock, then decode.

    Timeout falls back to unlocked reading and adds one to the skipped count.
    That fallback may miss a generation during rotation. Network filesystems
    may not honor the OS locking semantics used here.
    """
    records: list[dict[str, Any]] = []
    skipped = 0
    try:
        if not path.parent.exists():
            return records, skipped
    except OSError:
        return records, 1
    with ExitStack() as streams:
        with ExitStack() as locking:
            try:
                locking.enter_context(held_file_lock(_lock_path(path), deadline_seconds=LOCK_SECONDS))
            except (InboxLockTimeout, OSError):
                skipped += 1
            opened = []
            for source in (path.with_name(path.name + ".1"), path):
                try:
                    opened.append(streams.enter_context(source.open("rb")))
                except FileNotFoundError:
                    continue
                except OSError:
                    skipped += 1
        for stream in opened:
            try:
                for line in stream:
                    try:
                        if not line.endswith(b"\n"):
                            raise ValueError("partial line")
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("expected object")
                        records.append(record)
                    except (ValueError, UnicodeError, RecursionError):
                        skipped += 1
            except OSError:
                skipped += 1
    return records, skipped


def append_check(target: Path, check: ObservedCheck) -> bool:
    # Redact at the storage boundary too, even for a directly constructed record.
    payload = check.to_dict()
    payload["command"] = redact_command(check.command)
    payload["model"] = _safe_identity(check.model)
    payload["agent_type"] = _safe_identity(check.agent_type)
    if ObservedCheck.from_dict(payload) is None:
        return False
    return append_jsonl(target / OBSERVED_DIR_REL / CHECKS_FILE, payload)


def read_checks(target: Path) -> tuple[list[ObservedCheck], int]:
    records, skipped = read_jsonl(target / OBSERVED_DIR_REL / CHECKS_FILE)
    checks = []
    for record in records:
        check = ObservedCheck.from_dict(record)
        if check is None:
            skipped += 1
        else:
            checks.append(check)
    return checks, skipped
