"""Field bounds and lifecycle edges for Worklore items.

Burn-queue eligibility lives only in ``worklore_store.exclusion_bucket`` so the
queue rules and their exclusion buckets cannot drift apart.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

KINDS = frozenset({"repo", "fleet", "research", "writing", "admin", "experiment", "idea", "other"})
PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
TOKEN_APPETITES = frozenset({"small", "medium", "large", "max"})
EXECUTION_MODES = frozenset({"manual", "agent", "agent-with-review"})

TITLE_MAX = 240
DESCRIPTION_MAX = 8000
SCOPE_MAX = 128
BLOCKER_MAX = 2000
ACCEPTANCE_ITEM_MAX = 400
ACCEPTANCE_MAX_ITEMS = 20
DEPENDENCIES_MAX_ITEMS = 50
EVIDENCE_REFS_MAX_ITEMS = 20
BURN_RANK_MAX = 10000
TIMESTAMP_MAX = 64
URL_MAX = 2048

CREATE_FIELDS = frozenset(
    {
        "title",
        "kind",
        "description",
        "scope",
        "priority",
        "burn_eligible",
        "burn_rank",
        "token_appetite",
        "execution_mode",
        "acceptance",
        "blocker",
        "review_after",
        "spend_by",
    }
)

TRANSITIONS: dict[str, frozenset[str]] = {
    "captured": frozenset({"defining", "ready", "canceled"}),
    "defining": frozenset({"ready", "deferred", "canceled"}),
    "ready": frozenset({"claimed", "deferred", "canceled"}),
    "claimed": frozenset({"running", "blocked", "canceled"}),
    "running": frozenset({"verifying", "blocked", "canceled"}),
    "verifying": frozenset({"completed", "blocked", "canceled"}),
    "deferred": frozenset({"ready", "canceled"}),
    "blocked": frozenset({"ready", "canceled"}),
    "completed": frozenset({"archived"}),
    "canceled": frozenset({"archived"}),
}


class WorkloreValidationError(Exception):
    """A Worklore field or lifecycle check failed."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def contains_controls(value: str) -> bool:
    """True when ``value`` contains a C0 or C1 control character (including DEL).

    Terminal escapes start with ESC (0x1B), so this is also the terminal-control check.
    """
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value)


def _field_bound(message: str) -> WorkloreValidationError:
    return WorkloreValidationError(message, code="field-bound")


def _private_data(message: str) -> WorkloreValidationError:
    return WorkloreValidationError(message, code="private-data")


# A private home path is matched as a whole path segment, so an ordinary repo
# name (``escoffier-labs/homelab#4``), a public URL path
# (``https://github.com/users/octocat``), or a neutral workspace path
# (``/workspace/alice``) stays importable. User-profile directory spellings do not.
_PRIVATE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[A-Za-z]:)?[\\/](?:home|users|root)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_PRIVATE_ENV_MARKERS = (
    "%userprofile%",
    "%homepath%",
    "%homedrive%",
    "%appdata%",
    "%localappdata%",
    "$env:userprofile",
)
# Credential shapes, not entropy heuristics: a provider token prefix, a PEM
# private key header, a JWT, an assignment whose value is long enough to be a
# real secret, or URL userinfo.
_CREDENTIAL_RES = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abopsr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bglpat-[0-9A-Za-z_-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?:api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token|token)"
        r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+=]{16,}",
        re.IGNORECASE,
    ),
    re.compile(r"//[^\s/@]+:[^\s/@]+@"),
)
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]+$")


def contains_private_path(value: str) -> bool:
    """True when ``value`` embeds an operator home directory or user-profile path."""
    lowered = value.lower()
    return bool(_PRIVATE_PATH_RE.search(value)) or any(marker in lowered for marker in _PRIVATE_ENV_MARKERS)


def contains_credential(value: str) -> bool:
    """True when ``value`` embeds a credential-shaped token, key, or URL userinfo."""
    return any(pattern.search(value) for pattern in _CREDENTIAL_RES)


def assert_no_private_data(value: str, field: str) -> str:
    """Raise ``private-data`` when ``value`` carries a home path or a credential."""
    if contains_private_path(value):
        raise _private_data(f"{field} must not contain a private home path")
    if contains_credential(value):
        raise _private_data(f"{field} must not contain a credential-shaped value")
    return value


def safe_text(value: object, field: str, *, max_len: int, min_len: int = 1) -> str:
    """Canonical string check: type, control characters, and length are ``field-bound``;
    a home path or credential is ``private-data``."""
    return assert_no_private_data(_require_str(value, field, max_len=max_len, min_len=min_len), field)


def safe_optional_text(value: object, field: str, *, max_len: int, min_len: int = 1) -> str | None:
    """``safe_text`` for a field that may be absent. ``min_len=0`` keeps a field whose
    contract already accepts the empty string accepting it."""
    if value is None:
        return None
    return safe_text(value, field, max_len=max_len, min_len=min_len)


def safe_https_url(value: object, field: str = "url") -> str | None:
    """Accept only a well-formed https URL with no userinfo and no private data."""
    if value is None:
        return None
    text = _require_str(value, field, max_len=URL_MAX, min_len=1)
    try:
        parsed = urlsplit(text)
    except ValueError:
        raise _field_bound(f"{field} must be an https URL") from None
    if parsed.scheme != "https" or not parsed.netloc:
        raise _field_bound(f"{field} must be an https URL")
    if "@" in parsed.netloc:
        raise _private_data(f"{field} must not embed URL credentials")
    try:
        port = parsed.port
    except ValueError:
        raise _field_bound(f"{field} must be an https URL") from None
    if port is None and parsed.netloc.endswith(":"):
        raise _field_bound(f"{field} must be an https URL")
    if not parsed.hostname or not _HOST_RE.fullmatch(parsed.netloc):
        raise _field_bound(f"{field} must be an https URL")
    return assert_no_private_data(text, field)


