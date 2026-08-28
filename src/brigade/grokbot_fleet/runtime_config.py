"""Private Fleet Steward runtime, environment, and public registry projection."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

from .contracts import FleetError
from .registry import create_fleet_registry

HOST_PROBE_ID = "linux-host-summary-v1"
HOST_TIMEOUT_MS = 12_000
HOST_FRESHNESS_SECONDS = 14_400
PUBLIC_TIER = "infrastructure"
FLEET_SAFE_SERVICE_UNIT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*(?:@[A-Za-z0-9][A-Za-z0-9._:-]*)?\.service$")
SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
FLEET_PROBE_ENVIRONMENT_KEYS = (
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
ROLE_NAMES = ("control-plane", "hypervisor", "worker")
SERVICE_NAMES = ("research-bridge", "virtualization-api", "overlay-network")


def _runtime_config_error() -> NoReturn:
    raise FleetError("invalid_request", "Fleet runtime configuration is invalid")


def _environment_error() -> NoReturn:
    raise FleetError("invalid_request", "Fleet environment is invalid")


def _require_mapping(value: object, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _runtime_config_error()
    return value


def _parse_control_plane(raw: object) -> MappingProxyType:
    payload = _require_mapping(raw, {"adapter"})
    if payload.get("adapter") != "local":
        _runtime_config_error()
    return MappingProxyType({"adapter": "local"})


def _parse_linux_role(raw: object) -> MappingProxyType:
    payload = _require_mapping(raw, {"adapter", "ssh_alias"})
    alias = payload.get("ssh_alias")
    if payload.get("adapter") != "linux" or not isinstance(alias, str):
        _runtime_config_error()
    if len(alias) < 3 or not SSH_ALIAS_RE.fullmatch(alias):
        _runtime_config_error()
    return MappingProxyType({"adapter": "linux", "ssh_alias": alias})


def _parse_service_mapping(raw: object) -> MappingProxyType:
    payload = _require_mapping(raw, {"unit", "manager"})
    unit = payload.get("unit")
    manager = payload.get("manager")
    if not isinstance(unit, str) or not FLEET_SAFE_SERVICE_UNIT_PATTERN.fullmatch(unit):
        _runtime_config_error()
    if manager not in {"user", "system"}:
        _runtime_config_error()
    return MappingProxyType({"unit": unit, "manager": manager})


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
        raise FleetError("invalid_request", "Fleet environment is invalid") from exc
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
    except FleetError:
        raise
    except OSError as exc:
        raise FleetError("invalid_request", "Fleet environment is invalid") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if parent != -1:
            os.close(parent)


def parse_fleet_private_runtime(raw: object) -> MappingProxyType:
    payload = _require_mapping(raw, {"version", "roles", "services"})
    if payload.get("version") != 1:
        _runtime_config_error()
    roles_raw = _require_mapping(payload.get("roles"), set(ROLE_NAMES))
    services_raw = _require_mapping(payload.get("services"), set(SERVICE_NAMES))
    roles = MappingProxyType(
        {
            "control-plane": _parse_control_plane(roles_raw.get("control-plane")),
            "hypervisor": _parse_linux_role(roles_raw.get("hypervisor")),
            "worker": _parse_linux_role(roles_raw.get("worker")),
        }
    )
    if roles["hypervisor"]["ssh_alias"] == roles["worker"]["ssh_alias"]:
        _runtime_config_error()
    services = MappingProxyType({name: _parse_service_mapping(services_raw.get(name)) for name in SERVICE_NAMES})
    return MappingProxyType({"version": 1, "roles": roles, "services": services})


def project_fleet_public_registry(runtime: Mapping[str, Any]):
    roles = runtime["roles"]
    return create_fleet_registry(
        {
            "targets": [
                {
                    "alias": "control-plane",
                    "tier": PUBLIC_TIER,
                    "adapter": roles["control-plane"]["adapter"],
                    "probe_ids": [HOST_PROBE_ID],
                    "timeout_ms": HOST_TIMEOUT_MS,
                    "freshness_seconds": HOST_FRESHNESS_SECONDS,
                },
                {
                    "alias": "hypervisor",
                    "tier": PUBLIC_TIER,
                    "adapter": roles["hypervisor"]["adapter"],
                    "probe_ids": [HOST_PROBE_ID],
                    "timeout_ms": HOST_TIMEOUT_MS,
                    "freshness_seconds": HOST_FRESHNESS_SECONDS,
                },
                {
                    "alias": "worker",
                    "tier": PUBLIC_TIER,
                    "adapter": roles["worker"]["adapter"],
                    "probe_ids": [HOST_PROBE_ID],
                    "timeout_ms": HOST_TIMEOUT_MS,
                    "freshness_seconds": HOST_FRESHNESS_SECONDS,
                },
            ],
            "services": [
                {
                    "service_id": "research-bridge",
                    "target_alias": "control-plane",
                    "probe_id": HOST_PROBE_ID,
                },
                {
                    "service_id": "virtualization-api",
                    "target_alias": "hypervisor",
                    "probe_id": HOST_PROBE_ID,
                },
                {
                    "service_id": "overlay-network",
                    "target_alias": "worker",
                    "probe_id": HOST_PROBE_ID,
                },
            ],
        }
    )


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


def load_fleet_action_paths_env(env: Mapping[str, str]) -> dict[str, str]:
    action_state_path = _required_absolute_path(env.get("GROKBOT_FLEET_ACTION_STATE_PATH"))
    approval_dir = _required_absolute_path(env.get("GROKBOT_FLEET_APPROVAL_DIR"))
    if paths_overlap(action_state_path, approval_dir):
        _environment_error()
    return {"action_state_path": action_state_path, "approval_dir": approval_dir}


def load_fleet_runtime_env(
    env: Mapping[str, str],
    *,
    dispatch_token: str | None = None,
) -> dict[str, Any]:
    runtime_path = _required_absolute_path(env.get("GROKBOT_FLEET_RUNTIME_PATH"))
    ledger_path = _required_absolute_path(env.get("GROKBOT_FLEET_LEDGER_PATH"))
    action_state_path = _required_absolute_path(env.get("GROKBOT_FLEET_ACTION_STATE_PATH"))
    approval_dir = _required_absolute_path(env.get("GROKBOT_FLEET_APPROVAL_DIR"))
    assert_disjoint_paths([runtime_path, ledger_path, action_state_path, approval_dir])
    return {
        "token": _required_token(env.get("GROKBOT_FLEET_TOKEN"), dispatch_token),
        "host": _required_loopback_host(env.get("GROKBOT_FLEET_HOST")),
        "port": _required_port(env.get("GROKBOT_FLEET_PORT")),
        "runtime_path": runtime_path,
        "ledger_path": ledger_path,
        "action_state_path": action_state_path,
        "approval_dir": approval_dir,
    }


def fleet_probe_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env_source = os.environ if source is None else source
    environment: dict[str, str] = {}
    for key in FLEET_PROBE_ENVIRONMENT_KEYS:
        value = env_source.get(key)
        if value is not None:
            environment[key] = value
    return environment
