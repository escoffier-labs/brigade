"""Passive "new version available" notice.

Notices come only from the on-disk cache; the network refresh runs in a
detached child process so no user command ever waits on it. The endpoint
request carries no parameters and no install id - the User-Agent (version +
OS) plus the request itself is the entire signal. BRIGADE_NO_UPDATE_CHECK
disables both the notice and all network activity.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import __version__, component_paths, localio

CHECK_URL = "https://check.brigade.tools/v1/version"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
NOTIFY_INTERVAL_SECONDS = 24 * 60 * 60
_SKIP_COMMANDS = frozenset({"update", "completions"})


def cache_path(env: Mapping[str, str]) -> Path:
    return Path(component_paths.cache_root(env=env)) / "brigade" / "update-notify.json"


def parse_version(text: object) -> tuple[int, ...] | None:
    """Return an integer tuple for plain x.y.z strings, else None (fail closed)."""
    if not isinstance(text, str):
        return None
    try:
        return tuple(int(part) for part in text.strip().split("."))
    except ValueError:
        return None


def is_newer(candidate: str, current: str) -> bool:
    parsed_candidate = parse_version(candidate)
    parsed_current = parse_version(current)
    if parsed_candidate is None or parsed_current is None:
        return False
    return parsed_candidate > parsed_current


def _gated(argv: list[str], exit_code: int, env: Mapping[str, str], stderr: Any) -> bool:
    if env.get("BRIGADE_NO_UPDATE_CHECK") or env.get("CI"):
        return True
    if exit_code != 0:
        return True
    if argv and argv[0] in _SKIP_COMMANDS:
        return True
    isatty = getattr(stderr, "isatty", None)
    return not callable(isatty) or not isatty()


def _spawn_refresh() -> None:
    detach: dict[str, Any] = (
        {"start_new_session": True} if os.name == "posix" else {"creationflags": 0x00000008}  # DETACHED_PROCESS
    )
    subprocess.Popen(
        [sys.executable, "-m", "brigade", "update", "--notify-refresh"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **detach,
    )


def maybe_notify(
    argv: list[str],
    exit_code: int,
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
    stderr: Any = None,
    spawn: Callable[[], None] | None = None,
) -> None:
    """Print a cached update notice and kick off a background refresh.

    Never raises, never blocks on the network, never alters the exit code.
    """
    try:
        environment = os.environ if env is None else env
        err = sys.stderr if stderr is None else stderr
        current_time = time.time() if now is None else now
        spawn_refresh = _spawn_refresh if spawn is None else spawn
        if _gated(list(argv), exit_code, environment, err):
            return

        path = cache_path(environment)
        state = localio.read_json_dict(path) or {}

        latest = state.get("latest")
        if isinstance(latest, str) and is_newer(latest, __version__):
            notified_at = state.get("notified_at")
            notified_recently = (
                isinstance(notified_at, (int, float)) and current_time - float(notified_at) < NOTIFY_INTERVAL_SECONDS
            )
            if not notified_recently:
                err.write(
                    f'A new brigade release is available: {latest} (installed {__version__}). Run "brigade update".\n'
                )
                state["notified_at"] = current_time
                state["notified_version"] = latest
                localio.write_json(path, state)

        checked_at = state.get("checked_at")
        stale = not isinstance(checked_at, (int, float)) or current_time - float(checked_at) >= CHECK_INTERVAL_SECONDS
        if stale:
            spawn_refresh()
    except Exception:
        return


def run_refresh(*, env: Mapping[str, str] | None = None, now: float | None = None) -> int:
    """Fetch the latest version into the cache. Exit 0 no matter what."""
    environment = os.environ if env is None else env
    if environment.get("BRIGADE_NO_UPDATE_CHECK") or environment.get("CI"):
        return 0
    current_time = time.time() if now is None else now
    path = cache_path(environment)
    state = localio.read_json_dict(path) or {}
    state["checked_at"] = current_time
    try:
        request = urllib.request.Request(
            CHECK_URL,
            headers={"User-Agent": f"brigade-cli/{__version__} ({platform.system()})"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        latest = payload.get("latest") if isinstance(payload, dict) else None
        if parse_version(latest) is not None:
            state["latest"] = latest
    except Exception:
        pass
    try:
        localio.write_json(path, state)
    except OSError:
        pass
    return 0
