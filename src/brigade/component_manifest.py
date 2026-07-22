"""Versioned manifest for pinned native Brigade components."""

from __future__ import annotations

import json
import platform as host_platform
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from . import templates

SCHEMA_VERSION = 1
SUPPORTED_PLATFORMS: tuple[str, ...] = (
    "linux-amd64",
    "linux-arm64",
    "darwin-amd64",
    "darwin-arm64",
    "windows-amd64",
)
KNOWN_COMPONENT_IDS: tuple[str, ...] = (
    "agent-notify",
    "graphtrail",
    "graphtrail-mcp",
    "miseledger",
    "sessionfind",
)
# Components registered in KNOWN_COMPONENT_IDS but not yet shipped from a Brigade
# release. The bundled compatibility manifest carries them with empty assets so
# the loader accepts the manifest before the first release that publishes them;
# `brigade setup` skips them until a release manifest pins real native assets.
UNPUBLISHED_COMPONENT_IDS: frozenset[str] = frozenset({"agent-notify"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_PLATFORM = re.compile(r"^(linux|darwin|windows)-(amd64|arm64)$")


@dataclass(frozen=True)
class ComponentSource:
    repository: str
    release_tag: str | None = None


@dataclass(frozen=True)
class ComponentAsset:
    asset_name: str
    byte_size: int
    sha256: str
    download_url: str


@dataclass(frozen=True)
class Component:
    component_revision: str
    source: ComponentSource
    executable: str
    assets: dict[str, ComponentAsset]


@dataclass(frozen=True)
class ComponentManifest:
    path: Path
    schema_version: int
    brigade_version: str
    manifest_revision: str
    supported_platforms: tuple[str, ...]
    components: dict[str, Component]
    unknown_component_diagnostics: tuple[str, ...]


def manifest_path() -> Path:
    return templates.template_root() / "components" / "manifest-v1.json"


def load(path: Path | None = None, *, allow_standalone_legacy_revisions: bool = False) -> ComponentManifest:
    source = path or manifest_path()
    try:
        raw = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"component manifest could not be loaded: {source}") from exc
    return _load_raw(
        source,
        raw,
        allow_standalone_legacy_revisions=allow_standalone_legacy_revisions or path is None,
    )


def load_bytes(content: bytes, *, source: Path, allow_standalone_legacy_revisions: bool = False) -> ComponentManifest:
    """Parse an already verified manifest without writing it to disk."""
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"component manifest could not be loaded: {source}") from exc
    return _load_raw(source, raw, allow_standalone_legacy_revisions=allow_standalone_legacy_revisions)


def _load_raw(source: Path, raw: Any, *, allow_standalone_legacy_revisions: bool) -> ComponentManifest:
    if not isinstance(raw, dict):
        raise ValueError(
            f"unsupported component manifest schema version {None!r}; this Brigade supports {SCHEMA_VERSION}"
        )
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported component manifest schema version {schema_version!r}; this Brigade supports {SCHEMA_VERSION}"
        )
    _reject_unexpected_keys(
        raw,
        allowed=frozenset(
            {
                "schema_version",
                "brigade_version",
                "manifest_revision",
                "supported_platforms",
                "components",
            }
        ),
        owner="manifest",
    )
    brigade_version = _required_str(raw, "brigade_version", "manifest")
    manifest_revision = _required_str(raw, "manifest_revision", "manifest")
    supported_platforms = _supported_platforms(raw)
    components_raw = raw.get("components")
    if not isinstance(components_raw, dict):
        raise ValueError("component manifest field 'components' must be an object")

    unknown_ids: list[str] = []
    components: dict[str, Component] = {}
    for name, value in sorted(components_raw.items()):
        if not isinstance(name, str) or not name:
            raise ValueError("component manifest components must be named objects")
        if name not in KNOWN_COMPONENT_IDS:
            unknown_ids.append(name)
            continue
        if not isinstance(value, dict):
            raise ValueError(f"component manifest component {name!r} must be an object")
        components[name] = _parse_known_component(
            name, value, allow_standalone_legacy_revisions=allow_standalone_legacy_revisions
        )

    missing = [component_id for component_id in KNOWN_COMPONENT_IDS if component_id not in components]
    if missing:
        missing_list = ", ".join(missing)
        raise ValueError(f"component manifest is missing required components: {missing_list}")

    diagnostics = _unknown_component_diagnostics(unknown_ids)
    return ComponentManifest(
        path=source,
        schema_version=SCHEMA_VERSION,
        brigade_version=brigade_version,
        manifest_revision=manifest_revision,
        supported_platforms=supported_platforms,
        components=components,
        unknown_component_diagnostics=diagnostics,
    )


