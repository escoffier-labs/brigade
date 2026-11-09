"""Who a dispatch packet's memory text is for: this host's own harness or a connector (S25).

The default is ``connector``, which never gets private-class card text. A packet
is built for ``local`` only when the operator opted this host in to local recall
(S24, ``brigade work hooks install --recall-audience local``), which writes a
marker under the Brigade user home (``BRIGADE_HOME``, else ``~/.brigade``) at
``claude-hooks/recall-audience.json``. The marker is version 2 and binds the
host name, the machine id and the device and inode of the Brigade home, and
every one must match. The marker and its two directories must not be symlinks,
must be owned by this user and must not be group or world writable (POSIX). A
copied or synced home, a home mounted on another machine and a cloud sandbox
all stay ``connector``.

The environment only narrows: ``CLAUDE_CODE_REMOTE`` set to anything but an
explicit false value, or ``BRIGADE_RECALL_AUDIENCE`` set to anything but
``local``, makes the audience ``connector``. Any failure gives ``connector``.

This reads the marker S24's hook recall audience writes and writes nothing.
Once both land, one reader can serve hooks and dispatch packets.
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
AUDIENCES = frozenset({LOCAL, CONNECTOR})
AUDIENCE_ENV = "BRIGADE_RECALL_AUDIENCE"
REMOTE_ENV = "CLAUDE_CODE_REMOTE"
MARKER_DIR = "claude-hooks"
MARKER_NAME = "recall-audience.json"
MARKER_VERSION = 2
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_MARKER_MAX_BYTES = 4096
_POSIX = sys.platform != "win32"


def _home() -> Path:
    override = os.environ.get("BRIGADE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".brigade"


def marker_path() -> Path:
    return _home() / MARKER_DIR / MARKER_NAME


def _env_narrows() -> bool:
    if (os.environ.get(REMOTE_ENV) or "").strip().lower() not in _FALSE_VALUES:
        return True
    raw = os.environ.get(AUDIENCE_ENV)
    return raw is not None and raw.strip().lower() not in {"", LOCAL}


def _machine_id() -> str | None:
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip() or None
    if sys.platform == "darwin":
        return None
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(candidate).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if value:
            return value
    return None


def binding() -> dict[str, object]:
    """The host facts a local marker must record."""
    info = os.stat(os.path.realpath(_home()))
    return {"host": socket.gethostname(), "machine_id": _machine_id(), "home": [int(info.st_dev), int(info.st_ino)]}


def _owned_private(path: Path, *, directory: bool) -> bool:
    info = os.lstat(path)
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or not kind_ok:
        return False
    if not _POSIX:
        return True
    loose = 0o022 if directory else 0o077
    return info.st_uid == os.geteuid() and not info.st_mode & loose


def _marker_valid() -> bool:
    path = marker_path()
    if not (_owned_private(_home(), directory=True) and _owned_private(path.parent, directory=True)):
        return False
    if not _owned_private(path, directory=False) or os.lstat(path).st_size > _MARKER_MAX_BYTES:
        return False
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("v") != MARKER_VERSION or raw.get("audience") != LOCAL:
        return False
    return all(raw.get(key) == value for key, value in binding().items())


def host_audience() -> str:
    """``local`` only with a valid marker and no narrowing environment. Any doubt is ``connector``."""
    try:
        if _env_narrows() or not _marker_valid():
            return CONNECTOR
        return LOCAL
    except Exception:  # noqa: BLE001 - a missing, unreadable or odd marker is a connector audience
        return CONNECTOR
