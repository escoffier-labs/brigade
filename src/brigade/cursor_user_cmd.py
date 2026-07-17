"""Safe user-scoped Cursor onboarding for the Brigade work loop."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__, localio

STATE_VERSION = 1
MANAGED_MCP_NAMES = ("brigade", "graphtrail", "miseledger")


def _home_dir() -> Path:
    return Path.home()


def _cursor_root() -> Path:
    return _home_dir().expanduser().resolve() / ".cursor"


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_value(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return _digest_text(rendered)


def _json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _coowned_json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _mcp_servers() -> dict[str, dict[str, Any]]:
    return {
        "brigade": {
            "command": "brigade",
            "args": ["memory", "serve-mcp", "--stdio", "--target", "."],
            "timeout": 60,
        },
        "graphtrail": {"command": "graphtrail-mcp"},
        "miseledger": {"command": "miseledger", "args": ["mcp"], "timeout": 60},
    }


def _plugin_manifest() -> str:
    return _json_text(
        {
            "name": "brigade-loop",
            "displayName": "Brigade Work Loop",
            "version": __version__,
            "description": "Applies the Brigade, GraphTrail, and MiseLedger evidence loop to Cursor agent workspaces.",
            "author": {"name": "Escoffier Labs"},
            "license": "MIT",
            "keywords": ["brigade", "graphtrail", "miseledger", "verification", "receipts"],
            "category": "developer-tools",
            "rules": "./rules/",
        }
    )


def _rule_text() -> str:
    return """---
alwaysApply: true
---

# Brigade work loop

