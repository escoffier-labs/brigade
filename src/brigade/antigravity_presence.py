"""Project-scoped Antigravity hooks with silent, detached Fleet publication."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .localio import write_json
from .wiring import resolve_wired_target

HOOK_NAME = "brigade-presence"
HOOK_PATH = Path(".agents/hooks.json")
# agy-customizations/docs/hooks.md documents {} for PostToolUse, optional
# message objects for SessionStart/PreInvocation, and Stop decisions other than
# "continue" allowing the agent to stop. Live agy 1.4.3 denied tools on JSON
# output, so these observational hooks keep stdout empty and always exit 0.
# Never register PreToolUse: its required decision can block tools.
_EVENT_MAP = {"PreInvocation": "start", "PostToolUse": "heartbeat", "PostInvocation": "heartbeat", "Stop": "heartbeat"}


def _managed_hook() -> dict[str, object]:
    managed: dict[str, object] = {}
    for event, mapped in _EVENT_MAP.items():
        handler = {
            "type": "command",
            "command": f"brigade work presence-hook --harness antigravity --stdin --event {mapped}",
        }
        managed[event] = [{"matcher": "*", "hooks": [handler]}] if event == "PostToolUse" else [handler]
    return managed


def _read_hooks(path: Path) -> dict[str, object]:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Antigravity hook configuration must not be a symlink")
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(doc, dict):
        raise ValueError("Antigravity hook configuration must be an object")
    return doc


def install_hooks(target: Path) -> None:
    """Merge our named hook, preserving foreign entries and edited configurations."""
    path = target / HOOK_PATH
    doc = _read_hooks(path)
    managed = _managed_hook()
    if HOOK_NAME in doc:
        if doc[HOOK_NAME] != managed:
            raise ValueError("existing brigade-presence hook differs; preserved for review")
        return
    doc[HOOK_NAME] = managed
    write_json(path, doc)


def uninstall_hooks(target: Path) -> None:
    """Remove only the unedited managed definition; keep the co-owned file."""
    path = target / HOOK_PATH
    doc = _read_hooks(path)
    if HOOK_NAME not in doc:
        return
    if doc[HOOK_NAME] != _managed_hook():
        raise ValueError("existing brigade-presence hook differs; preserved for review")
    del doc[HOOK_NAME]
    write_json(path, doc)


def run_hook(event: str | None) -> None:
    """Read agy's camelCase identity fields; never publish in the hook process.

    agy does not include an event name in stdin. Installed handlers supply the
    normalized event. Stop refreshes presence; abandoned sessions expire by TTL.
    """
    from .fleet_client import _session_admin_write_node_id, load_fleet_settings
    from .fleet_session_presence import _read_bounded_json_object

    payload = _read_bounded_json_object()
    if payload is None or event not in {"start", "heartbeat"}:
        return
    settings = load_fleet_settings()
    if not settings["hub_url"] or not (settings["node_token"] or settings["admin_token"]):
        return
    if not settings["node_token"] and _session_admin_write_node_id()[1] is not None:
        return
    session = payload.get("conversationId")
    roots = payload.get("workspacePaths")
    if not isinstance(session, str) or not session or len(session) > 256:
        return
    if any(ord(ch) < 0x20 for ch in session):
        return
    if not isinstance(roots, list):
        return
    home = Path.home().resolve()
    seen: set[Path] = set()
    for root in roots:
        if not isinstance(root, str) or not root:
            continue
        try:
            workspace = Path(root)
            if not workspace.is_absolute() or workspace.resolve() == home:
                continue
            target = resolve_wired_target(str(workspace), harness="antigravity")
            if target is None:
                continue
            target = target.resolve()
            if target == home or target in seen:
                continue
            seen.add(target)
            _spawn_publisher(target, session, event)
        except (OSError, RuntimeError, ValueError):
            # One invalid workspace or failed spawn must not hide the others.
            continue


def _spawn_publisher(target: Path, session: str, event: str) -> None:
    """Launch one publisher without waiting or sharing agy's standard streams."""
    argv = [
        sys.executable,
        "-m",
        "brigade",
        "work",
        "presence-hook",
        "--harness",
        "antigravity",
        "--target",
        str(target),
        "--session",
        session,
        "--event",
        event,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(  # Do not wait: the Hub must never block agy or inherit its pipes.
        argv,
        cwd=Path(__file__).resolve().parent,  # Keep the workspace off Python's implicit import path.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
