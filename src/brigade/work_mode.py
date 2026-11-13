"""Work-loop mode and safety-rails settings (#2233).

The two settings are independent and live in different places:

- ``work.mode`` in ``.brigade/config.json``: ``strict`` (default) keeps the
  verification loop: hooks route raw checks through ``brigade work verify run``
  and the Stop gate wants a receipt. ``off`` keeps shared context (brief,
  recall, Fleet Hub messages, handoffs on request) and drops verification
  enforcement. Leak checks are unaffected, so a committed off setting is an
  acceptable repository choice.
- Safety rails: on by default. They keep the content guard, secret and
  credential scanning, and pre-push publication checks. The opt-out lives only
  in a per-user record outside every repository (``rails_record_path``), keyed
  by the canonical real path of the target. Nothing inside the repository can
  turn the rails off; an in-repo ``work.rails`` value is ignored and reported.
  Guard policies and baselines are never touched.

Every hook adapter reads the settings through this module. Readers fail safe:
an unreadable or invalid config resolves to ``strict``, and an unreadable or
invalid rails record leaves rails ``on``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import component_paths, localio
from .config import (
    CONFIG_REL_PATH,
    ABSENT_WORK_MODE,
    LEGACY_WORKSPACE_DIRNAMES,
    SUPPORTED_VERSIONS,
    WORK_MODES,
    load_config,
    validate_work_mode,
    write_config,
)

MODE_STRICT = "strict"
MODE_OBSERVE = "observe"
MODE_OFF = "off"
RAILS_STATES = ("on", "off")
# Exit status of `brigade work rails check` when rails are off for the target.
RAILS_OFF_EXIT = 3
RAILS_RECORD_NAME = "work-rails.json"
RAILS_RECORD_VERSION = 1

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
        # settings must not switch anything off (strict, rails on).
        if not isinstance(data, dict) or data.get("version") not in SUPPORTED_VERSIONS:
            return {}, path
        work = data.get("work")
        return (work if isinstance(work, dict) else {}), path
    return {}, None


def _work_settings(target: Path) -> dict[str, Any]:
    return _work_settings_with_source(target)[0]


def resolve_mode(target: Path) -> str:
    """Return the persisted work mode, or ``strict`` when unset or unreadable."""
    try:
        return validate_work_mode(_work_settings(target).get("mode", ABSENT_WORK_MODE))
    except ValueError:
        return MODE_STRICT


def is_strict(target: Path) -> bool:
    return resolve_mode(target) == MODE_STRICT


def is_observe(target: Path) -> bool:
    return resolve_mode(target) == MODE_OBSERVE


def pin_explicit_mode(target: Path) -> str | None:
    """Pin a readable, untracked keyless config without changing its behavior."""
    _, path = _work_settings_with_source(target)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") not in SUPPORTED_VERSIONS:
            return None
        work = data.get("work", {})
        if not isinstance(work, dict) or "mode" in work:
            return None
        tracked = subprocess.run(
            ["git", "-C", str(target), "ls-files", "--error-unmatch", path.relative_to(target).as_posix()],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if tracked.returncode == 0:
        return None
    mode = resolve_mode(target)
    return mode if _update(target, work_mode=mode) is None else None


def _case_insensitive_paths() -> bool:
    """Whether the platform's default filesystems compare paths case-insensitively."""
    return os.name == "nt" or sys.platform == "darwin"


def repository_key(target: Path) -> str:
    """Canonical key for ``target`` in the per-user rails record.

    The real path with every symlink resolved, so a symlinked path to the same
    repository shares one record. On Windows and macOS the key is also
    case-folded (``os.path.normcase`` plus ``casefold``, as skill pack paths
    compare), because their default filesystems ignore case.
    """
    real = os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(target))))
    return os.path.normcase(real).casefold() if _case_insensitive_paths() else real


def rails_record_path() -> Path | None:
    """The per-user rails record, outside every repository; None when no user config root exists.

    Linux: ``$XDG_CONFIG_HOME/brigade/work-rails.json`` (``~/.config`` by default).
    macOS: ``~/Library/Application Support/brigade/``. Windows: ``%LOCALAPPDATA%\\brigade\\``.
    """
    try:
        return Path(component_paths.config_root()) / "brigade" / RAILS_RECORD_NAME
    except ValueError:
        return None


def _inside(path: Path, target: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(target))
    except (OSError, ValueError):
        return False
    return True


def _enclosing_work_trees(target: Path) -> list[Path]:
    """``target`` and every ancestor that holds a ``.git`` entry, found without running git.

    A record inside any of these is content of the repository being pushed (or of
    a superproject that contains it), so it cannot be trusted to turn rails off.
    """
    try:
        current = Path(os.path.realpath(target))
    except OSError:
        return [target]
    trees = [current]
    for directory in (current, *current.parents):
        if (directory / ".git").exists() and directory not in trees:
            trees.append(directory)
    return trees


def _record_in_target_repository(record: Path, target: Path) -> bool:
    return any(_inside(record, tree) for tree in _enclosing_work_trees(target))


def _read_rails_off(record: Path) -> dict[str, Any]:
    """The ``rails_off`` map of the record; empty (rails on everywhere) when unreadable or invalid."""
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != RAILS_RECORD_VERSION:
        return {}
    entries = data.get("rails_off")
    return entries if isinstance(entries, dict) else {}


