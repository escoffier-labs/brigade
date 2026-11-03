"""Work-loop mode and safety-rails settings (#2233).

Two independent settings live under ``work`` in ``.brigade/config.json``:

- ``work.mode``: ``enforced`` (default) keeps the verification loop: hooks
  route raw checks through ``brigade work verify run`` and the Stop gate wants
  a receipt. ``yolo`` keeps shared context (brief, recall, Fleet Hub messages,
  handoffs on request) and drops verification enforcement.
- ``work.rails``: ``on`` (default) keeps the content guard, secret and
  credential scanning, and pre-push publication checks. ``off`` skips them in
  the managed hook paths. Guard policies and baselines are never touched.

Every hook adapter reads the settings through this module. Readers fail safe:
an unreadable or invalid config resolves to ``enforced`` with rails ``on``.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any

from .config import (
    CONFIG_REL_PATH,
    DEFAULT_WORK_MODE,
    LEGACY_WORKSPACE_DIRNAMES,
    WORK_MODES,
    load_config,
    validate_safety_rails,
    validate_work_mode,
    write_config,
)

MODE_ENFORCED = "enforced"
MODE_YOLO = "yolo"
RAILS_STATES = ("on", "off")
# Exit status of `brigade work rails check` when rails are off for the target.
RAILS_OFF_EXIT = 3
# Claude hook events whose only job is verification enforcement. Project-scope
# installs in yolo mode leave them out; the runtime also skips their checks so
# user-scope hooks shared across repos behave the same way.
YOLO_DROPPED_CLAUDE_EVENTS = ("PostToolUseFailure", "Stop")

RAILS_OFF_WARNING = (
    "warning: safety rails are off for {target}. Brigade's pre-push hook no longer scans for "
    "secrets, credentials, private hosts, or private paths before a push, and the history scan "
    "of pushed commits is skipped. Guard policies and baselines are unchanged; "
    "`brigade work rails on` restores the previous checks."
)


def _work_settings(target: Path) -> dict[str, Any]:
    candidates = [target / CONFIG_REL_PATH, *(target / legacy / "config.json" for legacy in LEGACY_WORKSPACE_DIRNAMES)]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        work = data.get("work") if isinstance(data, dict) else None
        return work if isinstance(work, dict) else {}
    return {}


def resolve_mode(target: Path) -> str:
    """Return the persisted work mode, or ``enforced`` when unset or unreadable."""
    try:
        return validate_work_mode(_work_settings(target).get("mode", DEFAULT_WORK_MODE))
    except ValueError:
        return MODE_ENFORCED


def is_yolo(target: Path) -> bool:
    return resolve_mode(target) == MODE_YOLO


def _config_is_tracked(target: Path) -> bool:
    """True when git tracks the config, or when that cannot be ruled out."""
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "ls-files", "--error-unmatch", "--", CONFIG_REL_PATH],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode == 0:
        return True
    # 1 means untracked inside a work tree; 128 means no repository at all.
    return result.returncode not in {1, 128}


def rails_off_requested(target: Path) -> bool:
    try:
        return not validate_safety_rails(_work_settings(target).get("rails", "on"))
    except ValueError:
        return False


def rails_enabled(target: Path) -> bool:
    """Return False only for an explicit, machine-local ``work.rails: off``.

    A committed config cannot turn the rails off: a pushed commit must never be
    able to disable the leak checks of everyone who later pushes from a clone.
    """
    return not rails_off_requested(target) or _config_is_tracked(target)


def summary(target: Path) -> dict[str, str]:
    return {"mode": resolve_mode(target), "rails": "on" if rails_enabled(target) else "off"}


def claude_project_events(target: Path, events: tuple[str, ...]) -> tuple[str, ...]:
    """Managed Claude events a project-scope install should carry for ``target``."""
    if not is_yolo(target):
        return events
    return tuple(event for event in events if event not in YOLO_DROPPED_CLAUDE_EVENTS)


def _update(target: Path, **changes: Any) -> str | None:
    try:
        cfg = load_config(target)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"unable to load Brigade config: {exc}"
    if cfg is None:
        return f"target is not Brigade-wired (no {CONFIG_REL_PATH}): {target}; run brigade init first"
    write_config(target, dataclasses.replace(cfg, **changes))
    return None


def set_mode(target: Path, mode: str) -> str | None:
    """Persist ``mode``; return an error string or None."""
    if mode not in WORK_MODES:
        return f"mode must be one of: {', '.join(WORK_MODES)}"
    return _update(target, work_mode=mode)


def set_rails(target: Path, state: str) -> str | None:
    """Persist ``state`` (``on`` or ``off``); return an error string or None."""
    if state not in RAILS_STATES:
        return "rails must be on or off"
    return _update(target, safety_rails=state == "on")


RAILS_HOOK_MARKER = "brigade work rails check"
TRACKED_CONFIG_NOTE = (
    "on: work.rails is off, but git tracks .brigade/config.json, so the checks stay on; "
    "a committed setting cannot disable them"
)


def pre_push_hook_gated(target: Path) -> bool | None:
    """Whether ``hooks/pre-push`` consults the rails setting; None when absent."""
    try:
        return RAILS_HOOK_MARKER in (target / "hooks" / "pre-push").read_text(encoding="utf-8")
    except OSError:
        return None


def doctor_checks(target: Path) -> list[tuple[str, str, str]]:
    """Doctor rows that state the mode and rails plainly."""
    mode = resolve_mode(target)
    if mode == MODE_YOLO:
        mode_detail = (
            "yolo: shared context stays on (brief, recall, Fleet Hub, handoffs on request); "
            "verification is not enforced and missing receipts are not reported; "
            "`brigade work mode set enforced` restores the loop"
        )
    else:
        mode_detail = "enforced: checks route through `brigade work verify run` and the Stop gate wants a receipt"
    checks = [("INFO", "work mode", mode_detail)]
    if rails_enabled(target):
        if rails_off_requested(target):
            checks.append(("WARN", "safety rails", TRACKED_CONFIG_NOTE))
        else:
            checks.append(("INFO", "safety rails", "on: content guard and pre-push publication checks are active"))
        return checks
    detail = (
        "off: the pre-push hook does not scan for secrets, credentials, private hosts, or private paths; "
        "`brigade work rails on` restores it"
    )
    if pre_push_hook_gated(target) is False:
        detail += (
            "; the installed hooks/pre-push predates the rails switch and still scans, "
            "refresh it from the current Brigade template to honor rails off"
        )
    checks.append(("WARN", "safety rails", detail))
    return checks