def resolve_asset(manifest: ComponentManifest, component_id: str, platform: str) -> ComponentAsset:
    component = manifest.components.get(component_id)
    if component is None:
        known = ", ".join(KNOWN_COMPONENT_IDS)
        raise ValueError(f"unknown component {component_id!r}; known components: {known}")
    if platform not in SUPPORTED_PLATFORMS:
        supported = ", ".join(SUPPORTED_PLATFORMS)
        raise ValueError(f"unsupported platform {platform!r}; supported platform keys: {supported}")
    asset = component.assets.get(platform)
    if asset is None:
        raise ValueError(
            f"unsupported-component-platform: component {component_id!r} has no pinned native "
            f"asset for {platform!r}; published platforms: "
            f"{', '.join(sorted(component.assets)) or 'none'}"
        )
    return asset


def published_component_ids(manifest: ComponentManifest) -> tuple[str, ...]:
    """Return the known components that have pinned native assets in ``manifest``.

    Order follows :data:`KNOWN_COMPONENT_IDS`; unpublished components (empty
    asset matrix) are excluded so setup, smoke, and rollback exact-set logic
    operate only on components a release actually ships.
    """
    return tuple(
        component_id
        for component_id in KNOWN_COMPONENT_IDS
        if component_id in manifest.components and manifest.components[component_id].assets
    )


def platform_key(*, system: str | None = None, machine: str | None = None) -> str:
    os_names = {"linux": "linux", "darwin": "darwin", "windows": "windows"}
    arch_names = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    system_value = (system or host_platform.system()).lower()
    machine_value = (machine or host_platform.machine()).lower()
    os_name = os_names.get(system_value)
    arch = arch_names.get(machine_value)
    if os_name is None or arch is None:
        resolved = f"{system_value}-{machine_value}"
        supported = ", ".join(SUPPORTED_PLATFORMS)
        raise ValueError(f"unsupported platform {resolved}; supported platform keys: {supported}")
    key = f"{os_name}-{arch}"
    if key not in SUPPORTED_PLATFORMS:
        supported = ", ".join(SUPPORTED_PLATFORMS)
        raise ValueError(f"unsupported platform {key}; supported platform keys: {supported}")
    return key


