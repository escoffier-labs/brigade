"""Explicit suppress/watch/escalate rules, expiry, and action eligibility."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .normalize import ESCALATE_CLASSES, NOISE_CLASSES

DEFAULT_SUPPRESSION_TTL = timedelta(days=7)
SUPPRESSION_KEYS = frozenset({"created_at", "expires_at", "fingerprint", "reason", "scope"})


def classify(
    record: Mapping[str, Any],
    *,
    suppressions: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    stamp = _aware(now)
    rule_class = str(record.get("rule_class") or "unknown")
    fingerprint = str(record.get("fingerprint") or "")
    matching = _matching_suppression(fingerprint, suppressions)
    if matching is not None and _is_expired(matching, stamp):
        return _result("watch", "expired-suppression", None)
    if matching is not None:
        return _result("suppress", str(matching["reason"]), str(matching["expires_at"]))
    if rule_class == "unknown" or not fingerprint:
        return _result("watch", "unknown-or-malformed", None)
    if rule_class in ESCALATE_CLASSES:
        return _result("escalate", f"high-confidence:{rule_class}", None)
    if rule_class in NOISE_CLASSES:
        expires = (stamp + DEFAULT_SUPPRESSION_TTL).isoformat().replace("+00:00", "Z")
        return _result("suppress", f"known-noise:{rule_class}", expires)
    return _result("watch", f"review-only:{rule_class}", None)


def is_action_eligible(classification: Mapping[str, Any]) -> bool:
    return classification.get("category") == "escalate"


def suppression_entry(
    *,
    reason: str,
    fingerprint: str,
    scope: str,
    created_at: str,
    expires_at: str,
) -> dict[str, str]:
    return {
        "reason": reason,
        "fingerprint": fingerprint,
        "scope": scope,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def _result(category: str, reason: str, expires_at: str | None) -> dict[str, Any]:
    return {"category": category, "reason": reason, "expires_at": expires_at}


def _matching_suppression(
    fingerprint: str,
    suppressions: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if not fingerprint:
        return None
    for item in suppressions:
        if not isinstance(item, Mapping) or set(item) != SUPPRESSION_KEYS:
            continue
        if item.get("fingerprint") != fingerprint:
            continue
        if not all(isinstance(item.get(key), str) and item[key] for key in SUPPRESSION_KEYS):
            continue
        return item
    return None


def _is_expired(item: Mapping[str, Any], now: datetime) -> bool:
    try:
        expires = _parse_time(str(item["expires_at"]))
    except ValueError:
        return True
    return expires <= now


def _parse_time(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("naive")
    return parsed


def _aware(value: datetime | None) -> datetime:
    stamp = value if value is not None else datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp
