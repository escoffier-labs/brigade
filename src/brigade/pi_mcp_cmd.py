"""Install, discover, and call MCP tools for Pi through the Brigade bridge."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__, localio, mcp_cmd, pi_mcp_bridge
from .render import emit as _emit

STATE_VERSION = 1
EXTENSION_REL = "extensions/brigade-mcp-bridge.js"
CATALOG_PROJECTION_REL = "brigade/catalog-projection.json"
STATE_REL = "brigade/install-state.json"


def _home_dir() -> Path:
    return Path.home()


def _pi_agent_root() -> Path:
    return _home_dir().expanduser().resolve() / ".pi" / "agent"


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_value(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return _digest_text(rendered)


def _json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _extension_source() -> str:
    template = Path(__file__).parent / "templates" / "pi" / "extensions" / "brigade-mcp-bridge.js"
    return template.read_text()


def _catalog_projection(target: Path) -> dict[str, Any]:
    servers, errors, warnings = mcp_cmd.load_canonical(target)
    enabled = {name: server for name, server in servers.items() if server.enabled}
    errors.extend(pi_mcp_bridge.qualified_name_errors(enabled))
    return {
        "target": str(target.resolve()),
        "catalog_fingerprint": _digest_value(
            {
                name: {
                    "transport": server.transport,
                    "enabled": server.enabled,
                    "command": server.command,
                    "args": list(server.args),
                    "url": server.url,
                    "timeout": server.timeout,
                }
                for name, server in sorted(enabled.items())
            }
        ),
        "servers": [
            {
                "name": name,
                "transport": server.transport,
                "enabled": server.enabled,
            }
            for name, server in sorted(enabled.items())
        ],
        "errors": errors,
        "warnings": warnings,
    }


def _desired_files(root: Path, target: Path) -> dict[Path, tuple[str, str]]:
    return {
        root / EXTENSION_REL: (_extension_source(), "extension"),
        root / CATALOG_PROJECTION_REL: (_json_text(_catalog_projection(target)), "catalog-projection"),
    }


def _state_path(root: Path) -> Path:
    return root / STATE_REL


def _load_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    empty: dict[str, Any] = {"version": STATE_VERSION, "files": {}}
    if not path.exists():
        return empty
    payload = localio.read_json_dict(path)
    if payload is None or payload.get("version") != STATE_VERSION or not isinstance(payload.get("files"), dict):
        empty["_read_error"] = "ownership state is unreadable"
        return empty
    return payload


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _file_item(
    root: Path,
    path: Path,
    text: str,
    surface: str,
    state: dict[str, Any],
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
        except (OSError, UnicodeError) as exc:
            return {
                "surface": surface,
                "path": str(path),
                "status": "conflict",
                "action": "skip",
                "detail": str(exc),
            }
        if live == desired:
            status, action = "current", "none"
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


def _public(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if key != "desired_fingerprint"} for item in items]


def discover(*, target: Path, timeout: float | None = None, json_output: bool = False) -> int:
    payload = pi_mcp_bridge.discover_tools(target, timeout=timeout)
    lines = [f"pi mcp bridge: discovered {len(payload.get('tools', []))} tool(s)"]
    return _emit(payload, json_output, lines, 0 if not payload.get("errors") else 1)


def call(
    *,
    target: Path,
    tool: str,
    args_json: str,
    timeout: float | None = None,
    json_output: bool = False,
) -> int:
    try:
        arguments = json.loads(args_json or "{}")
    except json.JSONDecodeError as exc:
        payload = {
            "error": True,
            "qualified_name": tool,
            "message": f"invalid --args-json: {exc.msg}",
            "failure_class": "invalid_arguments",
        }
        return _emit(payload, json_output, [f"error: {payload['message']}"], 2)
    if not isinstance(arguments, dict):
        payload = {
            "error": True,
            "qualified_name": tool,
            "message": "--args-json must decode to a JSON object",
            "failure_class": "invalid_arguments",
        }
        return _emit(payload, json_output, [f"error: {payload['message']}"], 2)
    payload = pi_mcp_bridge.call_qualified_tool(target, tool, arguments, timeout=timeout)
    lines = [f"pi mcp bridge call: {tool}"]
    if payload.get("error"):
        lines.append(f"error: {payload.get('message')}")
    return _emit(payload, json_output, lines, 1 if payload.get("error") else 0)


def install(*, target: Path, write: bool = False, json_output: bool = False) -> int:
    root = _pi_agent_root()
    state = _load_state(root)
    desired_files = _desired_files(root, target)
    items = [_file_item(root, path, text, surface, state) for path, (text, surface) in desired_files.items()]
    catalog_errors = _catalog_projection(target).get("errors") or []
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
    if write and not state_error and not catalog_errors:
        next_state: dict[str, Any] = {
            "version": STATE_VERSION,
            "package_version": __version__,
            "target": str(target.resolve()),
            "files": {},
        }
        for path, (text, _surface) in desired_files.items():
            item = next(item for item in items if item["path"] == str(path))
            if item["action"] in {"create", "update"}:
                localio.write_text_atomic(path, text)
                files_written.append(str(path))
            if item["status"] != "conflict":
                next_state["files"][_relative(root, path)] = item["desired_fingerprint"]
        state_path = _state_path(root)
        if next_state != state:
            localio.write_json(state_path, next_state)
            files_written.append(str(state_path))

    conflicts = [item for item in items if item["status"] == "conflict"]
    payload = {
        "schema_version": 1,
        "operation": "install",
        "harness": "pi",
        "scope": "user",
        "home": str(_home_dir().expanduser().resolve()),
        "target": str(target.resolve()),
        "write": write,
        "ready": not conflicts and not catalog_errors,
        "reload_required": bool(write and files_written) or not write,
        "files_written": files_written,
        "items": _public(items),
        "conflicts": _public(conflicts),
        "catalog_errors": catalog_errors,
    }
    return _emit(
        payload,
        json_output,
        [f"pi mcp bridge install: ready={payload['ready']}"],
        1 if conflicts or catalog_errors else 0,
    )


def uninstall(*, write: bool = False, json_output: bool = False) -> int:
    root = _pi_agent_root()
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

    for rel, fingerprint in sorted(state.get("files", {}).items()):
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

    if write and not conflicts:
        _state_path(root).unlink(missing_ok=True)
        for current in (root / "brigade", root / "extensions"):
            if current.is_dir():
                try:
                    current.rmdir()
                except OSError:
                    pass

    payload = {
        "schema_version": 1,
        "operation": "uninstall",
        "harness": "pi",
        "scope": "user",
        "write": write,
        "files_removed": files_removed,
        "items": items,
        "conflicts": conflicts,
        "reload_required": bool(write and files_removed),
    }
    return _emit(
        payload,
        json_output,
        [f"pi mcp bridge uninstall: removed {len(files_removed)} file(s)"],
        1 if conflicts else 0,
    )
