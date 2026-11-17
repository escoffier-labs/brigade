"""Enable, start, and unlink installed scheduled-care timers (#2589).

``brigade care install`` writes systemd unit files. Until now it never linked
the timer into ``timers.target.wants``, so an installed timer could sit dormant
forever. The link is a plain file operation, and a foreign entry at the link
path is reported and never replaced. A new link only takes effect when
``timers.target`` next starts, so install then runs ``daemon-reload`` and
``start <timer>`` best-effort through :func:`run_systemctl`, the one place a
``systemctl`` process is spawned. ``care uninstall`` removes only the link
Brigade created and stops the timer the same way.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from . import managed_block

WANTS_DIRNAME = "timers.target.wants"

ACTION_ENABLE = "enable"
ACTION_SKIP = "skip"
ACTION_CONFLICT = "conflict"
ACTION_NONE = "none"

STATUS_LINKED = "linked"
STATUS_PLANNED = "planned"
STATUS_ALREADY = "already"
STATUS_CONFLICT = "conflict"
STATUS_SKIPPED = "skipped"

ACTION_PRESERVE = managed_block.ACTION_PRESERVE

SYSTEMCTL_TIMEOUT_SECONDS = 30
DAEMON_RELOAD_COMMAND = "systemctl --user daemon-reload"


def _systemctl_subprocess(args: list[str]) -> tuple[bool, str]:
    """Run ``systemctl --user <args>``; return (ok, error text)."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return False, "systemctl command not found"
    except subprocess.TimeoutExpired:
        return False, f"systemctl --user {args[0]} timed out"
    except OSError as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout or "").strip()
    return False, detail or f"systemctl exited {result.returncode}"


def run_systemctl(args: list[str]) -> tuple[bool, str]:
    """The single systemctl runner. Tests monkeypatch this, never ``subprocess``."""
    return _systemctl_subprocess(args)


def entry_id_from_timer(name: str, identity: str) -> str:
    """Return the care entry id encoded in a namespaced timer unit name."""
    prefix = f"brigade-care-{identity}-"
    if name.startswith(prefix) and name.endswith(".timer"):
        return name[len(prefix) : -len(".timer")]
    return ""


def wants_dir(unit_dir: Path) -> Path:
    return unit_dir / WANTS_DIRNAME


def links_to_timer(*, unit_dir: Path, timer_name: str) -> bool:
    """True when the enable link exists and resolves to this timer unit."""
    link = wants_dir(unit_dir) / timer_name
    timer = unit_dir / timer_name
    if not link.is_symlink() or not timer.is_file():
        return False
    try:
        return link.resolve() == timer.resolve()
    except OSError:
        return False


def _is_brigade_owned_timer(timer: Path) -> bool:
    from .care_cmd import CARE_KIND, CARE_MARKER_STYLE

    try:
        text = managed_block.read_text_nofollow(timer)
    except (OSError, UnicodeDecodeError):
        return False
    return managed_block.parse_blocks(text, kind=CARE_KIND, style=CARE_MARKER_STYLE).status == "ok"


def _adopted_entry_ids(adopted: list[dict[str, Any]] | None) -> set[str]:
    ids: set[str] = set()
    for item in adopted or []:
        if not isinstance(item, dict):
            continue
        using = str(item.get("using") or "")
        if using:
            ids.add(using)
    return ids


def _wants_problem(unit_dir: Path, wants: Path) -> str:
    """Why Brigade must not write under *wants*, or an empty string when it may."""
    if wants.is_symlink():
        return f"{WANTS_DIRNAME} is a symlink; Brigade does not write through it"
    try:
        inside = wants.resolve().is_relative_to(unit_dir.resolve())
    except OSError:
        return f"{WANTS_DIRNAME} could not be resolved"
    if not inside:
        return f"{WANTS_DIRNAME} resolves outside the unit directory"
    return ""


def _skip_reason(
    *,
    result: dict[str, Any],
    timer: Path,
    entry_id: str,
    adopted_ids: set[str],
    enable: bool,
    dry_run: bool,
    blocked: bool,
) -> str:
    """Why Brigade will not link this timer, or an empty string when it may."""
    if blocked:
        return "install is blocked; resolve the unit conflict first"
    if not enable:
        return "--no-enable"
    if result.get("action") == ACTION_PRESERVE:
        return "unit was preserved; Brigade did not write it in this run"
    if entry_id and entry_id in adopted_ids:
        return "adopted registration; run the printed enable command to opt in"
    if not dry_run:
        # A dry run has not written the units yet; the real path must only
        # enable a timer that exists and is Brigade's own managed block.
        if not timer.is_file():
            return "timer unit file is absent"
        if not _is_brigade_owned_timer(timer):
            return "timer unit is not a Brigade-owned managed block"
    return ""


