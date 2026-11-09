"""Who a hook's memory recall is for: the operator's local harness or a connector audience (S18, S24).

The default is ``connector``, which never gets private-class text. A hook
recalls for ``local`` only when the operator opted this host in with
``brigade work hooks install --recall-audience local`` (or ``update``), which
writes a marker under the Brigade user home (``BRIGADE_HOME``, else
``~/.brigade``), outside any repository. Nothing writes it by default.

Threat model: the marker prevents accidental local trust on a copied, synced
or cloud home. It records the host name, the machine id (``/etc/machine-id``
or the Windows ``MachineGuid``, required there, and none on macOS) and the
device and inode of the Brigade home, and every one must match, so a copied
home, a home mounted on another machine or a container with a cloned host name
falls back to ``connector``. Another user must not be able to swap the files
in: on POSIX the home's parent may be group or world writable only with the
sticky bit and must be owned by this user or root, the home and
``claude-hooks/`` must not be symlinks, foreign owned or group or world
writable, and the checks, the marker read and write and the inode binding all
go through the same directory descriptors. A process running as the same user
is out of scope: it can read the hub token and the local cards directly, so
the boundary against it is the principal's hub grants, not this file.

Opting out writes a marker with audience ``connector`` first, atomically, and
then removes it, so a removal that fails still leaves recall on connector.

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
from typing import Callable

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
# macOS has no machine id Brigade reads. Everywhere else the binding needs one.
_MACHINE_ID_OPTIONAL = sys.platform == "darwin"
NO_MACHINE_ID = "this host has no readable machine id"
OPT_OUT_FAILED = "local recall may still be on: could not replace or remove the marker"
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


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


class _Refused(Exception):
    """A check refused the marker; the message names why, never the content."""


def _between_check_and_read() -> None:
    """No-op seam between the directory checks and the marker read. Tests swap paths here."""


def _check_dir_fd(fd: int, label: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise _Refused(f"{label} is not a directory")
    if info.st_uid != os.geteuid():
        raise _Refused(f"{label} is owned by another user")
    if info.st_mode & 0o022:
        raise _Refused(f"{label} is group or world writable")


def _open_child_dir(parent_fd: int, name: str, label: str) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise _Refused(f"{label} missing") from None
    except OSError:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise _Refused(f"{label} unreadable") from None
        if stat.S_ISLNK(info.st_mode):
            raise _Refused(f"{label} is a symlink") from None
        raise _Refused(f"{label} is not a directory") from None


class _Dirs:
    """The Brigade home and its claude-hooks directory, held open after their checks (POSIX).

    The parent of the home must not let another user swap the home: it may be
    group or world writable only with the sticky bit, and must be owned by this
    user or root. Every later read, write and the inode binding go through these
    descriptors, so the checks and the use stay bound to the same directories.
    """

    def __init__(self, *, create: bool) -> None:
        self.fds: list[int] = []
        home = Path(os.path.abspath(_brigade_home()))
        if create:
            try:
                home.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise _Refused(f"cannot create the Brigade home: {exc.strerror or exc}") from None
        try:
            parent = self._keep(os.open(home.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
            info = os.fstat(parent)
            if info.st_uid not in {os.geteuid(), 0}:
                raise _Refused("the Brigade home's parent is owned by another user")
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise _Refused("the Brigade home's parent is writable by others without the sticky bit")
            self.home = self._keep(_open_child_dir(parent, home.name, "the Brigade home"))
            _check_dir_fd(self.home, "the Brigade home")
            if create:
                try:
                    os.mkdir(MARKER_DIR, 0o700, dir_fd=self.home)
                except FileExistsError:
                    pass
            self.hooks = self._keep(_open_child_dir(self.home, MARKER_DIR, f"{MARKER_DIR}/"))
            _check_dir_fd(self.hooks, f"{MARKER_DIR}/")
        except BaseException:
            self.close()
            raise

    def _keep(self, fd: int) -> int:
        self.fds.append(fd)
        return fd

    def home_identity(self) -> list[int]:
        info = os.fstat(self.home)
        return [int(info.st_dev), int(info.st_ino)]

    def close(self) -> None:
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()

    def __enter__(self) -> _Dirs:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _windows_home_identity() -> list[int] | None:
    try:
        info = os.stat(os.path.realpath(_brigade_home()))
    except OSError:
        return None
    return [int(info.st_dev), int(info.st_ino)]


def _binding(home_identity: list[int] | None) -> dict[str, object]:
    return {"host": _host(), "machine_id": _machine_id(), "home": home_identity}


def _validate(raw: object, home_identity: list[int] | None) -> str | None:
    if not isinstance(raw, dict) or raw.get("v") != MARKER_VERSION:
        return "marker malformed or from an older Brigade"
    if raw.get("audience") != LOCAL:
        return NOT_ENABLED  # an opt-out writes audience connector
    binding = _binding(home_identity)
    if binding["machine_id"] is None and not _MACHINE_ID_OPTIONAL:
        return NO_MACHINE_ID
    if home_identity is None or any(raw.get(key) != value for key, value in binding.items()):
        return "marker written on another host"
    return None


def _load_in(dirs: _Dirs) -> dict | str:
    """The marker's JSON read through the checked descriptors, or why it cannot be read."""
    try:
        fd = os.open(MARKER_NAME, _FILE_FLAGS, dir_fd=dirs.hooks)
    except FileNotFoundError:
        return NOT_ENABLED
    except OSError:
        return "marker unreadable"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "marker is not a regular file"
        if info.st_uid != os.geteuid():
            return "marker owned by another user"
        if info.st_mode & 0o077:
            return "marker readable or writable by others"
        if info.st_size > _MARKER_MAX_BYTES:
            return "marker too large"
        data = os.read(fd, _MARKER_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "marker unreadable"
    return raw if isinstance(raw, dict) else "marker malformed or from an older Brigade"


def _refusal_text(refused: _Refused) -> str:
    return NOT_ENABLED if str(refused).endswith(" missing") else str(refused)


def _read_posix() -> str | None:
    try:
        with _Dirs(create=False) as dirs:
            _between_check_and_read()
            loaded = _load_in(dirs)
            if isinstance(loaded, str):
                return loaded
            return _validate(loaded, dirs.home_identity())
    except _Refused as refused:
        return _refusal_text(refused)


def _read_windows() -> str | None:
    path = marker_path()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return NOT_ENABLED
    except OSError:
        return "marker unreadable"
    if not stat.S_ISREG(info.st_mode):
        return "marker is not a regular file"
    if info.st_size > _MARKER_MAX_BYTES:
        return "marker too large"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "marker unreadable"
    return _validate(raw, _windows_home_identity())


def marker_problem() -> str | None:
    """Why the marker does not grant ``local`` here, or None when it does. Never raises."""
    try:
        return _read_posix() if _POSIX else _read_windows()
    except Exception:  # noqa: BLE001 - any failure is a refusal
        return "marker unreadable"


def audience() -> str:
    """``local`` only with a valid marker and no narrowing environment, else ``connector``."""
    try:
        if env_narrows() or marker_problem() is not None:
            return CONNECTOR
        return LOCAL
    except Exception:  # noqa: BLE001 - any doubt is a connector audience
        return CONNECTOR


def _write_in(dirs: _Dirs, body: str) -> None:
    """Replace the marker atomically inside the checked claude-hooks descriptor."""
    tmp = f".{MARKER_NAME}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=dirs.hooks)
    try:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, body.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, MARKER_NAME, src_dir_fd=dirs.hooks, dst_dir_fd=dirs.hooks)
    finally:
        try:
            os.unlink(tmp, dir_fd=dirs.hooks)
        except OSError:
            pass


