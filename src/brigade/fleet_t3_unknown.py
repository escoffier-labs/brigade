"""Display words for a machine whose T3 observer is not reporting.

Pure strings with no imports, so the Deck can use them without loading the Hub. The keys are the
reason codes ``fleet_t3_health.sessions_unknown_reason`` returns; a test pins the two together.
"""

from __future__ import annotations

T3_UNKNOWN_LABELS = {
    "sessions-unknown": "sessions unknown: T3 observer stale or unreadable",
    "t3-health-missing": "sessions unknown: T3 observer not reporting",
}


def unused_label(reason: str) -> str:
    """The Why-idle cell text: the T3 wording for a T3 reason, the raw reason otherwise."""
    return T3_UNKNOWN_LABELS.get(reason, reason)


def unknown_note(reason: str) -> tuple[str, bool]:
    """The machine-row note (text, warn) for an ``unknown`` station: T3 wording for a T3 reason."""
    return ("sessions unknown", True) if reason in T3_UNKNOWN_LABELS else ("no telemetry", False)


def idle_card_text(slots: int, unknown: int) -> str:
    """The Idle card value: spare slots, naming the stations whose sessions could not be counted."""
    return f"{slots} ({unknown} unknown)" if unknown else str(slots)


def t3_session_gaps(control_plane) -> list[str]:
    """The coverage gap for machines whose telemetry is fresh but whose T3 observer is not reporting."""
    machines = control_plane.machines if "machines" in control_plane.present else ()
    return ["machine sessions (T3 observer)"] if any(m.unused_reason in T3_UNKNOWN_LABELS for m in machines) else []
