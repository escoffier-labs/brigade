"""User-scope harness profile command logic.

Issue #438: managed-block parsing, ownership-state validation, skill/artifact
reconciliation, and aggregate install/uninstall/doctor. This module owns the
managed instruction block surface plan; profile records and native path
resolution live in the sibling ``harness_profiles`` module.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import harness_profiles, localio

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
        "workspace": str(workspace.expanduser().resolve()),
        "harness": harness,
        "instructions": {},
        "skills": {},
        "artifacts": {},
        "mcp": {},
    }


def write_profile_state(*, state_path: Path, state: dict[str, Any]) -> None:
    """Persist ``state`` atomically as sorted-key JSON at ``state_path``."""
    localio.write_json(state_path, state)


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
        if plan.action != "remove":
            continue
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
