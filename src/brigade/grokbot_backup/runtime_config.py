"""Private Backup Steward runtime, environment, and public registry projection."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

from .contracts import BackupError, parse_identifier

PUBLIC_ALIASES = (
    "configuration-nas",
    "configuration-cloud",
    "media-archive",
    "virtualization",
    "repository-sweep",
)
REPORTER_IDS = (
    "restic-target-summary-v1",
    "restic-restore-readiness-v1",
    "backup-operation-status-v1",
    "virtualization-backup-summary-v1",
    "repository-sweep-summary-v1",
)
ACTION_RUNNER_IDS = (
    "restic-backup-runner-v1",
    "restic-integrity-runner-v1",
    "restic-rehearsal-runner-v1",
)
ADAPTER_ENVIRONMENT_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)
FIXED_ARG_RE = __import__("re").compile(r"^(?:--)?[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _runtime_config_error() -> NoReturn:
    raise BackupError("invalid_request", "Backup runtime configuration is invalid")


def _environment_error() -> NoReturn:
    raise BackupError("invalid_request", "Backup environment is invalid")


def _not_found() -> NoReturn:
    raise BackupError("not_found", "Backup resource was not found")


def _require_mapping(value: object, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _runtime_config_error()
    return value


def _has_explicit_dot_segment(value: str) -> bool:
    return any(segment in {".", ".."} for segment in value.split("/"))


def normalize_absolute_path(path_value: str) -> str:
    trimmed = os.path.normpath(path_value).rstrip("/")
    return trimmed if trimmed else "/"


def _required_absolute_path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        _environment_error()
    if _has_explicit_dot_segment(value):
        _environment_error()
    return normalize_absolute_path(value)


def paths_overlap(left: str, right: str) -> bool:
    normalized_left = normalize_absolute_path(left)
    normalized_right = normalize_absolute_path(right)
    if normalized_left == normalized_right:
        return True
    return normalized_left.startswith(f"{normalized_right}/") or normalized_right.startswith(f"{normalized_left}/")


def assert_disjoint_paths(paths: list[str]) -> None:
    for left_index, left in enumerate(paths):
        for right in paths[left_index + 1 :]:
            if paths_overlap(left, right):
                _environment_error()


def _assert_secure_runtime_stat(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        _environment_error()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        _environment_error()


def read_secure_runtime_text(path_text: str) -> str:
    """Read a current-UID-owned mode-0600 runtime file through one no-follow descriptor."""
    from .. import grokbot_ops

    normalized = _required_absolute_path(path_text)
    path = Path(normalized)
    try:
        prior = os.lstat(normalized)
    except OSError as exc:
        raise BackupError("invalid_request", "Backup environment is invalid") from exc
    attributes = getattr(prior, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(prior.st_mode) or bool(attributes & reparse_point):
        _environment_error()
    _assert_secure_runtime_stat(prior)
    parent = -1
    descriptor = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if os.name == "posix":
            descriptor = os.open(path.name, flags, dir_fd=parent)
        else:
            from ..work_cmd import nt_dirfd

            descriptor = nt_dirfd.open_file(parent, path.name, os.O_RDONLY)
        info = os.fstat(descriptor)
        _assert_secure_runtime_stat(info)
        if (info.st_dev, info.st_ino) != (prior.st_dev, prior.st_ino):
            _environment_error()
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
            return handle.read()
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError("invalid_request", "Backup environment is invalid") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if parent != -1:
            os.close(parent)


def _parse_command(raw: object) -> MappingProxyType:
    payload = _require_mapping(raw, {"executable", "args"})
    executable = payload.get("executable")
    args = payload.get("args")
    if not isinstance(executable, str) or not executable.startswith("/") or "\0" in executable:
        _runtime_config_error()
    if _has_explicit_dot_segment(executable) or any(part == "" for part in executable.split("/")[1:]):
        _runtime_config_error()
    if not isinstance(args, list) or len(args) > 16:
        _runtime_config_error()
    parsed_args: list[str] = []
    for item in args:
        if not isinstance(item, str) or not 1 <= len(item) <= 64 or not FIXED_ARG_RE.fullmatch(item):
            _runtime_config_error()
        parsed_args.append(item)
    return MappingProxyType({"executable": executable, "args": tuple(parsed_args)})


def _parse_target(raw: object) -> MappingProxyType:
    allowed = {
        "reporter_id",
        "readiness_reporter_id",
        "status_reporter_id",
        "action_runner_ids",
        "stale_lock_seconds",
        "timeout_ms",
        "freshness_seconds",
    }
    if (
        not isinstance(raw, Mapping)
        or not {"reporter_id", "action_runner_ids", "stale_lock_seconds", "timeout_ms", "freshness_seconds"} <= set(raw)
        or not set(raw) <= allowed
    ):
        _runtime_config_error()
    reporter_id = raw.get("reporter_id")
    if reporter_id not in REPORTER_IDS:
        _runtime_config_error()
    runners = raw.get("action_runner_ids")
    if not isinstance(runners, list) or len(runners) > 8:
        _runtime_config_error()
    parsed_runners: list[str] = []
    for runner in runners:
        if runner not in ACTION_RUNNER_IDS or runner in parsed_runners:
            _runtime_config_error()
        parsed_runners.append(runner)
    stale = raw.get("stale_lock_seconds")
    timeout = raw.get("timeout_ms")
    freshness = raw.get("freshness_seconds")
    if type(stale) is not int or not 1 <= stale <= 604_800:
        _runtime_config_error()
    if type(timeout) is not int or not 250 <= timeout <= 30_000:
        _runtime_config_error()
    if type(freshness) is not int or not 0 <= freshness <= 86_400:
        _runtime_config_error()
    target: dict[str, Any] = {
        "reporter_id": reporter_id,
        "action_runner_ids": tuple(parsed_runners),
        "stale_lock_seconds": stale,
        "timeout_ms": timeout,
        "freshness_seconds": freshness,
    }
    for optional_key in ("readiness_reporter_id", "status_reporter_id"):
        if optional_key in raw:
            value = raw.get(optional_key)
            if value not in REPORTER_IDS:
                _runtime_config_error()
            target[optional_key] = value
    return MappingProxyType(target)


def parse_backup_private_runtime(raw: object) -> MappingProxyType:
    required = {"version", "safety_ready", "targets", "reporters", "action_runners"}
    allowed = required | {"max_concurrent_processes"}
    if not isinstance(raw, Mapping) or not required <= set(raw) or not set(raw) <= allowed:
        _runtime_config_error()
    payload = raw
    if payload.get("version") != 1 or not isinstance(payload.get("safety_ready"), bool):
        _runtime_config_error()
    targets_raw = _require_mapping(payload.get("targets"), set(PUBLIC_ALIASES))
    reporters_raw = _require_mapping(payload.get("reporters"), set(REPORTER_IDS))
    runners_raw = _require_mapping(payload.get("action_runners"), set(ACTION_RUNNER_IDS))
    targets = MappingProxyType({alias: _parse_target(targets_raw.get(alias)) for alias in PUBLIC_ALIASES})
    reporters = MappingProxyType({name: _parse_command(reporters_raw.get(name)) for name in REPORTER_IDS})
    action_runners = MappingProxyType({name: _parse_command(runners_raw.get(name)) for name in ACTION_RUNNER_IDS})
    reporter_executables = [command["executable"] for command in reporters.values()]
    runner_executables = [command["executable"] for command in action_runners.values()]
    if len(set(reporter_executables)) != len(reporter_executables):
        _runtime_config_error()
    if len(set(runner_executables)) != len(runner_executables):
        _runtime_config_error()
    referenced_reporters: set[str] = set()
    referenced_runners: set[str] = set()
    for target in targets.values():
        referenced_reporters.add(target["reporter_id"])
        if "readiness_reporter_id" in target:
            referenced_reporters.add(target["readiness_reporter_id"])
        if "status_reporter_id" in target:
            referenced_reporters.add(target["status_reporter_id"])
        referenced_runners.update(target["action_runner_ids"])
    if not referenced_reporters <= set(reporters):
        _runtime_config_error()
    if not referenced_runners <= set(action_runners):
        _runtime_config_error()
    max_concurrent = payload.get("max_concurrent_processes", 4)
    if type(max_concurrent) is not int or not 1 <= max_concurrent <= 8:
        _runtime_config_error()
    return MappingProxyType(
        {
            "version": 1,
            "safety_ready": payload["safety_ready"],
            "targets": targets,
            "reporters": reporters,
            "action_runners": action_runners,
            "max_concurrent_processes": max_concurrent,
        }
    )


class BackupRegistry:
    def __init__(self, safety_ready: bool, targets: tuple[MappingProxyType, ...]):
        self.safety_ready = safety_ready
        self.targets = targets
        self._by_alias = {target["alias"]: target for target in targets}

    def target(self, alias: str) -> MappingProxyType:
        try:
            parse_identifier(alias)
        except BackupError:
            _not_found()
        found = self._by_alias.get(alias)
        if found is None:
            _not_found()
        return found


def project_backup_registry(runtime: Mapping[str, Any]) -> BackupRegistry:
    targets = []
    for alias in PUBLIC_ALIASES:
        target = runtime["targets"][alias]
        runners = tuple(target["action_runner_ids"]) if runtime["safety_ready"] else ()
        public = {
            "alias": alias,
            "reporter_id": target["reporter_id"],
            "action_runner_ids": runners,
            "stale_lock_seconds": target["stale_lock_seconds"],
            "timeout_ms": target["timeout_ms"],
            "freshness_seconds": target["freshness_seconds"],
        }
        if "readiness_reporter_id" in target:
            public["readiness_reporter_id"] = target["readiness_reporter_id"]
        if "status_reporter_id" in target:
            public["status_reporter_id"] = target["status_reporter_id"]
        targets.append(MappingProxyType(public))
    return BackupRegistry(bool(runtime["safety_ready"]), tuple(targets))


def _required_token(value: object, dispatch_token: str | None) -> str:
    if not isinstance(value, str) or len(value) < 32:
        _environment_error()
    if dispatch_token is not None and value == dispatch_token:
        _environment_error()
    return value


def _required_loopback_host(value: object) -> str:
    if value in {"127.0.0.1", "::1"}:
        return str(value)
    _environment_error()


def _required_port(value: object) -> int:
    if not isinstance(value, str) or not value.isdigit():
        _environment_error()
    parsed = int(value)
    if parsed < 1 or parsed > 65_535:
        _environment_error()
    return parsed


def _required_max_concurrent(value: object) -> int:
    if value is None:
        return 4
    if not isinstance(value, str) or not value.isdigit():
        _environment_error()
    parsed = int(value)
    if parsed < 1 or parsed > 8:
        _environment_error()
    return parsed


def load_backup_runtime_env(
    env: Mapping[str, str],
    *,
    dispatch_token: str | None = None,
) -> dict[str, Any]:
    runtime_path = _required_absolute_path(env.get("GROKBOT_BACKUP_RUNTIME_PATH"))
    ledger_path = _required_absolute_path(env.get("GROKBOT_BACKUP_LEDGER_PATH"))
    action_state_path = _required_absolute_path(env.get("GROKBOT_BACKUP_ACTION_STATE_PATH"))
    approval_dir = _required_absolute_path(env.get("GROKBOT_BACKUP_APPROVAL_DIR"))
    assert_disjoint_paths([runtime_path, ledger_path, action_state_path, approval_dir])
    return {
        "token": _required_token(env.get("GROKBOT_BACKUP_TOKEN"), dispatch_token),
        "host": _required_loopback_host(env.get("GROKBOT_BACKUP_HOST")),
        "port": _required_port(env.get("GROKBOT_BACKUP_PORT")),
        "runtime_path": runtime_path,
        "ledger_path": ledger_path,
        "action_state_path": action_state_path,
        "approval_dir": approval_dir,
        "max_concurrent_processes": _required_max_concurrent(env.get("GROKBOT_BACKUP_MAX_CONCURRENT_PROCESSES")),
    }


def backup_adapter_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env_source = os.environ if source is None else source
    environment: dict[str, str] = {}
    for key in ADAPTER_ENVIRONMENT_KEYS:
        value = env_source.get(key)
        if value is not None:
            environment[key] = value
    return environment


def parse_runtime_json(text: str) -> MappingProxyType:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        _runtime_config_error()
    return parse_backup_private_runtime(payload)
