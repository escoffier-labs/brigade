"""Source-observation freshness for target-owned T3 lifecycle reports."""

from __future__ import annotations

from datetime import datetime

OBSERVATION_TTL_SECONDS = 300
CLOCK_SKEW_SECONDS = 300


def observation_state(state: str, harness: object, ts: object, *, now: datetime) -> str:
    """Project nonterminal T3 facts without treating delayed delivery as liveness.

    Callers handle terminal states first. No observation releases occupancy,
    ownership, or capacity. Small future skew is tolerated, larger skew and
    missing timezone/observation times remain unknown.
    """
    if harness != "t3-fleet" or state == "external.unknown":
        return state
    if not isinstance(ts, str):
        return "external.unknown"
    try:
        observed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "external.unknown"
    if observed.tzinfo is None:
        return "external.unknown"
    age = (now - observed).total_seconds()
    if age < -CLOCK_SKEW_SECONDS:
        return "external.unknown"
    return "run.stale" if age > OBSERVATION_TTL_SECONDS else state
