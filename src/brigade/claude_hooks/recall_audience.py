"""Who a hook's memory recall is for: the operator's local harness or a connector audience (S18, S24).

The default is ``connector``, which never gets private-class text. A hook
recalls for ``local`` only when the operator opted this host in with
``brigade work hooks install --recall-audience local`` (or ``update``), which
writes a marker under the Brigade user home (``BRIGADE_HOME``, else
``~/.brigade``), outside any repository. Nothing writes it by default.

Threat model: the marker prevents accidental local trust on a copied, synced
or cloud home. It records the host name, the machine id (``/etc/machine-id``,
the Windows ``MachineGuid``, none on macOS) and the device and inode of the
Brigade home, and every one must match, so a copied home, a home mounted on
another machine or a container with a cloned host name falls back to
``connector``. The marker and its two directories must not be symlinks, must
be owned by this user and must not be group or world writable (POSIX). A
process running as the same user is out of scope: it can read the hub token
and the local cards directly, so the boundary against it is the principal's
hub grants, not this file.

The environment can only narrow. ``CLAUDE_CODE_REMOTE`` set to anything but
an explicit false value, or ``BRIGADE_RECALL_AUDIENCE`` set to anything but
``local``, makes the audience ``connector`` and refuses a marker write. No
value widens it, and an unknown value counts as ``connector``. Any failure to
read or match the marker gives ``connector`` and never raises.
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
MARKER_DIR = "claude-hooks"
MARKER_NAME = "recall-audience.json"
MARKER_VERSION = 2
OPT_IN_COMMAND = "brigade work hooks install --target . --recall-audience local"
NOT_ENABLED = "local recall not enabled"
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_MARKER_MAX_BYTES = 4096
_POSIX = sys.platform != "win32"


def _brigade_home() -> Path:
    # Mirrors fleet_client.brigade_home without importing the fleet client into every hook.
    override = os.environ.get("BRIGADE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".brigade"


def marker_path() -> Path:
    return _brigade_home() / MARKER_DIR / MARKER_NAME


def env_narrows() -> bool:
    """True when the environment marks this session as outside the operator's machines."""
    if (os.environ.get(REMOTE_ENV) or "").strip().lower() not in _FALSE_VALUES:
        return True
    raw = os.environ.get(AUDIENCE_ENV)
    return raw is not None and raw.strip().lower() not in {"", LOCAL}


def _host() -> str:
    return socket.gethostname()


def _machine_id() -> str | None:
    """A stable per-machine id, or None where the platform has none Brigade reads."""
    try:
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
    except Exception:  # noqa: BLE001 - no id is a valid answer, never an error
        return None
    return None


def _home_identity(home: Path) -> list[int] | None:
    """Device and inode of the Brigade home's real path."""
    try:
        info = os.stat(os.path.realpath(home))
    except OSError:
        return None
    return [int(info.st_dev), int(info.st_ino)]


def _dir_problem(path: Path, label: str) -> str | None:
    """Why ``path`` cannot hold the marker: a symlink, foreign owner or loose mode (POSIX)."""
    if not _POSIX:
        return None
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return f"{label} missing"
    except OSError:
        return f"{label} unreadable"
    if stat.S_ISLNK(info.st_mode):
        return f"{label} is a symlink"
    if not stat.S_ISDIR(info.st_mode):
        return f"{label} is not a directory"
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and info.st_uid != geteuid():
        return f"{label} is owned by another user"
    if info.st_mode & 0o022:
        return f"{label} is group or world writable"
    return None


def _paths_problem() -> str | None:
    home = _brigade_home()
    return _dir_problem(home, "the Brigade home") or _dir_problem(home / MARKER_DIR, f"{MARKER_DIR}/")


def _binding() -> dict[str, object]:
    return {"host": _host(), "machine_id": _machine_id(), "home": _home_identity(_brigade_home())}


def marker_problem() -> str | None:
    """Why the marker does not grant ``local`` here, or None when it does."""
    path = marker_path()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return NOT_ENABLED
    except OSError:
        return "marker unreadable"
    problem = _paths_problem()
    if problem is not None:
        return problem
    if not stat.S_ISREG(info.st_mode):
        return "marker is not a regular file"
    geteuid = getattr(os, "geteuid", None) if _POSIX else None
    if geteuid is not None and info.st_uid != geteuid():
        return "marker owned by another user"
    if _POSIX and info.st_mode & 0o077:
        return "marker readable or writable by others"
    if info.st_size > _MARKER_MAX_BYTES:
        return "marker too large"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "marker unreadable"
    if not isinstance(raw, dict) or raw.get("v") != MARKER_VERSION or raw.get("audience") != LOCAL:
        return "marker malformed or from an older Brigade"
    binding = _binding()
    if binding["home"] is None or any(raw.get(key) != value for key, value in binding.items()):
        return "marker written on another host"
    return None


def audience() -> str:
    """``local`` only with a valid marker and no narrowing environment, else ``connector``."""
    try:
        if env_narrows() or marker_problem() is not None:
            return CONNECTOR
        return LOCAL
    except Exception:  # noqa: BLE001 - any doubt is a connector audience
        return CONNECTOR


def write_marker() -> str | None:
    """Opt this host in to local recall. Returns why it refused, or None once the marker is valid."""
    if env_narrows():
        return "this environment marks the session as remote"
    home = _brigade_home()
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (home / MARKER_DIR).mkdir(exist_ok=True, mode=0o700)
    except OSError as exc:
        return f"cannot create {home / MARKER_DIR}: {exc.strerror or exc}"
    problem = _paths_problem()
    if problem is not None:
        return problem
    path = marker_path()
    body = json.dumps({"v": MARKER_VERSION, "audience": LOCAL, **_binding()}, sort_keys=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        return f"cannot write {path}: {exc.strerror or exc}"
    return marker_problem()


def clear_marker() -> None:
    """Opt this host out: recall returns to the connector audience."""
    marker_path().unlink(missing_ok=True)


def doctor_check(recall_active: bool = True) -> tuple[str, str, str]:
    """``(status, name, detail)`` for doctor: ok with a valid marker, warn otherwise.

    Without an active recall target the hooks recall nothing, so a missing
    opt-in is information, not a warning.
    """
    name = "claude hooks: recall audience"
    if env_narrows():
        return "warn", name, "this environment marks the session as remote, so recall withholds private-class text"
    problem = marker_problem()
    if problem is None:
        return "ok", name, f"local recall enabled ({marker_path()})"
    if problem == NOT_ENABLED:
        return (
            "warn" if recall_active else "info",
            name,
            f"{NOT_ENABLED}, so recall withholds private-class text; run `{OPT_IN_COMMAND}`",
        )
    return (
        "warn",
        name,
        f"local recall marker refused: {problem} ({marker_path()}), so recall withholds private-class text; "
        f"fix it, then run `{OPT_IN_COMMAND}`",
    )
