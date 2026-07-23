"""User-scope harness profile command logic.

Issue #438: managed-block parsing, ownership-state validation, skill/artifact
reconciliation, and aggregate install/uninstall/doctor. This module owns the
managed instruction block surface plan; profile records and native path
resolution live in the sibling ``harness_profiles`` module.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__ as BRIGADE_VERSION
from . import cursor_user_cmd, harness_profiles, localio, mcp_adapters, mcp_cmd, skills_cmd, templates
from .toml_compat import loads as _toml_loads, TOMLDecodeError as _TOMLDecodeError

_RECOVERY_COMMAND = "brigade harness install <harness> --scope user --adopt --write"


@dataclass(frozen=True)
class SurfacePlan:
    surface: str
    path: Path
    status: str
    action: str
    desired_digest: str | None = None
    rendered: str | None = None
    detail: str | None = None


def digest_text(text: str) -> str:
    """Return the sha256 hex digest of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block(start: str, body: str, end: str) -> str:
    return f"{start}\n{body}\n{end}\n"


def _find_markers(text: str, start: str, end: str) -> tuple[int, int] | None:
    if text.count(start) != 1 or text.count(end) != 1:
        return None
    start_pos = text.find(start)
    end_pos = text.find(end)
    if end_pos <= start_pos:
        return None
    after_start = start_pos + len(start)
    before_end = end_pos
    if after_start >= len(text) or text[after_start] != "\n":
        return None
    if before_end <= 0 or text[before_end - 1] != "\n":
        return None
    return start_pos, end_pos


def _split_components(text: str, start: str, end: str) -> tuple[str, str, str] | None:
    found = _find_markers(text, start, end)
    if found is None:
        return None
    start_pos, end_pos = found
    body = text[start_pos + len(start) + 1 : end_pos - 1]
    before = text[:start_pos]
    block_end = end_pos + len(end)
    if block_end < len(text) and text[block_end] == "\n":
        block_end += 1
    after = text[block_end:]
    return before, body, after


def plan_instruction(
    *,
    path: Path,
    desired: str,
    state: dict[str, Any],
    adopt: bool = False,
) -> SurfacePlan:
    """Plan an install/update of the managed instruction block in ``path``."""
    start = harness_profiles.INSTRUCTION_START
    end = harness_profiles.INSTRUCTION_END
    desired_digest_body = digest_text(desired)
    instructions = state.get("instructions", {}) if state else {}
    owned_digest = instructions.get("digest") if isinstance(instructions, dict) else None

    if not path.exists():
        rendered = _block(start, desired, end)
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="missing",
            action="create",
            desired_digest=desired_digest_body,
            rendered=rendered,
        )

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="conflict",
            action="preserve",
            desired_digest=desired_digest_body,
            detail=str(exc),
        )

    components = _split_components(text, start, end)
    if components is None:
        if start not in text and end not in text:
            sep = "\n" if text else ""
            before = text
            rendered = before + sep + _block(start, desired, end)
            return SurfacePlan(
                surface="instruction",
                path=path,
                status="missing",
                action="create",
                desired_digest=desired_digest_body,
                rendered=rendered,
            )
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="conflict",
            action="preserve",
            desired_digest=desired_digest_body,
            detail=f"managed instruction markers are malformed; recover with: {_RECOVERY_COMMAND}",
        )

    before, body, after = components
    live_digest = digest_text(body)
    desired_digest = digest_text(desired)

    if live_digest == desired_digest:
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="current",
            action="none",
            desired_digest=desired_digest,
            rendered=None,
        )
    if owned_digest is not None and live_digest == owned_digest:
        rendered = before + _block(start, desired, end) + after
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="stale",
            action="update",
            desired_digest=desired_digest,
            rendered=rendered,
        )
    if adopt:
        rendered = before + _block(start, desired, end) + after
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="stale",
            action="update",
            desired_digest=desired_digest,
            rendered=rendered,
        )
    return SurfacePlan(
        surface="instruction",
        path=path,
        status="conflict",
        action="preserve",
        desired_digest=desired_digest,
        detail=f"foreign managed instruction block; recover with: {_RECOVERY_COMMAND}",
    )


def plan_instruction_removal(*, path: Path, state: dict[str, Any]) -> SurfacePlan:
    """Plan removal of the managed instruction block owned by ``state``."""
    start = harness_profiles.INSTRUCTION_START
    end = harness_profiles.INSTRUCTION_END
    instructions = state.get("instructions", {}) if state else {}
    owned_digest = instructions.get("digest") if isinstance(instructions, dict) else None

    if not path.exists():
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="absent",
            action="none",
            desired_digest=None,
            rendered=None,
        )

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="conflict",
            action="preserve",
            desired_digest=None,
            detail=str(exc),
        )

    components = _split_components(text, start, end)
    if components is None:
        if start not in text and end not in text:
            return SurfacePlan(
                surface="instruction",
                path=path,
                status="absent",
                action="none",
                desired_digest=None,
                rendered=None,
            )
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="conflict",
            action="preserve",
            desired_digest=None,
            detail=f"managed instruction markers are malformed; recover with: {_RECOVERY_COMMAND}",
        )

    before, body, after = components
    live_digest = digest_text(body)
    if owned_digest is not None and live_digest == owned_digest:
        if before.endswith("\n"):
            rendered = before[:-1] + after
        else:
            rendered = before + after
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="managed",
            action="remove",
            desired_digest=owned_digest,
            rendered=rendered,
        )
    return SurfacePlan(
        surface="instruction",
        path=path,
        status="conflict",
        action="preserve",
        desired_digest=owned_digest,
        detail=f"managed instruction block was edited; recover with: {_RECOVERY_COMMAND}",
    )


_SKILLS_OUTSIDE_ROOT = "path resolves outside the profile skills root"


@dataclass(frozen=True)
class SkillFilePlan:
    """A per-file plan entry for one file in one user-profile skill package."""

    skill_id: str
    relative_path: str
    path: Path
    status: str
    action: str
    desired_digest: str | None
    detail: str | None = None


