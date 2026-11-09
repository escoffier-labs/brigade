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


def read_token_file(raw: str, config_path: Path | None) -> str:
    """The token in a file named by the ``fleet.toml`` at ``config_path``, or "" when there is none.

    The caller passes the path it read that table from (see ``user_fleet_config``); this never
    resolves the home again. A path written with Windows backslashes is tried as written first,
    then with ``/``.
    """
    base = config_path
    if base is None:
        return ""
    candidates = [raw.strip()]
    if "\\" in raw:
        candidates.append(raw.strip().replace("\\", "/"))
    for candidate in candidates:
        path = token_path(candidate, base)
        if path is None:
            continue
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""
