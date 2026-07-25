"""Claude/Codex user-profile sync, doctor, and uninstall commands."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__ as BRIGADE_VERSION
from . import harness_profiles, localio, mcp_adapters, mcp_cmd, skills_cmd
from .toml_compat import TOMLDecodeError as _TOMLDecodeError
from .toml_compat import loads as _toml_loads

_RECOVERY_COMMAND = "brigade harness sync --target <harness> --scope user --adopt --write"
_SECTIONS = ("instructions", "skills", "generated", "mcp")
_HOOK_STATE_KEY = "hooks.json#sessionStart"
_LEGACY_MIGRATED = "legacy_migrated"
_LEGACY_CURSOR_INSTRUCTION = "plugins/local/brigade-loop/rules/brigade-loop.mdc"
_LEGACY_CURSOR_GENERATED = (
    "plugins/local/brigade-loop/.cursor-plugin/plugin.json",
    "hooks/brigade-session-start",
    "brigade/mcp.json",
)
_LEGACY_CURSOR_SKILL_ID = "brigade-work"
_LEGACY_CURSOR_SKILL_PREFIX = "skills/brigade-work/"


@dataclass(frozen=True)
class SurfacePlan:
    surface: str
    path: Path
    status: str
    action: str
    desired_digest: str | None = None
    rendered: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class LoadedProfileState:
    state: dict[str, Any]
    error: str | None
    migrated_from_legacy: bool = False


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def empty_profile_state(*, workspace: Path, harness: str) -> dict[str, Any]:
    return {
        "schema_version": harness_profiles.PROFILE_STATE_VERSION,
        "package_version": BRIGADE_VERSION,
        "workspace": str(workspace.expanduser().resolve()),
        "harness": harness,
        "instructions": {},
        "skills": {},
        "generated": {},
        "mcp": {},
    }


def write_profile_state(*, state_path: Path, state: dict[str, Any]) -> None:
    localio.write_json(state_path, state)


def is_legacy_install_state(state: dict[str, Any]) -> bool:
    """Return True for the superseded Cursor legacy installer state shape."""
    return (
        state.get("schema_version") is None
        and state.get("version") == 1
        and isinstance(state.get("files"), dict)
        and isinstance(state.get("hooks"), dict)
        and isinstance(state.get("mcp"), dict)
    )


def _legacy_migrated_record(**fields: Any) -> dict[str, Any]:
    return {**fields, _LEGACY_MIGRATED: True}


def _legacy_projected_fingerprint(fingerprint: str) -> str:
    """Normalize a legacy full-sha256 MCP attestation to profile projected form."""
    return fingerprint[:16] if len(fingerprint) >= 16 else fingerprint


def migrate_legacy_cursor_install_state(*, state: dict[str, Any], workspace: Path, harness: str) -> dict[str, Any]:
    """Map legacy Cursor install attestations into schema_version 2 profile state."""
    files = state["files"]
    hooks = state["hooks"]
    mcp = state["mcp"]
    migrated = empty_profile_state(workspace=workspace, harness=harness)
    package_version = state.get("package_version")
    if isinstance(package_version, str) and package_version:
        migrated["package_version"] = package_version

    rule_digest = files.get(_LEGACY_CURSOR_INSTRUCTION)
    if isinstance(rule_digest, str):
        migrated["instructions"] = _legacy_migrated_record(digest=rule_digest, created_file=True)

    generated: dict[str, dict[str, Any]] = {}
    for relative in _LEGACY_CURSOR_GENERATED:
        digest = files.get(relative)
        if isinstance(digest, str):
            generated[relative] = _legacy_migrated_record(digest=digest)
    hook_fingerprint = hooks.get("sessionStart")
    if isinstance(hook_fingerprint, str):
        generated[_HOOK_STATE_KEY] = _legacy_migrated_record(entry_fingerprint=hook_fingerprint)
    migrated["generated"] = generated

    skill_files: dict[str, str] = {}
    for relative, digest in files.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            continue
        if relative.startswith(_LEGACY_CURSOR_SKILL_PREFIX):
            skill_files[relative.removeprefix(_LEGACY_CURSOR_SKILL_PREFIX)] = digest
    if skill_files:
        migrated["skills"][_LEGACY_CURSOR_SKILL_ID] = _legacy_migrated_record(files=skill_files)

    for name, fingerprint in mcp.items():
        if not isinstance(name, str) or not isinstance(fingerprint, str):
            continue
        migrated["mcp"][name] = _legacy_migrated_record(
            projected_fingerprint=_legacy_projected_fingerprint(fingerprint),
            managed=True,
        )
    return migrated


def load_profile_state(*, state_path: Path, workspace: Path, harness: str) -> LoadedProfileState:
    """Read ownership state without writing, including version refreshes."""
    if not state_path.exists():
        return LoadedProfileState(empty_profile_state(workspace=workspace, harness=harness), None)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return LoadedProfileState({}, "ownership state is unreadable")
    if not isinstance(state, dict):
        return LoadedProfileState({}, "ownership state is not an object")
    if state.get("schema_version") != harness_profiles.PROFILE_STATE_VERSION:
        if harness == "cursor" and is_legacy_install_state(state):
            return LoadedProfileState(
                migrate_legacy_cursor_install_state(state=state, workspace=workspace, harness=harness),
                None,
                migrated_from_legacy=True,
            )
        return LoadedProfileState({}, f"unsupported ownership state version: {state.get('schema_version')}")
    if state.get("harness") != harness:
        return LoadedProfileState({}, f"ownership harness mismatch: {state.get('harness')} != {harness}")
    stored = state.get("workspace")
    resolved = Path(stored).expanduser().resolve() if isinstance(stored, str) and stored else stored
    expected = workspace.expanduser().resolve()
    if resolved != expected:
        return LoadedProfileState({}, f"ownership workspace mismatch: {resolved} != {expected}")
    for section in _SECTIONS:
        if not isinstance(state.get(section), dict):
            return LoadedProfileState({}, f"ownership state section is not an object: {section}")
    return LoadedProfileState(state, None)


def _block(body: str) -> str:
    return f"{harness_profiles.INSTRUCTION_START}\n{body}\n{harness_profiles.INSTRUCTION_END}\n"


def _split_components(text: str) -> tuple[str, str, str] | None:
    start, end = harness_profiles.INSTRUCTION_START, harness_profiles.INSTRUCTION_END
    if text.count(start) != 1 or text.count(end) != 1:
        return None
    start_pos, end_pos = text.find(start), text.find(end)
    after_start = start_pos + len(start)
    if end_pos <= start_pos or after_start >= len(text) or text[after_start] != "\n" or text[end_pos - 1] != "\n":
        return None
    before = text[:start_pos]
    body = text[after_start + 1 : end_pos - 1]
    after = text[end_pos + len(end) :]
    if after.startswith("\n"):
        after = after[1:]
    return before, body, after


def plan_instruction(*, path: Path, desired: str, state: dict[str, Any], adopt: bool = False) -> SurfacePlan:
    instruction_state = state.get("instructions", {}) if isinstance(state.get("instructions"), dict) else {}
    owned = instruction_state.get("digest")
    if not path.exists():
        return SurfacePlan("instruction", path, "missing", "create", digest_text(desired), _block(desired))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SurfacePlan("instruction", path, "conflict", "preserve", detail=str(exc))
    parts = _split_components(text)
    if parts is None:
        if harness_profiles.INSTRUCTION_START not in text and harness_profiles.INSTRUCTION_END not in text:
            return SurfacePlan(
                "instruction",
                path,
                "missing",
                "create",
                digest_text(desired),
                text + ("\n" if text else "") + _block(desired),
            )
        return SurfacePlan(
            "instruction", path, "conflict", "preserve", detail="managed instruction markers are malformed"
        )
    before, live, after = parts
    live_digest, desired_digest = digest_text(live), digest_text(desired)
    if live_digest == desired_digest:
        if owned == live_digest:
            return SurfacePlan("instruction", path, "current", "none", desired_digest)
        if adopt:
            return SurfacePlan("instruction", path, "adopted", "none", desired_digest)
        return SurfacePlan(
            "instruction",
            path,
            "conflict",
            "preserve",
            desired_digest,
            detail=f"matching managed instruction block is unowned; recover with: {_RECOVERY_COMMAND}",
        )
    if owned == live_digest or adopt:
        return SurfacePlan("instruction", path, "stale", "update", desired_digest, before + _block(desired) + after)
    return SurfacePlan(
        "instruction",
        path,
        "conflict",
        "preserve",
        detail=f"foreign managed instruction block; recover with: {_RECOVERY_COMMAND}",
    )


def plan_instruction_removal(*, path: Path, state: dict[str, Any]) -> SurfacePlan:
    if not path.exists():
        return SurfacePlan("instruction", path, "absent", "none")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SurfacePlan("instruction", path, "conflict", "preserve", detail=str(exc))
    parts = _split_components(text)
    if parts is None:
        return SurfacePlan(
            "instruction",
            path,
            "absent"
            if harness_profiles.INSTRUCTION_START not in text and harness_profiles.INSTRUCTION_END not in text
            else "conflict",
            "none"
            if harness_profiles.INSTRUCTION_START not in text and harness_profiles.INSTRUCTION_END not in text
            else "preserve",
            detail="managed instruction markers are malformed",
        )
    before, body, after = parts
    instruction_state = state.get("instructions", {}) if isinstance(state.get("instructions"), dict) else {}
    owned = instruction_state.get("digest")
    if owned == digest_text(body):
        if instruction_state.get("created_file") and not before and not after:
            return SurfacePlan("instruction", path, "managed", "remove")
        return SurfacePlan(
            "instruction",
            path,
            "managed",
            "remove",
            rendered=(before[:-1] if before.endswith("\n") else before) + after,
        )
    return SurfacePlan("instruction", path, "conflict", "preserve", detail="owned instruction block was edited")


def plan_managed_instruction(*, path: Path, desired: str, state: dict[str, Any], adopt: bool = False) -> SurfacePlan:
    """Whole-file owned instruction surface (no marked block, e.g. a managed rule file)."""
    instruction_state = state.get("instructions", {}) if isinstance(state.get("instructions"), dict) else {}
    owned = instruction_state.get("digest")
    desired_digest = digest_text(desired)
    if not path.exists():
        return SurfacePlan("instruction", path, "missing", "create", desired_digest, desired)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SurfacePlan("instruction", path, "conflict", "preserve", detail=str(exc))
    live_digest = digest_text(text)
    if live_digest == desired_digest:
        if owned == live_digest:
            return SurfacePlan("instruction", path, "current", "none", desired_digest)
        if adopt:
            return SurfacePlan("instruction", path, "adopted", "none", desired_digest)
        return SurfacePlan(
            "instruction",
            path,
            "conflict",
            "preserve",
            desired_digest,
            detail=f"matching managed instruction file is unowned; recover with: {_RECOVERY_COMMAND}",
        )
    if owned == live_digest or adopt:
        return SurfacePlan("instruction", path, "stale", "update", desired_digest, desired)
    return SurfacePlan(
        "instruction",
        path,
        "conflict",
        "preserve",
        detail=f"foreign managed instruction file; recover with: {_RECOVERY_COMMAND}",
    )


def plan_managed_instruction_removal(*, path: Path, state: dict[str, Any]) -> SurfacePlan:
    if not path.exists():
        return SurfacePlan("instruction", path, "absent", "none")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SurfacePlan("instruction", path, "conflict", "preserve", detail=str(exc))
    instruction_state = state.get("instructions", {}) if isinstance(state.get("instructions"), dict) else {}
    if instruction_state.get("digest") == digest_text(text):
        return SurfacePlan("instruction", path, "managed", "remove")
    return SurfacePlan("instruction", path, "conflict", "preserve", detail="owned instruction file was edited")


def _instruction_plan(profile, state: dict[str, Any], *, adopt: bool) -> SurfacePlan:
    desired = profile.instruction_text or harness_profiles.managed_instruction_text()
    if profile.instruction_mode == "managed-file":
        return plan_managed_instruction(path=profile.instruction_path, desired=desired, state=state, adopt=adopt)
    return plan_instruction(path=profile.instruction_path, desired=desired, state=state, adopt=adopt)


def _instruction_removal_plan(profile, state: dict[str, Any]) -> SurfacePlan:
    if profile.instruction_mode == "managed-file":
        return plan_managed_instruction_removal(path=profile.instruction_path, state=state)
    return plan_instruction_removal(path=profile.instruction_path, state=state)


def _lstat_conflict(path: Path, *, directory: bool) -> str | None:
    """Reject symlinks and non-directories before native skill writes.

    ``Path.resolve`` is too late here because it follows a symlink before the
    caller can reject it.  The profile writer must never create a file outside
    the native root, including through a broken symlink.
    """
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        return str(exc)
    if stat.S_ISLNK(mode):
        return "native skill path is a symlink"
    if directory and not stat.S_ISDIR(mode):
        return "native skill ancestor is not a directory"
    if not directory and not stat.S_ISREG(mode):
        return "native skill destination is not a regular file"
    return None


def _mcp_config_path(profile, workspace: Path) -> Path:
    if profile.mcp_path is not None:
        return profile.mcp_path
    return mcp_adapters.resolve_path(mcp_adapters.ADAPTERS[profile.mcp_harness], workspace)


def _native_surface_conflicts(profile, workspace: Path) -> list[dict[str, Any]]:
    """Reject symlinked profile surfaces before reading or mutating them."""
    mcp_path = _mcp_config_path(profile, workspace)
    surfaces = [
        ("profile-root", profile.user_root, True),
        ("instruction", profile.instruction_path, False),
        ("profile-directory", profile.state_path.parent, True),
        ("ownership-state", profile.state_path, False),
        ("profile-receipt", profile.receipt_path, False),
        ("mcp", mcp_path, False),
    ]
    surfaces.extend(("generated", profile.user_root / generated.relative, False) for generated in profile.generated)
    if profile.hook is not None:
        surfaces.append(("hook", profile.hook.path, False))
    conflicts: list[dict[str, Any]] = []
    for surface, path, directory in surfaces:
        error = _lstat_conflict(path, directory=directory)
        if error:
            conflicts.append(
                {
                    "surface": surface,
                    "path": str(path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": error,
                }
            )
    return conflicts


def _surface_conflict_result(profile, conflicts: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    return _result(
        profile,
        status="conflict",
        ready=False,
        items=conflicts,
        conflicts=conflicts,
        files_written=[],
        files_removed=[],
        mcp={"status": "conflict", "items": []},
        receipt_path=profile.receipt_path,
        receipt_state="unknown",
    ), False


def _skill_destination(profile, skill_id: str, relative: str) -> tuple[Path | None, str | None]:
    parts = Path(relative).parts
    if not skill_id or skill_id in {".", ".."} or skill_id.startswith("/") or ".." in Path(skill_id).parts:
        return None, "unsafe skill package id"
    if not relative or relative.startswith("/") or ".." in parts:
        return None, "unsafe skill file path"
    destination = profile.skills_root / skill_id / relative
    candidates = [profile.user_root]
    cursor = profile.user_root
    for component in destination.relative_to(profile.user_root).parts[:-1]:
        cursor = cursor / component
        candidates.append(cursor)
    for ancestor in candidates:
        error = _lstat_conflict(ancestor, directory=True)
        if error:
            return None, error
    error = _lstat_conflict(destination, directory=False)
    return (None, error) if error else (destination, None)


def _missing_skill_directories(profile, path: Path) -> list[str]:
    missing: list[str] = []
    cursor = path.parent
    while cursor != profile.skills_root and not cursor.exists():
        missing.append(str(cursor))
        cursor = cursor.parent
    return list(reversed(missing))


def _skill_plans(profile, state: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Build one read-only reconciliation plan for desired and owned skill files."""
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    writes: list[tuple[Path, bytes, str, str]] = []
    removes: list[Path] = []
    try:
        packages = skills_cmd.user_profile_skill_packages(
            workspace=workspace, harness=profile.harness, minimum_trust="workspace"
        )
    except Exception as exc:
        item = {
            "surface": "skills",
            "path": str(profile.skills_root),
            "status": "conflict",
            "action": "preserve",
            "detail": str(exc),
        }
        return {"items": [item], "conflicts": [item], "writes": [], "removes": [], "next": state["skills"], "prune": []}
    desired_packages = {package.skill_id: package for package in packages}
    desired_records: dict[str, dict[str, Any]] = {}
    expected_keys: set[tuple[str, str]] = set()
    for package in packages:
        record = state["skills"].get(package.skill_id, {})
        owned_files = (
            record.get("files", {}) if isinstance(record, dict) and isinstance(record.get("files"), dict) else {}
        )
        desired_records[package.skill_id] = {
            "source_identity": package.source_identity,
            "source_fingerprint": package.source_fingerprint,
            "metadata_fingerprint": package.metadata_fingerprint,
            "files": {relative: digest_bytes(data) for relative, data in sorted(package.files.items())},
            "created_directories": list(record.get("created_directories", [])) if isinstance(record, dict) else [],
        }
        for relative, data in sorted(package.files.items()):
            expected_keys.add((package.skill_id, relative))
            path, error = _skill_destination(profile, package.skill_id, relative)
            item = {
                "surface": "skill",
                "skill_id": package.skill_id,
                "path": str(path or (profile.skills_root / package.skill_id / relative)),
            }
            if error or path is None:
                item.update(status="conflict", action="preserve", detail=error or "unsafe skill destination")
                conflicts.append(item)
            elif not path.exists():
                item.update(status="missing", action="create")
                writes.append((path, data, package.skill_id, relative))
            elif digest_bytes(path.read_bytes()) == digest_bytes(data):
                if relative in owned_files:
                    item.update(status="current", action="none")
                else:
                    item.update(status="conflict", action="preserve", detail="matching skill file is unowned")
                    conflicts.append(item)
            elif owned_files.get(relative) == digest_bytes(path.read_bytes()):
                item.update(status="stale", action="update")
                writes.append((path, data, package.skill_id, relative))
            else:
                item.update(status="changed", action="preserve", detail="owned skill file was edited")
                conflicts.append(item)
            items.append(item)
    for skill_id, record in sorted(state["skills"].items()):
        if not isinstance(record, dict) or not isinstance(record.get("files"), dict):
            item = {
                "surface": "skill",
                "skill_id": skill_id,
                "path": str(profile.skills_root / skill_id),
                "status": "conflict",
                "action": "preserve",
                "detail": "skill ownership record is malformed",
            }
            items.append(item)
            conflicts.append(item)
            continue
        for relative, owned_digest in sorted(record["files"].items()):
            if (skill_id, relative) in expected_keys:
                continue
            path, error = _skill_destination(profile, skill_id, relative)
            item = {
                "surface": "skill",
                "skill_id": skill_id,
                "path": str(path or (profile.skills_root / skill_id / relative)),
            }
            if error:
                item.update(status="conflict", action="preserve", detail=error)
                conflicts.append(item)
            elif path is None or not path.exists():
                item.update(status="absent", action="remove")
            elif digest_bytes(path.read_bytes()) == owned_digest:
                if record.get(_LEGACY_MIGRATED):
                    item.update(status="current", action="none")
                    preserved = desired_records.get(skill_id)
                    if not isinstance(preserved, dict):
                        preserved = {}
                        desired_records[skill_id] = preserved
                    files_map = preserved.get("files")
                    if not isinstance(files_map, dict):
                        files_map = {}
                        preserved["files"] = files_map
                    files_map[relative] = owned_digest
                    preserved[_LEGACY_MIGRATED] = True
                else:
                    item.update(status="removed-registry", action="remove")
                    removes.append(path)
            else:
                item.update(status="changed", action="preserve", detail="removed-registry skill file was edited")
                conflicts.append(item)
            items.append(item)
    prune: list[Path] = []
    for skill_id, record in state["skills"].items():
        if skill_id not in desired_packages and isinstance(record, dict):
            prune.extend(Path(path) for path in record.get("created_directories", []) if isinstance(path, str))
    return {
        "items": items,
        "conflicts": conflicts,
        "writes": writes,
        "removes": removes,
        "next": desired_records,
        "prune": sorted(set(prune), reverse=True),
    }


