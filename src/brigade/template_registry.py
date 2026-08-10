"""Bundled template-profile registry: selection presets, harness versions, render hashes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import __version__
from .install import build_render_context, resolve_manifests
from .selection import Selection
from .templates import is_text, render, template_root
from .update_notify import parse_version

SCHEMA = "brigade.template_registry.v1"
PROFILE_IDENTITY_PREFIX = "brigade://template-profiles/"


class UnknownTemplateProfile(ValueError):
    """A named template profile is not in the bundled registry."""

    def __init__(self, profile_name: str) -> None:
        self.profile_name = profile_name
        super().__init__(f"unknown template profile: {profile_name!r}")


class UnsupportedHarnessVersion(ValueError):
    """A harness version is below the profile's supported minimum."""

    def __init__(self, harness_id: str, version: str, minimum: str) -> None:
        self.harness_id = harness_id
        self.version = version
        self.minimum = minimum
        super().__init__(f"harness {harness_id!r} version {version!r} is below profile minimum {minimum!r}")


BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "repo-claude": {
        "description": "Minimal repo install with Claude Code bridge.",
        "selection": {
            "depth": "repo",
            "harnesses": ["claude"],
            "owner": "claude",
            "includes": [],
        },
        "supported_harness_versions": {
            "claude": {"min": "0.0.0", "tested": "0.0.0"},
        },
    },
    "workspace-claude-codex": {
        "description": "Full workspace with Claude and Codex bridges.",
        "selection": {
            "depth": "workspace",
            "harnesses": ["claude", "codex"],
            "owner": "claude",
            "includes": [],
        },
        "supported_harness_versions": {
            "claude": {"min": "0.0.0", "tested": "0.0.0"},
            "codex": {"min": "0.0.0", "tested": "0.0.0"},
        },
    },
    "repo-claude-full": {
        "description": "Repo depth with Claude and the full kit (repo-extras).",
        "selection": {
            "depth": "repo",
            "harnesses": ["claude"],
            "owner": "claude",
            "includes": ["repo-extras"],
        },
        "supported_harness_versions": {
            "claude": {"min": "0.0.0", "tested": "0.0.0"},
        },
    },
}


def registry_path() -> Path:
    return template_root() / "template-registry.json"


def _version_at_least(version: str, minimum: str) -> bool:
    parsed_version = parse_version(version)
    parsed_minimum = parse_version(minimum)
    if parsed_version is None or parsed_minimum is None:
        return False
    width = max(len(parsed_version), len(parsed_minimum))
    return tuple(parsed_version + (0,) * (width - len(parsed_version))) >= tuple(
        parsed_minimum + (0,) * (width - len(parsed_minimum))
    )


def _validate_harness_version_spec(harness_id: str, spec: Any) -> dict[str, str]:
    if not isinstance(spec, dict):
        raise ValueError(f"supported_harness_versions[{harness_id!r}] must be an object")
    minimum = spec.get("min")
    tested = spec.get("tested")
    if not isinstance(minimum, str) or parse_version(minimum) is None:
        raise ValueError(f"supported_harness_versions[{harness_id!r}].min must be a semver string")
    if not isinstance(tested, str) or parse_version(tested) is None:
        raise ValueError(f"supported_harness_versions[{harness_id!r}].tested must be a semver string")
    return {"min": minimum, "tested": tested}


def _selection_from_mapping(raw: Mapping[str, Any]) -> Selection:
    depth = raw.get("depth")
    harnesses = raw.get("harnesses")
    owner = raw.get("owner", "this-repo")
    includes = raw.get("includes", [])
    if not isinstance(depth, str):
        raise ValueError("profile selection.depth must be a string")
    if not isinstance(harnesses, list) or not all(isinstance(item, str) for item in harnesses):
        raise ValueError("profile selection.harnesses must be a string list")
    if not isinstance(owner, str):
        raise ValueError("profile selection.owner must be a string")
    if not isinstance(includes, list) or not all(isinstance(item, str) for item in includes):
        raise ValueError("profile selection.includes must be a string list")
    selection = Selection(
        depth=depth,
        harnesses=list(harnesses),
        owner=owner,
        includes=list(includes),
    )
    selection.validate()
    return selection


