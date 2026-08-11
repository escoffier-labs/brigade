"""Bundled template-profile selection presets and rendered-file hash snapshot.

Compatibility / version truth for selected harnesses is sourced from the
existing ``harness-contract.v1`` fixtures (``tested_version``, ``provenance``,
``evidence``). This module does not publish a public registry schema, profile
URIs, or install-time harness version enforcement.
"""

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

SCHEMA = "brigade.template_profile_snapshot.v1"
HARNESS_CONTRACT_SCHEMA = "harness-contract.v1"

# Selection harness id -> primary harness-contract.v1 fixture stem.
HARNESS_CONTRACT_FIXTURES: dict[str, str] = {
    "claude": "claude-code",
    "codex": "codex-cli",
    "opencode": "opencode",
    "antigravity": "antigravity",
    "pi": "pi",
    "cursor": "cursor-cli",
    "grok": "grok-cli",
    "openclaw": "openclaw",
    "hermes": "hermes",
}

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "repo-claude": {
        "description": "Minimal repo install with Claude Code bridge.",
        "selection": {
            "depth": "repo",
            "harnesses": ["claude"],
            "owner": "claude",
            "includes": [],
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
    },
    "repo-claude-full": {
        "description": "Repo depth with Claude and the full kit (repo-extras).",
        "selection": {
            "depth": "repo",
            "harnesses": ["claude"],
            "owner": "claude",
            "includes": ["repo-extras"],
        },
    },
}


class UnknownTemplateProfile(ValueError):
    """A named template profile is not in the bundled snapshot."""

    def __init__(self, profile_name: str) -> None:
        self.profile_name = profile_name
        super().__init__(f"unknown template profile: {profile_name!r}")


def snapshot_path() -> Path:
    return template_root() / "template-profile-snapshot.json"


def harness_contract_fixtures_dir() -> Path:
    """Repo-relative harness-contract.v1 fixture directory."""
    return Path(__file__).resolve().parents[2] / "docs" / "research" / "fixtures" / "harness-contract.v1"


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


def _capability_truth(capability: Mapping[str, Any]) -> dict[str, Any]:
    cap_id = capability.get("id")
    tested_version = capability.get("tested_version")
    provenance = capability.get("provenance")
    evidence = capability.get("evidence")
    if not isinstance(cap_id, str) or not cap_id:
        raise ValueError("harness-contract capability.id must be a non-empty string")
    if tested_version is not None and not isinstance(tested_version, str):
        raise ValueError(f"harness-contract capability {cap_id!r} tested_version must be a string or null")
    if not isinstance(provenance, str) or not provenance:
        raise ValueError(f"harness-contract capability {cap_id!r} provenance must be a non-empty string")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(f"harness-contract capability {cap_id!r} evidence must be a non-empty list")
    normalized_evidence: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError(f"harness-contract capability {cap_id!r} evidence entries must be objects")
        kind = item.get("kind")
        reference = item.get("reference")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"harness-contract capability {cap_id!r} evidence.kind must be a non-empty string")
        if not isinstance(reference, str) or not reference:
            raise ValueError(f"harness-contract capability {cap_id!r} evidence.reference must be a non-empty string")
        normalized_evidence.append({"kind": kind, "reference": reference})
    return {
        "id": cap_id,
        "tested_version": tested_version,
        "provenance": provenance,
        "evidence": normalized_evidence,
    }


def load_harness_contract_fixture(contract_id: str, *, fixtures_dir: Path | None = None) -> dict[str, Any]:
    directory = fixtures_dir or harness_contract_fixtures_dir()
    path = directory / f"{contract_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"harness-contract fixture could not be loaded: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != HARNESS_CONTRACT_SCHEMA:
        raise ValueError(f"harness-contract fixture schema must be {HARNESS_CONTRACT_SCHEMA}: {path}")
    harness = payload.get("harness")
    if not isinstance(harness, dict) or harness.get("id") != contract_id:
        raise ValueError(f"harness-contract fixture harness.id must be {contract_id!r}: {path}")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError(f"harness-contract fixture capabilities must be a non-empty list: {path}")
    return payload


