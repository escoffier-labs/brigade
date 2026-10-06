"""Pure, bounded normalization of trusted managed Claude review check runs.

No provider calls, persistence, review approval or merge-policy decisions live
here. Callers supply an independently trusted app identity and current PR head.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit


OUTPUT_MAX_BYTES = 65536
MARKER_MAX_BYTES = 512
COUNT_MAX = 2147483647
OBJECT_ID_MAX = 2**63 - 1
_MARKER_TOKEN = "bughunter-severity"
_MARKER_RE = re.compile(r"<!-- bughunter-severity: (\{[^\r\n]*\}) -->")
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)")
_CHECK_PATH_RE = re.compile(
    r"/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})/(?:runs|check-runs)/([1-9][0-9]*)"
)
_LIFECYCLES = frozenset({"queued", "in_progress", "completed"})
_CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "timed_out",
        "action_required",
        "skipped",
        "stale",
        "startup_failure",
    }
)


@dataclass(frozen=True)
class SeverityCounts:
    """Documented keys. ``normal`` counts Important findings."""

    normal: int
    nit: int
    pre_existing: int


@dataclass(frozen=True)
class SeverityEvidence:
    disposition: str
    reason: str | None
    counts: SeverityCounts | None


@dataclass(frozen=True)
class ManagedReviewEvidence:
    """Sanitized observation, with execution, parsing and coverage independent."""

    provider: str
    source: str
    check_id: int
    app_id: int
    app_slug: str
    reviewed_head: str | None
    current_head: str
    observed_at: str
    started_at: str | None
    completed_at: str | None
    source_url: str | None
    lifecycle: str
    conclusion: str | None
    coverage: str
    severity: SeverityEvidence

    @property
    def identity_key(self) -> str:
        """Stable object identity for idempotent callers, independent of head."""
        return f"github:check-run:{self.app_id}:{self.check_id}"

    @property
    def current_findings_known(self) -> bool:
        return self.lifecycle == "completed" and self.coverage == "current" and self.severity.counts is not None

    @property
    def current_zero_findings(self) -> bool:
        counts = self.severity.counts
        return (
            self.current_findings_known
            and counts is not None
            and (counts.normal, counts.nit, counts.pre_existing)
            == (
                0,
                0,
                0,
            )
        )


def _unknown(reason: str) -> SeverityEvidence:
    return SeverityEvidence("unknown", reason, None)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("non-JSON constant")


def parse_severity_marker(text: object) -> SeverityEvidence:
    """Parse one final documented HTML comment, failing closed on unknown data.

    No raw text is returned, and absent or malformed data never yields zeros.
    Output is bounded before scanning or parsing, including its UTF-8 encoding.
    """
    if not isinstance(text, str):
        return _unknown("missing-or-non-text-output")
    if len(text) > OUTPUT_MAX_BYTES:
        return _unknown("output-too-large")
    try:
        if len(text.encode("utf-8")) > OUTPUT_MAX_BYTES:
            return _unknown("output-too-large")
    except UnicodeError:
        return _unknown("invalid-output-encoding")
    occurrences = text.count(_MARKER_TOKEN)
    if occurrences == 0:
        return _unknown("missing-marker")
    if occurrences != 1:
        return _unknown("duplicate-marker")
    line = text.rstrip().rsplit("\n", 1)[-1]
    if len(line.encode("utf-8")) > MARKER_MAX_BYTES:
        return _unknown("marker-too-large")
    match = _MARKER_RE.fullmatch(line)
    if match is None:
        return _unknown("malformed-marker")
    try:
        value = json.loads(match[1], object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return _unknown("invalid-json")
    if not isinstance(value, dict) or set(value) != {"normal", "nit", "pre_existing"}:
        return _unknown("unknown-schema")
    if any(type(count) is not int or count < 0 or count > COUNT_MAX for count in value.values()):
        return _unknown("invalid-count")
    return SeverityEvidence("known", None, SeverityCounts(value["normal"], value["nit"], value["pre_existing"]))


def _object_id(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= OBJECT_ID_MAX else None


def _head(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and _SHA_RE.fullmatch(value) else None


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40 or not _TIME_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _source_url(value: object, check_id: int) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != "github.com" or "?" in value or "#" in value:
        return None
    match = _CHECK_PATH_RE.fullmatch(parsed.path)
    if match is None or match[2] in {".", ".."} or match[3] != str(check_id):
        return None
    return value


def normalize_check_run(
    check_run: object,
    *,
    expected_app_id: int,
    expected_app_slug: str,
    current_head: str,
    observed_at: str,
) -> ManagedReviewEvidence | None:
    """Normalize one check, or reject a non-managed/unidentifiable object.

    Identity is caller-established, never inferred from a provider check name.
    Invalid caller head/time raise ValueError. Malformed provider fields remain
    unknown or are omitted. No official app ID is hardcoded.
    """
    app_id = _object_id(expected_app_id)
    if (
        app_id is None
        or not isinstance(expected_app_slug, str)
        or not _SLUG_RE.fullmatch(expected_app_slug)
        or expected_app_slug == "github-actions"
    ):
        return None
    if not isinstance(check_run, Mapping) or check_run.get("name") != "Claude Code Review":
        return None
    app = check_run.get("app")
    if not isinstance(app, Mapping) or _object_id(app.get("id")) != app_id or app.get("slug") != expected_app_slug:
        return None
    check_id = _object_id(check_run.get("id"))
    if check_id is None:
        return None
    head = _head(current_head)
    observed = _timestamp(observed_at)
    if head is None or observed is None:
        raise ValueError("current_head and observed_at must be a full SHA and timezone-aware ISO timestamp")
    reviewed = _head(check_run.get("head_sha"))
    coverage = "unknown" if reviewed is None else "current" if reviewed == head else "stale"
    status = check_run.get("status")
    lifecycle = status if isinstance(status, str) and status in _LIFECYCLES else "unknown"
    raw_conclusion = check_run.get("conclusion")
    conclusion = raw_conclusion if isinstance(raw_conclusion, str) and raw_conclusion in _CONCLUSIONS else None
    output = check_run.get("output")
    severity = parse_severity_marker(output.get("text") if isinstance(output, Mapping) else None)
    url = _source_url(check_run.get("details_url"), check_id) or _source_url(check_run.get("html_url"), check_id)
    return ManagedReviewEvidence(
        provider="github",
        source="managed-claude-code-review",
        check_id=check_id,
        app_id=app_id,
        app_slug=expected_app_slug,
        reviewed_head=reviewed,
        current_head=head,
        observed_at=observed,
        started_at=_timestamp(check_run.get("started_at")),
        completed_at=_timestamp(check_run.get("completed_at")),
        source_url=url,
        lifecycle=lifecycle,
        conclusion=conclusion,
        coverage=coverage,
        severity=severity,
    )