def _write_posix(make_body: Callable[[list[int] | None], str]) -> None:
    with _Dirs(create=True) as dirs:
        _write_in(dirs, make_body(dirs.home_identity()))


def _write_windows(make_body: Callable[[list[int] | None], str]) -> None:
    path = marker_path()
    # Create the home and claude-hooks first: a fresh home has no identity to bind
    # until it exists. (POSIX creates them inside _Dirs before reading the identity.)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = make_body(_windows_home_identity())
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write(audience_value: str) -> str | None:
    """Write a marker for ``audience_value``. Returns why it failed, or None.

    A local marker binds the home it is written into: the identity comes from
    the same open directory the file is created in.
    """
    if audience_value == LOCAL and _machine_id() is None and not _MACHINE_ID_OPTIONAL:
        return NO_MACHINE_ID

    def make_body(identity: list[int] | None) -> str:
        if audience_value != LOCAL:
            return json.dumps({"v": MARKER_VERSION, "audience": CONNECTOR}, sort_keys=True) + "\n"
        if identity is None:
            raise _Refused("the Brigade home has no device and inode")
        payload = {"v": MARKER_VERSION, "audience": LOCAL, **_binding(identity)}
        return json.dumps(payload, sort_keys=True) + "\n"

    try:
        (_write_posix if _POSIX else _write_windows)(make_body)
    except _Refused as refused:
        return str(refused)
    except OSError as exc:
        return f"cannot write {marker_path()}: {exc.strerror or exc}"
    return None