def _supported_platforms(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("supported_platforms")
    if not isinstance(value, list) or not value:
        raise ValueError("component manifest field 'supported_platforms' must be a non-empty array")
    platforms: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _PLATFORM.fullmatch(item):
            raise ValueError("component manifest field 'supported_platforms' must list Go-style platform keys")
        platforms.append(item)
    if tuple(platforms) != SUPPORTED_PLATFORMS:
        expected = ", ".join(SUPPORTED_PLATFORMS)
        found = ", ".join(platforms)
        raise ValueError(f"component manifest field 'supported_platforms' must be exactly: {expected} (found: {found})")
    return tuple(platforms)


def _parse_known_component(name: str, raw: dict[str, Any], *, allow_standalone_legacy_revisions: bool) -> Component:
    _reject_unexpected_keys(
        raw,
        allowed=frozenset({"component_revision", "source", "executable", "assets"}),
        owner=f"component {name!r}",
    )
    component_revision = _required_str(raw, "component_revision", name)
    if not allow_standalone_legacy_revisions and not _GIT_SHA.fullmatch(component_revision):
        raise ValueError(f"component {name!r} field 'component_revision' must be a 40-character lowercase git SHA")
    source_raw = raw.get("source")
    if not isinstance(source_raw, dict):
        raise ValueError(f"component {name!r} field 'source' must be an object")
    _reject_unexpected_keys(
        source_raw,
        allowed=frozenset({"repository", "release_tag"}),
        owner=f"component {name!r} source",
    )
    repository = _required_str(source_raw, "repository", name)
    if repository.count("/") != 1:
        raise ValueError(f"component {name!r} source.repository must be an owner/name pair")
    release_tag_raw = source_raw.get("release_tag")
    release_tag: str | None
    if release_tag_raw is None:
        release_tag = None
    elif not isinstance(release_tag_raw, str) or not release_tag_raw.strip():
        raise ValueError(f"component {name!r} field 'source.release_tag' must be a non-empty string when present")
    else:
        release_tag = release_tag_raw.strip()
    executable = _required_str(raw, "executable", name)
    if executable != name:
        raise ValueError(f"component {name!r} field 'executable' must equal the component id {name!r}")
    assets_raw = raw.get("assets")
    if not isinstance(assets_raw, dict):
        raise ValueError(f"component {name!r} field 'assets' must be an object")
    assets: dict[str, ComponentAsset] = {}
    for key, asset_raw in assets_raw.items():
        if not isinstance(key, str) or not _PLATFORM.fullmatch(key):
            raise ValueError(f"component {name!r} has invalid platform key {key!r}")
        if key not in SUPPORTED_PLATFORMS:
            raise ValueError(f"component {name!r} platform {key!r} is outside the Phase 1 support matrix")
        if not isinstance(asset_raw, dict):
            raise ValueError(f"component {name!r} platform {key!r} asset must be an object")
        assets[key] = _parse_asset(name, executable, key, asset_raw)
    _validate_asset_coverage(name, assets)
    _validate_release_tag_coverage(name, assets, release_tag)
    return Component(
        component_revision=component_revision,
        source=ComponentSource(repository=repository, release_tag=release_tag),
        executable=executable,
        assets=assets,
    )


def _validate_release_tag_coverage(
    name: str,
    assets: dict[str, ComponentAsset],
    release_tag: str | None,
) -> None:
    if assets:
        if not release_tag:
            raise ValueError(f"component {name!r} field 'source.release_tag' must be set when assets are published")
        return
    if release_tag is not None:
        raise ValueError(f"component {name!r} field 'source.release_tag' must be omitted when assets are unpublished")


def _validate_asset_coverage(name: str, assets: dict[str, ComponentAsset]) -> None:
    if not assets:
        if name not in UNPUBLISHED_COMPONENT_IDS:
            raise ValueError(f"component {name!r} must publish assets for every supported platform")
        return
    published = set(assets)
    expected = set(SUPPORTED_PLATFORMS)
    if published != expected:
        missing = ", ".join(sorted(expected - published))
        extra = ", ".join(sorted(published - expected))
        details: list[str] = []
        if missing:
            details.append(f"missing: {missing}")
        if extra:
            details.append(f"unexpected: {extra}")
        detail_text = "; ".join(details)
        raise ValueError(
            f"component {name!r} must publish the full supported platform matrix or no assets ({detail_text})"
        )


def _expected_asset_name(executable: str, platform: str) -> str:
    base = f"{executable}-{platform}"
    if platform == "windows-amd64":
        return f"{base}.exe"
    return base


def _parse_asset(component: str, executable: str, platform: str, raw: dict[str, Any]) -> ComponentAsset:
    owner = f"component {component!r} platform {platform!r}"
    _reject_unexpected_keys(
        raw,
        allowed=frozenset({"asset_name", "byte_size", "sha256", "download_url"}),
        owner=owner,
    )
    asset_name = _required_str(raw, "asset_name", f"{component}/{platform}")
    expected_name = _expected_asset_name(executable, platform)
    if asset_name != expected_name:
        raise ValueError(f"component {component!r} platform {platform!r} field 'asset_name' must be {expected_name!r}")
    if not _safe_asset_name(asset_name):
        raise ValueError(f"component {component!r} platform {platform!r} field 'asset_name' must be a basename")
    byte_size = raw.get("byte_size")
    if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size <= 0:
        raise ValueError(f"component {component!r} platform {platform!r} field 'byte_size' must be a positive integer")
    digest = raw.get("sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(
            f"component {component!r} platform {platform!r} field 'sha256' must be 64 lowercase hex characters"
        )
    download_url = _required_str(raw, "download_url", f"{component}/{platform}")
    _validate_download_url(component, platform, asset_name, download_url)
    return ComponentAsset(
        asset_name=asset_name,
        byte_size=byte_size,
        sha256=digest,
        download_url=download_url,
    )


def _validate_download_url(component: str, platform: str, asset_name: str, download_url: str) -> None:
    parsed = urlparse(download_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(
            f"component {component!r} platform {platform!r} field 'download_url' "
            "must be an immutable https URL with a hostname"
        )
    if parsed.query or parsed.fragment:
        raise ValueError(
            f"component {component!r} platform {platform!r} field 'download_url' "
            "must not include query or fragment components"
        )
    path_name = PurePosixPath(parsed.path).name
    if path_name != asset_name:
        raise ValueError(
            f"component {component!r} platform {platform!r} field 'download_url' "
            "final path segment must equal asset_name"
        )


def _required_str(data: dict[str, Any], key: str, owner: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"component {owner!r} field {key!r} must be a non-empty string")
    return value.strip()


def _reject_unexpected_keys(data: dict[str, Any], *, allowed: frozenset[str], owner: str) -> None:
    for key in sorted(data):
        if key not in allowed:
            raise ValueError(f"{owner} field {key!r} is not supported")


def _safe_asset_name(asset_name: str) -> bool:
    if "/" in asset_name or "\\" in asset_name or asset_name in {".", ".."}:
        return False
    return Path(asset_name).name == asset_name


def _unknown_component_diagnostics(unknown_ids: list[str]) -> tuple[str, ...]:
    if not unknown_ids:
        return ()
    known = ", ".join(KNOWN_COMPONENT_IDS)
    return tuple(
        f"component manifest lists unknown component {component_id!r}; known components: {known}"
        for component_id in unknown_ids
    )
