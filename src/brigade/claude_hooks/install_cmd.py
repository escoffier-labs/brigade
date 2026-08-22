"""Merge-safe installation of Brigade's Claude work-loop hooks."""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .. import localio
from ..config import load_config
from .package import (
    HOOK_SCRIPT_NAME,
    MANAGED_EVENTS,
    PACKAGE_ID,
    PACKAGE_VERSION,
    hook_script_text,
    is_legacy_handler,
    is_managed_handler,
    is_managed_user_handler,
    managed_groups,
    managed_user_groups,
)
from .paths import is_operator_home, resolve_claude_home

HooksScope = Literal["project", "user"]

SETTINGS_REL_PATH = Path(".claude/settings.json")
SIDECAR_REL_PATH = Path(".brigade/claude-hooks.json")
USER_SETTINGS_REL_PATH = Path("settings.json")
USER_SIDECAR_REL_PATH = Path("brigade/claude-hooks.json")
USER_HOOK_SCRIPT_REL_PATH = Path("hooks") / HOOK_SCRIPT_NAME


def _packaged_script_digest(*, pin: Path | None = None) -> str:
    return hashlib.sha256(hook_script_text(pin=pin).encode("utf-8")).hexdigest()


def _user_settings_path() -> Path:
    return resolve_claude_home() / USER_SETTINGS_REL_PATH


def _project_collides_with_user_settings(layout: _InstallLayout) -> bool:
    if layout.scope != "project":
        return False
    try:
        return layout.settings_path.expanduser().resolve() == _user_settings_path().resolve()
    except OSError:
        return False


def _project_scope_home_error(layout: _InstallLayout) -> str | None:
    if layout.scope != "project":
        return None
    if is_operator_home(layout.root) or _project_collides_with_user_settings(layout):
        return (
            f"refusing project-scope hook install at {layout.root}: "
            f"{layout.settings_rel} is Claude Code's user settings file. "
            "Use `brigade work hooks install --scope user`, or pass --target at a project directory."
        )
    return None


def _resolve_user_scope_pin(target: Path) -> tuple[Path | None, str | None]:
    """Pin user-scope hook-run only when --target is an explicit wired workspace."""
    if target == Path("."):
        return None, None
    try:
        resolved = target.expanduser().resolve()
    except OSError as exc:
        return None, f"unable to resolve --target: {type(exc).__name__}: {exc}"
    if is_operator_home(resolved):
        return None, (
            f"refusing to pin user-scope hooks to the home directory ({resolved}); "
            "pass --target at a wired workspace, or omit --target for multi-repo mode"
        )
    try:
        config = load_config(resolved)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"unable to load Brigade config: {type(exc).__name__}: {exc}"
    if config is None or "claude" not in config.selection.harnesses:
        return None, (
            f"user-scope --target must be a Claude-wired Brigade workspace: {resolved}; "
            "omit --target for multi-repo mode"
        )
    return resolved, None


def _pin_from_sidecar(sidecar: dict[str, Any] | None) -> Path | None:
    if not isinstance(sidecar, dict):
        return None
    raw = sidecar.get("pinned_target")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


@dataclass(frozen=True)
class _InstallLayout:
    scope: HooksScope
    root: Path
    settings_path: Path
    sidecar_path: Path
    script_path: Path | None
    settings_rel: str
    script_rel: str | None


def _layout(*, scope: HooksScope, target: Path) -> _InstallLayout:
    if scope == "user":
        root = resolve_claude_home()
        return _InstallLayout(
            scope=scope,
            root=root,
            settings_path=root / USER_SETTINGS_REL_PATH,
            sidecar_path=root / USER_SIDECAR_REL_PATH,
            script_path=root / USER_HOOK_SCRIPT_REL_PATH,
            settings_rel=str(USER_SETTINGS_REL_PATH),
            script_rel=str(USER_HOOK_SCRIPT_REL_PATH),
        )
    resolved = target.expanduser().resolve()
    return _InstallLayout(
        scope=scope,
        root=resolved,
        settings_path=resolved / SETTINGS_REL_PATH,
        sidecar_path=resolved / SIDECAR_REL_PATH,
        script_path=None,
        settings_rel=str(SETTINGS_REL_PATH),
        script_rel=None,
    )


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


def _is_managed(value: object, *, layout: _InstallLayout, event: str | None = None) -> bool:
    if layout.scope == "user" and layout.script_path is not None:
        return is_managed_user_handler(value, layout.script_path, event) or is_managed_handler(value, event)
    return is_managed_handler(value, event)