def digest_bytes(data: bytes) -> str:
    """Return the sha256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def empty_profile_state(*, workspace: Path, harness: str) -> dict[str, Any]:
    """Return a fresh schema-v2 ownership state seeded for ``workspace``/``harness``."""
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
    """Persist ``state`` atomically as sorted-key JSON at ``state_path``."""
    localio.write_json(state_path, state)


@dataclass(frozen=True)
class LoadedProfileState:
    """Result of loading and validating a profile ownership-state file."""

    state: dict[str, Any]
    error: str | None


_PROFILE_STATE_SECTIONS: tuple[str, ...] = ("instructions", "skills", "generated", "mcp")


def load_profile_state(*, state_path: Path, workspace: Path, harness: str) -> LoadedProfileState:
    """Load and validate the ownership state at ``state_path``.

    A missing path returns a fresh schema-v2 state seeded for ``workspace`` and
    ``harness`` with no error and no write. An existing path must be readable
    UTF-8 JSON. Validation order is: top-level object, exact schema version,
    harness identity, resolved workspace identity, then each of
    ``instructions``/``skills``/``generated``/``mcp`` being an object. A valid
    state is returned unchanged except ``package_version``: if it differs from
    the current ``BRIGADE_VERSION`` an independent top-level copy is written
    atomically and returned. Malformed files are never mutated.
    """
    if not state_path.exists():
        return LoadedProfileState(empty_profile_state(workspace=workspace, harness=harness), None)

    try:
        raw = state_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return LoadedProfileState({}, "ownership state is unreadable")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return LoadedProfileState({}, "ownership state is unreadable")

    if not isinstance(state, dict):
        return LoadedProfileState({}, "ownership state is not an object")

    version = state.get("schema_version")
    if version != harness_profiles.PROFILE_STATE_VERSION:
        return LoadedProfileState({}, f"unsupported ownership state version: {version}")

    if state.get("harness") != harness:
        return LoadedProfileState({}, f"ownership harness mismatch: {state.get('harness')} != {harness}")

    stored_workspace = state.get("workspace")
    if isinstance(stored_workspace, str) and stored_workspace:
        stored_resolved: Any = Path(stored_workspace).expanduser().resolve()
    else:
        stored_resolved = stored_workspace
    requested_resolved = workspace.expanduser().resolve()
    if stored_resolved != requested_resolved:
        return LoadedProfileState(
            {},
            f"ownership workspace mismatch: {stored_resolved} != {requested_resolved}",
        )

    for section in _PROFILE_STATE_SECTIONS:
        if not isinstance(state.get(section), dict):
            return LoadedProfileState({}, f"ownership state section is not an object: {section}")

    if state.get("package_version") != BRIGADE_VERSION:
        refreshed = dict(state)
        refreshed["package_version"] = BRIGADE_VERSION
        write_profile_state(state_path=state_path, state=refreshed)
        return LoadedProfileState(refreshed, None)

    return LoadedProfileState(state, None)


@dataclass(frozen=True)
class LoadedCursorProfileState:
    """Result of loading a Cursor profile ownership state, with optional migration.

    ``migration`` is ``"cursor-state-v1"`` when a legacy v1 state was migrated
    in memory, otherwise ``None``. ``retire_mcp_names`` names the live
    ``~/.cursor/mcp.json`` entries the caller may retire after a v1 migration;
    it is empty otherwise. ``retire_file_paths`` is the sorted tuple of absolute
    ``Path`` objects for legacy ``skill``/``mcp-catalog`` surfaces the caller
    may retire after a v1 migration; it is empty otherwise. The loader never
    persists migrated state and never edits MCP config: Task 7 owns the
    retirement + artifact-apply transaction.
    """

    state: dict[str, Any]
    error: str | None
    migration: str | None
    retire_mcp_names: tuple[str, ...]
    retire_file_paths: tuple[Path, ...]
    retire_file_ownership: tuple[tuple[Path, str], ...] = ()
    retire_mcp_ownership: tuple[tuple[str, str], ...] = ()


def load_cursor_profile_state(
    *, state_path: Path, workspace: Path, root: Path | None = None
) -> LoadedCursorProfileState:
    """Load a Cursor profile ownership state, migrating legacy v1 in memory.

    A missing state path or an existing schema-v2 state delegates to
    ``load_profile_state`` with ``harness="cursor"`` and reports no migration.
    A legacy v1 state (``"version": 1`` with no ``"schema_version"``) is
    migrated in memory by ``cursor_user_cmd.migrate_v1_state``: the returned
    state is schema-v2 with legacy file/hook ownership moved into ``generated``
    and ``skills``/``mcp`` left empty for the shared stages to own, and
    ``retire_mcp_names`` names the live ``~/.cursor/mcp.json`` entries the
    caller may retire. This loader never persists migrated state and never
    edits MCP config.
    """
    if not state_path.exists():
        loaded = load_profile_state(state_path=state_path, workspace=workspace, harness="cursor")
        return LoadedCursorProfileState(loaded.state, loaded.error, None, (), ())

    try:
        raw = state_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return LoadedCursorProfileState({}, "ownership state is unreadable", None, (), ())
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return LoadedCursorProfileState({}, "ownership state is unreadable", None, (), ())
    if not isinstance(payload, dict):
        return LoadedCursorProfileState({}, "ownership state is not an object", None, (), ())

    if payload.get("version") == 1 and "schema_version" not in payload:
        from . import cursor_user_cmd  # local import avoids a module-level cycle

        cursor_root = root if root is not None else cursor_user_cmd._cursor_root()
        migration = cursor_user_cmd.migrate_v1_state(root=cursor_root, workspace=workspace, state=payload)
        if migration.error is not None:
            return LoadedCursorProfileState({}, migration.error, None, (), ())
        return LoadedCursorProfileState(
            migration.state,
            None,
            "cursor-state-v1",
            migration.retire_mcp_names,
            migration.retire_file_paths,
            migration.retire_file_ownership,
            migration.retire_mcp_ownership,
        )

    loaded = load_profile_state(state_path=state_path, workspace=workspace, harness="cursor")
    return LoadedCursorProfileState(loaded.state, loaded.error, None, (), ())


def _validate_skill_id(skill_id: str) -> None:
    if not skill_id or skill_id == "." or skill_id.startswith("/"):
        raise ValueError(_SKILLS_OUTSIDE_ROOT)
    if ".." in Path(skill_id).parts:
        raise ValueError(_SKILLS_OUTSIDE_ROOT)


def _validate_relative_path(relative_path: str) -> None:
    if not relative_path or relative_path == "." or relative_path.startswith("/"):
        raise ValueError(_SKILLS_OUTSIDE_ROOT)
    if ".." in Path(relative_path).parts:
        raise ValueError(_SKILLS_OUTSIDE_ROOT)


def _resolve_skill_destination(*, skills_root: Path, skill_id: str, relative_path: str) -> Path:
    """Resolve and contain ``skills_root / skill_id / relative_path``.

    Rejects empty, ".", absolute, and any ``..`` component in either the skill
    id or the relative path, and rejects any resolved destination or package
    root that is not relative to ``skills_root.resolve()`` (which catches
    symlinked intermediate directories that escape the skills root).
    """
    _validate_skill_id(skill_id)
    _validate_relative_path(relative_path)
    root_resolved = skills_root.resolve()
    package_root = (skills_root / skill_id).resolve()
    if package_root == root_resolved or not package_root.is_relative_to(root_resolved):
        raise ValueError(_SKILLS_OUTSIDE_ROOT)
    destination = (skills_root / skill_id / relative_path).resolve()
    if destination == root_resolved or not destination.is_relative_to(root_resolved):
        raise ValueError(_SKILLS_OUTSIDE_ROOT)
    return destination


def _missing_parent_dirs(*, skills_root: Path, destination: Path) -> list[Path]:
    """Return ancestor directories under ``skills_root`` that do not yet exist.

    Walks from the file's parent up to (but not including) ``skills_root`` and
    collects the directories that do not yet exist, leaf-first. These are the
    directories Brigade will create by writing the file atomically.
    """
    root_resolved = skills_root.resolve()
    parent = destination.parent
    missing: list[Path] = []
    cursor = parent
    while cursor != root_resolved and root_resolved in cursor.parents:
        if cursor.exists():
            break
        missing.append(cursor)
        cursor = cursor.parent
    # leaf-first so created_directories can be recorded deepest-first later
    return missing


def _skill_record(*, state: dict[str, Any], skill_id: str) -> dict[str, Any]:
    skills = state.get("skills")
    if not isinstance(skills, dict):
        skills = {}
        state["skills"] = skills
    record = skills.get(skill_id)
    if not isinstance(record, dict):
        record = {}
        skills[skill_id] = record
    if not isinstance(record.get("files"), dict):
        record["files"] = {}
    if not isinstance(record.get("created_directories"), list):
        record["created_directories"] = []
    return record


def plan_skills(
    *,
    skills_root: Path,
    packages: tuple[Any, ...],
    state: dict[str, Any],
) -> tuple[SkillFilePlan, ...]:
    """Plan per-file install/update actions for ``packages`` under ``skills_root``.

    Iterates packages and their files in deterministic ``(skill_id, POSIX
    relative path)`` order. Validates every destination before any planning
    completes, so a single unsafe path rejects the whole plan.
    """
    skills = state.get("skills") if isinstance(state, dict) else None
    if not isinstance(skills, dict):
        skills = {}

    # Validate every destination first, before any planning.
    ordered: list[tuple[str, str, bytes, Path]] = []
    for package in sorted(packages, key=lambda pkg: pkg.skill_id):
        skill_id = package.skill_id
        for relative_path, data in sorted(package.files.items(), key=lambda item: item[0]):
            destination = _resolve_skill_destination(
                skills_root=skills_root, skill_id=skill_id, relative_path=relative_path
            )
            ordered.append((skill_id, relative_path, data, destination))

    plans: list[SkillFilePlan] = []
    for skill_id, relative_path, data, destination in ordered:
        desired_digest = digest_bytes(data)
        if not destination.exists():
            plans.append(
                SkillFilePlan(
                    skill_id=skill_id,
                    relative_path=relative_path,
                    path=destination,
                    status="missing",
                    action="create",
                    desired_digest=desired_digest,
                )
            )
            continue
        try:
            live = destination.read_bytes()
        except OSError as exc:
            plans.append(
                SkillFilePlan(
                    skill_id=skill_id,
                    relative_path=relative_path,
                    path=destination,
                    status="conflict",
                    action="preserve",
                    desired_digest=desired_digest,
                    detail=str(exc),
                )
            )
            continue
        live_digest = digest_bytes(live)
        if live_digest == desired_digest:
            plans.append(
                SkillFilePlan(
                    skill_id=skill_id,
                    relative_path=relative_path,
                    path=destination,
                    status="current",
                    action="none",
                    desired_digest=desired_digest,
                )
            )
            continue
        record = skills.get(skill_id)
        owned_files = record.get("files") if isinstance(record, dict) else None
        owned_digest = owned_files.get(relative_path) if isinstance(owned_files, dict) else None
        if owned_digest is not None and live_digest == owned_digest:
            plans.append(
                SkillFilePlan(
                    skill_id=skill_id,
                    relative_path=relative_path,
                    path=destination,
                    status="stale",
                    action="update",
                    desired_digest=desired_digest,
                )
            )
        else:
            plans.append(
                SkillFilePlan(
                    skill_id=skill_id,
                    relative_path=relative_path,
                    path=destination,
                    status="conflict",
                    action="preserve",
                    desired_digest=desired_digest,
                    detail="foreign skill file; recover with: brigade harness install <harness> --scope user --adopt --write",
                )
            )
    return tuple(plans)


def _clone_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return an independent copy of ``state`` deep enough for skill ownership."""
    clone: dict[str, Any] = {}
    for key, value in state.items():
        if key == "skills" and isinstance(value, dict):
            skills: dict[str, Any] = {}
            for skill_id, record in value.items():
                if isinstance(record, dict):
                    skills[skill_id] = {
                        "source_identity": record.get("source_identity"),
                        "source_fingerprint": record.get("source_fingerprint"),
                        "metadata_fingerprint": record.get("metadata_fingerprint"),
                        "files": dict(record["files"]) if isinstance(record.get("files"), dict) else {},
                        "created_directories": list(
                            record["created_directories"] if isinstance(record.get("created_directories"), list) else []
                        ),
                    }
                else:
                    skills[skill_id] = record
            clone[key] = skills
        else:
            clone[key] = value
    return clone


def apply_skill_plan(
    *,
    skills_root: Path,
    packages: tuple[Any, ...],
    plans: tuple[SkillFilePlan, ...],
    prior_state: dict[str, Any],
    state_path: Path,
) -> tuple[dict[str, Any], list[str]]:
    """Apply only ``create``/``update`` plans, persisting ownership per file.

    Returns an independent new state and the sorted absolute paths written. The
    caller-supplied ``prior_state`` is never mutated. Ownership for a file is
    recorded only when Brigade writes it; current/none files are claimed only
    if the prior state already owned them.
    """
    package_by_id = {pkg.skill_id: pkg for pkg in packages}
    new_state = _clone_state(prior_state)
    written: list[str] = []

    for plan in plans:
        if plan.action not in {"create", "update"}:
            continue
        package = package_by_id[plan.skill_id]
        data = package.files[plan.relative_path]
        missing = _missing_parent_dirs(skills_root=skills_root, destination=plan.path)
        localio.write_bytes_atomic(plan.path, data)
        written.append(str(plan.path))

        record = _skill_record(state=new_state, skill_id=plan.skill_id)
        record["source_identity"] = package.source_identity
        record["source_fingerprint"] = package.source_fingerprint
        record["metadata_fingerprint"] = package.metadata_fingerprint
        record["files"][plan.relative_path] = plan.desired_digest
        created = set(record["created_directories"])
        root_resolved = skills_root.resolve()
        for directory in missing:
            rel = directory.resolve().relative_to(root_resolved).as_posix()
            created.add(rel)
        record["created_directories"] = sorted(created)

        write_profile_state(state_path=state_path, state=new_state)

    return new_state, sorted(written)


def plan_skill_removals(
    *,
    skills_root: Path,
    state: dict[str, Any],
) -> tuple[SkillFilePlan, ...]:
    """Plan removal of every Brigade-owned skill file recorded in ``state``.

    Emits entries deepest relative path first. A live file whose digest matches
    the owned digest is ``managed/remove``; a missing file is ``absent/none``;
    a changed file is ``conflict/preserve``. State-derived paths are validated
    with the same containment check as ``plan_skills``.
    """
    skills = state.get("skills") if isinstance(state, dict) else None
    if not isinstance(skills, dict):
        return ()

    entries: list[tuple[str, str, str]] = []
    for skill_id, record in skills.items():
        if not isinstance(record, dict):
            continue
        owned_files = record.get("files")
        if not isinstance(owned_files, dict):
            continue
        for relative_path, owned_digest in owned_files.items():
            if not isinstance(owned_digest, str):
                continue
            entries.append((skill_id, relative_path, owned_digest))

    # deepest relative paths first: more path components, then reverse POSIX
    entries.sort(key=lambda item: (-len(Path(item[1]).parts), item[1], item[0]))

    plans: list[SkillFilePlan] = []
    for skill_id, relative_path, owned_digest in entries:
        destination = _resolve_skill_destination(
            skills_root=skills_root, skill_id=skill_id, relative_path=relative_path
        )
        if not destination.exists():
            plans.append(
                SkillFilePlan(
                    skill_id=skill_id,
                    relative_path=relative_path,
                    path=destination,
                    status="absent",
                    action="none",
                    desired_digest=owned_digest,
                )
            )
            continue
        try:
            live = destination.read_bytes()
        except OSError as exc:
            plans.append(
                SkillFilePlan(
                    skill_id=skill_id,
                    relative_path=relative_path,
                    path=destination,
                    status="conflict",
                    action="preserve",
                    desired_digest=owned_digest,
                    detail=str(exc),
                )
            )
            continue
        if digest_bytes(live) == owned_digest:
            plans.append(
                SkillFilePlan(
                    skill_id=skill_id,
                    relative_path=relative_path,
                    path=destination,
                    status="managed",
                    action="remove",
                    desired_digest=owned_digest,
                )
            )
        else:
            plans.append(
                SkillFilePlan(
                    skill_id=skill_id,
                    relative_path=relative_path,
                    path=destination,
                    status="conflict",
                    action="preserve",
                    desired_digest=owned_digest,
                    detail="owned skill file was edited; recover with: brigade harness install <harness> --scope user --adopt --write",
                )
            )
    return tuple(plans)


