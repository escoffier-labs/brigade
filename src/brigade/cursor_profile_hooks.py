"""Cursor lifecycle-hook ownership for user-scope harness profiles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import __version__ as BRIGADE_VERSION
from .harness_profiles import PROFILE_STATE_VERSION

HOOK_STATE_PREFIX = "hooks.json#"
HOOK_EVENTS = ("sessionStart", "postToolUse", "sessionEnd")
LEGACY_CURSOR_INSTRUCTION = "plugins/local/brigade-loop/rules/brigade-loop.mdc"
LEGACY_CURSOR_GENERATED = (
    "plugins/local/brigade-loop/.cursor-plugin/plugin.json",
    "hooks/brigade-session-start",
    "brigade/mcp.json",
)
LEGACY_CURSOR_SKILL_ID = "brigade-work"
LEGACY_CURSOR_SKILL_PREFIX = "skills/brigade-work/"


def migrate_legacy_install_state(*, state: dict[str, Any], workspace: Path, harness: str) -> dict[str, Any]:
    """Map legacy Cursor install attestations into profile schema version 2."""
    files = state["files"]
    hooks = state["hooks"]
    mcp = state["mcp"]
    migrated: dict[str, Any] = {
        "schema_version": PROFILE_STATE_VERSION,
        "package_version": BRIGADE_VERSION,
        "workspace": str(workspace.expanduser().resolve()),
        "harness": harness,
        "instructions": {},
        "skills": {},
        "generated": {},
        "mcp": {},
    }
    package_version = state.get("package_version")
    if isinstance(package_version, str) and package_version:
        migrated["package_version"] = package_version

    rule_digest = files.get(LEGACY_CURSOR_INSTRUCTION)
    if isinstance(rule_digest, str):
        migrated["instructions"] = {"digest": rule_digest, "created_file": True, "legacy_migrated": True}

    generated: dict[str, dict[str, Any]] = {}
    for relative in LEGACY_CURSOR_GENERATED:
        digest = files.get(relative)
        if isinstance(digest, str):
            generated[relative] = {"digest": digest, "legacy_migrated": True}
    for event in HOOK_EVENTS:
        fingerprint = hooks.get(event)
        if isinstance(fingerprint, str):
            generated[f"{HOOK_STATE_PREFIX}{event}"] = {
                "entry_fingerprint": fingerprint,
                "legacy_migrated": True,
            }
    migrated["generated"] = generated

    skill_files = {
        relative.removeprefix(LEGACY_CURSOR_SKILL_PREFIX): digest
        for relative, digest in files.items()
        if isinstance(relative, str) and isinstance(digest, str) and relative.startswith(LEGACY_CURSOR_SKILL_PREFIX)
    }
    if skill_files:
        migrated["skills"][LEGACY_CURSOR_SKILL_ID] = {"files": skill_files, "legacy_migrated": True}

    for name, fingerprint in mcp.items():
        if isinstance(name, str) and isinstance(fingerprint, str):
            migrated["mcp"][name] = {
                "projected_fingerprint": fingerprint[:16],
                "managed": True,
                "legacy_migrated": True,
            }
    return migrated


def _legacy_migration_recorded(state: dict[str, Any]) -> bool:
    """True when stored state carries a record stamped by the legacy install migration."""
    for section in ("instructions", "skills", "generated", "mcp"):
        value = state.get(section)
        if not isinstance(value, dict):
            continue
        if value.get("legacy_migrated") is True:
            return True
        if any(isinstance(record, dict) and record.get("legacy_migrated") is True for record in value.values()):
            return True
    return False


def plan(profile, state: dict[str, Any], *, adopt: bool) -> dict[str, Any]:
    """Plan each managed event while preserving co-owned hook entries."""
    from . import cursor_user_cmd

    hook = profile.hook
    path = hook.path
    records = state["generated"]
    empty: dict[str, Any] = {"items": [], "conflicts": [], "doc": None, "next": {}}
    doc, error = cursor_user_cmd._read_json_object(path)
    if doc is None:
        failure_items = [
            {
                "surface": "hook",
                "path": str(path),
                "name": event,
                "status": "conflict",
                "action": "preserve",
                "detail": error or "could not read hook configuration",
            }
            for event in hook.entries
        ]
        return {**empty, "items": failure_items, "conflicts": failure_items}
    document_error, repair_version = cursor_user_cmd.user_hooks_document_status(doc)
    if document_error:
        failure_items = [
            {
                "surface": "hook",
                "path": str(path),
                "name": event,
                "status": "conflict",
                "action": "preserve",
                "detail": document_error,
            }
            for event in hook.entries
        ]
        return {**empty, "items": failure_items, "conflicts": failure_items}
    hooks = doc.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    actions: dict[str, dict[str, Any]] = {}
    next_records: dict[str, dict[str, str]] = {}
    # The earlier profile sync migrated only sessionStart, so the legacy installer's identical
    # postToolUse and sessionEnd entries have no ownership record. Claim them on upgrade.
    legacy_migrated = _legacy_migration_recorded(state)
    for event, entry in hook.entries.items():
        item: dict[str, Any] = {"surface": "hook", "path": str(path), "name": event}
        entries = hooks.get(event)
        if entries is not None and not isinstance(entries, list):
            item.update(status="conflict", action="preserve", detail=f"existing {event} hooks must be a list")
            conflicts.append(item)
            items.append(item)
            continue
        entries = entries if isinstance(entries, list) else []
        record = records.get(f"{HOOK_STATE_PREFIX}{event}")
        record = record if isinstance(record, dict) else {}
        desired_fp = cursor_user_cmd._digest_value(entry)
        if entry in entries:
            if record.get("entry_fingerprint") == desired_fp:
                item.update(status="current", action="none")
            elif adopt:
                item.update(status="adopted", action="none")
            elif legacy_migrated and "entry_fingerprint" not in record:
                item.update(
                    status="adopted",
                    action="none",
                    detail=f"claimed the {event} entry placed by the legacy Cursor installer",
                )
            else:
                item.update(
                    status="conflict",
                    action="preserve",
                    detail=f"matching {event} hook entry is unowned; rerun with --adopt",
                )
                conflicts.append(item)
            next_records[f"{HOOK_STATE_PREFIX}{event}"] = {"entry_fingerprint": desired_fp}
        else:
            prior_fp = record.get("entry_fingerprint")
            prior_index = next(
                (
                    index
                    for index, old in enumerate(entries)
                    if prior_fp and cursor_user_cmd._digest_value(old) == prior_fp
                ),
                None,
            )
            if prior_index is not None:
                item.update(status="stale", action="update", prior_index=prior_index)
                actions[event] = {"action": "update", "prior_index": prior_index}
            else:
                item.update(status="missing", action="create")
                actions[event] = {"action": "create"}
            next_records[f"{HOOK_STATE_PREFIX}{event}"] = {"entry_fingerprint": desired_fp}
        items.append(item)
    if repair_version:
        items.append(
            {
                "surface": "hook",
                "path": str(path),
                "name": "version",
                "status": "stale",
                "action": "update",
                "detail": "hooks document predates the required version field",
            }
        )
    return {
        "items": items,
        "conflicts": conflicts,
        "doc": doc,
        "actions": actions,
        "repair_version": repair_version,
        "next": next_records,
    }


def uninstall_plan(profile, state: dict[str, Any]) -> dict[str, Any]:
    """Locate only the fingerprinted Brigade entries for removal."""
    from . import cursor_user_cmd

    hook = profile.hook
    path = hook.path
    records = state["generated"]
    managed = {
        event: record.get("entry_fingerprint")
        for event in hook.entries
        if isinstance((record := records.get(f"{HOOK_STATE_PREFIX}{event}")), dict)
    }
    managed = {event: fingerprint for event, fingerprint in managed.items() if isinstance(fingerprint, str)}
    if not managed:
        return {"items": [], "conflicts": [], "doc": None, "indices": {}}
    doc, error = cursor_user_cmd._read_json_object(path)
    hooks: dict[str, Any] = {}
    if doc is not None and isinstance(doc.get("hooks"), dict):
        hooks = doc["hooks"]
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    indices: dict[str, int] = {}
    for event, fingerprint in managed.items():
        item: dict[str, Any] = {"surface": "hook", "path": str(path), "name": event}
        entries = hooks.get(event)
        index = (
            next((i for i, entry in enumerate(entries) if cursor_user_cmd._digest_value(entry) == fingerprint), None)
            if isinstance(entries, list)
            else None
        )
        if index is not None:
            item.update(status="managed", action="remove")
            indices[event] = index
        elif not path.exists():
            item.update(status="absent", action="none")
        else:
            item.update(
                status="conflict",
                action="preserve",
                detail=error or f"managed {event} hook entry was edited or removed",
            )
            conflicts.append(item)
        items.append(item)
    return {"items": items, "conflicts": conflicts, "doc": doc if indices else None, "indices": indices}