def _without_managed_or_legacy(groups: object, event: str, *, layout: _InstallLayout) -> list[object]:
    """Drop managed and legacy standalone handlers, preserving genuine foreign hooks.

    Groups that become empty after dropping handlers are removed so install/update
    reconciliation is idempotent and does not leave stale empty arrays behind.
    """
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
        foreign = [
            handler
            for handler in handlers
            if not _is_managed(handler, layout=layout) and not is_legacy_handler(handler)
        ]
        if foreign:
            updated = dict(group)
            updated["hooks"] = foreign
            kept.append(updated)
        elif not handlers:
            kept.append(group)
    return kept


def _merge_settings(settings: dict[str, Any], *, layout: _InstallLayout) -> dict[str, Any]:
    merged = dict(settings)
    raw_hooks = settings.get("hooks")
    existing_hooks: dict[str, Any] = raw_hooks if isinstance(raw_hooks, dict) else {}
    hooks: dict[str, Any] = dict(existing_hooks)
    if layout.scope == "user":
        assert layout.script_path is not None
        specs = managed_user_groups(layout.script_path)
    else:
        specs = managed_groups()
    for event in MANAGED_EVENTS:
        hooks[event] = _without_managed_or_legacy(hooks.get(event), event, layout=layout) + specs[event]
    merged["hooks"] = hooks
    return merged


def _remove_settings(settings: dict[str, Any], *, layout: _InstallLayout) -> dict[str, Any]:
    merged = dict(settings)
    raw_hooks = settings.get("hooks")
    existing_hooks: dict[str, Any] = raw_hooks if isinstance(raw_hooks, dict) else {}
    hooks: dict[str, Any] = {}
    for event, groups in existing_hooks.items():
        if event not in MANAGED_EVENTS or not isinstance(groups, list):
            hooks[event] = groups
            continue
        cleaned = _without_managed_or_legacy(groups, event, layout=layout)
        if cleaned:
            hooks[event] = cleaned
    if hooks:
        merged["hooks"] = hooks
    else:
        merged.pop("hooks", None)
    return merged


def _resolve_hooks_target(target: Path) -> tuple[Path | None, str | None]:
    """Allow install/update on Brigade-wired Claude targets or existing user settings."""
    target = target.expanduser().resolve()
    settings_path = target / SETTINGS_REL_PATH
    try:
        config = load_config(target)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"unable to load Brigade config: {type(exc).__name__}: {exc}"
    if config is not None and "claude" in config.selection.harnesses:
        return target, None
    if settings_path.exists():
        return target, None
    return None, (
        f"target is not wired for Claude and has no {SETTINGS_REL_PATH}: {target}; "
        "run brigade init with the Claude harness, or point --target at a tree that "
        f"already has {SETTINGS_REL_PATH}"
    )


def _sidecar(layout: _InstallLayout) -> dict[str, Any] | None:
    return localio.read_json_dict(layout.sidecar_path)


def _write_hook_script(path: Path, *, pin: Path | None = None) -> str:
    text = hook_script_text(pin=pin)
    localio.write_text_atomic(path, text)
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return localio.file_sha256(path)


def _write_package(
    target: Path,
    *,
    action: str,
    scope: HooksScope,
) -> tuple[dict[str, Any] | None, str | None]:
    layout = _layout(scope=scope, target=target)
    pin: Path | None = None
    if layout.scope == "project":
        home_error = _project_scope_home_error(layout)
        if home_error is not None:
            return None, home_error
        resolved, error = _resolve_hooks_target(target)
        if resolved is None:
            return None, error
        layout = _layout(scope=scope, target=resolved)
        home_error = _project_scope_home_error(layout)
        if home_error is not None:
            return None, home_error
    else:
        pin, pin_error = _resolve_user_scope_pin(target)
        if pin_error is not None:
            return None, pin_error
    settings, error = _load_settings(layout.settings_path)
    if settings is None:
        return None, error
    now = localio.utc_now_iso()
    previous = _sidecar(layout) or {}
    script_digest: str | None = None
    if layout.script_path is not None:
        script_digest = _write_hook_script(layout.script_path, pin=pin)
    localio.write_json(layout.settings_path, _merge_settings(settings, layout=layout))
    sidecar = {
        "version": 1,
        "package_id": PACKAGE_ID,
        "package_version": PACKAGE_VERSION,
        "settings_path": layout.settings_rel,
        "managed_events": list(MANAGED_EVENTS),
        "installed_at": previous.get("installed_at") or now,
        "updated_at": now,
    }
    if layout.scope == "user":
        sidecar["scope"] = layout.scope
        if layout.script_rel is not None:
            sidecar["script_path"] = layout.script_rel
        if script_digest is not None:
            sidecar["script_digest"] = script_digest
        if pin is not None:
            sidecar["pinned_target"] = str(pin)
    localio.write_json(layout.sidecar_path, sidecar)
    return {"action": action, "target": str(layout.root), **sidecar}, None