def enable_timers(
    *,
    unit_dir: Path,
    results: list[dict[str, Any]],
    identity: str,
    adopted: list[dict[str, Any]] | None = None,
    enable: bool = True,
    dry_run: bool = False,
    blocked: bool = False,
) -> dict[str, Any]:
    """Link each Brigade-owned timer into ``timers.target.wants`` and start it.

    *results* is the unit install result list from ``care install``. A timer is
    not linked when the operator passed ``--no-enable``, when the unit was
    preserved, adopted, or is not a Brigade-owned managed block, and when the
    timer file is absent. A timer that is already linked reports ``already`` and
    enabled in every case, because the link is the fact. A foreign entry at the
    link path, or a ``timers.target.wants`` that is a symlink or resolves outside
    the unit directory, is reported as a conflict and nothing is written. Outside
    a dry run, each timer linked in this run is then started (see
    :func:`activate_timers`).
    """
    wants = wants_dir(unit_dir)
    adopted_ids = _adopted_entry_ids(adopted)
    wants_problem = _wants_problem(unit_dir, wants)
    timers: list[dict[str, Any]] = []
    for result in results:
        name = str(result.get("name") or "")
        if not name.endswith(".timer"):
            continue
        entry_id = entry_id_from_timer(name, identity)
        timer = unit_dir / name
        link = wants / name
        record: dict[str, Any] = {
            "name": name,
            "entry": entry_id,
            "path": str(timer),
            "link": str(link),
            "action": ACTION_SKIP,
            "status": STATUS_SKIPPED,
            "enabled": False,
        }
        timers.append(record)
        reason = _skip_reason(
            result=result,
            timer=timer,
            entry_id=entry_id,
            adopted_ids=adopted_ids,
            enable=enable,
            dry_run=dry_run,
            blocked=blocked,
        )
        if links_to_timer(unit_dir=unit_dir, timer_name=name):
            record.update(action=ACTION_NONE, status=STATUS_ALREADY, enabled=True)
            # A rewritten unit that is already linked still needs a reload and
            # restart, or systemd keeps running the old schedule.
            record["rewritten"] = not dry_run and result.get("action") in (
                managed_block.ACTION_CREATE,
                managed_block.ACTION_UPDATE,
            )
            if reason == "--no-enable":
                record["detail"] = "--no-enable did not remove the existing enable link"
            elif reason:
                record["detail"] = f"{reason}; the existing enable link was left in place"
            continue
        if reason:
            record["detail"] = reason
            continue
        if wants_problem:
            record.update(action=ACTION_CONFLICT, status=STATUS_CONFLICT, detail=wants_problem)
            continue
        if link.is_symlink() or link.exists():
            record.update(
                action=ACTION_CONFLICT,
                status=STATUS_CONFLICT,
                detail="a foreign entry already exists at the link path; Brigade left it untouched",
            )
            continue
        record.update(action=ACTION_ENABLE, status=STATUS_PLANNED if dry_run else STATUS_LINKED)
        if dry_run:
            continue
        try:
            wants.mkdir(parents=True, exist_ok=True)
            # systemd reads a POSIX relative target; Path would emit a backslash on Windows.
            link.symlink_to(f"../{name}")
        except OSError as exc:
            record.update(action=ACTION_SKIP, status=STATUS_SKIPPED, detail=f"failed to create link: {exc}")
            continue
        record["enabled"] = True
    outcome: dict[str, Any] = {
        "wants_dir": str(wants),
        "enabled": all(bool(timer["enabled"]) for timer in timers),
        "linked": [str(timer["name"]) for timer in timers if timer["status"] == STATUS_LINKED],
        "conflicts": [str(timer["name"]) for timer in timers if timer["action"] == ACTION_CONFLICT],
        "skipped": [str(timer["name"]) for timer in timers if timer["action"] == ACTION_SKIP],
        "daemon_reload": None,
        "started": [],
        "start_failed": [],
        "timers": timers,
    }
    if not dry_run:
        activate_timers(outcome)
    return outcome


def activate_timers(enable: dict[str, Any]) -> None:
    """Run ``daemon-reload`` then ``start`` for each timer linked in this run.

    A wants link only takes effect when ``timers.target`` next starts, so a freshly
    linked timer would not fire on a running host without this. Best-effort: a
    missing or failing systemctl keeps the link and records the error per timer.
    """
    fresh = [timer for timer in enable["timers"] if timer["status"] == STATUS_LINKED]
    rewritten = [timer for timer in enable["timers"] if timer.get("rewritten")]
    if not fresh and not rewritten:
        return
    reloaded, reload_error = run_systemctl(["daemon-reload"])
    enable["daemon_reload"] = {"ok": reloaded, "error": reload_error or None}
    for timer in fresh + rewritten:
        if reloaded:
            # restart applies a changed OnCalendar to a running timer and starts a stopped one.
            verb = "start" if timer in fresh else "restart"
            started, error = run_systemctl([verb, str(timer["name"])])
        else:
            started, error = False, f"not attempted: daemon-reload failed ({reload_error})"
        timer["started"] = started
        timer["start_error"] = error or None
        enable["started" if started else "start_failed"].append(timer["name"])


