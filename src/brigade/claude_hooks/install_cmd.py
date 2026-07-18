"""Merge-safe installation of Brigade's project-scoped Claude hooks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .. import localio
from ..config import load_config
from .package import MANAGED_EVENTS, PACKAGE_ID, PACKAGE_VERSION, is_managed_handler, managed_groups

SETTINGS_REL_PATH = Path(".claude/settings.json")
SIDECAR_REL_PATH = Path(".brigade/claude-hooks.json")


def _load_settings(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unable to read Claude settings: {type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, "Claude settings must contain a JSON object"
    hooks = payload.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        return None, "Claude settings `hooks` must contain a JSON object"
    if isinstance(hooks, dict):
        for event in MANAGED_EVENTS:
            groups = hooks.get(event)
            if groups is not None and not isinstance(groups, list):
                return None, f"Claude settings `hooks.{event}` must contain a JSON array"
    return payload, None


def _without_managed(groups: object, event: str) -> list[object]:
    if not isinstance(groups, list):
        return []
    kept: list[object] = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            kept.append(group)
            continue
        foreign = [handler for handler in handlers if not is_managed_handler(handler)]
        if foreign:
            updated = dict(group)
            updated["hooks"] = foreign
            kept.append(updated)
        elif not handlers:
            kept.append(group)
    return kept


def _merge_settings(settings: dict[str, Any]) -> dict[str, Any]:
    merged = dict(settings)
    raw_hooks = settings.get("hooks")
    existing_hooks: dict[str, Any] = raw_hooks if isinstance(raw_hooks, dict) else {}
    hooks: dict[str, Any] = dict(existing_hooks)
    specs = managed_groups()
    for event in MANAGED_EVENTS:
        hooks[event] = _without_managed(hooks.get(event), event) + specs[event]
    merged["hooks"] = hooks
    return merged


def _remove_settings(settings: dict[str, Any]) -> dict[str, Any]:
    merged = dict(settings)
    raw_hooks = settings.get("hooks")
    existing_hooks: dict[str, Any] = raw_hooks if isinstance(raw_hooks, dict) else {}
    hooks: dict[str, Any] = {}
    for event, groups in existing_hooks.items():
        if event not in MANAGED_EVENTS or not isinstance(groups, list):
            hooks[event] = groups
            continue
        cleaned = _without_managed(groups, event)
        if cleaned:
            hooks[event] = cleaned
    if hooks:
        merged["hooks"] = hooks
    else:
        merged.pop("hooks", None)
    return merged


def _wired_for_claude(target: Path) -> tuple[Path | None, str | None]:
    target = target.expanduser().resolve()
    try:
        config = load_config(target)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"unable to load Brigade config: {type(exc).__name__}: {exc}"
    if config is None or "claude" not in config.selection.harnesses:
        return None, f"target is not wired for Claude: {target}"
    return target, None


def _sidecar(target: Path) -> dict[str, Any] | None:
    return localio.read_json_dict(target / SIDECAR_REL_PATH)


def _write_package(target: Path, *, action: str) -> tuple[dict[str, Any] | None, str | None]:
    resolved, error = _wired_for_claude(target)
    if resolved is None:
        return None, error
    settings_path = resolved / SETTINGS_REL_PATH
    settings, error = _load_settings(settings_path)
    if settings is None:
        return None, error
    now = localio.utc_now_iso()
    previous = _sidecar(resolved) or {}
    localio.write_json(settings_path, _merge_settings(settings))
    sidecar = {
        "version": 1,
        "package_id": PACKAGE_ID,
        "package_version": PACKAGE_VERSION,
        "settings_path": str(SETTINGS_REL_PATH),
        "managed_events": list(MANAGED_EVENTS),
        "installed_at": previous.get("installed_at") or now,
        "updated_at": now,
    }
    localio.write_json(resolved / SIDECAR_REL_PATH, sidecar)
    return {"action": action, "target": str(resolved), **sidecar}, None


def hooks_install(*, target: Path, quiet: bool = False) -> int:
    payload, error = _write_package(target, action="install")
    if payload is None:
        if not quiet:
            print(f"error: {error}", file=sys.stderr)
        return 2
    if not quiet:
        print(f"claude hooks: installed {PACKAGE_ID}@{PACKAGE_VERSION} -> {payload['target']}")
    return 0


def hooks_update(*, target: Path, quiet: bool = False) -> int:
    payload, error = _write_package(target, action="update")
    if payload is None:
        if not quiet:
            print(f"error: {error}", file=sys.stderr)
        return 2
    if not quiet:
        print(f"claude hooks: updated {PACKAGE_ID}@{PACKAGE_VERSION} -> {payload['target']}")
    return 0


def _managed_signature(group: dict[str, Any], handler: dict[str, Any]) -> str:
    matcher = ["matcher" in group, group.get("matcher")]
    return json.dumps({"matcher": matcher, "handler": handler}, sort_keys=True, separators=(",", ":"))


def status_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    settings, error = _load_settings(target / SETTINGS_REL_PATH)
    if settings is None:
        return {
            "target": str(target),
            "package_id": PACKAGE_ID,
            "package_version": PACKAGE_VERSION,
            "installed": False,
            "current": False,
            "managed_events": [],
            "missing_events": list(MANAGED_EVENTS),
            "error": error,
        }
    raw_hooks = settings.get("hooks")
    hooks: dict[str, Any] = raw_hooks if isinstance(raw_hooks, dict) else {}
    present: list[str] = []
    current_events: list[str] = []
    foreign_count = 0
    expected_groups = managed_groups()
    for event, groups in hooks.items():
        managed_signatures: list[str] = []
        if isinstance(groups, list):
            for group in groups:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(handlers, list):
                    continue
                for handler in handlers:
                    if is_managed_handler(handler, event):
                        managed_signatures.append(_managed_signature(group, handler))
                    else:
                        foreign_count += 1
        if event in MANAGED_EVENTS and managed_signatures:
            present.append(event)
            expected_signatures = [
                _managed_signature(group, handler) for group in expected_groups[event] for handler in group["hooks"]
            ]
            if sorted(managed_signatures) == sorted(expected_signatures):
                current_events.append(event)
    sidecar = _sidecar(target)
    current_sidecar = bool(
        sidecar and sidecar.get("package_id") == PACKAGE_ID and sidecar.get("package_version") == PACKAGE_VERSION
    )
    ordered_present = [event for event in MANAGED_EVENTS if event in present]
    ordered_current = [event for event in MANAGED_EVENTS if event in current_events]
    missing = [event for event in MANAGED_EVENTS if event not in ordered_present]
    stale = [event for event in ordered_present if event not in ordered_current]
    return {
        "target": str(target),
        "package_id": PACKAGE_ID,
        "package_version": PACKAGE_VERSION,
        "installed": not missing and sidecar is not None,
        "current": not missing and not stale and current_sidecar,
        "managed_events": ordered_present,
        "current_events": ordered_current,
        "missing_events": missing,
        "stale_events": stale,
        "foreign_handler_count": foreign_count,
        "sidecar": sidecar,
        "error": error,
    }


def hooks_status(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    if json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        state = "current" if payload["current"] else ("installed" if payload["installed"] else "not installed")
        print(f"claude hooks: {state}")
        print(f"target: {payload['target']}")
        print(f"events: {', '.join(payload['managed_events']) or '(none)'}")
        if payload["missing_events"]:
            print(f"missing: {', '.join(payload['missing_events'])}")
        print(f"foreign_handlers: {payload.get('foreign_handler_count', 0)}")
        if payload.get("error"):
            print(f"error: {payload['error']}")
    return 2 if payload.get("error") else 0


def hooks_uninstall(*, target: Path, quiet: bool = False) -> int:
    target = target.expanduser().resolve()
    settings_path = target / SETTINGS_REL_PATH
    settings, error = _load_settings(settings_path)
    if settings is None:
        if not quiet:
            print(f"error: {error}", file=sys.stderr)
        return 2
    cleaned = _remove_settings(settings)
    if settings_path.exists() or cleaned:
        localio.write_json(settings_path, cleaned)
    (target / SIDECAR_REL_PATH).unlink(missing_ok=True)
    if not quiet:
        print(f"claude hooks: uninstalled {PACKAGE_ID} -> {target}")
    return 0