def hooks_install(*, target: Path, scope: HooksScope = "project", quiet: bool = False) -> int:
    payload, error = _write_package(target, action="install", scope=scope)
    if payload is None:
        if not quiet:
            print(f"error: {error}", file=sys.stderr)
        return 2
    if not quiet:
        location = payload["target"] if scope == "project" else f"{payload['target']} ({scope})"
        pin = payload.get("pinned_target")
        suffix = f" pin={pin}" if pin else ""
        print(f"claude hooks: installed {PACKAGE_ID}@{PACKAGE_VERSION} -> {location}{suffix}")
    return 0


def hooks_update(*, target: Path, scope: HooksScope = "project", quiet: bool = False) -> int:
    payload, error = _write_package(target, action="update", scope=scope)
    if payload is None:
        if not quiet:
            print(f"error: {error}", file=sys.stderr)
        return 2
    if not quiet:
        location = payload["target"] if scope == "project" else f"{payload['target']} ({scope})"
        print(f"claude hooks: updated {PACKAGE_ID}@{PACKAGE_VERSION} -> {location}")
    return 0


def _managed_signature(group: dict[str, Any], handler: dict[str, Any]) -> str:
    matcher = ["matcher" in group, group.get("matcher")]
    return json.dumps({"matcher": matcher, "handler": handler}, sort_keys=True, separators=(",", ":"))


def _expected_groups(layout: _InstallLayout) -> dict[str, list[dict[str, Any]]]:
    if layout.scope == "user":
        assert layout.script_path is not None
        return managed_user_groups(layout.script_path)
    return managed_groups()


def status_payload(target: Path, *, scope: HooksScope = "project") -> dict[str, Any]:
    layout = _layout(scope=scope, target=target)
    settings, error = _load_settings(layout.settings_path)
    sidecar = _sidecar(layout)
    pin = _pin_from_sidecar(sidecar)
    script_digest: str | None = None
    script_current = True
    if layout.script_path is not None:
        if layout.script_path.is_file():
            script_digest = localio.file_sha256(layout.script_path)
            script_current = script_digest == _packaged_script_digest(pin=pin)
        else:
            script_current = False
    if settings is None:
        payload: dict[str, Any] = {
            "target": str(layout.root),
            "package_id": PACKAGE_ID,
            "package_version": PACKAGE_VERSION,
            "installed": False,
            "current": False,
            "managed_events": [],
            "missing_events": list(MANAGED_EVENTS),
            "error": error,
            "duplicate_handler_count": 0,
            "scope_widened": False,
        }
        if layout.scope == "user":
            payload["scope"] = layout.scope
            payload["script_path"] = str(layout.script_path)
            payload["script_installed"] = False
            payload["script_current"] = False
        return payload
    raw_hooks = settings.get("hooks")
    hooks: dict[str, Any] = raw_hooks if isinstance(raw_hooks, dict) else {}
    present: list[str] = []
    current_events: list[str] = []
    foreign_count = 0
    legacy_count = 0
    legacy_events: list[str] = []
    duplicate_count = 0
    expected_groups = _expected_groups(layout)
    for event, groups in hooks.items():
        managed_signatures: list[str] = []
        if isinstance(groups, list):
            for group in groups:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(handlers, list):
                    continue
                for handler in handlers:
                    if _is_managed(handler, layout=layout, event=event):
                        if isinstance(handler, dict) and isinstance(group, dict):
                            managed_signatures.append(_managed_signature(group, handler))
                    elif is_legacy_handler(handler):
                        legacy_count += 1
                        if event not in legacy_events:
                            legacy_events.append(event)
                    else:
                        foreign_count += 1
        duplicate_count += len(managed_signatures) - len(set(managed_signatures))
        if event in MANAGED_EVENTS and managed_signatures:
            present.append(event)
            expected_signatures = [
                _managed_signature(group, handler) for group in expected_groups[event] for handler in group["hooks"]
            ]
            if sorted(managed_signatures) == sorted(expected_signatures):
                current_events.append(event)
    current_sidecar = bool(
        sidecar
        and sidecar.get("package_id") == PACKAGE_ID
        and sidecar.get("package_version") == PACKAGE_VERSION
        and (layout.scope == "project" or sidecar.get("scope") == "user")
    )
    ordered_present = [event for event in MANAGED_EVENTS if event in present]
    ordered_current = [event for event in MANAGED_EVENTS if event in current_events]
    missing = [event for event in MANAGED_EVENTS if event not in ordered_present]
    stale = [event for event in ordered_present if event not in ordered_current]
    ordered_legacy_events = [event for event in MANAGED_EVENTS if event in legacy_events]
    script_installed = layout.script_path is None or layout.script_path.is_file()
    installed = not missing and sidecar is not None and script_installed
    scope_widened = _project_collides_with_user_settings(layout)
    current = (
        not missing
        and not stale
        and current_sidecar
        and script_current
        and (layout.script_path is None or script_installed)
        and duplicate_count == 0
        and not scope_widened
    )
    result = {
        "target": str(layout.root),
        "package_id": PACKAGE_ID,
        "package_version": PACKAGE_VERSION,
        "installed": installed,
        "current": current,
        "managed_events": ordered_present,
        "current_events": ordered_current,
        "missing_events": missing,
        "stale_events": stale,
        "foreign_handler_count": foreign_count,
        "legacy_handler_count": legacy_count,
        "legacy_events": ordered_legacy_events,
        "duplicate_handler_count": duplicate_count,
        "scope_widened": scope_widened,
        "sidecar": sidecar,
        "error": error,
    }
    if layout.scope == "user":
        result["scope"] = layout.scope
        if pin is not None:
            result["pinned_target"] = str(pin)
    if layout.script_path is not None:
        result["script_path"] = str(layout.script_path)
        result["script_installed"] = script_installed
        result["script_current"] = script_current
        if script_digest is not None:
            result["script_digest"] = script_digest
    return result


