"""Installed native component state schema v1."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade import localio
from brigade.component_manifest import KNOWN_COMPONENT_IDS, SUPPORTED_PLATFORMS

SCHEMA_VERSION = 1
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "brigade_version",
        "manifest_revision",
        "platform",
        "installed_at",
        "components",
    }
)
_RECORD_KEYS = frozenset(
    {
        "component_revision",
        "asset_name",
        "byte_size",
        "sha256",
        "download_url",
        "executable",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class InstalledComponentRecord:
    component_revision: str
    asset_name: str
    byte_size: int
    sha256: str
    download_url: str
    executable: str


@dataclass(frozen=True)
class InstalledState:
    schema_version: int
    brigade_version: str
    manifest_revision: str
    platform: str
    installed_at: str
    components: dict[str, InstalledComponentRecord]


def _non_empty_str(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _load_record(value: Any) -> InstalledComponentRecord | None:
    if not isinstance(value, dict) or set(value.keys()) != _RECORD_KEYS:
        return None
    byte_size = value["byte_size"]
    if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size <= 0:
        return None
    component_revision = _non_empty_str(value["component_revision"])
    asset_name = _non_empty_str(value["asset_name"])
    download_url = _non_empty_str(value["download_url"])
    executable = _non_empty_str(value["executable"])
    sha256 = value["sha256"]
    if (
        component_revision is None
        or asset_name is None
        or download_url is None
        or executable is None
        or not isinstance(sha256, str)
        or not _SHA256.fullmatch(sha256)
    ):
        return None
    return InstalledComponentRecord(
        component_revision=component_revision,
        asset_name=asset_name,
        byte_size=byte_size,
        sha256=sha256,
        download_url=download_url,
        executable=executable,
    )


def _load_components(value: Any) -> dict[str, InstalledComponentRecord] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, InstalledComponentRecord] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or key not in KNOWN_COMPONENT_IDS:
            return None
        record = _load_record(item)
        if record is None:
            return None
        result[key] = record
    return result


def load_installed_state(path: Path) -> InstalledState | None:
    """Load and validate installed state from path; return None when invalid or missing."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload.keys()) != _TOP_LEVEL_KEYS:
        return None
    schema_version = payload["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        return None
    if schema_version != SCHEMA_VERSION:
        return None
    brigade_version = _non_empty_str(payload["brigade_version"])
    manifest_revision = _non_empty_str(payload["manifest_revision"])
    platform = _non_empty_str(payload["platform"])
    installed_at = _non_empty_str(payload["installed_at"])
    if (
        brigade_version is None
        or manifest_revision is None
        or platform is None
        or installed_at is None
        or platform not in SUPPORTED_PLATFORMS
        or localio.parse_iso_datetime(installed_at) is None
    ):
        return None
    components = _load_components(payload["components"])
    if components is None:
        return None
    return InstalledState(
        schema_version=SCHEMA_VERSION,
        brigade_version=brigade_version,
        manifest_revision=manifest_revision,
        platform=platform,
        installed_at=installed_at,
        components=components,
    )


def render_installed_state(state: InstalledState) -> dict[str, Any]:
    """Render state to a JSON-serializable dict with sorted component keys."""
    return {
        "schema_version": state.schema_version,
        "brigade_version": state.brigade_version,
        "manifest_revision": state.manifest_revision,
        "platform": state.platform,
        "installed_at": state.installed_at,
        "components": {
            key: {
                "component_revision": record.component_revision,
                "asset_name": record.asset_name,
                "byte_size": record.byte_size,
                "sha256": record.sha256,
                "download_url": record.download_url,
                "executable": record.executable,
            }
            for key, record in sorted(state.components.items())
        },
    }


def state_digest_map(state: InstalledState) -> dict[str, str]:
    """Return a mapping of component id to sha256 digest for change detection."""
    return {key: record.sha256 for key, record in sorted(state.components.items())}


def should_rotate_previous(current: InstalledState | None, next_state: InstalledState) -> bool:
    """Return True when the previous state should rotate to the current one.

    Rotation happens on first install, manifest revision change, or any
    component digest change. Idempotent repeat installs with identical
    manifest revisions and digest maps leave the previous state untouched.
    """
    if current is None:
        return False
    if current.manifest_revision != next_state.manifest_revision:
        return True
    return state_digest_map(current) != state_digest_map(next_state)


def write_installed_state(path: Path, state: InstalledState) -> None:
    """Write installed state atomically as sorted JSON."""
    localio.write_json(path, render_installed_state(state))
