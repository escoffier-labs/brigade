"""Passive reporting for Claude sessions whose hooks have timed out."""

from __future__ import annotations

import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import anchored_io, session_state

_RECENT_LIMIT = 64
_SCAN_LIMIT = 2048
# Runtime state names have a sanitized slug and an eight-digit hash. Sidecars
# use the same basename, including when a timeout predates the state write.
_STATE_FILE = re.compile(r"^(.+-[0-9a-f]{8}\.json)(?:\.timeouts(?:\.pending)?)?$")


def _recent_candidates(target: Path, threshold: float) -> tuple[list[str], bool]:
    newest: dict[str, float] = {}
    with anchored_io.session_directory(target) as anchor:
        names = anchored_io.dirfd.child_names(anchor.fd, _SCAN_LIMIT + 1)
        for name in names[:_SCAN_LIMIT]:
            match = _STATE_FILE.fullmatch(name)
            if match is None:
                continue
            try:
                info = anchored_io.dirfd.stat_child(anchor.fd, name, allow_reparse=True)
            except FileNotFoundError:
                continue
            state_name = match[1]
            if stat.S_ISREG(info.st_mode) and info.st_mtime >= threshold:
                newest[state_name] = max(newest.get(state_name, threshold), info.st_mtime)
        anchor.validate()
    return sorted(newest, key=newest.__getitem__, reverse=True), len(names) > _SCAN_LIMIT


def check(target: Path) -> tuple[str, str, str] | None:
    label = "Claude hook timeout latch"
    try:
        threshold = (datetime.now(UTC) - timedelta(days=1)).timestamp()
        candidates, scan_cut_short = _recent_candidates(target, threshold)
        count = sum(session_state.read_latch_for_state_file(target, name) for name in candidates[:_RECENT_LIMIT])
    except FileNotFoundError:
        return None
    except Exception:
        return "WARN", label, "could not read recent Claude hook session state"
    if count or scan_cut_short or len(candidates) > _RECENT_LIMIT:
        scope = f"past 24 hours, checked at most {_RECENT_LIMIT} most recent sessions"
        if scan_cut_short:
            scope += f", directory scan cut short after {_SCAN_LIMIT} entries"
        return (
            "WARN" if count else "INFO",
            label,
            f"{count} recent session(s) bypass Claude hooks after repeated timeouts ({scope})",
        )
    return None
