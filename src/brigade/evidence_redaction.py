"""Origin-scoped evidence-ingestion redaction policy (#498).

Classifies capture sources, redacts sensitive values before persistence, and
records counts only. Removed bytes never persist on the verdict, the envelope
record, or the stored item. Scanner failure, timeout, or unavailability cannot
produce a clean verdict. A new policy version applies to future writes only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .guard.engine import scan_text
from .guard.policy import Policy
from .guard.rules import DEFAULT_RULES
from .guard.types import Action, Finding, GuardResult, ScanOptions

SCHEMA = "brigade.evidence-redaction.v1"
SCHEMA_VERSION = 1
POLICY_VERSION = "brigade.evidence-redaction.v1"

ORIGINS = frozenset({"operator-input", "workspace", "agent-session", "external-service", "external-web", "unknown"})
STATUSES = frozenset({"clean", "redacted", "error"})
FAILED_PLACEHOLDER = "[ingestion-redaction-failed]"
MAX_DETECTORS = 32

# Work-inbox producers. Unknown sources stay workspace, matching the existing
# ledger default. Envelope origin ``unknown`` is a separate fail-closed class.
SOURCE_ORIGINS: dict[str, str] = {
    "manual": "operator-input",
    "backup-health": "workspace",
    "chat-memory-sweep": "agent-session",
    "code-review": "workspace",
    "context-pack": "workspace",
    "handoff-ingest": "agent-session",
    "learning-loop": "workspace",
    "memory-care": "workspace",
    "memory-refresh": "workspace",
    "project-consolidation": "workspace",
    "repo-fleet": "workspace",
    "repo-fleet-release": "workspace",
    "roadmap-audit": "workspace",
    "scanner-health": "workspace",
    "security-scan": "workspace",
    "tool-catalog": "workspace",
}

# Origin-scoped detector categories. ``unknown`` is fail-closed maximum scope.
ORIGIN_CATEGORIES: dict[str, frozenset[str]] = {
    "operator-input": frozenset({"secret"}),
    "workspace": frozenset({"secret"}),
    "agent-session": frozenset({"secret", "pii"}),
    "external-service": frozenset({"secret", "pii", "infrastructure"}),
    "external-web": frozenset({"secret", "pii", "infrastructure"}),
    "unknown": frozenset({"secret", "pii", "infrastructure", "attribution"}),
}

_RECORD_KEYS = frozenset({"schema", "schema_version", "policy_version", "origin", "status", "count", "detectors"})
_FORBIDDEN_RECORD_KEYS = frozenset(
    {
        "bytes",
        "excerpts",
        "match",
        "matches",
        "original",
        "raw",
        "removed",
        "secrets",
        "snippet",
        "text",
        "value",
        "values",
    }
)
_EXAMPLE_RULE_PREFIX = "example-"

ScanFn = Callable[..., GuardResult]


@dataclass(frozen=True)
class RedactionVerdict:
    """Ingest-time redaction decision. ``persisted_text`` is the only payload."""

    policy_version: str
    origin: str
    status: str
    count: int
    detectors: tuple[str, ...]
    persisted_text: str

    def record(self) -> dict[str, Any]:
        """Return the persistable count record. Never includes removed bytes."""
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "policy_version": self.policy_version,
            "origin": self.origin,
            "status": self.status,
            "count": self.count,
            "detectors": list(self.detectors),
        }


def classify_source_origin(source: str) -> str:
    """Map a producer source name to a closed envelope origin."""
    cleaned = source.strip() if isinstance(source, str) else ""
    if not cleaned:
        return "workspace"
    return SOURCE_ORIGINS.get(cleaned, "workspace")


def resolve_origin(origin: str) -> str:
    """Return a closed origin. Unknown or empty values fail closed to ``unknown``."""
    if origin in ORIGIN_CATEGORIES:
        return origin
    return "unknown"


def ingest_verdict_is_clean(record: object) -> bool:
    """Return True only for an explicit completed clean scan of the current policy.

    Missing, malformed, error, or other-version records are not clean. Existing
    rows without a redaction record therefore cannot look scanned-clean.
    """
    if validate_redaction_record(record):
        return False
    assert isinstance(record, Mapping)
    return record.get("status") == "clean" and record.get("count") == 0


def validate_redaction_record(record: object) -> list[str]:
    """Return problems on a persistable redaction record. Extra value keys fail."""
    if not isinstance(record, Mapping):
        return ["redaction must be a JSON object"]
    errors: list[str] = []
    extra = sorted(set(record) - _RECORD_KEYS)
    forbidden = sorted(set(record) & _FORBIDDEN_RECORD_KEYS)
    if forbidden:
        errors.append("redaction must not store removed values")
    elif extra:
        errors.append("redaction has unsupported keys")
    if record.get("schema") != SCHEMA:
        errors.append(f"redaction.schema must be {SCHEMA!r}")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"redaction.schema_version must be {SCHEMA_VERSION}")
    if record.get("policy_version") != POLICY_VERSION:
        errors.append(f"redaction.policy_version must be {POLICY_VERSION!r}")
    origin = record.get("origin")
    if origin not in ORIGINS:
        errors.append(f"redaction.origin {origin!r} is not in the closed set")
    status = record.get("status")
    if status not in STATUSES:
        errors.append(f"redaction.status {status!r} is not in the closed set")
    count = record.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        errors.append("redaction.count must be a nonnegative integer")
    elif status == "clean" and count != 0:
        errors.append("redaction.status clean requires count 0")
    elif status == "redacted" and count < 1:
        errors.append("redaction.status redacted requires a positive count")
    detectors = record.get("detectors")
    if not isinstance(detectors, Sequence) or isinstance(detectors, (str, bytes)):
        errors.append("redaction.detectors must be a list")
    else:
        if len(detectors) > MAX_DETECTORS:
            errors.append(f"redaction.detectors exceeds {MAX_DETECTORS} entries")
        for detector in detectors:
            if not isinstance(detector, str) or not detector or detector.startswith(_EXAMPLE_RULE_PREFIX):
                errors.append("redaction.detectors entries must be non-empty detector ids")
                break
            if any(ch.isspace() for ch in detector) or "=" in detector or len(detector) > 64:
                errors.append("redaction.detectors entries must be detector ids, not values")
                break
    return errors


def apply_origin_redaction(
    text: str,
    *,
    origin: str,
    scanner: ScanFn | None = None,
) -> RedactionVerdict:
    """Redact ``text`` for one origin. Fail closed. Never return removed values."""
    resolved = resolve_origin(origin)
    scan = scanner or scan_text
    try:
        result = scan(
            text,
            policy=_policy_for_origin(resolved),
            options=ScanOptions(honor_allow_comments=False, include_opf=False),
        )
    except Exception:
        return _error_verdict(resolved)
    if not isinstance(result, GuardResult):
        return _error_verdict(resolved)
    redacting = [finding for finding in result.findings if _counts(finding)]
    detectors = tuple(sorted({finding.rule_id for finding in redacting})[:MAX_DETECTORS])
    count = len(redacting)
    persisted = result.redacted_text if isinstance(result.redacted_text, str) else FAILED_PLACEHOLDER
    if persisted is text and count:
        return _error_verdict(resolved)
    status = "redacted" if count else "clean"
    return RedactionVerdict(
        policy_version=POLICY_VERSION,
        origin=resolved,
        status=status,
        count=count,
        detectors=detectors,
        persisted_text=persisted,
    )


def apply_and_record(text: str, *, origin: str, scanner: ScanFn | None = None) -> tuple[str, dict[str, Any], bool]:
    """Return ``(persisted_text, record, failed)`` for an ingest writer."""
    verdict = apply_origin_redaction(text, origin=origin, scanner=scanner)
    return verdict.persisted_text, verdict.record(), verdict.status == "error"


def _error_verdict(origin: str) -> RedactionVerdict:
    return RedactionVerdict(
        policy_version=POLICY_VERSION,
        origin=origin,
        status="error",
        count=0,
        detectors=(),
        persisted_text=FAILED_PLACEHOLDER,
    )


def _counts(finding: Finding) -> bool:
    return finding.redacts and finding.start < finding.end and not finding.rule_id.startswith(_EXAMPLE_RULE_PREFIX)


def _policy_for_origin(origin: str) -> Policy:
    categories = ORIGIN_CATEGORIES[origin]
    allow_rules: dict[str, Action] = {
        rule.id: "allow"
        for rule in DEFAULT_RULES
        if rule.category not in categories or rule.id.startswith(_EXAMPLE_RULE_PREFIX)
    }
    defaults: dict[str, Action] = {
        "infrastructure": "allow",
        "secret": "allow",
        "pii": "allow",
        "personal": "allow",
        "business": "allow",
        "attribution": "allow",
        "tooling": "allow",
    }
    for category in categories:
        defaults[category] = "redact"
    return Policy(name=f"evidence-redaction:{origin}", defaults=defaults, rules=allow_rules)
