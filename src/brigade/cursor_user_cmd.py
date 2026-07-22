"""Safe user-scoped Cursor onboarding for the Brigade work loop."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__, component_bins

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
    # Engine servers resolve to the managed absolute path from `brigade setup`
    # so the generated config does not depend on Cursor's spawn-time PATH. The
    # bare name is kept only as a pre-setup fallback.
    def _absolute(resolved: str | None, fallback: str) -> str:
        return str(Path(resolved).expanduser().resolve()) if resolved else fallback

    return {
        "brigade": {
            "command": "brigade",
            "args": ["memory", "serve-mcp", "--stdio", "--target", "."],
            "timeout": 60,
        },
        "graphtrail": {"command": _absolute(component_bins.resolve("graphtrail-mcp"), "graphtrail-mcp")},
        "miseledger": {
            "command": _absolute(component_bins.resolve("miseledger"), "miseledger"),
            "args": ["mcp"],
            "timeout": 60,
        },
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
    """Legacy wrapper over the aggregate ``harness_profile_cmd.install`` for Cursor.

    Delegates with ``harness="cursor"`` and ``workspace=Path.cwd()`` so the
    shared instruction/skill/generated orchestration owns every surface. The
    aggregate payload (``results`` list, top-level ``reload_required``) is
    printed by the aggregate emitter; this wrapper only forwards the exit code.
    """
    from . import harness_profile_cmd

    return harness_profile_cmd.install(
        harness="cursor",
        workspace=Path.cwd(),
        write=write,
        allow_global_stdio=False,
        adopt=False,
        json_output=json_output,
        home=_home_dir(),
    )


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
    """Legacy wrapper over the aggregate ``harness_profile_cmd.uninstall`` for Cursor."""
    from . import harness_profile_cmd

    return harness_profile_cmd.uninstall(
        harness="cursor",
        workspace=Path.cwd(),
        write=write,
        json_output=json_output,
        home=_home_dir(),
    )


def _check(check_id: str, ok: bool, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": "OK" if ok else "FAIL", "detail": detail}


def doctor(*, json_output: bool = False) -> int:
    """Legacy wrapper over the aggregate ``harness_profile_cmd.doctor`` for Cursor."""
    from . import harness_profile_cmd

    return harness_profile_cmd.doctor(
        harness="cursor",
        workspace=Path.cwd(),
        verify_mcp=False,
        json_output=json_output,
        home=_home_dir(),
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return ""


def _file_matches(path: Path, expected: str) -> bool:
    return path.is_file() and _read_text(path) == expected