In a Brigade-wired repository, invoke the global `brigade-work` skill before substantive work. Start with `brigade work brief --target .`. Run checks that should count through `brigade work verify run --target . --command "<test command>" --capture brigade-work`. Capture failures too, export new receipts to MiseLedger when it is installed, and finish durable work with a Memory Handoff.
"""


def _hook_text() -> str:
    context = (
        "BRIGADE WORK LOOP: In a Brigade-wired repository, invoke the global brigade-work skill before substantive "
        "work. Start with brigade work brief --target .. Run checks that should count through brigade work verify "
        "run with --capture brigade-work. Capture failures, export new receipts to MiseLedger when installed, and "
        "finish durable work with a Memory Handoff."
    )
    return (
        "#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' "
        + _shell_single_quote(_json_text({"additional_context": context}).strip())
        + "\n"
    )


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _desired_files(root: Path) -> dict[Path, tuple[str, bool, str]]:
    package = Path(__file__).parent / "templates" / "skills" / "brigade-work"
    plugin = root / "plugins" / "local" / "brigade-loop"
    skill = root / "skills" / "brigade-work"
    desired: dict[Path, tuple[str, bool, str]] = {
        plugin / ".cursor-plugin" / "plugin.json": (_plugin_manifest(), False, "plugin"),
        plugin / "rules" / "brigade-loop.mdc": (_rule_text(), False, "rule"),
        root / "hooks" / "brigade-session-start": (_hook_text(), True, "hook"),
        root / "brigade" / "mcp.json": (
            _json_text({"version": 1, "servers": _mcp_servers()}),
            False,
            "mcp-catalog",
        ),
    }
    for name in ("SKILL.md", "skill.json", "CHANGELOG.md"):
        desired[skill / name] = ((package / name).read_text(), False, "skill")
    return desired


def _state_path(root: Path) -> Path:
    return root / "brigade" / "install-state.json"


def _load_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    empty: dict[str, Any] = {"version": STATE_VERSION, "files": {}, "hooks": {}, "mcp": {}}
    if not path.exists():
        return empty
    payload, error = _read_json_object(path)
    if payload is None:
        empty["_read_error"] = error or "ownership state is unreadable"
        return empty
    if payload.get("version") != STATE_VERSION:
        empty["_read_error"] = f"unsupported ownership state version: {payload.get('version')!r}"
        return empty
    for key in ("files", "hooks", "mcp"):
        if not isinstance(payload.get(key), dict):
            payload[key] = {}
    return payload


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _file_item(
    root: Path, path: Path, text: str, executable: bool, surface: str, state: dict[str, Any]
) -> dict[str, Any]:
    desired = _digest_text(text)
    stored = state["files"].get(_relative(root, path))
    if not path.exists():
        status, action = "missing", "create"
    elif not path.is_file():
        status, action = "conflict", "skip"
    else:
        try:
            live = _digest_text(path.read_text())
            mode_current = not executable or bool(path.stat().st_mode & 0o111)
        except (OSError, UnicodeError) as exc:
            return {
                "surface": surface,
                "path": str(path),
                "status": "conflict",
                "action": "skip",
                "detail": str(exc),
                "desired_fingerprint": desired,
            }
        if live == desired and mode_current:
            status, action = "current", "none"
        elif live == desired:
            status, action = "changed", "update"
        elif stored == live:
            status, action = "changed", "update"
        else:
            status, action = "conflict", "skip"
    return {
        "surface": surface,
        "path": str(path),
        "status": status,
        "action": action,
        "desired_fingerprint": desired,
    }


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(payload, dict):
        return None, "existing JSON configuration must be an object"
    return payload, None


def _hook_entry(root: Path) -> dict[str, str]:
    return {"command": str(root / "hooks" / "brigade-session-start")}


def _plan_hook(root: Path, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    path = root / "hooks.json"
    doc, error = _read_json_object(path)
    item: dict[str, Any] = {"surface": "hook-config", "path": str(path), "name": "sessionStart"}
    if doc is None:
        item.update(status="conflict", action="skip", detail=error or "could not read hooks configuration")
        return item, None, error
    hooks = doc.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        item.update(status="conflict", action="skip", detail="existing hooks field must be an object")
        return item, None, str(item["detail"])
    entries = hooks.get("sessionStart")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        item.update(status="conflict", action="skip", detail="existing sessionStart hooks must be a list")
        return item, None, str(item["detail"])
    desired = _hook_entry(root)
    desired_fp = _digest_value(desired)
    if desired in entries:
        item.update(status="current", action="none", desired_fingerprint=desired_fp)
    else:
        prior = state["hooks"].get("sessionStart")
        prior_index = next((index for index, entry in enumerate(entries) if _digest_value(entry) == prior), None)
        if prior_index is None:
            item.update(status="missing", action="create", desired_fingerprint=desired_fp)
        else:
            item.update(status="changed", action="update", desired_fingerprint=desired_fp, prior_index=prior_index)
    return item, doc, None


def _plan_mcp(root: Path, state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    path = root / "mcp.json"
    doc, error = _read_json_object(path)
    if doc is None:
        return (
            [{"surface": "mcp-config", "path": str(path), "status": "conflict", "action": "skip", "detail": error}],
            None,
            error,
        )
    servers = doc.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        detail = "existing mcpServers field must be an object"
        return (
            [{"surface": "mcp-config", "path": str(path), "status": "conflict", "action": "skip", "detail": detail}],
            None,
            detail,
        )
    items: list[dict[str, Any]] = []
    for name, desired in _mcp_servers().items():
        desired_fp = _digest_value(desired)
        live = servers.get(name)
        if live == desired:
            status, action = "current", "none"
        elif name not in servers:
            status, action = "missing", "create"
        elif state["mcp"].get(name) == _digest_value(live):
            status, action = "changed", "update"
        else:
            status, action = "conflict", "skip"
        items.append(
            {
                "surface": "mcp-config",
                "path": str(path),
                "name": name,
                "status": status,
                "action": action,
                "desired_fingerprint": desired_fp,
            }
        )
    return items, doc, None


def _public(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    private = {"desired_fingerprint", "prior_index"}
    return [{key: value for key, value in item.items() if key not in private} for item in items]


def _emit(payload: dict[str, Any], *, json_output: bool, rc: int) -> int:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        action = payload.get("operation", "cursor user profile")
        print(f"cursor user profile: {action}")
        for item in payload.get("items", []):
            name = f" [{item['name']}]" if item.get("name") else ""
            print(f"- {item['status']}: {item['path']}{name}")
        if payload.get("reload_required"):
            print("next: reload Cursor windows")
    return rc


def install(*, write: bool = False, json_output: bool = False) -> int:
    root = _cursor_root()
    state = _load_state(root)
    desired_files = _desired_files(root)
    items = [
        _file_item(root, path, text, executable, surface, state)
        for path, (text, executable, surface) in desired_files.items()
    ]
    hook_item, hooks_doc, _ = _plan_hook(root, state)
    mcp_items, mcp_doc, _ = _plan_mcp(root, state)
    items.extend([hook_item, *mcp_items])
    state_error = state.get("_read_error")
    if state_error:
        items.append(
            {
                "surface": "ownership-state",
                "path": str(_state_path(root)),
                "status": "conflict",
                "action": "skip",
                "detail": str(state_error),
            }
        )
    files_written: list[str] = []

    if write and not state_error:
        next_state: dict[str, Any] = {
            "version": STATE_VERSION,
            "package_version": __version__,
            "files": {},
            "hooks": {},
            "mcp": {},
        }
        for item in items:
            if item["surface"] in {"hook-config", "mcp-config"}:
                continue
            path = Path(item["path"])
            if item["action"] in {"create", "update"}:
                text, executable, _ = desired_files[path]
                localio.write_text_atomic(path, text)
                if executable:
                    path.chmod(path.stat().st_mode | 0o755)
                files_written.append(str(path))
            if item["status"] != "conflict":
                next_state["files"][_relative(root, path)] = item["desired_fingerprint"]
            elif _relative(root, path) in state["files"]:
                next_state["files"][_relative(root, path)] = state["files"][_relative(root, path)]

        if hooks_doc is not None and hook_item["status"] != "conflict":
            hooks = hooks_doc.setdefault("hooks", {})
            entries = hooks.setdefault("sessionStart", [])
            desired_hook = _hook_entry(root)
            if hook_item["action"] == "create":
                entries.append(desired_hook)
            elif hook_item["action"] == "update":
                entries[hook_item["prior_index"]] = desired_hook
            if hook_item["action"] != "none":
                path = root / "hooks.json"
                localio.write_text_atomic(path, _coowned_json_text(hooks_doc))
                files_written.append(str(path))
            next_state["hooks"]["sessionStart"] = hook_item["desired_fingerprint"]
        elif "sessionStart" in state["hooks"]:
            next_state["hooks"]["sessionStart"] = state["hooks"]["sessionStart"]

        if mcp_doc is not None:
            servers = mcp_doc.setdefault("mcpServers", {})
            changed = False
            desired_servers = _mcp_servers()
            for item in mcp_items:
                if item["status"] == "conflict":
                    if item.get("name") in state["mcp"]:
                        next_state["mcp"][item["name"]] = state["mcp"][item["name"]]
                    continue
                name = item["name"]
                if item["action"] in {"create", "update"}:
                    servers[name] = desired_servers[name]
                    changed = True
                next_state["mcp"][name] = item["desired_fingerprint"]
            if changed:
                path = root / "mcp.json"
                localio.write_text_atomic(path, _coowned_json_text(mcp_doc))
                files_written.append(str(path))
        else:
            next_state["mcp"].update(state["mcp"])

        state_path = _state_path(root)
        if next_state != state:
            localio.write_json(state_path, next_state)
            files_written.append(str(state_path))

    conflicts = [item for item in items if item["status"] == "conflict"]
    payload = {
        "schema_version": 1,
        "operation": "install",
        "harness": "cursor",
        "scope": "user",
        "home": str(_home_dir().expanduser().resolve()),
        "write": write,
        "ready": not conflicts,
        "reload_required": bool(write and files_written) or not write,
        "files_written": files_written,
        "items": _public(items),
        "conflicts": _public(conflicts),
    }
    return _emit(payload, json_output=json_output, rc=1 if conflicts else 0)


def _remove_owned_leaf_dirs(root: Path) -> None:
    plugin = root / "plugins" / "local" / "brigade-loop"
    for current in (
        plugin / ".cursor-plugin",
        plugin / "rules",
        plugin,
        root / "skills" / "brigade-work",
        root / "brigade",
    ):
        if not current.is_dir():
            continue
        try:
            current.rmdir()
        except OSError:
            continue


def uninstall(*, write: bool = False, json_output: bool = False) -> int:
    root = _cursor_root()
    state = _load_state(root)
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    files_removed: list[str] = []
    state_error = state.get("_read_error")
    if state_error:
        state_item = {
            "surface": "ownership-state",
            "path": str(_state_path(root)),
            "status": "conflict",
            "action": "preserve",
            "detail": str(state_error),
        }
        items.append(state_item)
        conflicts.append(state_item)

    for rel, fingerprint in sorted(state["files"].items()):
        path = root / rel
        item = {"surface": "owned-file", "path": str(path)}
        if not path.exists():
            item.update(status="absent", action="none")
        elif path.is_file():
            try:
                live_fingerprint = _digest_text(path.read_text())
            except (OSError, UnicodeError) as exc:
                item.update(status="conflict", action="preserve", detail=str(exc))
                conflicts.append(item)
            else:
                if live_fingerprint == fingerprint:
                    item.update(status="managed", action="remove")
                    if write:
                        path.unlink()
                        files_removed.append(str(path))
                else:
                    item.update(status="conflict", action="preserve")
                    conflicts.append(item)
        else:
            item.update(status="conflict", action="preserve")
            conflicts.append(item)
        items.append(item)

    hook_path = root / "hooks.json"
    hook_fp = state["hooks"].get("sessionStart")
    hook_removed = False
    if hook_fp:
        doc, error = _read_json_object(hook_path)
        hook_item: dict[str, Any] = {"surface": "hook-config", "path": str(hook_path), "name": "sessionStart"}
        entries = None
        if doc is not None and isinstance(doc.get("hooks"), dict):
            entries = doc["hooks"].get("sessionStart", [])
        index = (
            next((i for i, entry in enumerate(entries) if _digest_value(entry) == hook_fp), None)
            if isinstance(entries, list)
            else None
        )
        if index is not None:
            hook_item.update(status="managed", action="remove")
            if write:
                assert doc is not None and isinstance(entries, list)
                entries.pop(index)
                if not entries:
                    doc["hooks"].pop("sessionStart", None)
                localio.write_text_atomic(hook_path, _coowned_json_text(doc))
                hook_removed = True
        elif not hook_path.exists():
            hook_item.update(status="absent", action="none")
        else:
            hook_item.update(status="conflict", action="preserve", detail=error or "managed hook was edited or removed")
            conflicts.append(hook_item)
        items.append(hook_item)

    mcp_path = root / "mcp.json"
    mcp_doc, mcp_error = _read_json_object(mcp_path)
    mcp_servers = mcp_doc.get("mcpServers") if mcp_doc is not None else None
    mcp_config_absent = not mcp_path.exists() or (mcp_doc is not None and "mcpServers" not in mcp_doc)
    mcp_changed = False
    for name, fingerprint in sorted(state["mcp"].items()):
        item = {"surface": "mcp-config", "path": str(mcp_path), "name": name}
        if isinstance(mcp_servers, dict) and name in mcp_servers and _digest_value(mcp_servers[name]) == fingerprint:
            item.update(status="managed", action="remove")
            if write:
                mcp_servers.pop(name)
                mcp_changed = True
        elif mcp_config_absent or (isinstance(mcp_servers, dict) and name not in mcp_servers):
            item.update(status="absent", action="none")
        else:
            item.update(status="conflict", action="preserve", detail=mcp_error or "managed server was edited")
            conflicts.append(item)
        items.append(item)
    if write and mcp_changed and mcp_doc is not None:
        localio.write_text_atomic(mcp_path, _coowned_json_text(mcp_doc))

    if write and not conflicts:
        state_path = _state_path(root)
        state_path.unlink(missing_ok=True)
        _remove_owned_leaf_dirs(root)

    payload = {
        "schema_version": 1,
        "operation": "uninstall",
        "harness": "cursor",
        "scope": "user",
        "write": write,
        "files_removed": files_removed,
        "items": items,
        "conflicts": conflicts,
        "reload_required": bool(write and (files_removed or mcp_changed or hook_removed)),
    }
    return _emit(payload, json_output=json_output, rc=1 if conflicts else 0)


def _check(check_id: str, ok: bool, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": "OK" if ok else "FAIL", "detail": detail}


def doctor(*, json_output: bool = False) -> int:
    root = _cursor_root()
    desired = _desired_files(root)
    rule = root / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"
    manifest = root / "plugins" / "local" / "brigade-loop" / ".cursor-plugin" / "plugin.json"
    skill_dir = root / "skills" / "brigade-work"
    hook_script = root / "hooks" / "brigade-session-start"
    hooks_doc, _ = _read_json_object(root / "hooks.json")
    hook_entries: object = None
    if hooks_doc is not None and isinstance(hooks_doc.get("hooks"), dict):
        hook_entries = hooks_doc["hooks"].get("sessionStart")
    mcp_doc, _ = _read_json_object(root / "mcp.json")
    live_servers = mcp_doc.get("mcpServers") if mcp_doc is not None else None
    checks = [
        _check(
            "plugin-current",
            _file_matches(manifest, desired[manifest][0]),
            str(manifest),
        ),
        _check(
            "rule-always-applied",
            _file_matches(rule, desired[rule][0]) and "alwaysApply: true" in _read_text(rule),
            str(rule),
        ),
        _check(
            "skill-current",
            all(_file_matches(path, desired[path][0]) for path in desired if path.parent == skill_dir),
            str(skill_dir),
        ),
        _check(
            "session-hook",
            _file_matches(hook_script, desired[hook_script][0])
            and bool(hook_script.stat().st_mode & 0o111)
            and isinstance(hook_entries, list)
            and _hook_entry(root) in hook_entries,
            str(root / "hooks.json"),
        ),
    ]
    expected_servers = _mcp_servers()
    for name in MANAGED_MCP_NAMES:
        checks.append(
            _check(
                f"mcp-{name}",
                isinstance(live_servers, dict) and live_servers.get(name) == expected_servers[name],
                str(root / "mcp.json"),
            )
        )
    ready = all(item["status"] == "OK" for item in checks)
    payload = {
        "schema_version": 1,
        "operation": "doctor",
        "harness": "cursor",
        "scope": "user",
        "ready": ready,
        "checks": checks,
        "next": "reload Cursor windows" if ready else "run brigade harness install cursor --scope user --write",
    }
    return _emit(payload, json_output=json_output, rc=0 if ready else 1)


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return ""


def _file_matches(path: Path, expected: str) -> bool:
    return path.is_file() and _read_text(path) == expected