def _profile_record(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    description = raw.get("description")
    selection_raw = raw.get("selection")
    versions_raw = raw.get("supported_harness_versions")
    if not isinstance(description, str) or not description:
        raise ValueError(f"profile {name!r} needs a non-empty description")
    if not isinstance(selection_raw, dict):
        raise ValueError(f"profile {name!r} selection must be an object")
    selection = _selection_from_mapping(selection_raw)
    if not isinstance(versions_raw, dict):
        raise ValueError(f"profile {name!r} supported_harness_versions must be an object")
    versions: dict[str, dict[str, str]] = {}
    for harness_id in selection.harnesses:
        if harness_id not in versions_raw:
            raise ValueError(f"profile {name!r} must declare supported_harness_versions for harness {harness_id!r}")
        versions[harness_id] = _validate_harness_version_spec(harness_id, versions_raw[harness_id])
    extra = set(versions_raw) - set(selection.harnesses)
    if extra:
        raise ValueError(
            f"profile {name!r} supported_harness_versions has entries for unselected harnesses: {sorted(extra)}"
        )
    return {
        "description": description,
        "identity": f"{PROFILE_IDENTITY_PREFIX}{name}",
        "selection": {
            "depth": selection.depth,
            "harnesses": selection.harnesses,
            "owner": selection.owner,
            "includes": selection.includes,
        },
        "supported_harness_versions": versions,
    }


def compute_profile_renders(selection: Selection) -> list[dict[str, Any]]:
    """Return deterministic rendered-file hashes for a selection."""
    files, _, _ = resolve_manifests(selection)
    context = build_render_context(selection)
    root = template_root()
    renders: list[dict[str, Any]] = []
    for entry in sorted(files, key=lambda item: item["dst"]):
        src = root / entry["src"]
        if not src.is_file():
            raise FileNotFoundError(f"template missing for render hash: {src}")
        if is_text(entry["src"]):
            content = render(src.read_text(encoding="utf-8"), context).encode("utf-8")
            rendered = True
        else:
            content = src.read_bytes()
            rendered = False
        renders.append(
            {
                "dst": entry["dst"],
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_size": len(content),
                "rendered": rendered,
            }
        )
    return renders


def build_registry(profile_names: Sequence[str] | None = None) -> dict[str, Any]:
    names = sorted(profile_names or BUILTIN_PROFILES)
    profiles: dict[str, dict[str, Any]] = {}
    renders: dict[str, list[dict[str, Any]]] = {}
    for name in names:
        raw = BUILTIN_PROFILES.get(name)
        if raw is None:
            raise ValueError(f"unknown built-in profile: {name!r}")
        profile = _profile_record(name, raw)
        profiles[name] = profile
        selection = _selection_from_mapping(profile["selection"])
        renders[name] = compute_profile_renders(selection)
    return {
        "schema": SCHEMA,
        "brigade_version": __version__,
        "profiles": profiles,
        "renders": renders,
    }


def render_registry(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _validate_render_records(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError("render records must be a list")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("render record must be an object")
        dst = record.get("dst")
        sha256 = record.get("sha256")
        byte_size = record.get("byte_size")
        rendered = record.get("rendered")
        if not isinstance(dst, str) or not dst or dst in seen:
            raise ValueError(f"invalid or duplicate render dst: {dst!r}")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError(f"render dst {dst!r} needs a sha256 digest")
        if not isinstance(byte_size, int) or byte_size < 0:
            raise ValueError(f"render dst {dst!r} needs a non-negative byte_size")
        if not isinstance(rendered, bool):
            raise ValueError(f"render dst {dst!r} needs a rendered boolean")
        seen.add(dst)
        normalized.append(
            {
                "dst": dst,
                "sha256": sha256,
                "byte_size": byte_size,
                "rendered": rendered,
            }
        )
    return normalized


def load_registry(path: Path | None = None) -> dict[str, Any]:
    source_path = path or registry_path()
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"template registry could not be loaded: {source_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"template registry schema must be {SCHEMA}")
    brigade_version = payload.get("brigade_version")
    if not isinstance(brigade_version, str) or not brigade_version:
        raise ValueError("template registry brigade_version must be a non-empty string")
    profiles_raw = payload.get("profiles")
    renders_raw = payload.get("renders")
    if not isinstance(profiles_raw, dict):
        raise ValueError("template registry profiles must be an object")
    if not isinstance(renders_raw, dict):
        raise ValueError("template registry renders must be an object")
    profiles: dict[str, dict[str, Any]] = {}
    for name in sorted(profiles_raw):
        profile = _profile_record(name, profiles_raw[name])
        profiles[name] = profile
        expected_renders = compute_profile_renders(_selection_from_mapping(profile["selection"]))
        actual_renders = _validate_render_records(renders_raw.get(name))
        if actual_renders != expected_renders:
            raise ValueError(f"template registry render digest mismatch for profile {name!r}")
    extra_renders = set(renders_raw) - set(profiles)
    if extra_renders:
        raise ValueError(f"template registry renders has unknown profiles: {sorted(extra_renders)}")
    return {
        "schema": SCHEMA,
        "brigade_version": brigade_version,
        "profiles": profiles,
        "renders": {name: renders_raw[name] for name in sorted(profiles)},
    }


def list_profile_names(path: Path | None = None) -> list[str]:
    return sorted(load_registry(path)["profiles"])


def resolve_profile(profile_name: str, path: Path | None = None) -> Selection:
    """Resolve a named bundled profile to a validated Selection."""
    registry = load_registry(path)
    profile = registry["profiles"].get(profile_name)
    if profile is None:
        raise UnknownTemplateProfile(profile_name)
    selection = _selection_from_mapping(profile["selection"])
    return selection


def check_harness_version(
    profile_name: str,
    harness_id: str,
    version: str,
    *,
    path: Path | None = None,
) -> None:
    """Raise UnsupportedHarnessVersion when version is below the profile minimum."""
    registry = load_registry(path)
    profile = registry["profiles"].get(profile_name)
    if profile is None:
        raise UnknownTemplateProfile(profile_name)
    versions = profile.get("supported_harness_versions", {})
    spec = versions.get(harness_id)
    if not isinstance(spec, dict):
        raise ValueError(f"profile {profile_name!r} does not select harness {harness_id!r}")
    minimum = spec["min"]
    if not _version_at_least(version, minimum):
        raise UnsupportedHarnessVersion(harness_id, version, minimum)


def profile_metadata(profile_name: str, *, path: Path | None = None) -> dict[str, Any]:
    registry = load_registry(path)
    profile = registry["profiles"].get(profile_name)
    if profile is None:
        raise UnknownTemplateProfile(profile_name)
    return deepcopy(profile)