def _link_is_ours(*, link: Path, unit_dir: Path, name: str) -> bool:
    """True for a symlink Brigade (or ``systemctl enable``) made to this timer unit."""
    if not link.is_symlink():
        return False
    try:
        pointed = os.readlink(link)
    except OSError:
        return False
    if pointed in (f"../{name}", str(unit_dir / name)):
        return True
    try:
        return link.resolve() == (unit_dir / name).resolve()
    except OSError:
        return False


def finish_uninstall(
    *, unit_dir: Path, removed: list[str], skipped: list[str], dry_run: bool = False
) -> dict[str, Any]:
    """Unlink and stop the timers ``care uninstall`` is removing.

    A wants entry is removed only when it is a symlink to the unit being removed
    (live, or dangling after the unit is gone); any other entry is reported as
    ``kept`` and left alone, and a symlinked or outside ``timers.target.wants``
    is never written through. Each removed timer is then stopped, best-effort.
    """
    wants = wants_dir(unit_dir)
    removed_timers = [name for name in removed if name.endswith(".timer")]
    # A skipped timer whose file is already gone may still have a dangling link.
    gone = [name for name in skipped if name.endswith(".timer") and not (unit_dir / name).exists()]
    outcome: dict[str, Any] = {
        "wants_dir": str(wants),
        "removed": [],
        "kept": [],
        "conflict": "",
        "stopped": [],
        "errors": {},
    }
    outcome["conflict"] = _wants_problem(unit_dir, wants) if wants.is_symlink() or wants.exists() else ""
    for name in removed_timers + gone:
        link = wants / name
        if outcome["conflict"] or not (link.is_symlink() or link.exists()):
            continue
        if not _link_is_ours(link=link, unit_dir=unit_dir, name=name):
            outcome["kept"].append(name)
            continue
        if not dry_run:
            try:
                link.unlink()
            except OSError as exc:
                outcome["kept"].append(name)
                outcome["errors"][name] = f"failed to remove link: {exc}"
                continue
        outcome["removed"].append(name)
    if not dry_run:
        for name in removed_timers:
            if name not in outcome["removed"]:
                # Never linked by us, so systemd never loaded it as wanted; nothing to stop.
                continue
            stopped, error = run_systemctl(["stop", name])
            if stopped or "not loaded" in (error or ""):
                outcome["stopped"].append(name)
            else:
                outcome["errors"][name] = error
    return outcome


def next_commands(*, identity: str, enable: dict[str, Any]) -> list[str]:
    """Commands the operator must still run after install could not do them itself."""
    reloaded = bool((enable.get("daemon_reload") or {}).get("ok"))
    commands = [] if reloaded else [DAEMON_RELOAD_COMMAND]
    for timer in enable.get("timers") or []:
        if timer.get("started") is False:
            commands.append(f"systemctl --user start {timer['name']}")
        elif not timer.get("enabled") and timer.get("status") != STATUS_PLANNED:
            commands.append(f"systemctl --user enable --now {timer['name']}")
    commands.append(f"systemctl --user list-timers 'brigade-care-{identity}-*.timer'")
    return commands


def install_notes(*, enable: dict[str, Any]) -> list[str]:
    """Accurate post-install notes for the systemd backend."""
    timers = enable.get("timers") or []
    notes: list[str] = []
    if enable.get("conflicts"):
        notes.append(
            f"note: Brigade wrote unit files and left {WANTS_DIRNAME} untouched for: "
            f"{', '.join(enable['conflicts'])} (a foreign entry or a symlinked directory); "
            "fix that yourself before enabling the timer."
        )
    elif not timers:
        notes.append("note: Brigade wrote unit files; no systemd timer was selected for this target.")
    elif enable.get("enabled"):
        started = len(enable.get("started") or [])
        tail = f", and started {started} newly linked timer(s)" if started else ""
        notes.append(f"note: Brigade wrote unit files and linked each timer under {WANTS_DIRNAME}{tail}.")
    failed = [timer for timer in timers if timer.get("started") is False]
    if failed:
        notes.append(
            f"note: {len(failed)} newly linked timer(s) could not be started now "
            f"({failed[0].get('start_error')}); run the printed systemctl --user commands."
        )
    if timers and not enable.get("enabled"):
        disabled = sum(1 for timer in timers if not timer.get("enabled"))
        notes.append(
            f"note: Brigade wrote unit files but {disabled} timer(s) remain disabled; "
            "run the printed systemctl --user enable --now commands after daemon-reload."
        )
    return notes
