"""Handoff health checks shared by CLI doctors."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import hashlib
import re
import sys
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import scrub
from ..budgets import HANDOFF_BACKLOG_STALE_SECONDS
from ..config import load_config as load_brigade_config
from ..localio import write_json as _write_json
from ..selection import WRITER_INBOXES as _WRITER_INBOX_MAP

OK = "ok"

WARN = "warn"

FAIL = "fail"

WRITER_INBOXES = tuple(_WRITER_INBOX_MAP.values())

IGNORED_HANDOFF_NAMES = {"TEMPLATE.md"}

DEFAULT_STALE_AFTER_MINUTES = 90

HANDOFF_DRAFT_STALE_HOURS = 72

BACKLOG_STALE_SECONDS = HANDOFF_BACKLOG_STALE_SECONDS

MAX_INGESTOR_WARNING_SIGNALS = 5

CARD_ACTIONS = ("create-card", "update-card")

NO_CARD_ACTION = "no-card"

HANDOFF_ACTIONS = (*CARD_ACTIONS, NO_CARD_ACTION)

CARD_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9._-]+\.md$")

DOCUMENT_TARGETS = ("TOOLS.md", "USER.md")

DOCUMENT_TARGET_PREFIXES = ("rules/", ".learnings/")

DEFAULT_DRAFT_INBOX = _WRITER_INBOX_MAP["codex"]

DEFAULT_DRAFT_DOCUMENT = ".learnings/LEARNINGS.md"

DEFAULT_WARNING_PATTERNS = (
    "Warnings:",
    "SKIP ",
    "PROMOTE-SKIP",
    "ROUTE-SKIP",
    "NO_REPLY",
    "NO_UPDATES",
    "unreachable",
    "timeout",
    "timed out",
    "no route",
)

ISSUE_SOURCE = "handoff-ingest"


@dataclass(frozen=True)
class WatchedInbox:
    root: Path
    inbox: str


@dataclass(frozen=True)
class IngestorConfig:
    log_path: Path
    stale_after_minutes: int
    warning_patterns: tuple[str, ...]


@dataclass(frozen=True)
class SourceConfig:
    watched: tuple[WatchedInbox, ...]
    ingestor: IngestorConfig | None


@dataclass(frozen=True)
class InboxHealth:
    inbox: str
    path: Path
    exists: bool
    pending: int
    processed: int
    watched: bool
    oldest_pending_age_seconds: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "inbox": self.inbox,
            "path": str(self.path),
            "exists": self.exists,
            "pending": self.pending,
            "processed": self.processed,
            "watched": self.watched,
            "oldest_pending_age_seconds": self.oldest_pending_age_seconds,
        }


@dataclass(frozen=True)
class IngestorHealth:
    configured: bool
    log_path: Path | None
    exists: bool
    age_seconds: int | None
    stale_after_seconds: int | None
    stale: bool
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "log_path": str(self.log_path) if self.log_path else None,
            "exists": self.exists,
            "age_seconds": self.age_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "stale": self.stale,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class HandoffIssue:
    id: str
    category: str
    kind: str
    text: str
    repair: str
    evidence: str
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "kind": self.kind,
            "text": self.text,
            "repair": self.repair,
            "evidence": self.evidence,
            "metadata": self.metadata,
        }

    def as_import_record(self) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata.setdefault("source_item_key", _handoff_issue_source_key(self))
        metadata.setdefault("source_fingerprint", _handoff_issue_fingerprint(self, metadata))
        metadata.update(
            {
                "handoff_issue_id": self.id,
                "handoff_issue_category": self.category,
                "repair": self.repair,
                "evidence": self.evidence,
            }
        )
        return {
            "text": self.text,
            "kind": self.kind,
            "source": ISSUE_SOURCE,
            "metadata": metadata,
        }


@dataclass(frozen=True)
class HandoffLintResult:
    path: Path
    action: str | None
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    hints: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "action": self.action,
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "hints": list(self.hints),
        }


@dataclass(frozen=True)
class HandoffHealth:
    target: Path
    sources_path: Path | None
    sources_loaded: bool
    inboxes: tuple[InboxHealth, ...]
    ingestor: IngestorHealth
    lint: tuple[HandoffLintResult, ...]
    warnings: tuple[str, ...]
    failures: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": str(self.target),
            "sources_path": str(self.sources_path) if self.sources_path else None,
            "sources_loaded": self.sources_loaded,
            "inboxes": [inbox.as_dict() for inbox in self.inboxes],
            "ingestor": self.ingestor.as_dict(),
            "lint": [result.as_dict() for result in self.lint],
            "warnings": list(self.warnings),
            "failures": list(self.failures),
        }


@dataclass(frozen=True)
class HandoffDraft:
    id: str
    path: Path
    inbox: str
    created_at: str | None
    modified_at: str | None
    age_hours: float | None
    stale: bool
    lint: HandoffLintResult
    action: str | None
    target_card: str | None
    target_document: str | None
    source_import_id: str | None
    source_fingerprint: str | None
    scanner_provenance: dict[str, Any]
    status: str
    watched: bool
    ingestion_status: str | None
    ingest_run_id: str | None
    ingest_log_path: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "inbox": self.inbox,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "age_hours": self.age_hours,
            "stale": self.stale,
            "lint": self.lint.as_dict(),
            "action": self.action,
            "target_card": self.target_card,
            "target_document": self.target_document,
            "source_import_id": self.source_import_id,
            "source_fingerprint": self.source_fingerprint,
            "scanner_provenance": self.scanner_provenance,
            "status": self.status,
            "watched": self.watched,
            "ingestion_status": self.ingestion_status,
            "ingest_run_id": self.ingest_run_id,
            "ingest_log_path": self.ingest_log_path,
        }


def default_sources_path(target: Path) -> Path:
    return target / ".brigade" / "handoff-sources.json"


_LOOSE_FIELD_TEMPLATE = r"^\s*[-*]?\s*\*{0,2}%s\*{0,2}\s*:\s*(.+)$"
