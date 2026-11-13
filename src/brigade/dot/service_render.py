"""Descriptor-safe connector files and service rendering helpers."""

from __future__ import annotations
import re
import subprocess
import unicodedata
from . import transport as grokbot_mcp
from pathlib import Path
from brigade.connector_filesystem import (
    _mkdir_windows_child as _mkdir_windows_child,
    _open_parent_nofollow as _open_parent_nofollow,
    _open_windows_parent_nofollow as _open_windows_parent_nofollow,
    _path_is_symlink as _path_is_symlink,
    _read_regular_text as _read_regular_text,
    _write_text_nofollow_atomic as _write_text_nofollow_atomic,
    os as os,
    remove_regular_file as remove_regular_file,
)


CONFIG_DIR = Path(".brigade") / "grokbot"


DEFAULT_TIMEOUT_SECONDS = 5


LISTENER_RECOVERY_UNIT_FRAGMENT = "StartLimitIntervalSec=15min\nStartLimitBurst=3\n"


LISTENER_RECOVERY_SERVICE_FRAGMENT = "Restart=on-failure\nRestartSec=60s\nRestartPreventExitStatus=2\n"


_SYSTEMD_UNQUOTED_ARG_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")


class ServiceRenderError(RuntimeError):
    """Unit rendering or installation was refused."""


def unit_name(instance: str, client_id: str | None = None) -> str:
    name = instance if client_id is None else grokbot_mcp.deployment_name(instance, client_id)
    return f"brigade-grokbot-{name}.service"


def service_result_argv(instance: str, client_id: str | None = None) -> list[str]:
    return [
        "systemctl",
        "--user",
        "show",
        unit_name(instance, client_id),
        "--property=Result",
        "--value",
    ]


def inspect_service_result(
    instance: str, *, client_id: str | None = None, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, str]:
    """Return a sanitized service-result check. Never mutates systemd or echoes raw output."""
    failed = {"check": "service-result", "status": "fail"}
    argv = service_result_argv(instance, client_id)
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return failed
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    if completed.returncode == 0 and stdout.strip() == "success":
        return {"check": "service-result", "status": "ok"}
    return failed


def config_path(target: Path, instance: str, client_id: str | None = None) -> Path:
    name = instance if client_id is None else grokbot_mcp.deployment_name(instance, client_id)
    return target / CONFIG_DIR / f"{name}.json"


def _connect_host(host: str) -> str:
    if host in {"localhost", "0.0.0.0", "::", ""} or host.startswith("::"):
        return "127.0.0.1"
    return host


def _systemd_quote(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "$" in value
        or "%" in value
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise grokbot_mcp.ConfigurationError("invalid")
    if _SYSTEMD_UNQUOTED_ARG_RE.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
