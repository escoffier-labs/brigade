"""The per-user Brigade home for fleet credentials, ``hub_url`` and fleet state (#2423).

``BRIGADE_HOME`` set to a relative path resolves against the working directory. A workspace that
holds its own ``.brigade/fleet.toml`` could then choose the fleet hub and the token files whose
contents are sent to it. Every fleet reader resolves the home here, so a relative ``BRIGADE_HOME``
never selects a fleet file: strict and stateful callers get ``RelativeBrigadeHomeError``, and
best-effort callers see no file and log one warning per process.

A token file named in that ``fleet.toml`` resolves the same way: ``~`` expands, and a relative path
is read under the directory of that ``fleet.toml``, never under the working directory.
"""

from __future__ import annotations

import errno
import os
import shlex
import stat
import sys
from pathlib import Path, PurePath
from typing import Any

from . import fleet_client

RELATIVE_HOME_REFUSAL = (
    "BRIGADE_HOME must be an absolute path: a relative one resolves against the working directory, "
    "so a workspace's .brigade/fleet.toml could choose the fleet hub and credentials"
)
_WARNED = False


class RelativeBrigadeHomeError(fleet_client.FleetClientError):
    """``BRIGADE_HOME`` is relative, so the fleet home would follow the working directory."""

    code = "relative_brigade_home"


def absolute_brigade_home() -> Path | None:
    """The per-user Brigade home, or None when ``BRIGADE_HOME`` is set to a relative path."""
    home = fleet_client.brigade_home()
    return home if home.is_absolute() else None


def fleet_home() -> Path:
    """The per-user Brigade home for fleet state; raises ``RelativeBrigadeHomeError`` when relative."""
    home = absolute_brigade_home()
    if home is None:
        raise RelativeBrigadeHomeError(RELATIVE_HOME_REFUSAL)
    return home


def fleet_config_path(*, strict: bool = False) -> Path | None:
    """The user-level ``fleet.toml``, never one chosen by a relative ``BRIGADE_HOME``.

    With a relative ``BRIGADE_HOME`` a strict caller gets ``RelativeBrigadeHomeError``. Any other
    caller gets None, so the file counts as absent, and one warning per process names the problem.
    """
    global _WARNED
    home = absolute_brigade_home()
    if home is not None:
        return home / fleet_client.FLEET_CONFIG_REL_PATH.name
    if strict:
        raise RelativeBrigadeHomeError(RELATIVE_HOME_REFUSAL)
    if not _WARNED:
        _WARNED = True
        fleet_client._LOG.warning("fleet: %s; ignoring the fleet configuration file", RELATIVE_HOME_REFUSAL)
    return None


def user_fleet_config(*, strict: bool = False) -> tuple[Path | None, dict[str, Any]]:
    """The guarded ``fleet.toml`` path and its ``[fleet]`` table, from one resolution of the home.

    Token files named in that table are read with ``read_token_file(raw, path)`` against this same
    path, so a ``BRIGADE_HOME`` change between the two reads cannot move them to another home.
    """
    path = fleet_config_path(strict=strict)
    return path, ({} if path is None else fleet_client._read_fleet_section(path, strict=strict))


def user_fleet_section(*, strict: bool = False) -> dict[str, Any]:
    """The ``[fleet]`` table of the user-level ``fleet.toml``; {} when absent or refused."""
    return user_fleet_config(strict=strict)[1]


def resolve_against(path: PurePath, config_dir: PurePath) -> PurePath | None:
    """``path`` when absolute, else ``path`` under ``config_dir``.

    None for a Windows drive-relative (``C:token``) or root-relative (``\\token``) path, which would
    still follow the working drive or directory.
    """
    if path.is_absolute():
        return path
    if path.drive or path.root:
        return None
    return config_dir / path


def token_path(raw: str, config_path: Path) -> Path | None:
    """A token file path from ``config_path``: ``~`` expanded, a relative one under its directory."""
    resolved = resolve_against(Path(raw.strip()).expanduser(), config_path.parent)
    return None if resolved is None else Path(resolved)


