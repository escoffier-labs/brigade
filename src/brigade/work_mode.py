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
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .config import (
    CONFIG_REL_PATH,
    DEFAULT_WORK_MODE,
    LEGACY_WORKSPACE_DIRNAMES,
    SUPPORTED_VERSIONS,
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

RAILS_OFF_WARNING = (
    "warning: safety rails are off for {target}. Brigade's pre-push hook no longer scans for "
    "secrets, credentials, private hosts, or private paths before a push, and the history scan "
    "of pushed commits is skipped. Guard policies and baselines are unchanged; "
    "`brigade work rails on` restores the previous checks."
)


def _config_candidates(target: Path) -> list[Path]:
    return [target / CONFIG_REL_PATH, *(target / legacy / "config.json" for legacy in LEGACY_WORKSPACE_DIRNAMES)]


def _work_settings_with_source(target: Path) -> tuple[dict[str, Any], Path | None]:
    """The ``work`` settings and the config file that supplied them (the first that exists)."""
    for path in _config_candidates(target):
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, path
        # Match load_config: a config with an unsupported version is invalid, so its
        # settings must not switch anything off (enforced, rails on).
        if not isinstance(data, dict) or data.get("version") not in SUPPORTED_VERSIONS:
            return {}, path
        work = data.get("work")
        return (work if isinstance(work, dict) else {}), path
    return {}, None


def _work_settings(target: Path) -> dict[str, Any]:
    return _work_settings_with_source(target)[0]


def resolve_mode(target: Path) -> str:
    """Return the persisted work mode, or ``enforced`` when unset or unreadable."""
    try:
        return validate_work_mode(_work_settings(target).get("mode", DEFAULT_WORK_MODE))
    except ValueError:
        return MODE_ENFORCED


def is_yolo(target: Path) -> bool:
    return resolve_mode(target) == MODE_YOLO


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=5, check=False)


def _config_is_tracked(target: Path, path: Path | None = None) -> bool:
    """True when rails-off cannot be honored for the config at ``path``.

    Rails off counts only for a plain, machine-local, untracked file. This returns
    True (rails stay on) when git tracks the config, when git cannot say, and when
    the file is reached through a symlink: the path itself or any parent directory
    inside the target is a symlink, or it resolves outside the target. A committed
    ``.brigade`` symlink to a tracked directory would otherwise let a pushed commit
    disable the checks for every clone. It also returns True when the outer index
    tracks the file or has an entry at an ancestor path inside the target, such as
    a submodule gitlink, and when the file sits inside a nested repository or
    submodule work tree (an ancestor below the target holds ``.git``): in a
    recursive clone a committed ``.brigade`` submodule appears as a gitlink, not
    as the file. ``path`` is the file that actually supplied the settings, which
    may be a legacy location; it defaults to the canonical config.
    """
    config_file = target / CONFIG_REL_PATH if path is None else path
    try:
        relative_parts = config_file.relative_to(target).parts
    except ValueError:
        return True
    probe = target
    try:
        for index, part in enumerate(relative_parts):
            probe = probe / part
            if probe.is_symlink():
                return True
            # A directory between the target and the config with its own .git is a
            # nested repository or submodule; its content is repository-controlled.
            if index < len(relative_parts) - 1 and os.path.lexists(probe / ".git"):
                return True
        resolved_target = target.resolve()
        relative = config_file.resolve().relative_to(resolved_target).as_posix()
    except (OSError, RuntimeError, ValueError):
        return True
    ancestors = list(PurePosixPath(relative).parents)[:-1]  # drop "."
    try:
        result = _git(resolved_target, "ls-files", "--error-unmatch", "--", relative)
        if result.returncode == 128:
            # 128 is also "pathspec is in submodule"; only no repository at all is untracked.
            return _git(resolved_target, "rev-parse", "--is-inside-work-tree").returncode == 0
        if result.returncode != 1:
            return True  # tracked (0) or git could not say
        # An index entry at an ancestor path itself (a submodule gitlink, mode 160000,
        # or a committed file or symlink) means the outer repository controls what
        # sits there. Tracked siblings under a plain directory do not count.
        for ancestor in ancestors:
            staged = _git(resolved_target, "ls-files", "--stage", "-z", "--", ancestor.as_posix())
            if staged.returncode != 0:
                return True
            for entry in staged.stdout.split(b"\0"):
                if entry.partition(b"\t")[2] == ancestor.as_posix().encode():
                    return True
    except (OSError, subprocess.SubprocessError):
        return True
    return False


def _rails_off_source(target: Path) -> Path | None:
    """The config file that explicitly sets ``work.rails: off``, or None."""
    settings, source = _work_settings_with_source(target)
    try:
        off = not validate_safety_rails(settings.get("rails", "on"))
    except ValueError:
        return None
    return source if off else None


def rails_off_requested(target: Path) -> bool:
    return _rails_off_source(target) is not None


def rails_enabled(target: Path) -> bool:
    """Return False only for an explicit, machine-local ``work.rails: off``.

    A committed config cannot turn the rails off: a pushed commit must never be
    able to disable the leak checks of everyone who later pushes from a clone.
    """
    source = _rails_off_source(target)
    return source is None or _config_is_tracked(target, source)


def summary(target: Path) -> dict[str, str]:
    return {"mode": resolve_mode(target), "rails": "on" if rails_enabled(target) else "off"}


def _update(target: Path, **changes: Any) -> str | None:
    try:
        cfg = load_config(target)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"unable to load Brigade config: {exc}"
    if cfg is None:
        return f"target is not Brigade-wired (no {CONFIG_REL_PATH}): {target}; run brigade init first"
    try:
        write_config(target, dataclasses.replace(cfg, **changes))
    except OSError as exc:
        return f"unable to write Brigade config: {exc}"
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
    "on: work.rails is off, but git tracks .brigade/config.json (or it comes through a symlink, "
    "a submodule, or a nested repository), so the checks stay on; a committed setting cannot disable them"
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
    settings = _work_settings(target)
    for key, validate, fallback in (("mode", validate_work_mode, "enforced"), ("rails", validate_safety_rails, "on")):
        if key in settings:
            try:
                validate(settings[key])
            except ValueError:
                checks.append(("WARN", f"work.{key}", f"invalid value ignored; using the {fallback} default"))
    if rails_enabled(target):
        if rails_off_requested(target):
            checks.append(("WARN", "safety rails", TRACKED_CONFIG_NOTE))
        elif pre_push_hook_gated(target) is None:
            checks.append(
                (
                    "INFO",
                    "safety rails",
                    "on: content guard and pre-push publication checks run only when hooks/pre-push is "
                    "installed; none was found here",
                )
            )
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