def _require_str(value: object, field: str, *, max_len: int, min_len: int = 0) -> str:
    if not isinstance(value, str):
        raise _field_bound(f"{field} must be a string")
    if contains_controls(value):
        raise _field_bound(f"{field} must not contain control characters")
    if len(value) < min_len or len(value) > max_len:
        raise _field_bound(f"{field} must be {min_len} to {max_len} characters")
    return value


def _choice(value: object, field: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed or contains_controls(value):
        raise _field_bound(f"{field} must be one of {sorted(allowed)}")
    return value


def _int_flag(value: object, field: str) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if value in (0, 1):
        return int(value)
    raise _field_bound(f"{field} must be a boolean")


def _burn_rank(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _field_bound("burn_rank must be an integer")
    if value < 0 or value > BURN_RANK_MAX:
        raise _field_bound(f"burn_rank must be 0 to {BURN_RANK_MAX}")
    return value


def _acceptance_items(value: object, *, required: bool) -> list[str]:
    if value is None:
        items: list[object] = []
    elif not isinstance(value, list):
        raise _field_bound("acceptance must be an array of strings")
    else:
        items = value
    if required and not items:
        raise WorkloreValidationError("acceptance is required", code="acceptance-required")
    if len(items) > ACCEPTANCE_MAX_ITEMS:
        if required:
            raise WorkloreValidationError(
                f"acceptance must have at most {ACCEPTANCE_MAX_ITEMS} items",
                code="acceptance-required",
            )
        raise _field_bound(f"acceptance must have at most {ACCEPTANCE_MAX_ITEMS} items")
    parsed: list[str] = []
    for index, item in enumerate(items):
        text = safe_text(
            item,
            f"acceptance[{index}]",
            max_len=ACCEPTANCE_ITEM_MAX,
            min_len=1,
        )
        if not text.strip():
            raise _field_bound(f"acceptance[{index}] must be a non-empty string")
        parsed.append(text)
    if required and not parsed:
        raise WorkloreValidationError("acceptance is required", code="acceptance-required")
    return parsed


def as_datetime(value: str) -> datetime:
    """Parse an accepted Worklore timestamp, normalizing a trailing ``Z`` and naive input to UTC."""
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def parse_timestamp(value: object, field: str) -> str | None:
    """Return an ISO-8601 timestamp, or None for an absent one, so malformed input fails loudly."""
    if value is None:
        return None
    text = _require_str(value, field, max_len=TIMESTAMP_MAX)
    if not text:
        return None
    try:
        as_datetime(text)
    except ValueError:
        raise _field_bound(f"{field} must be an ISO-8601 timestamp") from None
    return text


def parse_create(raw: object) -> dict[str, Any]:
    """Normalize a native create body and apply Worklore defaults.

    Every durable text field runs through ``safe_text`` / ``safe_optional_text``, so a
    native create, and the patch that reuses this function, are held to the same
    private-path and credential rules as an imported observation.
    """
    if not isinstance(raw, Mapping):
        raise _field_bound("create body must be an object")
    unknown = [name for name in raw if name not in CREATE_FIELDS]
    if unknown:
        raise WorkloreValidationError(f"unknown field: {unknown[0]}", code="unknown-field")
    if "title" not in raw or "kind" not in raw:
        raise _field_bound("title and kind are required")
    payload = dict(raw)
    return {
        "title": safe_text(payload["title"], "title", max_len=TITLE_MAX, min_len=1),
        "kind": _choice(payload["kind"], "kind", KINDS),
        "description": safe_optional_text(
            payload.get("description", ""), "description", max_len=DESCRIPTION_MAX, min_len=0
        )
        or "",
        "scope": safe_optional_text(payload.get("scope"), "scope", max_len=SCOPE_MAX, min_len=0),
        "status": "captured",
        "priority": _choice(payload.get("priority", "normal"), "priority", PRIORITIES),
        "burn_eligible": _int_flag(payload.get("burn_eligible", 0), "burn_eligible"),
        "burn_rank": _burn_rank(payload.get("burn_rank", 1000)),
        "token_appetite": _choice(
            payload.get("token_appetite", "medium"),
            "token_appetite",
            TOKEN_APPETITES,
        ),
        "execution_mode": _choice(
            payload.get("execution_mode", "manual"),
            "execution_mode",
            EXECUTION_MODES,
        ),
        "acceptance": _acceptance_items(payload.get("acceptance", []), required=False),
        "blocker": safe_optional_text(payload.get("blocker"), "blocker", max_len=BLOCKER_MAX, min_len=0),
        "review_after": parse_timestamp(payload.get("review_after"), "review_after"),
        "spend_by": parse_timestamp(payload.get("spend_by"), "spend_by"),
    }


def assert_ready(item: Mapping[str, Any]) -> None:
    """Raise when a ready item is missing a valid acceptance list."""
    _acceptance_items(item.get("acceptance"), required=True)


def can_transition(current: str, target: str) -> bool:
    """True when ``current`` may move to ``target`` on the Worklore lifecycle."""
    return target in TRANSITIONS.get(current, frozenset())
