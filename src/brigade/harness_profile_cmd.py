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

_RECOVERY_COMMAND = "brigade harness sync --target <claude|codex> --scope user --adopt --write"
_SECTIONS = ("instructions", "skills", "generated", "mcp")


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


def _native_surface_conflicts(profile, workspace: Path) -> list[dict[str, Any]]:
    """Reject symlinked profile surfaces before reading or mutating them."""
    mcp_path = mcp_adapters.resolve_path(mcp_adapters.ADAPTERS[profile.mcp_harness], workspace)
    surfaces = (
        ("profile-root", profile.user_root, True),
        ("instruction", profile.instruction_path, False),
        ("profile-directory", profile.state_path.parent, True),
        ("ownership-state", profile.state_path, False),
        ("profile-receipt", profile.receipt_path, False),
        ("mcp", mcp_path, False),
    )
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
    path = mcp_adapters.resolve_path(adapter, workspace)
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
    path = mcp_adapters.resolve_path(adapter, workspace)
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
    path = mcp_adapters.resolve_path(adapter, workspace)
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
        "migration": None,
        "capabilities": {},
        "mcp": mcp,
        "receipt_path": str(receipt_path),
        "receipt_state": receipt_state,
    }


def _receipt_ownership(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "instruction_fingerprint": state.get("instructions", {}).get("digest"),
        "skills": state.get("skills", {}),
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
    instruction = plan_instruction(
        path=profile.instruction_path, desired=harness_profiles.managed_instruction_text(), state=state, adopt=adopt
    )
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
        proposed["instructions"] = (
            {"digest": instruction.desired_digest, "created_file": created_file} if instruction.desired_digest else {}
        )
    proposed["skills"] = skill_plan["next"]
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
            localio.write_text_atomic(instruction.path, instruction.rendered or "")
            files_written.append(str(instruction.path))
            changed = True
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
    plan = plan_instruction_removal(path=profile.instruction_path, state=state)
    items = [{"surface": "instruction", "path": str(plan.path), "status": plan.status, "action": plan.action}]
    conflicts = [] if plan.status != "conflict" else [items[0] | {"detail": plan.detail or "instruction conflict"}]
    skill_plan = _skill_uninstall_plan(profile, state)
    items.extend(skill_plan["items"])
    conflicts.extend(skill_plan["conflicts"])
    mcp_plan = _mcp_uninstall_plan(profile, state, workspace)
    items.extend(mcp_plan["items"])
    conflicts.extend(mcp_plan["conflicts"])
    proposed = json.loads(json.dumps(state))
    proposed["instructions"] = {}
    proposed["skills"] = {}
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
        for path in skill_plan["removes"]:
            path.unlink(missing_ok=True)
            files_removed.append(str(path))
        files_removed.extend(_prune_created_directories(skill_plan["prune"], profile.skills_root))
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
    instruction = plan_instruction(
        path=profile.instruction_path, desired=harness_profiles.managed_instruction_text(), state=state
    )
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
    mcp, mcp_ok = _verify_mcp(profile, state, workspace) if verify_mcp else ({"status": "pending", "items": []}, True)
    ready = not conflicts and mcp_ok
    return _result(
        profile,
        status="current" if ready else "conflict",
        ready=ready,
        items=[item, *skill_plan["items"]],
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
    profiles = harness_profiles.resolve_slice1_profiles(harness=harness, home=home or Path.home(), workspace=workspace)

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