def apply_skill_removals(
    *,
    skills_root: Path,
    plans: tuple[SkillFilePlan, ...],
    state: dict[str, Any],
    state_path: Path,
) -> list[str]:
    """Unlink only ``managed/remove`` files, then prune empty recorded dirs.

    After each removed file, that file's ownership record is dropped from state
    and the state is persisted immediately. Then state-recorded
    ``created_directories`` are removed deepest-first with ``rmdir()`` only;
    nonempty or unowned directories are kept. A skill record is cleaned only
    when no owned files and no recorded directories remain. Returns the sorted
    absolute paths of files actually removed.
    """
    new_state = _clone_state(state)
    root_resolved = skills_root.resolve()
    removed: list[str] = []

    for plan in plans:
        if plan.action == "remove":
            try:
                plan.path.unlink()
            except FileNotFoundError:
                continue
            except PermissionError:
                raise
            removed.append(str(plan.path))
            record = new_state.get("skills", {}).get(plan.skill_id)
            if isinstance(record, dict) and isinstance(record.get("files"), dict):
                record["files"].pop(plan.relative_path, None)
            write_profile_state(state_path=state_path, state=new_state)
        elif plan.action == "none" and plan.status == "absent":
            # the owned file is already gone on disk: clear stale ownership so a
            # repeated uninstall converges. Conflict surfaces keep ownership.
            record = new_state.get("skills", {}).get(plan.skill_id)
            if isinstance(record, dict) and isinstance(record.get("files"), dict):
                record["files"].pop(plan.relative_path, None)
            write_profile_state(state_path=state_path, state=new_state)

    # prune recorded created directories deepest-first; rmdir only, never unlink
    skills = new_state.get("skills")
    if isinstance(skills, dict):
        all_dirs: list[tuple[str, str]] = []
        for skill_id, record in skills.items():
            if not isinstance(record, dict):
                continue
            for rel in record.get("created_directories", []) or []:
                if isinstance(rel, str):
                    all_dirs.append((skill_id, rel))
        all_dirs.sort(key=lambda item: (-len(Path(item[1]).parts), item[1], item[0]))
        for skill_id, rel in all_dirs:
            directory = (root_resolved / rel).resolve()
            if not directory.is_relative_to(root_resolved):
                continue
            try:
                directory.rmdir()
            except OSError:
                continue
            record = skills.get(skill_id)
            if isinstance(record, dict) and isinstance(record.get("created_directories"), list):
                record["created_directories"] = [d for d in record["created_directories"] if d != rel]

    # clean empty skill records: no owned files and no recorded dirs remain
    if isinstance(skills, dict):
        for skill_id in list(skills.keys()):
            record = skills[skill_id]
            if not isinstance(record, dict):
                continue
            files = record.get("files")
            dirs = record.get("created_directories")
            if isinstance(files, dict) and not files and isinstance(dirs, list) and not dirs:
                del skills[skill_id]

    write_profile_state(state_path=state_path, state=new_state)
    return sorted(removed)


# --- Issue #438 Task 7: aggregate orchestration ---

_MCP_PENDING = {"status": "pending", "items": []}
_PRIVATE_KEYS = ("digest", "fingerprint", "desired_digest", "desired_fingerprint", "prior_index", "_rel")


def _strip_private(value):
    if isinstance(value, dict):
        return {
            k: _strip_private(v) for k, v in value.items() if not any(k.endswith(p) or p in k for p in _PRIVATE_KEYS)
        }
    if isinstance(value, list):
        return [_strip_private(v) for v in value]
    return value


def _result(
    harness,
    *,
    status,
    ready,
    instruction_ready,
    skills_ready,
    reload_hint,
    items,
    conflicts,
    files_written,
    files_removed,
    migration,
    capabilities,
    mcp=None,
):
    return {
        "harness": harness,
        "status": status,
        "ready": ready,
        "instruction_ready": instruction_ready,
        "skills_ready": skills_ready,
        "reload_hint": reload_hint,
        "items": _strip_private(items),
        "conflicts": _strip_private(conflicts),
        "files_written": sorted(files_written),
        "files_removed": sorted(files_removed),
        "migration": migration,
        "capabilities": capabilities,
        "mcp": dict(mcp) if mcp is not None else dict(_MCP_PENDING),
    }