def hooks_status(*, target: Path, scope: HooksScope = "project", json_output: bool = False) -> int:
    payload = status_payload(target, scope=scope)
    if json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        state = "current" if payload["current"] else ("installed" if payload["installed"] else "not installed")
        print(f"claude hooks: {state}")
        if scope == "user":
            print(f"scope: {payload['scope']}")
        print(f"target: {payload['target']}")
        print(f"events: {', '.join(payload['managed_events']) or '(none)'}")
        if payload["missing_events"]:
            print(f"missing: {', '.join(payload['missing_events'])}")
        if payload.get("stale_events"):
            print(f"stale: {', '.join(payload['stale_events'])}")
        if payload.get("script_path"):
            script_state = (
                "current"
                if payload.get("script_current")
                else ("installed" if payload.get("script_installed") else "missing")
            )
            print(f"script: {script_state} ({payload['script_path']})")
        print(f"foreign_handlers: {payload.get('foreign_handler_count', 0)}")
        print(f"legacy_handlers: {payload.get('legacy_handler_count', 0)}")
        if payload.get("duplicate_handler_count"):
            print(f"duplicate_handlers: {payload['duplicate_handler_count']}")
        if payload.get("scope_widened"):
            print("warning: project settings path is Claude Code user settings; use --scope user")
        if payload.get("pinned_target"):
            print(f"pinned_target: {payload['pinned_target']}")
        if payload.get("error"):
            print(f"error: {payload['error']}")
    return 2 if payload.get("error") else 0


def hooks_uninstall(*, target: Path, scope: HooksScope = "project", quiet: bool = False) -> int:
    layout = _layout(scope=scope, target=target)
    settings_path = layout.settings_path
    settings, error = _load_settings(settings_path)
    if settings is None:
        if not quiet:
            print(f"error: {error}", file=sys.stderr)
        return 2
    cleaned = _remove_settings(settings, layout=layout)
    if settings_path.exists() or cleaned:
        localio.write_json(settings_path, cleaned)
    if layout.script_path is not None and layout.script_path.is_file():
        sidecar = _sidecar(layout) or {}
        recorded = sidecar.get("script_digest")
        if recorded is None or localio.file_sha256(layout.script_path) == recorded:
            layout.script_path.unlink(missing_ok=True)
    layout.sidecar_path.unlink(missing_ok=True)
    if not quiet:
        location = str(layout.root) if scope == "project" else f"{layout.root} ({scope})"
        print(f"claude hooks: uninstalled {PACKAGE_ID} -> {location}")
    return 0
