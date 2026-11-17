"""Enable installed scheduled-care timers without starting a subprocess.

``brigade care install`` writes systemd unit files. Until now it never linked
the timer into ``timers.target.wants``, so an installed timer could sit dormant
forever (issue #2589). This module performs the link as a plain file operation:
no ``systemctl`` process is started, and a foreign entry at the link path is
reported and never replaced.
"""

from __future__ import annotations

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
    """Link each Brigade-owned timer into ``timers.target.wants``.

    *results* is the unit install result list from ``care install``. A timer is
    skipped when the operator passed ``--no-enable``, when the unit was
    preserved, adopted, or is not a Brigade-owned managed block, and when the
    timer file is absent. An existing entry that resolves to the timer is left
    alone; anything else at the link path is reported as a conflict.
    """
    wants = wants_dir(unit_dir)
    adopted_ids = _adopted_entry_ids(adopted)
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
        if blocked:
            record["detail"] = "install is blocked; resolve the unit conflict first"
            continue
        if not enable:
            record["detail"] = "--no-enable"
            continue
        if result.get("action") == ACTION_PRESERVE:
            record["detail"] = "unit was preserved; Brigade did not write it in this run"
            continue
        if entry_id and entry_id in adopted_ids:
            record["detail"] = "adopted registration; run the printed enable command to opt in"
            continue
        if not dry_run:
            # A dry run has not written the units yet; the real path must only
            # enable a timer that exists and is Brigade's own managed block.
            if not timer.is_file():
                record["detail"] = "timer unit file is absent"
                continue
            if not _is_brigade_owned_timer(timer):
                record["detail"] = "timer unit is not a Brigade-owned managed block"
                continue
        if links_to_timer(unit_dir=unit_dir, timer_name=name):
            record.update(action=ACTION_NONE, status=STATUS_ALREADY, enabled=True)
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
            link.symlink_to(Path("..") / name)
        except OSError as exc:
            record.update(action=ACTION_SKIP, status=STATUS_SKIPPED, detail=f"failed to create link: {exc}")
            continue
        record["enabled"] = True
    linked = [str(timer["name"]) for timer in timers if timer["status"] == STATUS_LINKED]
    conflicts = [str(timer["name"]) for timer in timers if timer["action"] == ACTION_CONFLICT]
    skipped = [str(timer["name"]) for timer in timers if timer["action"] == ACTION_SKIP]
    return {
        "wants_dir": str(wants),
        "enabled": all(bool(timer["enabled"]) for timer in timers),
        "linked": linked,
        "conflicts": conflicts,
        "skipped": skipped,
        "timers": timers,
    }


def next_commands(*, identity: str, enable: dict[str, Any]) -> list[str]:
    """Commands the operator must still run after install touched no service state."""
    commands = ["systemctl --user daemon-reload"]
    commands.extend(
        f"systemctl --user enable --now {timer['name']}"
        for timer in enable.get("timers") or []
        if not timer.get("enabled")
    )
    commands.append(f"systemctl --user list-timers 'brigade-care-{identity}-*.timer'")
    return commands


def install_notes(*, enable: dict[str, Any]) -> list[str]:
    """Accurate post-install notes for the systemd backend."""
    timers = enable.get("timers") or []
    notes: list[str] = []
    if enable.get("conflicts"):
        notes.append(
            "note: Brigade wrote unit files and left an existing foreign entry in "
            f"{WANTS_DIRNAME} untouched: {', '.join(enable['conflicts'])}; "
            "remove or rename it yourself before enabling that timer."
        )
    elif not timers:
        notes.append("note: Brigade wrote unit files; no systemd timer was selected for this target.")
    elif enable.get("enabled"):
        notes.append(f"note: Brigade wrote unit files and enabled each timer under {WANTS_DIRNAME}.")
    if timers and not enable.get("enabled"):
        disabled = sum(1 for timer in timers if not timer.get("enabled"))
        notes.append(
            f"note: Brigade wrote unit files but {disabled} timer(s) remain disabled; "
            "run the printed systemctl --user enable --now commands after daemon-reload."
        )
    return notes