def _skill_uninstall_plan(profile, state: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    removes: list[Path] = []
    prune: list[Path] = []
    for skill_id, record in sorted(state["skills"].items()):
        if not isinstance(record, dict) or not isinstance(record.get("files"), dict):
            item = {
                "surface": "skill",
                "skill_id": skill_id,
                "path": str(profile.skills_root / skill_id),
                "status": "conflict",
                "action": "preserve",
                "detail": "skill ownership record is malformed",
            }
            items.append(item)
            conflicts.append(item)
            continue
        for relative, owned_digest in sorted(record["files"].items()):
            path, error = _skill_destination(profile, skill_id, relative)
            item = {
                "surface": "skill",
                "skill_id": skill_id,
                "path": str(path or (profile.skills_root / skill_id / relative)),
            }
            if error:
                item.update(status="conflict", action="preserve", detail=error)
                conflicts.append(item)
            elif path is None or not path.exists():
                item.update(status="absent", action="remove")
            elif digest_bytes(path.read_bytes()) == owned_digest:
                item.update(status="managed", action="remove")
                removes.append(path)
            else:
                item.update(status="conflict", action="preserve", detail="owned skill file was edited")
                conflicts.append(item)
            items.append(item)
        prune.extend(Path(path) for path in record.get("created_directories", []) if isinstance(path, str))
    return {"items": items, "conflicts": conflicts, "removes": removes, "prune": sorted(set(prune), reverse=True)}


def _missing_directories(root: Path, path: Path) -> list[str]:
    missing: list[str] = []
    cursor = path.parent
    while cursor != root and not cursor.exists():
        missing.append(str(cursor))
        cursor = cursor.parent
    return list(reversed(missing))


def _generated_destination(profile, relative: str) -> tuple[Path | None, str | None]:
    parts = Path(relative).parts
    if not relative or relative.startswith("/") or ".." in parts:
        return None, "unsafe generated file path"
    destination = profile.user_root / relative
    candidates = [profile.user_root]
    cursor = profile.user_root
    for component in destination.relative_to(profile.user_root).parts[:-1]:
        cursor = cursor / component
        candidates.append(cursor)
    for ancestor in candidates:
        error = _lstat_conflict(ancestor, directory=True)
        if error:
            return None, error
    error = _lstat_conflict(destination, directory=False)
    return (None, error) if error else (destination, None)


def _generated_plans(profile, state: dict[str, Any], *, adopt: bool) -> dict[str, Any]:
    """Plan whole-file Brigade-owned generated artifacts (plugin manifests, hook scripts)."""
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    writes: list[tuple[Path, str, bool, str]] = []
    removes: list[Path] = []
    prune: list[Path] = []
    records = state["generated"]
    desired = {generated.relative: generated for generated in profile.generated}
    next_records: dict[str, dict[str, Any]] = {}
    for generated in profile.generated:
        record = records.get(generated.relative)
        record = record if isinstance(record, dict) else {}
        desired_digest = digest_text(generated.text)
        path, error = _generated_destination(profile, generated.relative)
        item = {"surface": "generated", "path": str(path or (profile.user_root / generated.relative))}
        if error or path is None:
            item.update(status="conflict", action="preserve", detail=error or "unsafe generated destination")
            conflicts.append(item)
        elif not path.exists():
            item.update(status="missing", action="create")
            writes.append((path, generated.text, generated.executable, generated.relative))
        else:
            try:
                live_digest = digest_text(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                item.update(status="conflict", action="preserve", detail=str(exc))
                conflicts.append(item)
                items.append(item)
                next_records[generated.relative] = record
                continue
            if live_digest == desired_digest:
                if record.get("digest") == live_digest:
                    item.update(status="current", action="none")
                elif adopt:
                    item.update(status="adopted", action="none")
                else:
                    item.update(status="conflict", action="preserve", detail="matching generated file is unowned")
                    conflicts.append(item)
            elif record.get("digest") == live_digest or adopt:
                item.update(status="stale", action="update")
                writes.append((path, generated.text, generated.executable, generated.relative))
            else:
                item.update(status="conflict", action="preserve", detail="owned generated file was edited")
                conflicts.append(item)
        items.append(item)
        next_records[generated.relative] = {
            "digest": desired_digest,
            "created_directories": list(record.get("created_directories", [])),
        }
    for relative, record in sorted(records.items()):
        if relative == _HOOK_STATE_KEY or relative in desired:
            continue
        item = {"surface": "generated", "path": str(profile.user_root / relative)}
        if not isinstance(record, dict) or not isinstance(record.get("digest"), str):
            item.update(status="conflict", action="preserve", detail="generated ownership record is malformed")
            conflicts.append(item)
        else:
            path, error = _generated_destination(profile, relative)
            if error or path is None:
                item.update(status="conflict", action="preserve", detail=error or "unsafe generated destination")
                conflicts.append(item)
            elif not path.exists():
                item.update(status="absent", action="remove")
            elif digest_text(path.read_text(encoding="utf-8")) == record["digest"]:
                if record.get(_LEGACY_MIGRATED):
                    item.update(status="current", action="none")
                    next_records[relative] = dict(record)
                else:
                    item.update(status="removed-profile", action="remove")
                    removes.append(path)
            else:
                item.update(status="conflict", action="preserve", detail="removed-profile generated file was edited")
                conflicts.append(item)
        if isinstance(record, dict):
            prune.extend(Path(path) for path in record.get("created_directories", []) if isinstance(path, str))
        items.append(item)
    return {
        "items": items,
        "conflicts": conflicts,
        "writes": writes,
        "removes": removes,
        "next": next_records,
        "prune": sorted(set(prune), reverse=True),
    }


def _generated_uninstall_plan(profile, state: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    removes: list[Path] = []
    prune: list[Path] = []
    for relative, record in sorted(state["generated"].items()):
        if relative == _HOOK_STATE_KEY:
            continue
        item = {"surface": "generated", "path": str(profile.user_root / relative)}
        if not isinstance(record, dict) or not isinstance(record.get("digest"), str):
            item.update(status="conflict", action="preserve", detail="generated ownership record is malformed")
            conflicts.append(item)
        else:
            path, error = _generated_destination(profile, relative)
            if error or path is None:
                item.update(status="conflict", action="preserve", detail=error or "unsafe generated destination")
                conflicts.append(item)
            elif not path.exists():
                item.update(status="absent", action="remove")
            elif digest_text(path.read_text(encoding="utf-8")) == record["digest"]:
                item.update(status="managed", action="remove")
                removes.append(path)
            else:
                item.update(status="conflict", action="preserve", detail="owned generated file was edited")
                conflicts.append(item)
        if isinstance(record, dict):
            prune.extend(Path(path) for path in record.get("created_directories", []) if isinstance(path, str))
        items.append(item)
    return {"items": items, "conflicts": conflicts, "removes": removes, "prune": sorted(set(prune), reverse=True)}


def _hook_plan(profile, state: dict[str, Any], *, adopt: bool) -> dict[str, Any]:
    """Plan one Brigade-managed entry inside a co-owned JSON hook config."""
    from . import cursor_user_cmd

    hook = profile.hook
    path = hook.path
    record = state["generated"].get(_HOOK_STATE_KEY)
    record = record if isinstance(record, dict) else {}
    desired_fp = cursor_user_cmd._digest_value(hook.entry)
    empty: dict[str, Any] = {"items": [], "conflicts": [], "doc": None, "next": record}
    doc, error = cursor_user_cmd._read_json_object(path)
    item: dict[str, Any] = {"surface": "hook", "path": str(path), "name": "sessionStart"}
    if doc is None:
        item.update(status="conflict", action="preserve", detail=error or "could not read hook configuration")
        return {**empty, "items": [item], "conflicts": [item]}
    hooks = doc.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        item.update(status="conflict", action="preserve", detail="existing hooks field must be an object")
        return {**empty, "items": [item], "conflicts": [item]}
    entries = hooks.get("sessionStart") if isinstance(hooks, dict) else None
    if entries is not None and not isinstance(entries, list):
        item.update(status="conflict", action="preserve", detail="existing sessionStart hooks must be a list")
        return {**empty, "items": [item], "conflicts": [item]}
    entries = entries if isinstance(entries, list) else []
    conflicts: list[dict[str, Any]] = []
    if hook.entry in entries:
        if record.get("entry_fingerprint") == desired_fp:
            item.update(status="current", action="none")
        elif adopt:
            item.update(status="adopted", action="none")
        else:
            item.update(
                status="conflict",
                action="preserve",
                detail="matching hook entry is unowned; rerun with --adopt",
            )
            conflicts.append(item)
    else:
        prior_fp = record.get("entry_fingerprint")
        prior_index = next(
            (
                index
                for index, entry in enumerate(entries)
                if prior_fp and cursor_user_cmd._digest_value(entry) == prior_fp
            ),
            None,
        )
        if prior_index is not None:
            item.update(status="stale", action="update", prior_index=prior_index)
        else:
            item.update(status="missing", action="create")
    return {
        "items": [item],
        "conflicts": conflicts,
        "doc": doc,
        "next": {"entry_fingerprint": desired_fp},
    }


def _hook_uninstall_plan(profile, state: dict[str, Any]) -> dict[str, Any]:
    from . import cursor_user_cmd

    hook = profile.hook
    path = hook.path
    record = state["generated"].get(_HOOK_STATE_KEY)
    fingerprint = record.get("entry_fingerprint") if isinstance(record, dict) else None
    if not fingerprint:
        return {"items": [], "conflicts": [], "doc": None, "index": None}
    doc, error = cursor_user_cmd._read_json_object(path)
    item: dict[str, Any] = {"surface": "hook", "path": str(path), "name": "sessionStart"}
    entries: Any = None
    if doc is not None and isinstance(doc.get("hooks"), dict):
        entries = doc["hooks"].get("sessionStart")
    index = (
        next((i for i, entry in enumerate(entries) if cursor_user_cmd._digest_value(entry) == fingerprint), None)
        if isinstance(entries, list)
        else None
    )
    if index is not None:
        item.update(status="managed", action="remove")
        return {"items": [item], "conflicts": [], "doc": doc, "index": index}
    if not path.exists():
        item.update(status="absent", action="none")
        return {"items": [item], "conflicts": [], "doc": None, "index": None}
    item.update(status="conflict", action="preserve", detail=error or "managed hook entry was edited or removed")
    return {"items": [item], "conflicts": [item], "doc": None, "index": None}


def _malformed_native_config(adapter, text: str | None) -> str | None:
    if text is None:
        return None
    try:
        doc = json.loads(text) if adapter.fmt == "json" else _toml_loads(text)
    except (json.JSONDecodeError, _TOMLDecodeError):
        return "existing native MCP config is malformed"
    if not isinstance(doc, dict):
        return "existing native MCP config is malformed"
    node: Any = doc
    for part in adapter.top_key.split("."):
        if part not in node:
            return None
        node = node[part]
        if not isinstance(node, dict):
            return "existing native MCP config is malformed"
    return None


def _mcp_plan(
    profile, state: dict[str, Any], workspace: Path, *, allow_global_stdio: bool, adopt: bool
) -> dict[str, Any]:
    adapter = mcp_adapters.ADAPTERS[profile.mcp_harness]
    path = profile.mcp_path or mcp_adapters.resolve_path(adapter, workspace)
    servers, errors, _warnings = mcp_cmd.load_canonical(workspace)
    if errors:
        # A workspace need not opt into project MCP at all.  Existing owned
        # entries are still planned as removals below only when a catalog is
        # readable, avoiding a blind native-config mutation on a bad catalog.
        if not state["mcp"]:
            return {
                "items": [],
                "conflicts": [],
                "path": path,
                "adapter": adapter,
                "text": None,
                "updates": {},
                "remove": set(),
                "next": {},
            }
        if all(isinstance(record, dict) and record.get(_LEGACY_MIGRATED) for record in state["mcp"].values()):
            text = path.read_text(encoding="utf-8") if path.is_file() else None
            legacy_items = [
                {
                    "surface": "mcp",
                    "path": str(path),
                    "name": name,
                    "status": "current",
                    "action": "none",
                }
                for name in sorted(state["mcp"])
            ]
            return {
                "items": legacy_items,
                "conflicts": [],
                "path": path,
                "adapter": adapter,
                "text": text,
                "updates": {},
                "remove": set(),
                "next": state["mcp"],
            }
        item = {
            "surface": "mcp",
            "path": str(path),
            "status": "conflict",
            "action": "preserve",
            "detail": "; ".join(errors),
        }
        return {
            "items": [item],
            "conflicts": [item],
            "path": path,
            "adapter": adapter,
            "text": None,
            "updates": {},
            "remove": set(),
            "next": state["mcp"],
        }
    desired = {
        name: server
        for name, server in servers.items()
        if server.enabled and mcp_cmd._server_targets_harness(server, profile.mcp_harness)
    }
    if any(not server.is_remote for server in desired.values()) and not allow_global_stdio:
        item = {
            "surface": "mcp",
            "path": str(path),
            "status": "conflict",
            "action": "preserve",
            "detail": "stdio MCP servers require --allow-global-stdio",
        }
        return {
            "items": [item],
            "conflicts": [item],
            "path": path,
            "adapter": adapter,
            "text": None,
            "updates": {},
            "remove": set(),
            "next": state["mcp"],
        }
    text = path.read_text(encoding="utf-8") if path.is_file() else None
    malformed = _malformed_native_config(adapter, text)
    if malformed:
        item = {"surface": "mcp", "path": str(path), "status": "malformed", "action": "preserve", "detail": malformed}
        return {
            "items": [item],
            "conflicts": [item],
            "path": path,
            "adapter": adapter,
            "text": text,
            "updates": {},
            "remove": set(),
            "next": state["mcp"],
        }
    live = adapter.read_file(text)
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    updates: dict[str, dict[str, Any]] = {}
    ownership: dict[str, dict[str, Any]] = {}
    remove: set[str] = set()
    for name in sorted(set(desired) | set(state["mcp"])):
        if name not in desired:
            record = state["mcp"].get(name)
            item = {"surface": "mcp", "path": str(path), "name": name}
            if not isinstance(record, dict):
                item.update(status="conflict", action="preserve", detail="MCP ownership record is malformed")
                conflicts.append(item)
            elif not record.get("managed"):
                item.update(status="unmanaged", action="preserve")
            elif name not in live:
                item.update(status="absent", action="remove")
            elif localio.stable_hash(live[name]) == record.get("projected_fingerprint"):
                if record.get(_LEGACY_MIGRATED):
                    item.update(status="current", action="none")
                    ownership[name] = record
                else:
                    item.update(status="removed-catalog", action="remove")
                    remove.add(name)
            else:
                item.update(status="conflict", action="preserve", detail="removed-catalog MCP entry was edited")
                conflicts.append(item)
            items.append(item)
            continue
        server = desired[name]
        provider = mcp_cmd._project_server(workspace, profile.mcp_harness, server)
        fingerprint = localio.stable_hash(provider)
        record = state["mcp"].get(name, {}) if isinstance(state["mcp"].get(name), dict) else {}
        item = {"surface": "mcp", "path": str(path), "name": name}
        if name not in live:
            item.update(status="missing", action="create")
            updates[name] = provider
            ownership[name] = {"projected_fingerprint": fingerprint, "managed": True}
        elif not record:
            if not adopt:
                item.update(
                    status="conflict",
                    action="preserve",
                    detail="matching or foreign native MCP entry is unowned; rerun with --adopt",
                )
                conflicts.append(item)
            else:
                item.update(
                    status="adopted", action="none" if localio.stable_hash(live[name]) == fingerprint else "update"
                )
                if item["action"] == "update":
                    updates[name] = provider
                ownership[name] = {"projected_fingerprint": fingerprint, "managed": False}
        elif localio.stable_hash(live[name]) == fingerprint:
            item.update(status="current", action="none")
            ownership[name] = record
        elif record.get("managed") and localio.stable_hash(live[name]) == record.get("projected_fingerprint"):
            item.update(status="stale", action="update")
            updates[name] = provider
            ownership[name] = {"projected_fingerprint": fingerprint, "managed": True}
        elif adopt:
            item.update(status="adopted", action="update")
            updates[name] = provider
            ownership[name] = {"projected_fingerprint": fingerprint, "managed": False}
        else:
            item.update(
                status="conflict", action="preserve", detail="owned native MCP entry was edited; rerun with --adopt"
            )
            conflicts.append(item)
        items.append(item)
    return {
        "items": items,
        "conflicts": conflicts,
        "path": path,
        "adapter": adapter,
        "text": text,
        "updates": updates,
        "remove": remove,
        "next": ownership,
    }


def _mcp_uninstall_plan(profile, state: dict[str, Any], workspace: Path) -> dict[str, Any]:
    adapter = mcp_adapters.ADAPTERS[profile.mcp_harness]
    path = profile.mcp_path or mcp_adapters.resolve_path(adapter, workspace)
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    if not state["mcp"]:
        return {"items": items, "conflicts": conflicts, "path": path, "adapter": adapter, "text": None, "remove": set()}
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, UnicodeDecodeError) as exc:
        item = {"surface": "mcp", "path": str(path), "status": "conflict", "action": "preserve", "detail": str(exc)}
        return {"items": [item], "conflicts": [item], "path": path, "adapter": adapter, "text": None, "remove": set()}
    malformed = _malformed_native_config(adapter, text)
    if malformed:
        item = {"surface": "mcp", "path": str(path), "status": "conflict", "action": "preserve", "detail": malformed}
        return {"items": [item], "conflicts": [item], "path": path, "adapter": adapter, "text": text, "remove": set()}
    live = adapter.read_file(text)
    remove: set[str] = set()
    for name, record in sorted(state["mcp"].items()):
        item = {"surface": "mcp", "path": str(path), "name": name}
        if not isinstance(record, dict):
            item.update(status="conflict", action="preserve", detail="MCP ownership record is malformed")
            conflicts.append(item)
        elif not record.get("managed"):
            item.update(status="unmanaged", action="preserve")
        elif name not in live:
            item.update(status="absent", action="remove")
        elif localio.stable_hash(live[name]) == record.get("projected_fingerprint"):
            item.update(status="managed", action="remove")
            remove.add(name)
        else:
            item.update(status="conflict", action="preserve", detail="owned native MCP entry was edited")
            conflicts.append(item)
        items.append(item)
    return {"items": items, "conflicts": conflicts, "path": path, "adapter": adapter, "text": text, "remove": remove}


def _verify_mcp(profile, state: dict[str, Any], workspace: Path) -> tuple[dict[str, Any], bool]:
    adapter = mcp_adapters.ADAPTERS[profile.mcp_harness]
    path = profile.mcp_path or mcp_adapters.resolve_path(adapter, workspace)
    items: list[dict[str, Any]] = []
    if not state["mcp"]:
        return {"status": "ready", "items": items}, True
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {
            "status": "conflict",
            "items": [{"surface": "mcp", "path": str(path), "status": "missing", "action": "preserve"}],
        }, False
    malformed = _malformed_native_config(adapter, text)
    if malformed:
        return {
            "status": "conflict",
            "items": [
                {"surface": "mcp", "path": str(path), "status": "malformed", "action": "preserve", "detail": malformed}
            ],
        }, False
    live = adapter.read_file(text)
    servers, errors, _warnings = mcp_cmd.load_canonical(workspace)
    ok = not errors
    for name, record in sorted(state["mcp"].items()):
        item = {"surface": "mcp", "path": str(path), "name": name, "action": "none"}
        server = servers.get(name)
        if server is None or not server.enabled or not mcp_cmd._server_targets_harness(server, profile.mcp_harness):
            item.update(status="conflict", detail="owned projection is absent from the canonical catalog")
            ok = False
        elif name not in live:
            item.update(status="missing")
            ok = False
        elif localio.stable_hash(live[name]) != localio.stable_hash(
            mcp_cmd._project_server(workspace, profile.mcp_harness, server)
        ):
            item.update(status="edited")
            ok = False
        elif not isinstance(record, dict):
            item.update(status="conflict", detail="ownership record is malformed")
            ok = False
        else:
            item.update(status="current")
        items.append(item)
    return {"status": "ready" if ok else "conflict", "items": items}, ok


def _result(
    profile,
    *,
    status: str,
    ready: bool,
    items: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    files_written: list[str],
    files_removed: list[str],
    mcp: dict[str, Any],
    receipt_path: Path,
    receipt_state: str,
    migration: str | None = None,
) -> dict[str, Any]:
    return {
        "harness": profile.harness,
        "status": status,
        "ready": ready,
        "instruction_ready": not any(item["surface"] == "instruction" for item in conflicts),
        "skills_ready": not any(item["surface"] == "skill" for item in conflicts),
        "reload_hint": profile.reload_hint,
        "items": items,
        "conflicts": conflicts,
        "files_written": sorted(files_written),
        "files_removed": sorted(files_removed),
        "migration": migration,
        "capabilities": {},
        "mcp": mcp,
        "receipt_path": str(receipt_path),
        "receipt_state": receipt_state,
    }


def _receipt_ownership(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "instruction_fingerprint": state.get("instructions", {}).get("digest"),
        "skills": state.get("skills", {}),
        "generated_fingerprints": {
            name: record.get("digest") or record.get("entry_fingerprint")
            for name, record in sorted(state.get("generated", {}).items())
            if isinstance(record, dict)
        },
        "mcp_fingerprints": {
            name: record.get("projected_fingerprint")
            for name, record in sorted(state["mcp"].items())
            if isinstance(record, dict)
        },
    }


def _receipt(
    profile, *, operation: str, items: list[dict[str, Any]], state: dict[str, Any], applied: bool
) -> dict[str, Any]:
    return {
        "version": 1,
        "harness": profile.harness,
        "operation": operation,
        "planned_actions": [
            {"surface": item["surface"], "action": item["action"], "status": item["status"]} for item in items
        ],
        "applied_actions": [
            {"surface": item["surface"], "action": item["action"]}
            for item in items
            if item["action"] in {"create", "update", "remove"}
        ]
        if applied
        else [],
        "ownership_fingerprints": _receipt_ownership(state),
    }


def _uninstall_receipt_conflict(profile, state: dict[str, Any]) -> dict[str, Any] | None:
    """Return a conflict item unless the sync receipt attests to ``state``."""
    path = profile.receipt_path
    item = {"surface": "profile-receipt", "path": str(path), "status": "conflict", "action": "preserve"}
    if not path.exists():
        return item | {"detail": "profile receipt is missing"}
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return item | {"detail": "profile receipt is unreadable"}
    if not isinstance(receipt, dict):
        return item | {"detail": "profile receipt is not an object"}
    if receipt.get("version") != 1 or receipt.get("harness") != profile.harness or receipt.get("operation") != "sync":
        return item | {"detail": "profile receipt does not match this profile"}
    if receipt.get("ownership_fingerprints") != _receipt_ownership(state):
        return item | {"detail": "profile receipt ownership fingerprints drifted"}
    return None


def _state_item(path: Path, *, before: dict[str, Any], after: dict[str, Any], remove: bool = False) -> dict[str, Any]:
    if remove:
        return {
            "surface": "ownership-state",
            "path": str(path),
            "status": "present" if path.exists() else "absent",
            "action": "remove" if path.exists() else "none",
        }
    return {
        "surface": "ownership-state",
        "path": str(path),
        "status": "current" if before == after and path.exists() else "stale",
        "action": "none" if before == after and path.exists() else ("create" if not path.exists() else "update"),
    }


def _receipt_item(path: Path, *, state_action: str, remove: bool = False) -> dict[str, Any]:
    if remove:
        return {
            "surface": "profile-receipt",
            "path": str(path),
            "status": "present" if path.exists() else "absent",
            "action": "remove" if path.exists() else "none",
        }
    action = "none" if state_action == "none" and path.exists() else ("create" if not path.exists() else "update")
    return {
        "surface": "profile-receipt",
        "path": str(path),
        "status": "current" if action == "none" else "stale",
        "action": action,
    }


def _prune_created_directories(paths: list[Path], root: Path) -> list[str]:
    removed: list[str] = []
    for path in sorted(set(paths), reverse=True):
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path == root or _lstat_conflict(path, directory=True):
            continue
        try:
            path.rmdir()
        except OSError:
            continue
        removed.append(str(path))
    return removed


def _sync_profile(
    profile, workspace: Path, *, write: bool, allow_global_stdio: bool, adopt: bool
) -> tuple[dict[str, Any], bool]:
    surface_conflicts = _native_surface_conflicts(profile, workspace)
    if surface_conflicts:
        return _surface_conflict_result(profile, surface_conflicts)
    loaded = load_profile_state(state_path=profile.state_path, workspace=workspace, harness=profile.harness)
    migrated_from_legacy = loaded.migrated_from_legacy
    if loaded.error:
        conflict = {
            "surface": "ownership-state",
            "path": str(profile.state_path),
            "status": "conflict",
            "action": "preserve",
            "detail": loaded.error,
        }
        return _result(
            profile,
            status="conflict",
            ready=False,
            items=[conflict],
            conflicts=[conflict],
            files_written=[],
            files_removed=[],
            mcp={"status": "pending", "items": []},
            receipt_path=profile.receipt_path,
            receipt_state="missing",
        ), False
    state = json.loads(json.dumps(loaded.state))
    instruction_existed = profile.instruction_path.exists()
    instruction = _instruction_plan(profile, state, adopt=adopt)
    items = [
        {
            "surface": "instruction",
            "path": str(instruction.path),
            "status": instruction.status,
            "action": instruction.action,
        }
    ]
    conflicts = (
        []
        if instruction.status != "conflict"
        else [items[0] | {"detail": instruction.detail or "instruction conflict"}]
    )
    skill_plan = _skill_plans(profile, state, workspace)
    items.extend(skill_plan["items"])
    conflicts.extend(skill_plan["conflicts"])
    generated_plan = _generated_plans(profile, state, adopt=adopt)
    items.extend(generated_plan["items"])
    conflicts.extend(generated_plan["conflicts"])
    hook_plan = _hook_plan(profile, state, adopt=adopt) if profile.hook is not None else None
    if hook_plan is not None:
        items.extend(hook_plan["items"])
        conflicts.extend(hook_plan["conflicts"])
    mcp_plan = _mcp_plan(profile, state, workspace, allow_global_stdio=allow_global_stdio, adopt=adopt)
    items.extend(mcp_plan["items"])
    conflicts.extend(mcp_plan["conflicts"])
    proposed = json.loads(json.dumps(state))
    if instruction.status != "conflict":
        old_instruction = state.get("instructions", {})
        created_file = (
            old_instruction.get("created_file", not instruction_existed)
            if isinstance(old_instruction, dict)
            else not instruction_existed
        )
        if instruction.desired_digest:
            instruction_record: dict[str, Any] = {
                "digest": instruction.desired_digest,
                "created_file": created_file,
            }
            if isinstance(old_instruction, dict) and old_instruction.get("created_directories"):
                instruction_record["created_directories"] = list(old_instruction["created_directories"])
            if isinstance(old_instruction, dict) and old_instruction.get(_LEGACY_MIGRATED):
                instruction_record[_LEGACY_MIGRATED] = True
            proposed["instructions"] = instruction_record
        else:
            proposed["instructions"] = {}
    proposed["skills"] = skill_plan["next"]
    proposed["generated"] = generated_plan["next"]
    if hook_plan is not None:
        proposed["generated"][_HOOK_STATE_KEY] = hook_plan["next"]
    proposed["mcp"] = mcp_plan["next"]
    proposed["package_version"] = BRIGADE_VERSION
    state_item = _state_item(profile.state_path, before=state, after=proposed)
    receipt_item = _receipt_item(profile.receipt_path, state_action=state_item["action"])
    items.extend([state_item, receipt_item])
    ready = not conflicts
    files_written: list[str] = []
    files_removed: list[str] = []
    if write and ready:
        changed = False
        if instruction.action in {"create", "update"}:
            instruction_dirs = (
                _missing_directories(profile.user_root, instruction.path)
                if profile.instruction_mode == "managed-file"
                else []
            )
            localio.write_text_atomic(instruction.path, instruction.rendered or "")
            files_written.append(str(instruction.path))
            changed = True
            if profile.instruction_mode == "managed-file":
                existing_dirs = proposed["instructions"].get("created_directories", [])
                proposed["instructions"]["created_directories"] = sorted(set(existing_dirs) | set(instruction_dirs))
        created_dirs: dict[str, list[str]] = {}
        for path, data, skill_id, _relative in skill_plan["writes"]:
            created_dirs.setdefault(skill_id, []).extend(_missing_skill_directories(profile, path))
            localio.write_bytes_atomic(path, data)
            files_written.append(str(path))
            changed = True
        for skill_id, directories in created_dirs.items():
            existing = proposed["skills"][skill_id].setdefault("created_directories", [])
            proposed["skills"][skill_id]["created_directories"] = sorted(set(existing) | set(directories))
        for path in skill_plan["removes"]:
            path.unlink()
            files_removed.append(str(path))
            changed = True
        generated_dirs: dict[str, list[str]] = {}
        for path, text, executable, relative in generated_plan["writes"]:
            generated_dirs.setdefault(relative, []).extend(_missing_directories(profile.user_root, path))
            localio.write_text_atomic(path, text)
            if executable:
                path.chmod(path.stat().st_mode | 0o755)
            files_written.append(str(path))
            changed = True
        for relative, directories in generated_dirs.items():
            existing = proposed["generated"][relative].setdefault("created_directories", [])
            proposed["generated"][relative]["created_directories"] = sorted(set(existing) | set(directories))
        for path in generated_plan["removes"]:
            path.unlink()
            files_removed.append(str(path))
            changed = True
        files_removed.extend(_prune_created_directories(generated_plan["prune"], profile.user_root))
        if hook_plan is not None:
            hook_item = hook_plan["items"][0]
            if hook_item["action"] in {"create", "update"}:
                from . import cursor_user_cmd

                hook_doc = hook_plan["doc"] if hook_plan["doc"] is not None else {}
                entries = hook_doc.setdefault("hooks", {}).setdefault("sessionStart", [])
                if hook_item["action"] == "create":
                    entries.append(profile.hook.entry)
                else:
                    entries[hook_item["prior_index"]] = profile.hook.entry
                localio.write_text_atomic(profile.hook.path, cursor_user_cmd._coowned_json_text(hook_doc))
                files_written.append(str(profile.hook.path))
                changed = True
        if mcp_plan["updates"] or mcp_plan["remove"]:
            localio.write_text_atomic(
                mcp_plan["path"],
                mcp_plan["adapter"].write_file(mcp_plan["text"], mcp_plan["updates"], mcp_plan["remove"]),
            )
            files_written.append(str(mcp_plan["path"]))
            changed = True
        files_removed.extend(_prune_created_directories(skill_plan["prune"], profile.skills_root))
        if state_item["action"] != "none" or changed:
            write_profile_state(state_path=profile.state_path, state=proposed)
            files_written.append(str(profile.state_path))
        receipt = _receipt(profile, operation="sync", items=items, state=proposed, applied=True)
        if receipt_item["action"] != "none" or changed:
            localio.write_json(profile.receipt_path, receipt)
            files_written.append(str(profile.receipt_path))
        return _result(
            profile,
            status="updated" if files_written or files_removed else "current",
            ready=True,
            items=items,
            conflicts=[],
            files_written=files_written,
            files_removed=files_removed,
            mcp={"status": "ready", "items": mcp_plan["items"]},
            receipt_path=profile.receipt_path,
            receipt_state="applied" if files_written or files_removed else "current",
            migration="legacy-v1" if migrated_from_legacy else None,
        ), bool(files_written or files_removed)
    receipt_state = "present" if profile.receipt_path.exists() else "missing"
    return _result(
        profile,
        status="current" if ready else "conflict",
        ready=ready,
        items=items,
        conflicts=conflicts,
        files_written=[],
        files_removed=[],
        mcp={"status": "ready" if not mcp_plan["conflicts"] else "conflict", "items": mcp_plan["items"]},
        receipt_path=profile.receipt_path,
        receipt_state=receipt_state,
        migration="legacy-v1" if migrated_from_legacy and ready else None,
    ), False


def _uninstall_profile(profile, workspace: Path, *, write: bool) -> tuple[dict[str, Any], bool]:
    surface_conflicts = _native_surface_conflicts(profile, workspace)
    if surface_conflicts:
        return _surface_conflict_result(profile, surface_conflicts)
    loaded = load_profile_state(state_path=profile.state_path, workspace=workspace, harness=profile.harness)
    if loaded.error:
        conflict = {
            "surface": "ownership-state",
            "path": str(profile.state_path),
            "status": "conflict",
            "action": "preserve",
            "detail": loaded.error,
        }
        return _result(
            profile,
            status="conflict",
            ready=False,
            items=[conflict],
            conflicts=[conflict],
            files_written=[],
            files_removed=[],
            mcp={"status": "pending", "items": []},
            receipt_path=profile.receipt_path,
            receipt_state="missing",
        ), False
    state = json.loads(json.dumps(loaded.state))
    receipt_conflict = _uninstall_receipt_conflict(profile, state)
    if receipt_conflict:
        return _result(
            profile,
            status="conflict",
            ready=False,
            items=[receipt_conflict],
            conflicts=[receipt_conflict],
            files_written=[],
            files_removed=[],
            mcp={"status": "pending", "items": []},
            receipt_path=profile.receipt_path,
            receipt_state="present" if profile.receipt_path.exists() else "missing",
        ), False
    plan = _instruction_removal_plan(profile, state)
    items = [{"surface": "instruction", "path": str(plan.path), "status": plan.status, "action": plan.action}]
    conflicts = [] if plan.status != "conflict" else [items[0] | {"detail": plan.detail or "instruction conflict"}]
    skill_plan = _skill_uninstall_plan(profile, state)
    items.extend(skill_plan["items"])
    conflicts.extend(skill_plan["conflicts"])
    generated_plan = _generated_uninstall_plan(profile, state)
    items.extend(generated_plan["items"])
    conflicts.extend(generated_plan["conflicts"])
    hook_plan = _hook_uninstall_plan(profile, state) if profile.hook is not None else None
    if hook_plan is not None:
        items.extend(hook_plan["items"])
        conflicts.extend(hook_plan["conflicts"])
    mcp_plan = _mcp_uninstall_plan(profile, state, workspace)
    items.extend(mcp_plan["items"])
    conflicts.extend(mcp_plan["conflicts"])
    proposed = json.loads(json.dumps(state))
    proposed["instructions"] = {}
    proposed["skills"] = {}
    proposed["generated"] = {}
    proposed["mcp"] = {}
    state_item = _state_item(profile.state_path, before=state, after=proposed, remove=True)
    receipt_item = _receipt_item(profile.receipt_path, state_action=state_item["action"], remove=True)
    items.extend([state_item, receipt_item])
    files_removed: list[str] = []
    files_written: list[str] = []
    if write and not conflicts:
        if plan.action == "remove":
            if plan.rendered is None:
                plan.path.unlink(missing_ok=True)
            else:
                localio.write_text_atomic(plan.path, plan.rendered)
            files_removed.append(str(plan.path))
        instruction_dirs = state.get("instructions", {})
        instruction_dirs = (
            [Path(path) for path in instruction_dirs.get("created_directories", []) if isinstance(path, str)]
            if isinstance(instruction_dirs, dict)
            else []
        )
        for path in skill_plan["removes"]:
            path.unlink(missing_ok=True)
            files_removed.append(str(path))
        files_removed.extend(_prune_created_directories(skill_plan["prune"], profile.skills_root))
        for path in generated_plan["removes"]:
            path.unlink(missing_ok=True)
            files_removed.append(str(path))
        files_removed.extend(_prune_created_directories(instruction_dirs + generated_plan["prune"], profile.user_root))
        if hook_plan is not None and hook_plan["doc"] is not None and hook_plan["index"] is not None:
            from . import cursor_user_cmd

            hook_doc = hook_plan["doc"]
            entries = hook_doc["hooks"]["sessionStart"]
            entries.pop(hook_plan["index"])
            if not entries:
                hook_doc["hooks"].pop("sessionStart", None)
            localio.write_text_atomic(profile.hook.path, cursor_user_cmd._coowned_json_text(hook_doc))
            files_removed.append(str(profile.hook.path))
        if mcp_plan["remove"]:
            localio.write_text_atomic(
                mcp_plan["path"], mcp_plan["adapter"].write_file(mcp_plan["text"], {}, mcp_plan["remove"])
            )
            files_removed.append(str(mcp_plan["path"]))
        if profile.state_path.exists():
            profile.state_path.unlink()
            files_removed.append(str(profile.state_path))
        if profile.receipt_path.exists():
            profile.receipt_path.unlink()
            files_removed.append(str(profile.receipt_path))
    changed = bool(files_removed)
    return _result(
        profile,
        status="conflict" if conflicts else ("updated" if changed else "current"),
        ready=not conflicts,
        items=items,
        conflicts=conflicts,
        files_written=files_written,
        files_removed=files_removed,
        mcp={"status": "ready" if not mcp_plan["conflicts"] else "conflict", "items": mcp_plan["items"]},
        receipt_path=profile.receipt_path,
        receipt_state="removed" if changed else ("present" if profile.receipt_path.exists() else "missing"),
    ), changed


def _doctor_profile(profile, workspace: Path, *, verify_mcp: bool) -> tuple[dict[str, Any], bool]:
    surface_conflicts = _native_surface_conflicts(profile, workspace)
    if surface_conflicts:
        return _surface_conflict_result(profile, surface_conflicts)
    loaded = load_profile_state(state_path=profile.state_path, workspace=workspace, harness=profile.harness)
    if loaded.error:
        conflict = {
            "surface": "ownership-state",
            "path": str(profile.state_path),
            "status": "conflict",
            "action": "preserve",
            "detail": loaded.error,
        }
        return _result(
            profile,
            status="conflict",
            ready=False,
            items=[conflict],
            conflicts=[conflict],
            files_written=[],
            files_removed=[],
            mcp={"status": "conflict", "items": []},
            receipt_path=profile.receipt_path,
            receipt_state="missing",
        ), False
    state = loaded.state
    instruction = _instruction_plan(profile, state, adopt=False)
    item = {
        "surface": "instruction",
        "path": str(instruction.path),
        "status": instruction.status,
        "action": instruction.action,
    }
    conflicts = [item] if instruction.status != "current" else []
    skill_plan = _skill_plans(profile, state, workspace)
    skill_issues = [entry for entry in skill_plan["items"] if entry["status"] != "current"]
    conflicts.extend(skill_issues)
    generated_plan = _generated_plans(profile, state, adopt=False)
    generated_issues = [entry for entry in generated_plan["items"] if entry["status"] != "current"]
    conflicts.extend(generated_issues)
    hook_items: list[dict[str, Any]] = []
    if profile.hook is not None:
        hook_plan = _hook_plan(profile, state, adopt=False)
        hook_items = hook_plan["items"]
        conflicts.extend(entry for entry in hook_items if entry["status"] != "current")
    mcp, mcp_ok = _verify_mcp(profile, state, workspace) if verify_mcp else ({"status": "pending", "items": []}, True)
    ready = not conflicts and mcp_ok
    return _result(
        profile,
        status="current" if ready else "conflict",
        ready=ready,
        items=[item, *skill_plan["items"], *generated_plan["items"], *hook_items],
        conflicts=conflicts,
        files_written=[],
        files_removed=[],
        mcp=mcp,
        receipt_path=profile.receipt_path,
        receipt_state="present" if profile.receipt_path.exists() else "missing",
    ), False


def _run(
    operation: str,
    *,
    harness: str,
    workspace: Path,
    write: bool = False,
    allow_global_stdio: bool = False,
    adopt: bool = False,
    verify_mcp: bool = False,
    json_output: bool = False,
    home: Path | None = None,
) -> int:
    profiles = harness_profiles.resolve_user_profiles(harness=harness, home=home or Path.home(), workspace=workspace)

    def run_profile(profile, *, profile_write: bool) -> tuple[dict[str, Any], bool]:
        if operation == "sync":
            return _sync_profile(
                profile, workspace, write=profile_write, allow_global_stdio=allow_global_stdio, adopt=adopt
            )
        elif operation == "uninstall":
            return _uninstall_profile(profile, workspace, write=profile_write)
        return _doctor_profile(profile, workspace, verify_mcp=verify_mcp)

    if write and len(profiles) > 1 and operation in {"sync", "uninstall"}:
        preflight = [run_profile(profile, profile_write=False) for profile in profiles]
        if any(not result["ready"] for result, _reload in preflight):
            results = [result for result, _reload in preflight]
            reload_required = False
        else:
            applied = [run_profile(profile, profile_write=True) for profile in profiles]
            results = [result for result, _reload in applied]
            reload_required = any(reload for _result, reload in applied)
    else:
        completed = [run_profile(profile, profile_write=write) for profile in profiles]
        results = [result for result, _reload in completed]
        reload_required = any(reload for _result, reload in completed)
    payload = {
        "operation": operation,
        "results": results,
        "ready": all(result["ready"] for result in results),
        "reload_required": reload_required,
    }
    if json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        for result in results:
            print(
                f"{result['harness']}: {result['status']} receipt={result['receipt_path']} ({result['receipt_state']})"
            )
    return 0 if payload["ready"] else 1


def sync(
    *,
    harness: str,
    workspace: Path,
    write: bool = False,
    allow_global_stdio: bool = False,
    adopt: bool = False,
    json_output: bool = False,
    home: Path | None = None,
) -> int:
    return _run(
        "sync",
        harness=harness,
        workspace=workspace,
        write=write,
        allow_global_stdio=allow_global_stdio,
        adopt=adopt,
        json_output=json_output,
        home=home,
    )


def uninstall(
    *, harness: str, workspace: Path, write: bool = False, json_output: bool = False, home: Path | None = None
) -> int:
    return _run("uninstall", harness=harness, workspace=workspace, write=write, json_output=json_output, home=home)


def doctor(
    *, harness: str, workspace: Path, verify_mcp: bool = False, json_output: bool = False, home: Path | None = None
) -> int:
    return _run(
        "doctor", harness=harness, workspace=workspace, verify_mcp=verify_mcp, json_output=json_output, home=home
    )
