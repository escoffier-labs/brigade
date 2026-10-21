"""Owner-only t3-dispatch runtime JSON."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

from .contracts import CALLER_RE, NAME_RE, REF_RE, T3DispatchError

RUNTIME_SCHEMA = "brigade.grokbot.t3-dispatch-runtime.v1"
RUNTIME_KEYS = frozenset(
    {
        "schema",
        "enabled",
        "t3_fleet_bin",
        "git_bin",
        "repositories",
        "hosts",
        "seats",
        "default_seat",
        "fallback_seat",
        "fleet_enrolled",
        "max_open",
        "daily_limit",
        "prompt_max_bytes",
        "ledger_path",
        "wake_path",
        "writable_paths",
    }
)
MAX_RUNTIME_BYTES = 65_536
MAX_REPOSITORIES = 50
MAX_WRITABLE_PATHS = 8
REMOTE_RE = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+\.git$")


@dataclass(frozen=True)
class Repository:
    remote: str
    default_branch: str


@dataclass(frozen=True)
class IntakePolicy:
    label: str
    queued_label: str
    repositories: tuple[tuple[str, str], ...]
    caller: str
    gh_bin: str
    max_per_pass: int
    headroom: int


@dataclass(frozen=True)
class Runtime:
    enabled: bool
    t3_fleet_bin: str
    git_bin: str
    repositories: Mapping[str, Repository]
    hosts: tuple[str, ...]
    seats: tuple[str, ...]
    default_seat: str
    fallback_seat: str | None
    fleet_enrolled: bool
    max_open: int
    daily_limit: int
    prompt_max_bytes: int
    ledger_path: Path
    wake_path: Path
    writable_paths: tuple[str, ...]
    intake: IntakePolicy | None = None
    hub_token_file: Path | None = None
    hub_wake_target: str = "default"
    hub_recipients: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


def _bad() -> T3DispatchError:
    return T3DispatchError("invalid_request")


def require_posix_storage() -> None:
    """The dispatch design requires POSIX owner-only storage, including SQLite sidecars."""
    if sys.platform == "win32" or os.name != "posix":
        raise T3DispatchError("unsupported-platform")


def read_owner_file(path: Path, *, max_bytes: int) -> bytes:
    """Read a regular, owner-only, non-symlink file of bounded size."""
    require_posix_storage()
    if not path.is_absolute():
        raise _bad()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _bad() from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _bad()
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > max_bytes:
            raise _bad()
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise _bad()
    return data


def _abs(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _bad()
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise _bad()
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise _bad()
    return value


def _int(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad()
    return value


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _bad()
    if any(not isinstance(item, str) or not NAME_RE.fullmatch(item) for item in value):
        raise _bad()
    if len(set(value)) != len(value):
        raise _bad()
    return tuple(value)


def _repositories(value: object) -> Mapping[str, Repository]:
    if not isinstance(value, dict) or not value or len(value) > MAX_REPOSITORIES:
        raise _bad()
    parsed: dict[str, Repository] = {}
    for repo_id, entry in value.items():
        if not isinstance(repo_id, str) or not NAME_RE.fullmatch(repo_id):
            raise _bad()
        if not isinstance(entry, dict) or set(entry) != {"remote", "default_branch"}:
            raise _bad()
        remote = entry["remote"]
        branch = entry["default_branch"]
        if not isinstance(remote, str) or not REMOTE_RE.fullmatch(remote):
            raise _bad()
        if not isinstance(branch, str) or not REF_RE.fullmatch(branch) or ".." in branch:
            raise _bad()
        parsed[repo_id] = Repository(remote=remote, default_branch=branch)
    return MappingProxyType(parsed)


def _intake(value: object, repositories: Mapping[str, Repository], max_open: int) -> IntakePolicy:
    keys = {"label", "queued_label", "repositories", "caller", "gh_bin", "max_per_pass", "headroom"}
    if not isinstance(value, dict) or set(value) != keys:
        raise _bad()
    for key in ("label", "queued_label"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,49}", value[key]):
            raise _bad()
    if value["label"] == value["queued_label"]:
        raise _bad()
    if not isinstance(value["caller"], str) or not CALLER_RE.fullmatch(value["caller"]):
        raise _bad()
    pairs = []
    for repo_id in _names(value["repositories"]):
        repo = repositories.get(repo_id)
        if repo is None:
            raise _bad()
        match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.git", repo.remote)
        if match is None:
            raise _bad()
        pairs.append((repo_id, f"{match[1]}/{match[2]}"))
    return IntakePolicy(
        label=value["label"],
        queued_label=value["queued_label"],
        repositories=tuple(pairs),
        caller=value["caller"],
        gh_bin=_abs(value["gh_bin"]),
        max_per_pass=_int(value["max_per_pass"], 1, 5),
        headroom=_int(value["headroom"], 1, max_open - 1),
    )


def _hub_recipients(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict) or len(value) > 16:
        raise _bad()
    parsed = {}
    for name, node_id in value.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or not isinstance(node_id, str):
            raise _bad()
        try:
            if str(UUID(node_id)) != node_id:
                raise _bad()
        except ValueError:
            raise _bad() from None
        parsed[name] = node_id
    return MappingProxyType(parsed)


def parse_runtime(payload: object) -> Runtime:
    if (
        not isinstance(payload, dict)
        or set(payload) - {"intake", "hub_token_file", "hub_wake_target", "hub_recipients"} != RUNTIME_KEYS
    ):
        raise _bad()
    if payload["schema"] != RUNTIME_SCHEMA:
        raise _bad()
    wake_target = payload.get("hub_wake_target", "default")
    if not isinstance(wake_target, str) or not CALLER_RE.fullmatch(wake_target):
        raise _bad()
    seats = _names(payload["seats"])
    default_seat = payload["default_seat"]
    if default_seat not in seats:
        raise _bad()
    fallback_seat = payload["fallback_seat"]
    if fallback_seat is not None and (fallback_seat not in seats or fallback_seat == default_seat):
        raise _bad()
    writable = payload["writable_paths"]
    if not isinstance(writable, list) or len(writable) > MAX_WRITABLE_PATHS:
        raise _bad()
    repositories = _repositories(payload["repositories"])
    max_open = _int(payload["max_open"], 1, 20)
    return Runtime(
        hub_token_file=Path(_abs(payload["hub_token_file"])) if "hub_token_file" in payload else None,
        hub_wake_target=wake_target,
        hub_recipients=_hub_recipients(payload.get("hub_recipients", {})),
        enabled=_bool(payload["enabled"]),
        t3_fleet_bin=_abs(payload["t3_fleet_bin"]),
        git_bin=_abs(payload["git_bin"]),
        repositories=repositories,
        hosts=_names(payload["hosts"]),
        seats=seats,
        default_seat=default_seat,
        fallback_seat=fallback_seat,
        fleet_enrolled=_bool(payload["fleet_enrolled"]),
        max_open=max_open,
        daily_limit=_int(payload["daily_limit"], 1, 200),
        prompt_max_bytes=_int(payload["prompt_max_bytes"], 1024, 32768),
        ledger_path=Path(_abs(payload["ledger_path"])),
        wake_path=Path(_abs(payload["wake_path"])),
        writable_paths=tuple(_abs(item) for item in writable),
        intake=_intake(payload["intake"], repositories, max_open) if "intake" in payload else None,
    )


def load_runtime(path: Path) -> Runtime:
    try:
        payload: Any = json.loads(read_owner_file(path, max_bytes=MAX_RUNTIME_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _bad() from exc
    return parse_runtime(payload)


def validate_runtime_reference(value: object) -> str:
    """Validate an instance-config `runtime_path` reference and return it unchanged."""
    require_posix_storage()
    reference = _abs(value)
    load_runtime(Path(reference))
    return reference
