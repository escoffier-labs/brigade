"""Unsigned observations of checks, kept separately from verification receipts."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from . import guard, localio
from .agent_identity import MODEL_SOURCES, AgentIdentity
from .claude_hooks.envelope import redact_paths

OBSERVED_DIR_REL = Path(".brigade/work/observed")
CHECKS_FILE = "checks.jsonl"
CHECK_SCHEMA = "brigade.observed_check.v1"
STATUSES = ("ok", "failed", "unknown")
ROTATE_BYTES = 4 * 1024 * 1024
COMMAND_MAX_CHARS = 1000
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


def redact_command(command: str) -> str:
    return redact_paths(guard.redact_text(command))[:COMMAND_MAX_CHARS]


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
        model=identity.model,
        model_source=identity.model_source,
        agent_type=identity.agent_type,
        session_fingerprint=session_fingerprint,
        tree_fingerprint=localio.tree_fingerprint(target),
        tool=normalize_tool(command),
        command=redact_command(command),
        status=status if status in STATUSES else "unknown",
        exit_code=exit_code,
        duration_ms=duration_ms,
    )


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < ROTATE_BYTES:
        return
    lock = path.with_name(path.name + ".rotate.lock")
    try:
        if time.time() - lock.stat().st_mtime > 60:
            lock.unlink()
    except FileNotFoundError:
        pass
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)
    try:
        if path.exists() and path.stat().st_size >= ROTATE_BYTES:
            try:
                os.replace(path, path.with_name(path.name + ".1"))
            except PermissionError:
                # Windows may refuse replacement while another appender has it open.
                pass
    finally:
        lock.unlink(missing_ok=True)


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> bool:
    """Append one encoded line in one write; storage errors never escape."""
    try:
        line = (json.dumps(dict(payload), ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
        try:
            return os.write(fd, line) == len(line)
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        return False


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    skipped = 0
    for source in (path.with_name(path.name + ".1"), path):
        try:
            with source.open("rb") as stream:
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
        except FileNotFoundError:
            continue
        except OSError:
            skipped += 1
    return records, skipped


def append_check(target: Path, check: ObservedCheck) -> bool:
    # Redact at the storage boundary too, even for a directly constructed record.
    payload = check.to_dict()
    payload["command"] = redact_command(check.command)
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