def harness_compatibility_from_contracts(
    harnesses: Sequence[str],
    *,
    fixtures_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Project harness-contract.v1 truth for each selected harness id."""
    compatibility: dict[str, dict[str, Any]] = {}
    for harness_id in harnesses:
        contract_id = HARNESS_CONTRACT_FIXTURES.get(harness_id)
        if contract_id is None:
            raise ValueError(
                f"no harness-contract.v1 fixture mapping for harness {harness_id!r}; "
                "refusing to invent compatibility claims"
            )
        fixture = load_harness_contract_fixture(contract_id, fixtures_dir=fixtures_dir)
        capabilities = [_capability_truth(item) for item in fixture["capabilities"]]
        capabilities.sort(key=lambda item: item["id"])
        compatibility[harness_id] = {
            "contract_id": contract_id,
            "surface": fixture["harness"]["surface"],
            "capabilities": capabilities,
        }
    return compatibility


def _profile_record(
    name: str,
    raw: Mapping[str, Any],
    *,
    fixtures_dir: Path | None = None,
    compatibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    description = raw.get("description")
    selection_raw = raw.get("selection")
    if not isinstance(description, str) or not description:
        raise ValueError(f"profile {name!r} needs a non-empty description")
    if not isinstance(selection_raw, dict):
        raise ValueError(f"profile {name!r} selection must be an object")
    selection = _selection_from_mapping(selection_raw)
    if compatibility is None:
        harness_compatibility = harness_compatibility_from_contracts(
            selection.harnesses,
            fixtures_dir=fixtures_dir,
        )
    else:
        if not isinstance(compatibility, dict):
            raise ValueError(f"profile {name!r} harness_compatibility must be an object")
        expected = harness_compatibility_from_contracts(selection.harnesses, fixtures_dir=fixtures_dir)
        if compatibility != expected:
            raise ValueError(f"profile {name!r} harness_compatibility does not match harness-contract.v1 fixtures")
        harness_compatibility = deepcopy(compatibility)
    return {
        "description": description,
        "selection": {
            "depth": selection.depth,
            "harnesses": selection.harnesses,
            "owner": selection.owner,
            "includes": selection.includes,
        },
        "harness_compatibility": harness_compatibility,
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


def build_snapshot(
    profile_names: Sequence[str] | None = None,
    *,
    fixtures_dir: Path | None = None,
) -> dict[str, Any]:
    names = sorted(profile_names or BUILTIN_PROFILES)
    profiles: dict[str, dict[str, Any]] = {}
    renders: dict[str, list[dict[str, Any]]] = {}
    for name in names:
        raw = BUILTIN_PROFILES.get(name)
        if raw is None:
            raise ValueError(f"unknown built-in profile: {name!r}")
        profile = _profile_record(name, raw, fixtures_dir=fixtures_dir)
        profiles[name] = profile
        selection = _selection_from_mapping(profile["selection"])
        renders[name] = compute_profile_renders(selection)
    return {
        "schema": SCHEMA,
        "brigade_version": __version__,
        "profiles": profiles,
        "renders": renders,
    }


def render_snapshot(payload: Mapping[str, Any]) -> str:
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


def load_snapshot(
    path: Path | None = None,
    *,
    fixtures_dir: Path | None = None,
    verify_contracts: bool = True,
) -> dict[str, Any]:
    source_path = path or snapshot_path()
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"template profile snapshot could not be loaded: {source_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"template profile snapshot schema must be {SCHEMA}")
    brigade_version = payload.get("brigade_version")
    if not isinstance(brigade_version, str) or not brigade_version:
        raise ValueError("template profile snapshot brigade_version must be a non-empty string")
    profiles_raw = payload.get("profiles")
    renders_raw = payload.get("renders")
    if not isinstance(profiles_raw, dict):
        raise ValueError("template profile snapshot profiles must be an object")
    if not isinstance(renders_raw, dict):
        raise ValueError("template profile snapshot renders must be an object")
    profiles: dict[str, dict[str, Any]] = {}
    renders: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(profiles_raw):
        raw = profiles_raw[name]
        if not isinstance(raw, dict):
            raise ValueError(f"profile {name!r} must be an object")
        compatibility = raw.get("harness_compatibility")
        if verify_contracts:
            profile = _profile_record(name, raw, fixtures_dir=fixtures_dir, compatibility=compatibility)
        else:
            description = raw.get("description")
            selection_raw = raw.get("selection")
            if not isinstance(description, str) or not description:
                raise ValueError(f"profile {name!r} needs a non-empty description")
            if not isinstance(selection_raw, dict):
                raise ValueError(f"profile {name!r} selection must be an object")
            selection = _selection_from_mapping(selection_raw)
            if not isinstance(compatibility, dict):
                raise ValueError(f"profile {name!r} harness_compatibility must be an object")
            profile = {
                "description": description,
                "selection": {
                    "depth": selection.depth,
                    "harnesses": selection.harnesses,
                    "owner": selection.owner,
                    "includes": selection.includes,
                },
                "harness_compatibility": deepcopy(compatibility),
            }
        profiles[name] = profile
        expected_renders = compute_profile_renders(_selection_from_mapping(profile["selection"]))
        actual_renders = _validate_render_records(renders_raw.get(name))
        if actual_renders != expected_renders:
            raise ValueError(f"template profile snapshot render digest mismatch for profile {name!r}")
        renders[name] = actual_renders
    extra_renders = set(renders_raw) - set(profiles)
    if extra_renders:
        raise ValueError(f"template profile snapshot renders has unknown profiles: {sorted(extra_renders)}")
    return {
        "schema": SCHEMA,
        "brigade_version": brigade_version,
        "profiles": profiles,
        "renders": renders,
    }


def list_profile_names(path: Path | None = None) -> list[str]:
    return sorted(load_snapshot(path, verify_contracts=False)["profiles"])


def resolve_profile(profile_name: str, path: Path | None = None) -> Selection:
    """Resolve a named bundled profile to a validated Selection."""
    snapshot = load_snapshot(path, verify_contracts=False)
    profile = snapshot["profiles"].get(profile_name)
    if profile is None:
        raise UnknownTemplateProfile(profile_name)
    return _selection_from_mapping(profile["selection"])


def profile_metadata(profile_name: str, *, path: Path | None = None) -> dict[str, Any]:
    snapshot = load_snapshot(path, verify_contracts=False)
    profile = snapshot["profiles"].get(profile_name)
    if profile is None:
        raise UnknownTemplateProfile(profile_name)
    return deepcopy(profile)
