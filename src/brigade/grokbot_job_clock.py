"""UTC timestamp helpers shared by the Grok Bot queue.

Every stored queue timestamp is a ``Z``-suffixed ISO-8601 UTC string, and every
comparison (lease liveness, job deadline, clock regression) round-trips through
these three functions so storage, the hub, and the CLI cannot disagree about
what a moment is.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .grokbot_job_validation import GrokbotJobError


def timestamp(now: datetime | None) -> tuple[str, datetime]:
    """Return the stored form and the aware UTC instant for ``now``."""
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return format_timestamp(value), value


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GrokbotJobError("corrupt-storage")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GrokbotJobError("corrupt-storage") from exc
    if parsed.tzinfo is None or format_timestamp(parsed) != value:
        raise GrokbotJobError("corrupt-storage")
    return parsed.astimezone(timezone.utc)