def open_token_fd(path: str) -> int:
    """Open a token file without following the final path component."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        # NONBLOCK lets callers check the file type before any read.
        return os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
    if sys.platform == "win32":
        # Windows uses a handle opened with FILE_FLAG_OPEN_REPARSE_POINT.
        try:
            from .work_cmd import nt_dirfd
        except ImportError:  # no ctypes on this build: fail closed as a credential error
            raise ValueError from None
        return nt_dirfd.open_path_file(os.path.abspath(path), os.O_RDONLY)
    raise ValueError


class FleetTokenFileError(fleet_client.FleetClientError):
    """A configured fleet token file failed validation or could not be read."""

    code = "token-file-refused"


class FleetTokenPermissionError(FleetTokenFileError):
    """A fleet token file has the wrong owner or mode."""


def _checks_posix_permissions() -> bool:
    # Windows uses ACLs and synthesizes mode bits without POSIX ownership semantics.
    return os.name == "posix"


def _open_configured_token(raw: str, config_path: Path) -> tuple[int, str] | None:
    candidates = [raw.strip()]
    if "\\" in raw:
        candidates.append(raw.strip().replace("\\", "/"))
    for candidate in candidates:
        try:
            path = token_path(candidate, config_path)
        except (OSError, ValueError, RuntimeError):
            raise FleetTokenFileError(
                f"{candidate}: unresolvable token file path. Update the configured path."
            ) from None
        if path is None:
            raise FleetTokenFileError(f"{candidate}: unresolvable token file path. Update the configured path.")
        try:
            # Configured symlinks resolve to a target whose descriptor is checked below.
            target = path.resolve(strict=True)
            fd = open_token_fd(str(target))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RuntimeError) as exc:
            if isinstance(exc, (RuntimeError, ValueError)) or exc.errno == errno.ELOOP:
                raise FleetTokenFileError(f"{path}: unresolvable token file path. Check the configured path.") from None
            raise FleetTokenFileError(
                f"{path}: cannot open token file. Check the path and access permissions."
            ) from None
        try:
            info = os.fstat(fd)
            label = str(path) if target == path else f"{path} (target {target})"
            if not stat.S_ISREG(info.st_mode):
                raise FleetTokenFileError(f"{label}: expected a regular token file. Update the configured path.")
            if _checks_posix_permissions():
                uid = os.geteuid()
                quoted = shlex.quote(str(target))
                mode = stat.S_IMODE(info.st_mode)
                if info.st_uid != uid:
                    import pwd

                    try:
                        owner = shlex.quote(pwd.getpwuid(uid).pw_name)
                    except (KeyError, OSError):
                        owner = str(uid)
                    raise FleetTokenPermissionError(
                        f"{label}: wrong owner (UID {info.st_uid}). Run chown {owner} {quoted}."
                    )
                if mode & 0o077:
                    raise FleetTokenPermissionError(
                        f"{label}: too permissive (mode {mode:04o}). Run chmod 600 {quoted}."
                    )
                detail = f"{label}: ok (mode {mode:04o})"
            else:
                detail = f"{label}: ok (POSIX ownership and mode checks skipped)"
            return fd, detail
        except OSError:
            os.close(fd)
            raise FleetTokenFileError(f"{path}: cannot inspect token file. Check access permissions.") from None
        except BaseException:
            os.close(fd)
            raise
    return None


def read_token_file(raw: str, config_path: Path | None) -> str:
    """Read a configured token, returning an empty string for a missing file."""
    opened = _open_configured_token(raw, config_path) if config_path is not None else None
    if opened is None:
        return ""
    fd, _ = opened
    try:
        with open(fd, encoding="utf-8", closefd=False) as stream:
            return stream.read().strip()
    except (OSError, UnicodeError):
        raise FleetTokenFileError(f"{raw.strip()}: cannot read token file. Check access and UTF-8 encoding.") from None
    finally:
        os.close(fd)


def load_fleet_settings(
    *, strict: bool = False, include_node: bool = True, include_admin: bool = True, optional_admin: bool = False
) -> dict[str, str]:
    """Resolve the endpoint and requested credentials from the environment or guarded config.

    Nonempty environment settings override their file settings. Excluded
    credentials remain empty. Strict mode applies to reading the config.
    Message authentication can compare an available admin credential without
    requiring its file to be usable.
    """
    hub_url = os.environ.get("BRIGADE_FLEET_HUB_URL", "").strip()
    admin_token = os.environ.get("BRIGADE_FLEET_TOKEN", "").strip() if include_admin else ""
    node_token = os.environ.get("BRIGADE_FLEET_NODE_TOKEN", "").strip() if include_node else ""
    if not hub_url or (include_admin and not admin_token) or (include_node and not node_token):
        config_path, section = user_fleet_config(strict=strict)
        if not hub_url and isinstance(section.get("hub_url"), str):
            hub_url = section["hub_url"].strip()
        if include_admin and not admin_token:
            token_file = section.get("token_file")
            if isinstance(token_file, str) and token_file.strip():
                try:
                    admin_token = read_token_file(token_file, config_path)
                except FleetTokenFileError:
                    if not optional_admin:
                        raise
        if include_node and not node_token:
            node_token_file = section.get("node_token_file")
            if isinstance(node_token_file, str) and node_token_file.strip():
                node_token = read_token_file(node_token_file, config_path)
    return {"hub_url": hub_url, "admin_token": admin_token, "node_token": node_token}
