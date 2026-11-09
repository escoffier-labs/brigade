"""Who a hook's memory recall is for: the operator's local harness or a connector audience (S18, S24).

The default is ``connector``, which never gets private-class text. A hook
recalls for ``local`` only when this host carries the marker that the local
hook install writes: a 0600 file owned by this user under the Brigade user
home (``BRIGADE_HOME``, else ``~/.brigade``), outside any repository, so a
cloud sandbox that checks out a repository does not get it. The marker names
the host it was written on, so a copied or synced home does not carry it.

The environment can only narrow. ``CLAUDE_CODE_REMOTE`` set to anything but
an explicit false value, or ``BRIGADE_RECALL_AUDIENCE`` set to anything but
``local``, makes the audience ``connector``. No value widens it, and an
unknown value counts as ``connector``.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
from pathlib import Path

LOCAL = "local"
CONNECTOR = "connector"
AUDIENCE_ENV = "BRIGADE_RECALL_AUDIENCE"
REMOTE_ENV = "CLAUDE_CODE_REMOTE"
MARKER_REL = Path("claude-hooks") / "recall-audience.json"
MARKER_VERSION = 1
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_MARKER_MAX_BYTES = 4096


def _brigade_home() -> Path:
    # Mirrors fleet_client.brigade_home without importing the fleet client into every hook.
    override = os.environ.get("BRIGADE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".brigade"


def marker_path() -> Path:
    return _brigade_home() / MARKER_REL


def env_narrows() -> bool:
    """True when the environment marks this session as outside the operator's machines."""
    if (os.environ.get(REMOTE_ENV) or "").strip().lower() not in _FALSE_VALUES:
        return True
    raw = os.environ.get(AUDIENCE_ENV)
    return raw is not None and raw.strip().lower() not in {"", LOCAL}


def _host() -> str:
    return socket.gethostname()


def marker_problem() -> str | None:
    """Why the local marker does not grant ``local`` here, or None when it does."""
    path = marker_path()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    posix = sys.platform != "win32"
    geteuid = getattr(os, "geteuid", None) if posix else None
    if geteuid is not None and info.st_uid != geteuid():
        return "owned by another user"
    if posix and info.st_mode & 0o077:
        return "readable or writable by others"
    if info.st_size > _MARKER_MAX_BYTES:
        return "too large"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(raw, dict) or raw.get("v") != MARKER_VERSION or raw.get("audience") != LOCAL:
        return "malformed"
    if raw.get("host") != _host():
        return "written on another host"
    return None


def audience() -> str:
    """``local`` only with a valid marker and no narrowing environment, else ``connector``."""
    try:
        if env_narrows() or marker_problem() is not None:
            return CONNECTOR
        return LOCAL
    except Exception:  # noqa: BLE001 - any doubt is a connector audience
        return CONNECTOR


def write_marker() -> bool:
    """Write the local marker. Refused (False) when the environment narrows the audience."""
    if env_narrows():
        return False
    path = marker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = json.dumps({"v": MARKER_VERSION, "audience": LOCAL, "host": _host()}, sort_keys=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        return False
    return marker_problem() is None


def doctor_check() -> tuple[str, str, str]:
    """``(status, name, detail)`` for doctor: ok with a valid marker, warn otherwise."""
    name = "claude hooks: recall audience"
    if env_narrows():
        return "warn", name, "this environment marks the session as remote, so recall withholds private-class text"
    problem = marker_problem()
    if problem is None:
        return "ok", name, f"local marker at {marker_path()}"
    return (
        "warn",
        name,
        f"local marker {problem} ({marker_path()}), so recall withholds private-class text; "
        "run `brigade work hooks update --target .` on this host to restore local recall",
    )
