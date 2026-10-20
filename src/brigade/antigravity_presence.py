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
_EVENT_MAP = {"PreInvocation": "start", "PostToolUse": "heartbeat", "PostInvocation": "heartbeat", "Stop": "heartbeat"}


def install_hooks(target: Path) -> None:
    """Merge our named hook, preserving foreign entries and edited configurations."""
    path = target / HOOK_PATH
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Antigravity hook configuration must not be a symlink")
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(doc, dict):
        raise ValueError("Antigravity hook configuration must be an object")
    managed: dict[str, object] = {}
    for event, mapped in _EVENT_MAP.items():
        handler = {
            "type": "command",
            "command": f"brigade work presence-hook --harness antigravity --stdin --event {mapped}",
        }
        managed[event] = [{"matcher": "*", "hooks": [handler]}] if event == "PostToolUse" else [handler]
    if HOOK_NAME in doc:
        if doc[HOOK_NAME] != managed:
            raise ValueError("existing brigade-presence hook differs; preserved for review")
        return
    doc[HOOK_NAME] = managed
    write_json(path, doc)


def run_hook(event: str | None) -> None:
    """Read agy's camelCase identity fields; never publish in the hook process.

    agy does not include an event name in stdin. Installed handlers supply the
    normalized event. Stop refreshes presence; abandoned sessions expire by TTL.
    """
    from .fleet_session_presence import _read_bounded_json_object

    payload = _read_bounded_json_object()
    if payload is None or event not in {"start", "heartbeat"}:
        return
    session = payload.get("conversationId")
    roots = payload.get("workspacePaths")
    if not isinstance(session, str) or not session or len(session) > 256:
        return
    if any(ord(ch) < 0x20 for ch in session):
        return
    if not isinstance(roots, list) or not roots or not isinstance(roots[0], str) or not roots[0]:
        return
    workspace = Path(roots[0])
    if not workspace.is_absolute():
        return
    home = Path.home().resolve()
    if workspace.resolve() == home:
        return
    target = resolve_wired_target(str(workspace), harness="antigravity")
    if target is None or target.resolve() == home:
        return
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
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