def rails_enabled(target: Path) -> bool:
    """Return False only when the per-user record turns rails off for this exact canonical path.

    Nothing inside the repository is read, so no committed file, symlink,
    submodule, or nested repository can disable the leak checks of anyone who
    pushes from a clone. A record that resolves inside ``target`` or inside any
    work tree that encloses it is ignored.
    """
    record = rails_record_path()
    if record is None or _record_in_target_repository(record, target):
        return True
    return repository_key(target) not in _read_rails_off(record)


def repo_rails_setting(target: Path) -> Path | None:
    """A config file inside ``target`` that sets ``work.rails``, which Brigade ignores."""
    for path in _config_candidates(target):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        work = data.get("work") if isinstance(data, dict) else None
        if isinstance(work, dict) and "rails" in work:
            return path
    return None


def ignored_repo_rails_note(target: Path) -> str | None:
    path = repo_rails_setting(target)
    if path is None:
        return None
    try:
        shown = path.relative_to(target).as_posix()
    except ValueError:
        shown = str(path)
    return (
        f"{shown} sets work.rails, which is ignored: safety rails are a per-user setting kept outside the "
        "repository; use `brigade work rails on|off` and remove the key from the file"
    )


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
    try:
        mode = validate_work_mode(mode)
    except ValueError:
        return f"mode must be one of: {', '.join(WORK_MODES)}"
    return _update(target, work_mode=mode)


def set_rails(target: Path, state: str) -> str | None:
    """Record ``state`` (``on`` or ``off``) for ``target`` in the per-user record; return an error or None.

    Never writes inside the repository. ``off`` needs a Brigade-wired target;
    ``on`` always clears the record entry.
    """
    if state not in RAILS_STATES:
        return "rails must be on or off"
    record = rails_record_path()
    if record is None:
        return "no per-user config directory (HOME, or LOCALAPPDATA on Windows, is unset)"
    if _record_in_target_repository(record, target):
        return f"the per-user rails record {record} sits inside the repository at or above {target}; rails stay on"
    if state == "off":
        try:
            if load_config(target) is None:
                return f"target is not Brigade-wired (no {CONFIG_REL_PATH}): {target}; run brigade init first"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return f"unable to load Brigade config: {exc}"
    entries = dict(_read_rails_off(record))
    key = repository_key(target)
    if state == "off":
        entries[key] = {"set_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    elif key in entries:
        # Membership, not the value, turns rails off, so clear any entry, null included.
        del entries[key]
    else:
        return None
    try:
        localio.write_json(record, {"version": RAILS_RECORD_VERSION, "rails_off": entries})
    except OSError as exc:
        return f"unable to write the per-user rails record {record}: {exc}"
    return None


def hook_root_mismatch(target: Path) -> str | None:
    """The Git top level when it differs from ``target``: the pre-push hook checks that path instead."""
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = result.stdout.strip()
    if result.returncode != 0 or not top or repository_key(Path(top)) == repository_key(target):
        return None
    return top


RAILS_HOOK_MARKER = "brigade work rails check"


def pre_push_hook_gated(target: Path) -> bool | None:
    """Whether ``hooks/pre-push`` consults the rails setting; None when absent."""
    try:
        return RAILS_HOOK_MARKER in (target / "hooks" / "pre-push").read_text(encoding="utf-8")
    except OSError:
        return None


def doctor_checks(target: Path) -> list[tuple[str, str, str]]:
    """Doctor rows that state the mode and rails plainly."""
    mode = resolve_mode(target)
    if mode == MODE_OFF:
        mode_detail = (
            "off: shared context stays on (brief, recall, Fleet Hub, handoffs on request); "
            "verification is not enforced and missing receipts are not reported; "
            "`brigade work mode set strict` restores the loop"
        )
    elif mode == MODE_OBSERVE:
        mode_detail = "observe: checks are recorded, nothing is blocked"
    else:
        mode_detail = "strict: checks route through `brigade work verify run` and the Stop gate wants a receipt"
    checks = [("INFO", "work mode", mode_detail)]
    settings = _work_settings(target)
    if "mode" in settings:
        try:
            validate_work_mode(settings["mode"])
        except ValueError:
            checks.append(("WARN", "work.mode", "invalid value ignored; using strict"))
    if "mode" not in settings:
        checks.append(("INFO", "work.mode", "unpinned; use `brigade work mode set strict` to pin it"))
    ignored = ignored_repo_rails_note(target)
    if ignored is not None:
        checks.append(("WARN", "work.rails", ignored))
    if rails_enabled(target):
        if pre_push_hook_gated(target) is None:
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
        f"off: your per-user record ({rails_record_path()}) turns them off for this exact path; the pre-push "
        "hook does not scan for secrets, credentials, private hosts, or private paths; "
        "`brigade work rails on` restores it"
    )
    if pre_push_hook_gated(target) is False:
        detail += (
            "; the installed hooks/pre-push predates the rails switch and still scans, "
            "refresh it from the current Brigade template to honor rails off"
        )
    checks.append(("WARN", "safety rails", detail))
    return checks