def _git_tracks(workspace: Path, rel: str) -> bool | None:
    """Return True if ``workspace/rel`` is inside a git work tree and tracked.

    ``None`` means "could not prove tracked" (not in a work tree, or git failed);
    callers may proceed in that case. Uses ``stdin=DEVNULL`` and a 10s timeout.
    """
    try:
        inside = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    try:
        tracked = subprocess.run(
            ["git", "-C", str(workspace), "ls-files", "--error-unmatch", rel],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if tracked.returncode == 0:
        return True
    return None


def _load_state_for_profile(profile, workspace):
    """Return (state, error, migration, retire_mcp_names, retire_file_paths,
    retire_file_ownership, retire_mcp_ownership)."""
    if profile.harness == "cursor":
        loaded = load_cursor_profile_state(state_path=profile.state_path, workspace=workspace, root=profile.user_root)
        return (
            loaded.state,
            loaded.error,
            loaded.migration,
            loaded.retire_mcp_names,
            loaded.retire_file_paths,
            loaded.retire_file_ownership,
            loaded.retire_mcp_ownership,
        )
    loaded = load_profile_state(state_path=profile.state_path, workspace=workspace, harness=profile.harness)
    return loaded.state, loaded.error, None, (), (), (), ()


def _plan_instruction_for_profile(profile, state, adopt, workspace, *, guard_tracked_write=True):
    """Return (plan_or_None, conflict_item_or_None). plan is a SurfacePlan or None when cursor.

    When ``guard_tracked_write`` is True (install), a workspace file tracked by
    git is a write conflict. When False (doctor), the tracked-ness is ignored:
    doctor only reports whether the current content is the managed block, so a
    tracked file carrying current managed content is ready.
    """
    if profile.instruction_path is None:
        return None, None
    path = profile.instruction_path
    if profile.harness == "openclaw" and guard_tracked_write:
        try:
            rel = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            rel = path.name
        tracked = _git_tracks(workspace, rel)
        if tracked is True:
            return None, {
                "surface": "instruction",
                "path": str(path),
                "status": "conflict",
                "action": "preserve",
                "detail": f"tracked by git; refusing to mutate repo file: {path}",
            }
    desired = harness_profiles.managed_instruction_text()
    plan = plan_instruction(path=path, desired=desired, state=state, adopt=adopt)
    if plan.status == "conflict":
        return plan, {
            "surface": "instruction",
            "path": str(path),
            "status": "conflict",
            "action": "preserve",
            "detail": plan.detail,
        }
    return plan, None


def _plan_skills_for_profile(profile, state, workspace):
    """Return (plans, conflict_items, error). On ValueError/error returns empty plans + conflict."""
    conflicts = []
    try:
        packages = skills_cmd.user_profile_skill_packages(
            workspace=workspace, harness=profile.harness, minimum_trust="workspace"
        )
    except Exception as exc:  # registry failures are a profile conflict, not a crash
        return (
            (),
            [
                {
                    "surface": "skills",
                    "path": str(profile.skills_root),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": str(exc),
                }
            ],
            None,
        )
    try:
        plans = plan_skills(skills_root=profile.skills_root, packages=packages, state=state)
    except ValueError as exc:
        return (
            (),
            [
                {
                    "surface": "skills",
                    "path": str(profile.skills_root),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": str(exc),
                }
            ],
            None,
        )
    for p in plans:
        if p.status == "conflict":
            conflicts.append(
                {
                    "surface": "skill",
                    "skill_id": p.skill_id,
                    "path": str(p.path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": p.detail,
                }
            )
    return plans, conflicts, None


def _plan_cursor_generated(profile, state, adopt=False):
    """Plan plugin/rule/hook generated files against v2 ``generated`` ownership.

    ``adopt`` reclassifies a foreign, well-formed (regular, readable) generated
    file as ``stale/update`` so an explicit ``--adopt --write`` claims it. There
    is no implicit adoption: without ``adopt`` a foreign generated file remains a
    conflict.
    """
    root = profile.user_root
    generated = cursor_user_cmd.cursor_generated_files(root)
    gen_state = state.get("generated", {}) if isinstance(state, dict) else {}
    owned_files = gen_state.get("files", {}) if isinstance(gen_state, dict) else {}
    items, conflicts, writes = [], [], []
    for path, (text, executable, surface) in generated.items():
        rel = cursor_user_cmd._relative(root, path)
        desired_digest = digest_text(text)
        if not path.exists():
            status, action = "missing", "create"
        elif not path.is_file():
            status, action = "conflict", "preserve"
        else:
            try:
                live = path.read_text()
                mode_ok = not executable or bool(path.stat().st_mode & 0o111)
            except (OSError, UnicodeError) as exc:
                items.append(
                    {
                        "surface": surface,
                        "path": str(path),
                        "status": "conflict",
                        "action": "preserve",
                        "detail": str(exc),
                    }
                )
                conflicts.append(items[-1])
                continue
            live_digest = digest_text(live)
            if live_digest == desired_digest and mode_ok:
                status, action = "current", "none"
            elif live_digest == desired_digest:
                status, action = "stale", "update"
            elif owned_files.get(rel) == live_digest:
                status, action = "stale", "update"
            elif adopt:
                status, action = "stale", "update"
            else:
                status, action = "conflict", "preserve"
        item = {"surface": surface, "path": str(path), "status": status, "action": action}
        items.append(item)
        if status == "conflict":
            conflicts.append(item)
        if action in {"create", "update"}:
            writes.append((path, text, executable, rel, desired_digest))
    return items, conflicts, writes


def _plan_cursor_hook(profile, state):
    """Plan the co-owned hooks.json sessionStart registration against v2 ownership."""
    root = profile.user_root
    hook_path = root / "hooks.json"
    doc, error = cursor_user_cmd._read_json_object(hook_path)
    desired_hook = cursor_user_cmd._hook_entry(root)
    desired_fp = cursor_user_cmd._digest_value(desired_hook)
    gen_state = state.get("generated", {}) if isinstance(state, dict) else {}
    owned_hooks = gen_state.get("hooks", {}) if isinstance(gen_state, dict) else {}
    item = {"surface": "hook-config", "path": str(hook_path), "name": "sessionStart"}
    if doc is None:
        item.update(status="conflict", action="preserve", detail=error or "could not read hooks configuration")
        return item, [item], None, None
    hooks = doc.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        item.update(status="conflict", action="preserve", detail="existing hooks field must be an object")
        return item, [item], None, None
    entries = hooks.get("sessionStart")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        item.update(status="conflict", action="preserve", detail="existing sessionStart hooks must be a list")
        return item, [item], None, None
    if desired_hook in entries:
        item.update(status="current", action="none")
        return item, [], None, desired_fp
    prior_fp = owned_hooks.get("sessionStart")
    prior_index = None
    if isinstance(prior_fp, str):
        prior_index = next((i for i, e in enumerate(entries) if cursor_user_cmd._digest_value(e) == prior_fp), None)
    if prior_index is None:
        item.update(status="missing", action="create")
    else:
        item.update(status="stale", action="update", prior_index=prior_index)
    return item, [], (doc, desired_hook, item), desired_fp


def _missing_dirs_under(path: Path, root: Path) -> list[Path]:
    """Return ancestor dirs under ``root`` (not including ``root``) missing before a write, leaf-first."""
    missing: list[Path] = []
    cur = path.parent
    root_resolved = root.resolve()
    while cur != root_resolved and root_resolved in cur.parents:
        if cur.exists():
            break
        missing.append(cur)
        cur = cur.parent
    return missing


def _apply_instruction(plan, state):
    if plan is None or plan.action not in {"create", "update"}:
        return None
    # instruction files live directly in the user root; created dirs under root are rare.
    localio.write_text_atomic(plan.path, plan.rendered)
    state["instructions"] = {"digest": plan.desired_digest, "created_directories": []}
    return str(plan.path)


def _apply_cursor_generated(writes, state, root):
    written = []
    gen = state.setdefault("generated", {})
    files = gen.setdefault("files", {})
    if not isinstance(files, dict):
        files = {}
        gen["files"] = files
    created_dirs = gen.setdefault("created_directories", [])
    if not isinstance(created_dirs, list):
        created_dirs = []
        gen["created_directories"] = created_dirs
    for path, text, executable, rel, desired_digest in writes:
        # Record only the brigade-loop plugin leaf dirs as created so uninstall
        # rmdir()s those, never foreign-occupied ancestors like ``plugins/local``
        # or the shared ``hooks/`` dir (which foreign hooks may share).
        localio.write_text_atomic(path, text)
        if executable:
            try:
                path.chmod(path.stat().st_mode | 0o755)
            except OSError:
                pass
        files[rel] = desired_digest
        parts = Path(rel).parts
        if parts[:3] == ("plugins", "local", "brigade-loop"):
            # record the leaf parent dir and the brigade-loop package root
            for depth in (len(parts) - 1, 3):
                cand = "/".join(parts[:depth])
                if cand and cand not in created_dirs:
                    created_dirs.append(cand)
        written.append(str(path))
    return written


def _apply_cursor_hook(hook_plan, state, root):
    if hook_plan is None:
        return None
    doc, desired_hook, item = hook_plan
    if item["action"] not in {"create", "update"}:
        return None
    hooks = doc.setdefault("hooks", {})
    entries = hooks.setdefault("sessionStart", [])
    if item["action"] == "create":
        entries.append(desired_hook)
    else:
        entries[item["prior_index"]] = desired_hook
    hook_path = root / "hooks.json"
    localio.write_text_atomic(hook_path, cursor_user_cmd._coowned_json_text(doc))
    gen = state.setdefault("generated", {})
    hooks_owned = gen.setdefault("hooks", {})
    if not isinstance(hooks_owned, dict):
        hooks_owned = {}
        gen["hooks"] = hooks_owned
    hooks_owned["sessionStart"] = cursor_user_cmd._digest_value(desired_hook)
    return str(hook_path)


def _retire_cursor_legacy(root, retire_file_ownership, retire_mcp_ownership):
    """Revalidate then retire legacy Cursor surfaces as one atomic batch.

    For every ``(path, stored_digest)``: reject symlink / non-regular / missing
    / changed digest before unlink. For every ``(name, stored_digest)``: reread
    ``root/mcp.json`` and digest-match the current live value before pop. If any
    candidate mismatches, perform zero retirement mutations for the whole batch
    and return its conflicts. No partial retirement after revalidation failure.

    Returns ``(removed_paths, conflict_items)``.
    """
    conflicts: list[dict] = []

    for path, stored in retire_file_ownership:
        if path.is_symlink() or not path.is_file():
            conflicts.append(
                {
                    "surface": "retire-file",
                    "path": str(path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": f"retirement target is missing or not a regular file: {path}",
                }
            )
            continue
        try:
            live = path.read_text()
        except (OSError, UnicodeError) as exc:
            conflicts.append(
                {
                    "surface": "retire-file",
                    "path": str(path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": f"retirement target is unreadable: {path}: {exc}",
                }
            )
            continue
        if digest_text(live) != stored:
            conflicts.append(
                {
                    "surface": "retire-file",
                    "path": str(path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": f"retirement target was edited: {path}",
                }
            )

    mcp_path = root / "mcp.json"
    live_doc, read_error = cursor_user_cmd._read_json_object(mcp_path)
    live_servers = live_doc.get("mcpServers") if isinstance(live_doc, dict) else None
    if retire_mcp_ownership:
        if live_doc is None:
            conflicts.append(
                {
                    "surface": "retire-mcp",
                    "path": str(mcp_path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": read_error or "cursor mcp config is unreadable",
                }
            )
        elif not isinstance(live_servers, dict):
            conflicts.append(
                {
                    "surface": "retire-mcp",
                    "path": str(mcp_path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": "cursor mcp config mcpServers must be an object",
                }
            )
        else:
            for name, stored in retire_mcp_ownership:
                if name not in live_servers:
                    conflicts.append(
                        {
                            "surface": "retire-mcp",
                            "path": str(mcp_path),
                            "name": name,
                            "status": "conflict",
                            "action": "preserve",
                            "detail": f"retirement mcp entry is missing: {name}",
                        }
                    )
                    continue
                if cursor_user_cmd._digest_value(live_servers[name]) != stored:
                    conflicts.append(
                        {
                            "surface": "retire-mcp",
                            "path": str(mcp_path),
                            "name": name,
                            "status": "conflict",
                            "action": "preserve",
                            "detail": f"retirement mcp entry was edited: {name}",
                        }
                    )

    if conflicts:
        return [], conflicts

    removed: list[str] = []
    root_resolved = root.resolve()
    for path, _stored in retire_file_ownership:
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            continue
        cur = path.parent
        while cur != root_resolved and root_resolved in cur.parents:
            try:
                cur.rmdir()
            except OSError:
                break
            cur = cur.parent

    if retire_mcp_ownership and isinstance(live_doc, dict) and isinstance(live_servers, dict):
        for name, _stored in retire_mcp_ownership:
            live_servers.pop(name, None)
        localio.write_text_atomic(mcp_path, cursor_user_cmd._coowned_json_text(live_doc))
    return removed, []


def _replan_skills_after_retire(profile, state, workspace):
    """Recompute registry packages and plan skills against the post-retirement fs.

    Returns ``(plans, conflicts)``. A registry failure or unsafe path is a
    per-skill conflict (plans empty); the caller preserves accurate per-file
    state and reports a conflict rather than lying ready.
    """
    conflicts: list[dict] = []
    try:
        packages = skills_cmd.user_profile_skill_packages(
            workspace=workspace, harness=profile.harness, minimum_trust="workspace"
        )
    except Exception as exc:
        return (), [
            {
                "surface": "skills",
                "path": str(profile.skills_root),
                "status": "conflict",
                "action": "preserve",
                "detail": str(exc),
            }
        ]
    try:
        plans = plan_skills(skills_root=profile.skills_root, packages=packages, state=state)
    except ValueError as exc:
        return (), [
            {
                "surface": "skills",
                "path": str(profile.skills_root),
                "status": "conflict",
                "action": "preserve",
                "detail": str(exc),
            }
        ]
    for p in plans:
        if p.status == "conflict":
            conflicts.append(
                {
                    "surface": "skill",
                    "skill_id": p.skill_id,
                    "path": str(p.path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": p.detail,
                }
            )
    return plans, conflicts


# --- Issue #438 Task 4: Pi generated MCP extension + redacted bridge descriptor ---


def render_pi_extension() -> str:
    """Return the packaged Pi MCP extension source.

    The template lives at ``templates/pi/brigade-mcp/index.ts`` (already packaged
    via ``pyproject.toml``'s ``templates/**/*`` glob) and uses Node built-ins
    only. It carries no baked workspace placeholder: it reads its bridge
    descriptor from its own ``~/.pi/agent/brigade/mcp-profile.json`` path at
    runtime.
    """
    path = templates.template_root() / "pi" / "brigade-mcp" / "index.ts"
    return path.read_text(encoding="utf-8")


def _resolve_brigade_entrypoint() -> str | None:
    """Resolve the ``brigade`` CLI to an absolute path for the bridge descriptor.

    Mirrors ``cursor_user_cmd._mcp_servers()``'s ``component_bins.resolve``
    comment: the generated descriptor must not depend on Pi's spawn-time PATH, so
    the bridge command's first element is resolved to an absolute path at
    generation time. ``shutil.which`` is preferred; the venv sibling of the
    running interpreter is a fallback for unactivated-venv test runs.
    """
    found = shutil.which("brigade")
    candidates = [Path(found).expanduser() if found else None]
    candidates.append(Path(sys.executable).expanduser().resolve().parent / "brigade")
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate.resolve())
    return None


def _pi_mcp_artifact_paths(profile) -> tuple[Path, Path]:
    """Return ``(extension_path, descriptor_path)`` for the Pi MCP profile."""
    root = profile.user_root
    return (
        root / "extensions" / "brigade-mcp" / "index.ts",
        root / "brigade" / "mcp-profile.json",
    )


def _pi_bridge_descriptor(profile, workspace) -> dict[str, Any]:
    """Build the redacted Pi bridge descriptor for ``workspace``.

    Carries the resolved workspace, the bridge command with the Brigade
    entrypoint resolved to an absolute path and the canonical catalog digest
    repeated as the required ``--catalog-digest`` argument, and the enabled
    server names and descriptions only: no env values, headers, downstream argv,
    or secrets.
    """
    del profile  # paths are derived from the workspace, not the profile record
    workspace = workspace.expanduser().resolve()
    digest = mcp_cmd.catalog_digest(workspace)
    entrypoint = _resolve_brigade_entrypoint()
    if entrypoint is None:
        raise ValueError("cannot resolve an absolute Brigade CLI path for the Pi bridge")
    workspace_str = str(workspace)
    bridge_command = [
        entrypoint,
        "mcp",
        "bridge",
        "--stdio",
        "--target",
        workspace_str,
        "--catalog-digest",
        digest,
    ]
    servers = mcp_cmd._servers_for_bridge(workspace)
    server_list = [{"name": name, "description": server.description} for name, server in sorted(servers.items())]
    return {
        "workspace": workspace_str,
        "bridge_command": bridge_command,
        "catalog_digest": digest,
        "servers": server_list,
    }


def _plan_pi_mcp(profile, state, workspace, allow_global_stdio):
    """Plan the two owned Pi MCP artifacts against v2 ``generated`` ownership.

    Returns ``(items, conflicts, writes)`` where ``writes`` is a list of
    ``(path, text, rel, desired_digest)`` for ``create``/``update`` actions. The
    extension is generated from the packaged template; the descriptor is the
    redacted bridge descriptor. A foreign (externally edited) artifact is a
    conflict and is never overwritten. A stdio-bearing catalog requires
    ``--allow-global-stdio`` before the bridge descriptor is written, matching
    the user-scope stdio gate the native adapters use.
    """
    items: list[dict] = []
    conflicts: list[dict] = []
    writes: list[tuple[Path, str, str, str]] = []
    extension_path, descriptor_path = _pi_mcp_artifact_paths(profile)
    servers = mcp_cmd._servers_for_bridge(workspace)
    has_stdio = any(not server.is_remote for server in servers.values())
    if has_stdio and not allow_global_stdio:
        detail = "stdio MCP servers require --allow-global-stdio for the Pi bridge"
        item = {
            "surface": "mcp-bridge",
            "path": str(descriptor_path),
            "status": "conflict",
            "action": "preserve",
            "detail": detail,
        }
        items.append(item)
        conflicts.append(item)
        return items, conflicts, writes

    gen_state = state.get("generated", {}) if isinstance(state, dict) else {}
    owned_files = gen_state.get("files", {}) if isinstance(gen_state, dict) else {}
    try:
        descriptor = _pi_bridge_descriptor(profile, workspace)
    except ValueError as exc:
        item = {
            "surface": "mcp-bridge",
            "path": str(descriptor_path),
            "status": "conflict",
            "action": "preserve",
            "detail": str(exc),
        }
        items.append(item)
        conflicts.append(item)
        return items, conflicts, writes
    extension_text = render_pi_extension()
    descriptor_text = json.dumps(descriptor, indent=2, sort_keys=True) + "\n"
    artifacts = [
        (extension_path, extension_text, "extensions/brigade-mcp/index.ts"),
        (descriptor_path, descriptor_text, "brigade/mcp-profile.json"),
    ]
    for path, text, rel in artifacts:
        desired_digest = digest_text(text)
        if not path.exists():
            status, action = "missing", "create"
        elif not path.is_file():
            item = {
                "surface": "mcp",
                "path": str(path),
                "status": "conflict",
                "action": "preserve",
                "detail": f"Pi MCP artifact is not a regular file: {path}",
            }
            items.append(item)
            conflicts.append(item)
            continue
        else:
            try:
                live = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                item = {
                    "surface": "mcp",
                    "path": str(path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": str(exc),
                }
                items.append(item)
                conflicts.append(item)
                continue
            live_digest = digest_text(live)
            if live_digest == desired_digest:
                status, action = "current", "none"
            elif owned_files.get(rel) == live_digest:
                status, action = "stale", "update"
            else:
                item = {
                    "surface": "mcp",
                    "path": str(path),
                    "status": "conflict",
                    "action": "preserve",
                    "detail": "foreign Pi MCP artifact; recover with: "
                    "brigade harness install pi --scope user --adopt --write",
                }
                items.append(item)
                conflicts.append(item)
                continue
        items.append({"surface": "mcp", "path": str(path), "status": status, "action": action})
        if action in {"create", "update"}:
            writes.append((path, text, rel, desired_digest))
    return items, conflicts, writes


def _apply_pi_mcp(writes, state, profile):
    """Write the owned Pi MCP artifacts and record ownership in ``generated``.

    Returns the sorted absolute paths written. Files land at ``0600`` via
    ``localio.write_text_atomic``. Digests are recorded under
    ``state["generated"]["files"][rel]`` and created leaf directories under
    ``state["generated"]["created_directories"]``.
    """
    written: list[str] = []
    gen = state.setdefault("generated", {})
    if not isinstance(gen, dict):
        gen = {}
        state["generated"] = gen
    files = gen.setdefault("files", {})
    if not isinstance(files, dict):
        files = {}
        gen["files"] = files
    created_dirs = gen.setdefault("created_directories", [])
    if not isinstance(created_dirs, list):
        created_dirs = []
        gen["created_directories"] = created_dirs
    root_resolved = profile.user_root.resolve()
    for path, text, rel, desired_digest in writes:
        missing = _missing_dirs_under(path, profile.user_root)
        localio.write_text_atomic(path, text)
        files[rel] = desired_digest
        for directory in missing:
            rel_dir = directory.resolve().relative_to(root_resolved).as_posix()
            if rel_dir not in created_dirs:
                created_dirs.append(rel_dir)
        written.append(str(path))
    return sorted(written)


# --- Issue #438 Task 5: native MCP projection + Pi/native MCP uninstall ---


def _native_mcp_kimi_root(profile) -> Path | None:
    """Return the Kimi root value for the kimi-user adapter, else None."""
    if profile.mcp_harness == "kimi-user":
        return profile.user_root
    return None


def _malformed_native_config(adapter: mcp_adapters.McpAdapter, text: str | None) -> str | None:
    """Return a detail string when an existing native config is malformed.

    A missing file (``text is None``) is not malformed. JSON adapters require a
    JSON object whose ``top_key`` path is either absent or navigable to an
    object section. TOML adapters require parseable TOML with ``mcp_servers``
    absent or a table. The check never mutates the file; the caller blocks the
    whole MCP stage on a non-None return so a malformed native config is
    preserved byte-for-byte.
    """
    if text is None:
        return None
    if adapter.fmt == "json":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return "existing native MCP config is not valid JSON; refusing to overwrite"
        if not isinstance(doc, dict):
            return "existing native MCP config is not a JSON object; refusing to overwrite"
        node: Any = doc
        for part in adapter.top_key.split("."):
            if part not in node:
                return None  # section absent -> safe to create
            node = node[part]
            if not isinstance(node, dict):
                return f"existing native MCP config section {adapter.top_key!r} is not an object; refusing to overwrite"
        return None
    if adapter.fmt == "toml":
        try:
            doc = _toml_loads(text)
        except _TOMLDecodeError:
            return "existing native MCP config is not valid TOML; refusing to overwrite"
        if not isinstance(doc, dict):
            return "existing native MCP config is not a TOML table; refusing to overwrite"
        servers = doc.get("mcp_servers")
        if servers is not None and not isinstance(servers, dict):
            return "existing native MCP config mcp_servers section is not a table; refusing to overwrite"
        return None
    return None


def _plan_native_mcp(profile, state, workspace, allow_global_stdio, adopt):
    """Plan the native MCP projection for a profile with a non-None ``mcp_harness``.

    Returns ``(items, conflicts, write_plan)``. ``write_plan`` is ``None`` when
    nothing is to be written; otherwise a dict carrying the resolved path,
    adapter, the per-server owned provider dicts to merge, the remove set
    (always empty on install), and the ownership records to persist.

    A missing canonical catalog is a no-op (no items, no conflicts): the
    profile's instruction/skill stages still run, preserving the pre-MCP
    behavior. A stdio-bearing projection requires ``--allow-global-stdio``,
    matching the user-scope stdio gate the native adapters use. A malformed
    existing config blocks the stage without mutation. Foreign entries are
    conflicts unless ``adopt`` is set.
    """
    harness = profile.mcp_harness
    adapter = mcp_adapters.ADAPTERS[harness]
    kimi_root = _native_mcp_kimi_root(profile)
    path = mcp_adapters.resolve_path(adapter, workspace, kimi_root=kimi_root)
    servers, errors, _warnings = mcp_cmd.load_canonical(workspace)
    if errors:
        return [], [], None
    desired = {
        name: server
        for name, server in servers.items()
        if server.enabled and mcp_cmd._server_targets_harness(server, harness)
    }
    has_stdio = any(not server.is_remote for server in desired.values())
    if has_stdio and not allow_global_stdio:
        detail = "stdio MCP servers require --allow-global-stdio for user-scope projection"
        item = {
            "surface": "mcp",
            "path": str(path),
            "status": "conflict",
            "action": "preserve",
            "detail": detail,
        }
        return [item], [item], None

    if path.exists() and not path.is_file():
        item = {
            "surface": "mcp",
            "path": str(path),
            "status": "conflict",
            "action": "preserve",
            "detail": "existing native MCP config is not a regular file; refusing to overwrite",
        }
        return [item], [item], None
    try:
        existing_text = path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, UnicodeDecodeError) as exc:
        item = {
            "surface": "mcp",
            "path": str(path),
            "status": "conflict",
            "action": "preserve",
            "detail": f"existing native MCP config is unreadable: {exc}",
        }
        return [item], [item], None
    malformed = _malformed_native_config(adapter, existing_text)
    if malformed is not None:
        item = {"surface": "mcp", "path": str(path), "status": "conflict", "action": "preserve", "detail": malformed}
        return [item], [item], None

    live = adapter.read_file(existing_text)
    mcp_state = state.get("mcp") if isinstance(state.get("mcp"), dict) else {}
    items: list[dict] = []
    conflicts: list[dict] = []
    to_write: dict[str, dict[str, Any]] = {}
    ownership: dict[str, dict[str, str]] = {}
    for name, server in sorted(desired.items()):
        provider = mcp_cmd._project_server(workspace, harness, server)
        proj_fp = localio.stable_hash(provider)
        if name not in live:
            items.append({"surface": "mcp", "path": str(path), "name": name, "status": "missing", "action": "create"})
            to_write[name] = provider
            ownership[name] = {"projected_fingerprint": proj_fp}
            continue
        live_fp = localio.stable_hash(live[name])
        owned = mcp_state.get(name) if isinstance(mcp_state.get(name), dict) else {}
        owned_fp = owned.get("projected_fingerprint")
        if live_fp == proj_fp:
            items.append({"surface": "mcp", "path": str(path), "name": name, "status": "current", "action": "none"})
            ownership[name] = {"projected_fingerprint": proj_fp}
        elif owned_fp is not None and live_fp == owned_fp:
            items.append({"surface": "mcp", "path": str(path), "name": name, "status": "stale", "action": "update"})
            to_write[name] = provider
            ownership[name] = {"projected_fingerprint": proj_fp}
        elif adopt:
            items.append({"surface": "mcp", "path": str(path), "name": name, "status": "adopted", "action": "update"})
            to_write[name] = provider
            ownership[name] = {"projected_fingerprint": proj_fp}
        else:
            item = {
                "surface": "mcp",
                "path": str(path),
                "name": name,
                "status": "conflict",
                "action": "preserve",
                "detail": "a native MCP entry with this name already exists; "
                "remove it or rerun with --adopt to take ownership",
            }
            items.append(item)
            conflicts.append(item)
    write_plan = None
    if ownership:
        write_plan = {
            "path": path,
            "adapter": adapter,
            "owned": to_write,
            "remove": set(),
            "ownership": ownership,
            "existing_text": existing_text,
        }
    return items, conflicts, write_plan


def _apply_native_mcp(write_plan, state):
    """Merge the owned native MCP entries into the native config and record ownership.

    Returns the sorted absolute paths written. The merge preserves every
    foreign server and unrelated top-level key; atomic write lands the file at
    ``0600``. Ownership is recorded under ``state["mcp"][<name>]``.
    """
    if write_plan is None:
        return []
    path = write_plan["path"]
    adapter = write_plan["adapter"]
    if write_plan["owned"]:
        new_text = adapter.write_file(write_plan["existing_text"], write_plan["owned"], write_plan["remove"])
        localio.write_text_atomic(path, new_text)
    mcp_state = state.setdefault("mcp", {})
    if not isinstance(mcp_state, dict):
        mcp_state = {}
        state["mcp"] = mcp_state
    for name, record in write_plan["ownership"].items():
        mcp_state[name] = record
    return [str(path)] if write_plan["owned"] else []


def _plan_native_mcp_removal(profile, state, workspace):
    """Plan removal of owned native MCP entries for a profile.

    Returns ``(items, conflicts, remove_plan)``. ``remove_plan`` is ``None`` or
    a dict carrying the resolved path, adapter, the server names whose live
    projected value still matches the recorded ownership, and the existing
    text. Only matching entries are removed; an edited owned entry is a
    conflict with ownership retained; a missing config is already-removed; a
    malformed config blocks without mutation and retains ownership.
    """
    harness = profile.mcp_harness
    adapter = mcp_adapters.ADAPTERS[harness]
    kimi_root = _native_mcp_kimi_root(profile)
    path = mcp_adapters.resolve_path(adapter, workspace, kimi_root=kimi_root)
    mcp_state = state.get("mcp") if isinstance(state.get("mcp"), dict) else {}
    owned_names = {name for name, record in mcp_state.items() if isinstance(record, dict)}

    if not path.exists():
        # missing config: already removed. Surface an absent item per owned
        # entry so the caller can clear stale ownership; with no ownership this
        # is a clean no-op (nothing to verify, nothing to remove).
        if not owned_names:
            return [], [], None
        items = [
            {"surface": "mcp", "path": str(path), "name": name, "status": "absent", "action": "none"}
            for name in sorted(owned_names)
        ]
        return items, [], {"path": path, "adapter": adapter, "remove": set(), "absent": set(), "clear_all": True}

    if not path.is_file():
        item = {
            "surface": "mcp",
            "path": str(path),
            "status": "conflict",
            "action": "preserve",
            "detail": "existing native MCP config is not a regular file; refusing to remove entries",
        }
        return [item], [item], None

    try:
        existing_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        detail = f"existing native MCP config is unreadable: {exc}"
        items = [
            {
                "surface": "mcp",
                "path": str(path),
                "name": name,
                "status": "conflict",
                "action": "preserve",
                "detail": detail,
            }
            for name in sorted(owned_names)
        ]
        if not items:
            items.append(
                {"surface": "mcp", "path": str(path), "status": "conflict", "action": "preserve", "detail": detail}
            )
        return items, items, None
    malformed = _malformed_native_config(adapter, existing_text)
    if malformed is not None:
        # A malformed native config cannot be parsed to distinguish owned from
        # foreign entries, so Brigade cannot confirm a safe removal: block
        # without mutating and retain any recorded ownership for a later retry.
        items: list[dict] = []
        conflicts: list[dict] = []
        if owned_names:
            for name in sorted(owned_names):
                item = {
                    "surface": "mcp",
                    "path": str(path),
                    "name": name,
                    "status": "conflict",
                    "action": "preserve",
                    "detail": malformed,
                }
                items.append(item)
                conflicts.append(item)
        else:
            item = {
                "surface": "mcp",
                "path": str(path),
                "status": "conflict",
                "action": "preserve",
                "detail": malformed,
            }
            items.append(item)
            conflicts.append(item)
        return items, conflicts, None

    if not owned_names:
        # parseable config with no Brigade ownership: nothing to remove.
        return [], [], None

    live = adapter.read_file(existing_text)
    items = []
    conflicts = []
    to_remove: set[str] = set()
    absent_names: set[str] = set()
    for name in sorted(owned_names):
        record = mcp_state.get(name) if isinstance(mcp_state.get(name), dict) else {}
        owned_fp = record.get("projected_fingerprint")
        if name not in live:
            # owned entry already gone from the file: clear stale ownership
            items.append({"surface": "mcp", "path": str(path), "name": name, "status": "absent", "action": "none"})
            absent_names.add(name)
            continue
        live_fp = localio.stable_hash(live[name])
        if owned_fp is not None and live_fp == owned_fp:
            items.append({"surface": "mcp", "path": str(path), "name": name, "status": "managed", "action": "remove"})
            to_remove.add(name)
        else:
            item = {
                "surface": "mcp",
                "path": str(path),
                "name": name,
                "status": "conflict",
                "action": "preserve",
                "detail": "owned native MCP entry was edited; recover with: "
                "brigade harness install <harness> --scope user --adopt --write",
            }
            items.append(item)
            conflicts.append(item)
    remove_plan = None
    if to_remove or absent_names:
        remove_plan = {
            "path": path,
            "adapter": adapter,
            "remove": to_remove,
            "absent": absent_names,
            "existing_text": existing_text,
        }
    return items, conflicts, remove_plan


def _apply_native_mcp_removal(remove_plan, state):
    """Remove digest-matching owned native MCP entries and clear ownership.

    Returns the sorted absolute paths written (the config file is rewritten
    when any entry is removed). Foreign servers and unrelated top-level keys
    are preserved. Ownership for removed and already-absent entries is
    cleared; conflicted entries keep their ownership for a future retry.
    """
    if remove_plan is None:
        return []
    mcp_state = state.get("mcp") if isinstance(state.get("mcp"), dict) else {}
    if remove_plan.get("clear_all"):
        for name in list(mcp_state):
            mcp_state.pop(name, None)
        return []
    to_remove = remove_plan["remove"]
    absent_names = remove_plan["absent"]
    if to_remove:
        path = remove_plan["path"]
        adapter = remove_plan["adapter"]
        new_text = adapter.write_file(remove_plan["existing_text"], {}, to_remove)
        localio.write_text_atomic(path, new_text)
    for name in to_remove | absent_names:
        mcp_state.pop(name, None)
    return [str(remove_plan["path"])] if to_remove else []


def _plan_generated_removals(profile, state):
    """Plan removal of owned generated files recorded under ``state["generated"]``.

    Shared by the Cursor plugin/rule/hook surfaces and the Pi MCP extension +
    bridge descriptor. Returns ``(items, conflicts, writes)`` where ``writes``
    is the list of ``(path, rel)`` tuples whose live bytes still match the
    recorded ownership and may be unlinked.
    """
    root = profile.user_root
    gen_state = state.get("generated", {}) if isinstance(state, dict) else {}
    owned_files = gen_state.get("files", {}) if isinstance(gen_state, dict) else {}
    if not isinstance(owned_files, dict):
        return [], [], []
    items: list[dict] = []
    conflicts: list[dict] = []
    writes: list[tuple[Path, str]] = []
    for rel, owned_digest in sorted(owned_files.items()):
        if not isinstance(owned_digest, str):
            continue
        path = root / rel
        item = {"surface": "generated", "path": str(path), "_rel": rel}
        if not path.exists():
            item.update(status="absent", action="none")
        elif not path.is_file():
            item.update(status="conflict", action="preserve", detail="owned generated file is not a regular file")
            conflicts.append(item)
        else:
            try:
                live = digest_text(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                item.update(status="conflict", action="preserve", detail=str(exc))
                conflicts.append(item)
            else:
                if live == owned_digest:
                    item.update(status="managed", action="remove")
                    writes.append((path, rel))
                else:
                    item.update(
                        status="conflict",
                        action="preserve",
                        detail="owned generated file was edited; recover with: "
                        "brigade harness install <harness> --scope user --adopt --write",
                    )
                    conflicts.append(item)
        items.append(item)
    return items, conflicts, writes


def _install_profile(profile, workspace, write, adopt, allow_global_stdio=False):
    state, error, migration, retire_mcp, retire_paths, retire_file_own, retire_mcp_own = _load_state_for_profile(
        profile, workspace
    )
    items: list[dict] = []
    conflicts: list[dict] = []
    files_written: list[str] = []

    if error is not None:
        conflicts.append(
            {
                "surface": "ownership-state",
                "path": str(profile.state_path),
                "status": "conflict",
                "action": "preserve",
                "detail": error,
            }
        )
        return (
            _result(
                profile.harness,
                status="conflict",
                ready=False,
                instruction_ready=False,
                skills_ready=False,
                reload_hint=profile.reload_hint,
                items=conflicts,
                conflicts=conflicts,
                files_written=[],
                files_removed=[],
                migration=migration,
                capabilities=profile.capabilities,
            ),
            False,
        )

    instr_plan, instr_conflict = _plan_instruction_for_profile(
        profile, state, adopt, workspace, guard_tracked_write=True
    )
    if instr_conflict is not None:
        conflicts.append(instr_conflict)
    if instr_plan is not None:
        items.append(
            {
                "surface": "instruction",
                "path": str(profile.instruction_path),
                "status": instr_plan.status,
                "action": instr_plan.action,
            }
        )
    instruction_ready = instr_conflict is None

    is_migration = migration is not None
    # Migration defers the skill stage until after legacy retirement so the
    # registry becomes the sole owner of the skill surface; pre-retirement
    # skill plans would see the legacy bundled copy and are not final.
    if is_migration:
        skill_plans: tuple[SkillFilePlan, ...] = ()
        skill_conflicts: list[dict] = []
    else:
        skill_plans, skill_conflicts, _ = _plan_skills_for_profile(profile, state, workspace)
    conflicts.extend(skill_conflicts)
    for p in skill_plans:
        items.append(
            {"surface": "skill", "skill_id": p.skill_id, "path": str(p.path), "status": p.status, "action": p.action}
        )
    skills_ready = not skill_conflicts

    gen_items, gen_conflicts, gen_writes = [], [], []
    hook_item, hook_conflict, hook_plan, hook_fp = None, [], None, None
    if profile.harness == "cursor":
        gen_items, gen_conflicts, gen_writes = _plan_cursor_generated(profile, state, adopt=adopt)
        items.extend(gen_items)
        conflicts.extend(gen_conflicts)
        hook_item, hook_conflict, hook_plan, hook_fp = _plan_cursor_hook(profile, state)
        items.append({k: v for k, v in hook_item.items() if k != "prior_index"})
        conflicts.extend(hook_conflict)
    generated_ready = not gen_conflicts and not hook_conflict

    pi_mcp_items: list[dict] = []
    pi_mcp_conflicts: list[dict] = []
    pi_mcp_writes: list[tuple[Path, str, str, str]] = []
    if profile.harness == "pi":
        pi_mcp_items, pi_mcp_conflicts, pi_mcp_writes = _plan_pi_mcp(profile, state, workspace, allow_global_stdio)
        items.extend(pi_mcp_items)
        conflicts.extend(pi_mcp_conflicts)

    # Native MCP projection for non-Pi, non-Cursor profiles. Cursor keeps its
    # own v1 MCP retirement path; Pi uses the aggregate bridge above.
    native_mcp_items: list[dict] = []
    native_mcp_conflicts: list[dict] = []
    native_mcp_write: dict[str, Any] | None = None
    if profile.mcp_harness is not None and profile.harness not in ("pi", "cursor"):
        native_mcp_items, native_mcp_conflicts, native_mcp_write = _plan_native_mcp(
            profile, state, workspace, allow_global_stdio, adopt
        )
        items.extend(native_mcp_items)
        conflicts.extend(native_mcp_conflicts)
    mcp_ready = not pi_mcp_conflicts and not native_mcp_conflicts

    ready = instruction_ready and skills_ready and generated_ready and mcp_ready
    files_removed: list[str] = []

    if write and ready:
        if is_migration and profile.harness == "cursor":
            # Revalidate + retire legacy surfaces BEFORE any new-surface write so
            # a TOCTOU retirement conflict leaves no partial schema-v2 state and
            # never persists v2 as if successful. Zero mutations on any mismatch.
            removed, retire_conflicts = _retire_cursor_legacy(profile.user_root, retire_file_own, retire_mcp_own)
            if retire_conflicts:
                conflicts.extend(retire_conflicts)
                return _result(
                    profile.harness,
                    status="conflict",
                    ready=False,
                    instruction_ready=instruction_ready,
                    skills_ready=False,
                    reload_hint=profile.reload_hint,
                    items=items,
                    conflicts=conflicts,
                    files_written=[],
                    files_removed=[],
                    migration=migration,
                    capabilities=profile.capabilities,
                ), False
            files_removed.extend(removed)
        wf = _apply_instruction(instr_plan, state)
        if wf:
            files_written.append(wf)
        if profile.harness == "cursor":
            # 2. apply generated/hook repairs
            gw = _apply_cursor_generated(gen_writes, state, profile.user_root)
            files_written.extend(gw)
            hw = _apply_cursor_hook(hook_plan, state, profile.user_root)
            if hw:
                files_written.append(hw)
            if is_migration:
                # 3. legacy retirement already completed above; recompute packages
                #    + plan_skills against the post-retirement filesystem, then
                #    apply and own registry skill files
                skill_plans, skill_conflicts = _replan_skills_after_retire(profile, state, workspace)
                conflicts.extend(skill_conflicts)
                skills_ready = not skill_conflicts
                if not skills_ready:
                    # persist accurate per-file state and return conflict; do not lie ready
                    write_profile_state(state_path=profile.state_path, state=state)
                    return _result(
                        profile.harness,
                        status="conflict",
                        ready=False,
                        instruction_ready=instruction_ready,
                        skills_ready=False,
                        reload_hint=profile.reload_hint,
                        items=items,
                        conflicts=conflicts,
                        files_written=files_written,
                        files_removed=files_removed,
                        migration=migration,
                        capabilities=profile.capabilities,
                    ), bool(files_written or files_removed)
                for p in skill_plans:
                    items.append(
                        {
                            "surface": "skill",
                            "skill_id": p.skill_id,
                            "path": str(p.path),
                            "status": p.status,
                            "action": p.action,
                        }
                    )
                state, sw = apply_skill_plan(
                    skills_root=profile.skills_root,
                    packages=tuple(
                        skills_cmd.user_profile_skill_packages(
                            workspace=workspace, harness=profile.harness, minimum_trust="workspace"
                        )
                    ),
                    plans=skill_plans,
                    prior_state=state,
                    state_path=profile.state_path,
                )
                files_written.extend(sw)
            elif skill_plans:
                # non-migration cursor path retains normal skill behavior
                state, sw = apply_skill_plan(
                    skills_root=profile.skills_root,
                    packages=tuple(
                        skills_cmd.user_profile_skill_packages(
                            workspace=workspace, harness=profile.harness, minimum_trust="workspace"
                        )
                    ),
                    plans=skill_plans,
                    prior_state=state,
                    state_path=profile.state_path,
                )
                files_written.extend(sw)
        elif skill_plans:
            state, sw = apply_skill_plan(
                skills_root=profile.skills_root,
                packages=tuple(
                    skills_cmd.user_profile_skill_packages(
                        workspace=workspace, harness=profile.harness, minimum_trust="workspace"
                    )
                ),
                plans=skill_plans,
                prior_state=state,
                state_path=profile.state_path,
            )
            files_written.extend(sw)
        if profile.harness == "pi" and pi_mcp_writes:
            pw = _apply_pi_mcp(pi_mcp_writes, state, profile)
            files_written.extend(pw)
        if native_mcp_write is not None:
            nw = _apply_native_mcp(native_mcp_write, state)
            files_written.extend(nw)
        # 5. persist v2
        write_profile_state(state_path=profile.state_path, state=state)
        status = "updated" if (files_written or files_removed) else "current"
        reload_required = bool(files_written or files_removed)
    elif write and not ready:
        status = "conflict"
        reload_required = False
    else:
        status = "current" if ready else "conflict"
        reload_required = not write  # dry-run always signals reload hint per legacy cursor contract

    mcp_payload = None
    if profile.harness == "pi":
        mcp_payload = {
            "status": "ready" if mcp_ready else "conflict",
            "items": _strip_private(pi_mcp_items),
        }
    elif native_mcp_items:
        mcp_payload = {
            "status": "ready" if mcp_ready else "conflict",
            "items": _strip_private(native_mcp_items),
        }
    return _result(
        profile.harness,
        status=status,
        ready=ready,
        instruction_ready=instruction_ready,
        skills_ready=skills_ready,
        reload_hint=profile.reload_hint,
        items=items,
        conflicts=conflicts,
        files_written=files_written,
        files_removed=files_removed,
        migration=migration,
        capabilities=profile.capabilities,
        mcp=mcp_payload,
    ), reload_required


def _plan_cursor_generated_removals(profile, state):
    root = profile.user_root
    gen_state = state.get("generated", {}) if isinstance(state, dict) else {}
    owned_files = gen_state.get("files", {}) if isinstance(gen_state, dict) else {}
    if not isinstance(owned_files, dict):
        return [], []
    items, conflicts = [], []
    for rel, owned_digest in sorted(owned_files.items()):
        path = root / rel
        item = {"surface": "generated", "path": str(path)}
        if not path.exists():
            item.update(status="absent", action="none")
        elif not path.is_file():
            item.update(status="conflict", action="preserve")
            conflicts.append(item)
        else:
            try:
                live = digest_text(path.read_text())
            except (OSError, UnicodeError) as exc:
                item.update(status="conflict", action="preserve", detail=str(exc))
                conflicts.append(item)
            else:
                if live == owned_digest:
                    item.update(status="managed", action="remove")
                else:
                    item.update(status="conflict", action="preserve", detail="owned generated file was edited")
                    conflicts.append(item)
        items.append(item)
    return items, conflicts


def _plan_cursor_hook_removal(profile, state):
    root = profile.user_root
    gen_state = state.get("generated", {}) if isinstance(state, dict) else {}
    owned_hooks = gen_state.get("hooks", {}) if isinstance(gen_state, dict) else {}
    fp = owned_hooks.get("sessionStart") if isinstance(owned_hooks, dict) else None
    hook_path = root / "hooks.json"
    item = {"surface": "hook-config", "path": str(hook_path), "name": "sessionStart"}
    if not isinstance(fp, str):
        item.update(status="absent", action="none")
        return item, [], None
    doc, error = cursor_user_cmd._read_json_object(hook_path)
    entries = None
    if doc is not None and isinstance(doc.get("hooks"), dict):
        entries = doc["hooks"].get("sessionStart", [])
    index = (
        next((i for i, e in enumerate(entries) if cursor_user_cmd._digest_value(e) == fp), None)
        if isinstance(entries, list)
        else None
    )
    if index is not None:
        item.update(status="managed", action="remove")
        return item, [], (doc, index)
    if not hook_path.exists():
        item.update(status="absent", action="none")
        return item, [], None
    item.update(status="conflict", action="preserve", detail=error or "managed hook was edited or removed")
    return item, [item], None


def _uninstall_profile(profile, workspace, write):
    state, error, migration, _retire_mcp, _retire_paths, _retire_file_own, _retire_mcp_own = _load_state_for_profile(
        profile, workspace
    )
    items: list[dict] = []
    conflicts: list[dict] = []
    files_removed: list[str] = []

    if error is not None:
        conflicts.append(
            {
                "surface": "ownership-state",
                "path": str(profile.state_path),
                "status": "conflict",
                "action": "preserve",
                "detail": error,
            }
        )
        return (
            _result(
                profile.harness,
                status="conflict",
                ready=False,
                instruction_ready=False,
                skills_ready=False,
                reload_hint=profile.reload_hint,
                items=conflicts,
                conflicts=conflicts,
                files_written=[],
                files_removed=[],
                migration=None,
                capabilities=profile.capabilities,
            ),
            False,
        )

    instr_plan = None
    instr_conflict = None
    if profile.instruction_path is not None:
        instr_plan = plan_instruction_removal(path=profile.instruction_path, state=state)
        items.append(
            {
                "surface": "instruction",
                "path": str(profile.instruction_path),
                "status": instr_plan.status,
                "action": instr_plan.action,
            }
        )
        if instr_plan.status == "conflict":
            instr_conflict = items[-1]
            conflicts.append(items[-1])
    instruction_ready = instr_conflict is None

    skill_removals = plan_skill_removals(skills_root=profile.skills_root, state=state)
    for p in skill_removals:
        items.append(
            {"surface": "skill", "skill_id": p.skill_id, "path": str(p.path), "status": p.status, "action": p.action}
        )
        if p.status == "conflict":
            conflicts.append(items[-1])
    skills_ready = not any(p.status == "conflict" for p in skill_removals)

    gen_items, gen_conflicts = [], []
    hook_item, hook_conflict, hook_remove = None, [], None
    if profile.harness == "cursor":
        gen_items, gen_conflicts = _plan_cursor_generated_removals(profile, state)
        items.extend(gen_items)
        conflicts.extend(gen_conflicts)
        hook_item, hook_conflict, hook_remove = _plan_cursor_hook_removal(profile, state)
        items.append({k: v for k, v in hook_item.items() if k != "prior_index"})
        conflicts.extend(hook_conflict)
    generated_ready = not gen_conflicts and not hook_conflict

    # Native MCP removal (kimi-user, opencode-user, ...). Cursor keeps its own
    # v1 MCP retirement path; Pi uses the generated-artifacts path below.
    native_mcp_items: list[dict] = []
    native_mcp_conflicts: list[dict] = []
    native_mcp_remove: dict[str, Any] | None = None
    if profile.mcp_harness is not None and profile.harness not in ("pi", "cursor"):
        native_mcp_items, native_mcp_conflicts, native_mcp_remove = _plan_native_mcp_removal(profile, state, workspace)
        items.extend(native_mcp_items)
        conflicts.extend(native_mcp_conflicts)
    native_mcp_ready = not native_mcp_conflicts

    # Pi generated artifacts (extension + bridge descriptor) share the generic
    # generated-removal planner; created directories are swept after files.
    pi_gen_items: list[dict] = []
    pi_gen_conflicts: list[dict] = []
    pi_gen_writes: list[tuple[Path, str]] = []
    if profile.harness == "pi":
        pi_gen_items, pi_gen_conflicts, pi_gen_writes = _plan_generated_removals(profile, state)
        items.extend(pi_gen_items)
        conflicts.extend(pi_gen_conflicts)
    pi_gen_ready = not pi_gen_conflicts

    ready = instruction_ready and skills_ready and generated_ready and native_mcp_ready and pi_gen_ready

    reload_required = False
    if write:
        # Execute each independently safe plan even when other plans conflict:
        # digest-matching owned files are removed; conflict surfaces are preserved.
        if instr_plan is not None and instr_plan.action == "remove":
            localio.write_text_atomic(instr_plan.path, instr_plan.rendered)
            files_removed.append(str(instr_plan.path))
            state["instructions"] = {}
        elif instr_plan is not None and instr_plan.status == "absent":
            # owned file already gone: clear stale instruction ownership
            state["instructions"] = {}
        if skill_removals:
            removed = apply_skill_removals(
                skills_root=profile.skills_root,
                plans=skill_removals,
                state=state,
                state_path=profile.state_path,
            )
            files_removed.extend(removed)
            # always reload the persisted state (for cursor too): apply_skill_removals
            # persists a cloned new state, so the in-memory `state` is now stale.
            state = load_profile_state(
                state_path=profile.state_path, workspace=workspace, harness=profile.harness
            ).state
        if profile.harness == "cursor":
            for item in gen_items:
                if item["action"] == "remove":
                    p = Path(item["path"])
                    try:
                        p.unlink()
                        files_removed.append(str(p))
                    except FileNotFoundError:
                        pass
                    rel = cursor_user_cmd._relative(profile.user_root, p)
                    gen = state.get("generated", {})
                    if isinstance(gen.get("files"), dict):
                        gen["files"].pop(rel, None)
                elif item["action"] == "none" and item["status"] == "absent":
                    # owned generated file already gone: clear stale ownership
                    p = Path(item["path"])
                    rel = cursor_user_cmd._relative(profile.user_root, p)
                    gen = state.get("generated", {})
                    if isinstance(gen.get("files"), dict):
                        gen["files"].pop(rel, None)
            if hook_remove is not None:
                doc, index = hook_remove
                entries = doc["hooks"]["sessionStart"]
                entries.pop(index)
                if not entries:
                    doc["hooks"].pop("sessionStart", None)
                localio.write_text_atomic(profile.user_root / "hooks.json", cursor_user_cmd._coowned_json_text(doc))
                files_removed.append(str(profile.user_root / "hooks.json"))
                gen = state.get("generated", {})
                if isinstance(gen.get("hooks"), dict):
                    gen["hooks"].pop("sessionStart", None)
            # rmdir recorded created directories deepest-first; never recursive
            gen = state.get("generated", {})
            created = gen.get("created_directories", []) if isinstance(gen, dict) else []
            root_resolved = profile.user_root.resolve()
            for rel in sorted(created, key=lambda r: (-len(Path(r).parts), r)):
                d = (root_resolved / rel).resolve()
                if not d.is_relative_to(root_resolved):
                    continue
                try:
                    d.rmdir()
                except OSError:
                    continue
                if isinstance(created, list):
                    created.remove(rel)
        # Native MCP removal: rewrite the config preserving foreign entries,
        # clear ownership for removed and already-absent owned servers.
        if native_mcp_remove is not None:
            nw = _apply_native_mcp_removal(native_mcp_remove, state)
            files_removed.extend(nw)
        # Pi generated artifacts: unlink digest-matching owned files and clear
        # stale ownership for already-absent ones.
        if profile.harness == "pi":
            gen = state.get("generated", {})
            if not isinstance(gen, dict):
                gen = {}
                state["generated"] = gen
            owned_files = gen.get("files", {})
            if not isinstance(owned_files, dict):
                owned_files = {}
                gen["files"] = owned_files
            for p, rel in pi_gen_writes:
                try:
                    p.unlink()
                    files_removed.append(str(p))
                except FileNotFoundError:
                    pass
                owned_files.pop(rel, None)
            for item in pi_gen_items:
                if item.get("action") == "none" and item.get("status") == "absent":
                    owned_files.pop(item["_rel"], None)
            # rmdir recorded created directories deepest-first; never recursive
            created = gen.get("created_directories", []) if isinstance(gen, dict) else []
            root_resolved = profile.user_root.resolve()
            for rel in sorted(created, key=lambda r: (-len(Path(r).parts), r)):
                d = (root_resolved / rel).resolve()
                if not d.is_relative_to(root_resolved):
                    continue
                try:
                    d.rmdir()
                except OSError:
                    continue
                if isinstance(created, list):
                    created.remove(rel)
        # remove state file only when every owned section is empty (instructions,
        # skills, generated files/hooks/created dirs, and mcp). Keep state on
        # conflicts so the conflict surface stays owned for a future adopt.
        gen_state = state.get("generated", {}) if isinstance(state.get("generated"), dict) else {}
        owned_empty = (
            not state.get("instructions")
            and not state.get("skills")
            and not gen_state.get("files")
            and not gen_state.get("hooks")
            and not gen_state.get("created_directories")
            and not state.get("mcp")
        )
        if owned_empty and not conflicts:
            try:
                profile.state_path.unlink()
            except FileNotFoundError:
                pass
        else:
            write_profile_state(state_path=profile.state_path, state=state)
        reload_required = bool(files_removed)
        if conflicts:
            status = "conflict"
        elif files_removed:
            status = "updated"
        else:
            status = "current"
    elif write and not ready:
        status = "conflict"
        reload_required = False
    else:
        status = "current" if ready else "conflict"
        reload_required = not write

    return _result(
        profile.harness,
        status=status,
        ready=ready,
        instruction_ready=instruction_ready,
        skills_ready=skills_ready,
        reload_hint=profile.reload_hint,
        items=items,
        conflicts=conflicts,
        files_written=[],
        files_removed=files_removed,
        migration=None,
        capabilities=profile.capabilities,
    ), reload_required


def _doctor_profile(profile, workspace, verify_mcp):
    state, error, migration, _rm, _rf, _rfo, _rmo = _load_state_for_profile(profile, workspace)
    checks: list[dict] = []
    conflicts: list[dict] = []
    ready = True

    def _check(cid, ok, detail):
        checks.append({"id": cid, "status": "OK" if ok else "FAIL", "detail": detail})

    if error is not None:
        _check("ownership-state", False, error)
        conflicts.append(
            {
                "surface": "ownership-state",
                "path": str(profile.state_path),
                "status": "conflict",
                "action": "preserve",
                "detail": error,
            }
        )
        result = _result(
            profile.harness,
            status="conflict",
            ready=False,
            instruction_ready=False,
            skills_ready=False,
            reload_hint=profile.reload_hint,
            items=conflicts,
            conflicts=conflicts,
            files_written=[],
            files_removed=[],
            migration=None,
            capabilities=profile.capabilities,
        )
        result["checks"] = checks
        return result, False

    _check("ownership-state", True, str(profile.state_path))

    if profile.instruction_path is not None:
        instr_plan, instr_conflict = _plan_instruction_for_profile(
            profile, state, adopt=False, workspace=workspace, guard_tracked_write=False
        )
        ok = instr_plan is not None and instr_plan.status == "current"
        _check("instruction-current", ok, str(profile.instruction_path))
        if instr_conflict is not None:
            conflicts.append(instr_conflict)
        instruction_ready = ok
    else:
        instruction_ready = True
    ready = ready and instruction_ready

    skill_plans, skill_conflicts, _ = _plan_skills_for_profile(profile, state, workspace)
    skills_ok = not skill_conflicts and all(p.status == "current" for p in skill_plans)
    _check("skills-current", skills_ok, str(profile.skills_root))
    conflicts.extend(skill_conflicts)
    skills_ready = skills_ok
    ready = ready and skills_ready

    generated_ready = True
    if profile.harness == "cursor":
        gen_items, gen_conflicts, _gen_writes = _plan_cursor_generated(profile, state)
        gen_ok = True
        for item in gen_items:
            surface = item["surface"]
            ok = item["status"] == "current"
            _check(f"{surface}-current", ok, item["path"])
            gen_ok = gen_ok and ok
            if item["status"] == "conflict":
                conflicts.append(item)
        hook_item, hook_conflict, _hp, _hfp = _plan_cursor_hook(profile, state)
        hook_ok = hook_item["status"] == "current"
        _check("session-hook", hook_ok, hook_item["path"])
        if hook_conflict:
            conflicts.extend(hook_conflict)
        generated_ready = gen_ok and hook_ok
        ready = ready and generated_ready

    status = "current" if ready else "conflict"
    result = _result(
        profile.harness,
        status=status,
        ready=ready,
        instruction_ready=instruction_ready,
        skills_ready=skills_ready,
        reload_hint=profile.reload_hint,
        items=conflicts,
        conflicts=conflicts,
        files_written=[],
        files_removed=[],
        migration=migration,
        capabilities=profile.capabilities,
    )
    result["checks"] = checks
    return result, False


def _run_profiles(
    operation, *, harness, workspace, write, allow_global_stdio, adopt, verify_mcp, json_output, home=None
):
    if home is None:
        home = Path.home()
    workspace = workspace.expanduser().resolve()
    profiles = harness_profiles.resolve_slice1_profiles(harness=harness, home=home, workspace=workspace)
    results = []
    any_not_ready = False
    any_reload = False
    install_ops = {"install", "sync"}
    for profile in profiles:
        if operation in install_ops:
            result, reload_required = _install_profile(profile, workspace, write, adopt, allow_global_stdio)
        elif operation == "uninstall":
            result, reload_required = _uninstall_profile(profile, workspace, write)
        else:
            result, reload_required = _doctor_profile(profile, workspace, verify_mcp)
        results.append(result)
        if not result["ready"]:
            any_not_ready = True
        if reload_required:
            any_reload = True

    reload_required = (not write and operation in {*install_ops, "uninstall"}) or any_reload
    payload = {
        "schema_version": 1,
        "operation": operation,
        "harness": harness,
        "scope": "user",
        "workspace": str(workspace),
        "write": write,
        "ready": not any_not_ready,
        "reload_required": reload_required,
        "results": results,
    }
    if json_output:
        print(json.dumps(_strip_private(payload), indent=2, sort_keys=True))
    else:
        _emit_human(payload)
    return 1 if any_not_ready else 0


def _emit_human(payload):
    operation = payload.get("operation", "harness")
    print(f"harness {operation}: {payload['harness']}")
    for row in payload.get("results", []):
        marker = "ok" if row["ready"] else "conflict"
        print(f"- {row['harness']}: {row['status']} [{marker}]")
        for item in row.get("items", []):
            if item.get("status") == "conflict":
                name = f" [{item.get('name')}]" if item.get("name") else ""
                print(f"  ! {item['path']}{name}")
    if payload.get("reload_required"):
        print("next: reload harness windows")


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
    return _run_profiles(
        "sync",
        harness=harness,
        workspace=workspace,
        write=write,
        allow_global_stdio=allow_global_stdio,
        adopt=adopt,
        verify_mcp=False,
        json_output=json_output,
        home=home,
    )


def install(
    *,
    harness: str,
    workspace: Path,
    write: bool = False,
    allow_global_stdio: bool = False,
    adopt: bool = False,
    json_output: bool = False,
    home: Path | None = None,
) -> int:
    return _run_profiles(
        "install",
        harness=harness,
        workspace=workspace,
        write=write,
        allow_global_stdio=allow_global_stdio,
        adopt=adopt,
        verify_mcp=False,
        json_output=json_output,
        home=home,
    )


def uninstall(
    *, harness: str, workspace: Path, write: bool = False, json_output: bool = False, home: Path | None = None
) -> int:
    return _run_profiles(
        "uninstall",
        harness=harness,
        workspace=workspace,
        write=write,
        allow_global_stdio=False,
        adopt=False,
        verify_mcp=False,
        json_output=json_output,
        home=home,
    )


def doctor(
    *, harness: str, workspace: Path, verify_mcp: bool = False, json_output: bool = False, home: Path | None = None
) -> int:
    return _run_profiles(
        "doctor",
        harness=harness,
        workspace=workspace,
        write=False,
        allow_global_stdio=False,
        adopt=False,
        verify_mcp=verify_mcp,
        json_output=json_output,
        home=home,
    )
