"""The per-user Brigade home for fleet credentials, ``hub_url`` and fleet state (#2423).

``BRIGADE_HOME`` set to a relative path resolves against the working directory. A workspace that
holds its own ``.brigade/fleet.toml`` could then choose the fleet hub and the token files whose
contents are sent to it. Every fleet reader resolves the home here, so a relative ``BRIGADE_HOME``
never selects a fleet file: strict and stateful callers get ``RelativeBrigadeHomeError``, and
best-effort callers see no file and log one warning per process.
"""

from __future__ import annotations

from pathlib import Path
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


def user_fleet_section(*, strict: bool = False) -> dict[str, Any]:
    """The ``[fleet]`` table of the user-level ``fleet.toml``; {} when absent or refused."""
    path = fleet_config_path(strict=strict)
    return {} if path is None else fleet_client._read_fleet_section(path, strict=strict)