def write_marker() -> str | None:
    """Opt this host in to local recall. Returns why it refused, or None once the marker is valid."""
    if env_narrows():
        return "this environment marks the session as remote"
    try:
        return _write(LOCAL) or marker_problem()
    except Exception as exc:  # noqa: BLE001 - an opt-in reports, never raises
        return f"cannot write {marker_path()}: {type(exc).__name__}"


_CONNECTOR_BODY = json.dumps({"v": MARKER_VERSION, "audience": CONNECTOR}, sort_keys=True) + "\n"


def _unlink_in(dirs: _Dirs | None) -> None:
    """Remove the marker: through the checked descriptor on POSIX, by path on Windows."""
    if dirs is None:
        marker_path().unlink(missing_ok=True)
        return
    try:
        os.unlink(MARKER_NAME, dir_fd=dirs.hooks)
    except FileNotFoundError:
        pass


def _opt_out_posix() -> bool:
    """True once the marker is gone or reads as connector through the same descriptors."""
    try:
        with _Dirs(create=True) as dirs:
            try:
                _write_in(dirs, _CONNECTOR_BODY)
            except OSError:
                pass  # the removal or the re-read below decides
            try:
                _unlink_in(dirs)
                return True
            except OSError:
                pass
            loaded = _load_in(dirs)
            return (
                isinstance(loaded, dict) and loaded.get("v") == MARKER_VERSION and loaded.get("audience") == CONNECTOR
            )
    except _Refused:
        # The same checks refuse every read, so no marker here can grant local.
        return marker_problem() is not None
    except OSError:
        return marker_problem() is not None


def _opt_out_windows() -> bool:
    try:
        _write_windows(lambda _identity: _CONNECTOR_BODY)
    except OSError:
        pass
    try:
        _unlink_in(None)
        return True
    except OSError:
        pass
    try:
        raw = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get("v") == MARKER_VERSION and raw.get("audience") == CONNECTOR


def opt_out() -> str | None:
    """Opt this host out. Returns a fixed refusal unless recall is left on connector.

    A connector marker goes in first, atomically, then the file is removed. If
    the removal fails, the marker is read again through the same descriptors,
    and only a connector marker counts as success.
    """
    try:
        done = _opt_out_posix() if _POSIX else _opt_out_windows()
    except Exception:  # noqa: BLE001 - an opt-out reports, never raises
        done = False
    return None if done else f"{OPT_OUT_FAILED} ({marker_path()})"


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
