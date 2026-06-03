"""Handoff health checks shared by CLI doctors."""
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

OK = "ok"
WARN = "warn"
FAIL = "fail"

from . import scrub
from .selection import WRITER_INBOXES as _WRITER_INBOX_MAP

WRITER_INBOXES = tuple(_WRITER_INBOX_MAP.values())
IGNORED_HANDOFF_NAMES = {"TEMPLATE.md"}
DEFAULT_STALE_AFTER_MINUTES = 90
HANDOFF_DRAFT_STALE_HOURS = 72
# A handoff inbox with pending files whose oldest entry is older than this is
# treated as a stalled backlog: handoffs are being written but nothing is
# ingesting them. Catches the silent pile-up where a repo's inbox is never
# reached by the canonical ingester (e.g. an uncovered repo in the fleet).
# Canonical value lives in budgets.py so doctor/ingest/repos all agree.
from .budgets import HANDOFF_BACKLOG_STALE_SECONDS as BACKLOG_STALE_SECONDS
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "action": self.action,
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
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


def inspect(target: Path, sources: Path | None = None) -> HandoffHealth:
    target = target.expanduser().resolve()
    sources_path = sources.expanduser().resolve() if sources is not None else default_sources_path(target)
    source_config = SourceConfig(watched=(), ingestor=None)
    failures: list[str] = []
    sources_loaded = False

    if sources_path.is_file():
        try:
            source_config = _load_sources(target, sources_path)
        except ValueError as exc:
            failures.append(f"invalid handoff source config {sources_path}: {exc}")
        else:
            sources_loaded = True
    elif sources is not None:
        failures.append(f"handoff source config not found: {sources_path}")
        sources_path = sources_path
    else:
        sources_path = None

    watched = source_config.watched
    inboxes = tuple(_inspect_inbox(target, rel, watched) for rel in WRITER_INBOXES)
    ingestor = _inspect_ingestor(source_config.ingestor)
    lint_results = lint_targets(target)
    warnings: list[str] = []
    pending_total = sum(inbox.pending for inbox in inboxes)
    if pending_total and not sources_loaded and not failures:
        warnings.append(
            "pending handoffs exist but no .brigade/handoff-sources.json is configured"
        )
    for inbox in inboxes:
        if inbox.pending and not inbox.watched:
            warnings.append(
                f"{inbox.inbox} has {inbox.pending} pending handoff"
                f"{'s' if inbox.pending != 1 else ''} but is not watched by the source config"
            )
    for watched_inbox in watched:
        watched_path = watched_inbox.root / watched_inbox.inbox
        if not watched_path.exists():
            warnings.append(f"configured handoff source inbox is missing: {watched_path}")
    for result in lint_results:
        if not result.valid:
            warnings.append(
                f"handoff lint failed for {result.path}: {result.errors[0] if result.errors else 'invalid handoff'}"
            )
    warnings.extend(ingestor.warnings)

    return HandoffHealth(
        target=target,
        sources_path=sources_path,
        sources_loaded=sources_loaded,
        inboxes=inboxes,
        ingestor=ingestor,
        lint=lint_results,
        warnings=tuple(warnings),
        failures=tuple(failures),
    )


def doctor_checks(target: Path, sources: Path | None = None) -> list[tuple[str, str, str]]:
    health = inspect(target, sources=sources)
    checks: list[tuple[str, str, str]] = []
    if health.failures:
        for failure in health.failures:
            checks.append((FAIL, "handoff_sources", failure))
    elif health.sources_loaded:
        checks.append((OK, "handoff_sources", str(health.sources_path)))
    else:
        pending_total = sum(inbox.pending for inbox in health.inboxes)
        level = WARN if pending_total else OK
        checks.append((level, "handoff_sources", "not configured; no pending handoffs" if not pending_total else "not configured"))

    for inbox in health.inboxes:
        if inbox.pending and not inbox.watched:
            level = WARN
        elif not inbox.exists:
            level = OK
        else:
            level = OK
        watched = "yes" if inbox.watched else "no"
        exists = "yes" if inbox.exists else "no"
        detail = (
            f"{inbox.path} "
            f"(exists={exists}, pending={inbox.pending}, processed={inbox.processed}, watched={watched})"
        )
        checks.append((level, f"handoff_watch: {inbox.inbox}", detail))

    stale_backlog = [
        inbox
        for inbox in health.inboxes
        if inbox.pending
        and inbox.oldest_pending_age_seconds is not None
        and inbox.oldest_pending_age_seconds >= BACKLOG_STALE_SECONDS
    ]
    if stale_backlog:
        pending_total = sum(inbox.pending for inbox in stale_backlog)
        oldest = max(
            inbox.oldest_pending_age_seconds or 0 for inbox in stale_backlog
        )
        oldest_days = oldest // (24 * 60 * 60)
        names = ", ".join(inbox.inbox for inbox in stale_backlog)
        checks.append(
            (
                WARN,
                "handoff_backlog",
                f"{pending_total} pending handoff(s) not ingested, oldest {oldest_days}d old "
                f"({names}); ingester is not reaching this inbox",
            )
        )

    if source_config := _source_config_for_checks(health.target, health.sources_path):
        for watched_inbox in source_config.watched:
            watched_path = watched_inbox.root / watched_inbox.inbox
            if not watched_path.exists():
                checks.append((WARN, "handoff_source_coverage", f"configured inbox missing: {watched_path}"))

    if health.ingestor.configured:
        if not health.ingestor.exists:
            level = WARN
            detail = f"missing at {health.ingestor.log_path}"
        elif health.ingestor.stale:
            level = WARN
            detail = (
                f"{health.ingestor.log_path} "
                f"(age={_format_seconds(health.ingestor.age_seconds)}, "
                f"stale_after={_format_seconds(health.ingestor.stale_after_seconds)})"
            )
        elif health.ingestor.warnings:
            level = WARN
            detail = f"{health.ingestor.log_path} ({len(health.ingestor.warnings)} warning signal{'s' if len(health.ingestor.warnings) != 1 else ''})"
        else:
            level = OK
            detail = f"{health.ingestor.log_path} (age={_format_seconds(health.ingestor.age_seconds)})"
        checks.append((level, "handoff_ingestor", detail))
    else:
        checks.append((OK, "handoff_ingestor", "log not configured"))

    invalid_lint = [result for result in health.lint if not result.valid]
    if not health.lint:
        checks.append((OK, "handoff_lint", "no pending handoffs"))
    elif invalid_lint:
        checks.append((WARN, "handoff_lint", f"{len(invalid_lint)} invalid of {len(health.lint)} pending handoff files"))
        for result in invalid_lint:
            first_error = result.errors[0] if result.errors else "invalid handoff"
            checks.append((WARN, "handoff_lint", f"{result.path}: {first_error}"))
    else:
        checks.append((OK, "handoff_lint", f"{len(health.lint)} pending handoff file{'s' if len(health.lint) != 1 else ''} valid"))

    for warning in health.warnings:
        checks.append((WARN, "handoff_warning", warning))
    draft_payload = draft_queue_payload(target, sources=sources)
    for check in draft_payload["checks"]:
        checks.append((str(check.get("status")), str(check.get("name")), str(check.get("detail"))))
    return checks


def collect_issues(
    target: Path,
    sources: Path | None = None,
    categories: list[str] | None = None,
) -> list[HandoffIssue]:
    health = inspect(target, sources=sources)
    issues: list[HandoffIssue] = []
    wanted_categories = {category for category in categories or [] if category}
    for failure in health.failures:
        issues.append(
            _make_issue(
                category="source-config-invalid",
                kind="task",
                text=f"Repair handoff source config: {failure}",
                repair="Fix .brigade/handoff-sources.json so Brigade can compare local writer inboxes against canonical ingestor coverage.",
                evidence=failure,
                metadata={
                    "source_item_key": "handoff-ingest:source-config",
                    "source_path": str(health.sources_path) if health.sources_path else "",
                },
            )
        )
    pending_total = sum(inbox.pending for inbox in health.inboxes)
    if pending_total and not health.sources_loaded and not health.failures:
        issues.append(
            _make_issue(
                category="source-config-missing",
                kind="task",
                text="Configure handoff source coverage for pending writer inboxes",
                repair="Create .brigade/handoff-sources.json with every local handoff writer inbox that the canonical ingestor scans.",
                evidence=f"pending_handoffs={pending_total}",
                metadata={
                    "source_item_key": "handoff-ingest:source-config",
                    "pending_handoffs": pending_total,
                },
            )
        )
    source_config = _source_config_for_checks(health.target, health.sources_path)
    if source_config is not None and "source-inbox-missing" in wanted_categories:
        for watched_inbox in source_config.watched:
            watched_path = watched_inbox.root / watched_inbox.inbox
            if watched_path.exists():
                continue
            issues.append(
                _make_issue(
                    category="source-inbox-missing",
                    kind="task",
                    text=f"Repair missing configured handoff inbox {watched_inbox.inbox}",
                    repair="Create the local handoff inbox, remove stale coverage from .brigade/handoff-sources.json, or correct the configured root/inbox pair.",
                    evidence=str(watched_path),
                    metadata={
                        "source_item_key": f"handoff-ingest:source-inbox:{watched_inbox.inbox}",
                        "inbox": watched_inbox.inbox,
                        "path": str(watched_path),
                    },
                )
            )
    for inbox in health.inboxes:
        if inbox.pending and not inbox.watched:
            issues.append(
                _make_issue(
                    category="untracked-inbox",
                    kind="task",
                    text=(
                        f"Add {inbox.inbox} to handoff source config or move "
                        f"{inbox.pending} pending handoff"
                        f"{'s' if inbox.pending != 1 else ''}"
                    ),
                    repair=(
                        "Add the repo root and inbox path to .brigade/handoff-sources.json, "
                        "or move the pending files into an inbox the canonical ingestor scans."
                    ),
                    evidence=str(inbox.path),
                    metadata={
                        "source_item_key": f"handoff-ingest:untracked-inbox:{inbox.inbox}",
                        "inbox": inbox.inbox,
                        "path": str(inbox.path),
                        "pending": inbox.pending,
                    },
                )
            )

    for result in health.lint:
        if result.valid:
            continue
        first_error = result.errors[0] if result.errors else "invalid handoff"
        issues.append(
            _make_issue(
                category="lint",
                kind="task",
                text=f"Fix pending handoff lint error in {result.path.name}: {first_error}",
                repair=_lint_repair_for_result(result),
                evidence=str(result.path),
                metadata={
                    "path": str(result.path),
                    "action": result.action,
                    "errors": list(result.errors),
                },
            )
        )

    ingestor = health.ingestor
    if ingestor.configured:
        if not ingestor.exists:
            issues.append(
                _make_issue(
                    category="missing-log",
                    kind="incident",
                    text=f"Restore handoff ingestor latest-run log at {ingestor.log_path}",
                    repair=(
                        "Update ingestor.last_run_log to the actual latest-run log path, "
                        "or adjust the ingestor wrapper to write that log after each run."
                    ),
                    evidence=str(ingestor.log_path),
                    metadata={"log_path": str(ingestor.log_path)},
                )
            )
        elif ingestor.stale:
            issues.append(
                _make_issue(
                    category="stale-log",
                    kind="incident",
                    text=f"Investigate stale handoff ingestor run log at {ingestor.log_path}",
                    repair="Run the handoff ingestor, then fix the scheduler or wrapper if the log does not refresh.",
                    evidence=f"age={_format_seconds(ingestor.age_seconds)}, stale_after={_format_seconds(ingestor.stale_after_seconds)}",
                    metadata={
                        "log_path": str(ingestor.log_path),
                        "age_seconds": ingestor.age_seconds,
                        "stale_after_seconds": ingestor.stale_after_seconds,
                    },
                )
            )
        if ingestor.exists and ingestor.log_path is not None:
            issues.extend(_parse_ingestor_log_issues(ingestor.log_path))
    return _filter_issues_by_category(_dedupe_issues(issues), categories)


def lint(
    *,
    target: Path,
    paths: list[Path] | None = None,
    content_guard: bool = False,
    guard_policy: str = "personal",
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    results = lint_targets(target, paths=paths)
    guard_results = [_guard_handoff_path(path, target=target, policy=guard_policy) for path in [result.path for result in results]] if content_guard else []
    guard_ok = all(item.get("exit_code") == 0 for item in guard_results)
    payload = {
        "target": str(target),
        "count": len(results),
        "valid": all(result.valid for result in results) and guard_ok,
        "results": [result.as_dict() for result in results],
        "content_guard": guard_results,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"handoff lint: {target}")
    print(f"files: {len(results)}")
    for result in results:
        status = OK if result.valid else FAIL
        action = f" ({result.action})" if result.action else ""
        print(f"[{status}] {result.path}{action}")
        for error in result.errors:
            print(f"  - {error}")
        for warning in result.warnings:
            print(f"  warning: {warning}")
    if content_guard:
        print(f"content_guard_policy: {guard_policy}")
        for item in guard_results:
            status = OK if item.get("exit_code") == 0 else FAIL
            print(f"[{status}] content_guard: {item.get('path')} {item.get('detail')}")
    return 0 if payload["valid"] else 1


def _guard_handoff_path(path: Path, *, target: Path, policy: str) -> dict[str, Any]:
    result = scrub.run_scan(path, repo_target=target, policy=policy)
    stdout_summary = _short(" ".join(str(result.get("stdout") or "").split()), 320)
    stderr_summary = _short(" ".join(str(result.get("stderr") or "").split()), 320)
    return {
        "path": str(path),
        "policy": policy,
        "available": bool(result.get("available")),
        "exit_code": result.get("exit_code"),
        "status": result.get("status"),
        "detail": result.get("detail"),
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
    }


def lint_targets(target: Path, paths: list[Path] | None = None) -> tuple[HandoffLintResult, ...]:
    target = target.expanduser().resolve()
    candidates = tuple(_resolve_lint_path(target, path) for path in paths) if paths else _pending_handoff_paths(target)
    return tuple(lint_file(path) for path in candidates)


def lint_file(path: Path) -> HandoffLintResult:
    path = path.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    action: str | None = None
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return HandoffLintResult(
            path=path,
            action=None,
            valid=False,
            errors=(f"cannot read handoff file: {exc}",),
            warnings=(),
        )

    sections = _parse_markdown_sections(text)
    for required in ("Type", "Title", "Summary", "Recommended memory action"):
        if required not in sections or not _section_value(sections, required):
            errors.append(f"missing required section: {required}")

    action_value = _section_value(sections, "Recommended memory action")
    if action_value:
        action = action_value.splitlines()[0].strip().casefold()
        if action not in HANDOFF_ACTIONS:
            errors.append(
                "Recommended memory action must be one of: "
                + ", ".join(HANDOFF_ACTIONS)
            )

    if action in CARD_ACTIONS:
        _lint_card_action(sections, errors, warnings)
    elif action == NO_CARD_ACTION:
        _lint_no_card_action(sections, errors)

    return HandoffLintResult(
        path=path,
        action=action,
        valid=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _handoff_state_root(target: Path) -> Path:
    return target / ".brigade" / "handoffs"


def _handoff_archive_root(target: Path) -> Path:
    return _handoff_state_root(target) / "archive"


def _handoff_archive_records_path(target: Path) -> Path:
    return _handoff_state_root(target) / "archive.jsonl"


def _handoff_ingest_runs_root(target: Path) -> Path:
    return _handoff_state_root(target) / "ingest-runs"


def _handoff_closeouts_root(target: Path) -> Path:
    return _handoff_state_root(target) / "closeouts"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _load_source_config_for_drafts(target: Path, sources: Path | None = None) -> tuple[SourceConfig, Path | None, list[str], bool]:
    target = target.expanduser().resolve()
    sources_path = sources.expanduser().resolve() if sources is not None else default_sources_path(target)
    if sources_path.is_file():
        try:
            return _load_sources(target, sources_path), sources_path, [], True
        except ValueError as exc:
            return SourceConfig(watched=(), ingestor=None), sources_path, [f"invalid handoff source config {sources_path}: {exc}"], False
    if sources is not None:
        return SourceConfig(watched=(), ingestor=None), sources_path, [f"handoff source config not found: {sources_path}"], False
    return SourceConfig(watched=(), ingestor=None), None, [], False


def _draft_inbox_specs(target: Path, sources: Path | None = None) -> tuple[list[tuple[Path, str, bool]], list[str], bool]:
    target = target.expanduser().resolve()
    config, _, errors, loaded = _load_source_config_for_drafts(target, sources=sources)
    specs: dict[tuple[str, str], tuple[Path, str, bool]] = {}
    for rel in WRITER_INBOXES:
        path = (target / rel).resolve()
        specs[(str(path), rel)] = (path, rel, _is_watched(target, rel, config.watched))
    for watched in config.watched:
        path = (watched.root / watched.inbox).resolve()
        label = watched.inbox if watched.root == target.resolve() else str(path)
        specs[(str(path), label)] = (path, label, True)
    return list(specs.values()), errors, loaded


def _draft_paths(target: Path, sources: Path | None = None) -> tuple[list[tuple[Path, str, bool]], list[str], bool]:
    paths: list[tuple[Path, str, bool]] = []
    specs, errors, loaded = _draft_inbox_specs(target, sources=sources)
    for inbox_path, inbox, watched in specs:
        if not inbox_path.is_dir():
            continue
        for candidate in sorted(inbox_path.glob("*.md")):
            if not candidate.is_file():
                continue
            if candidate.name.startswith(".") or candidate.name in IGNORED_HANDOFF_NAMES:
                continue
            paths.append((candidate.resolve(), inbox, watched))
    return paths, errors, loaded


def _path_timestamp(path: Path, attr: str) -> tuple[str | None, float | None]:
    try:
        stat = path.stat()
    except OSError:
        return None, None
    value = stat.st_ctime if attr == "created" else stat.st_mtime
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(value)), value


def _iso_from_timestamp(value: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() if value is None else value))


def _normalize_receipt_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _receipt_path_value(item: object) -> str | None:
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, dict):
        for key in ("handoff_path", "path", "draft_path"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _receipt_target_value(item: object) -> str | None:
    if isinstance(item, dict):
        for key in ("target", "target_card", "target_document", "card", "document"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _path_match_keys(target: Path, value: str | Path) -> set[str]:
    text = str(value)
    raw = Path(text).expanduser()
    resolved = raw if raw.is_absolute() else target / raw
    keys = {text, raw.name}
    try:
        keys.add(str(resolved.resolve()))
    except OSError:
        keys.add(str(resolved))
    return {key for key in keys if key}


def _ingest_receipt_path(target: Path, run_id: str) -> Path:
    return _handoff_ingest_runs_root(target) / f"{run_id}.json"


def _normalize_ingest_receipt(target: Path, payload: dict[str, Any], *, source_path: Path | None = None) -> dict[str, Any]:
    run_id = str(payload.get("run_id") or (source_path.stem if source_path is not None else "")).strip()
    if not run_id:
        raise ValueError("receipt missing run_id")
    normalized = {
        "run_id": run_id,
        "started_at": payload.get("started_at") if isinstance(payload.get("started_at"), str) else None,
        "completed_at": payload.get("completed_at") if isinstance(payload.get("completed_at"), str) else None,
        "source_root": str(payload.get("source_root") or target),
        "inbox_paths": [str(item) for item in _normalize_receipt_list(payload.get("inbox_paths")) if str(item)],
        "processed_handoff_paths": [
            str(path)
            for item in _normalize_receipt_list(payload.get("processed_handoff_paths"))
            if (path := _receipt_path_value(item))
        ],
        "promoted_card_targets": [
            {
                "handoff_path": path,
                "target": _receipt_target_value(item),
            }
            for item in _normalize_receipt_list(payload.get("promoted_card_targets"))
            if (path := _receipt_path_value(item))
        ],
        "routed_document_targets": [
            {
                "handoff_path": path,
                "target": _receipt_target_value(item),
            }
            for item in _normalize_receipt_list(payload.get("routed_document_targets"))
            if (path := _receipt_path_value(item))
        ],
        "skipped_handoff_paths": [
            str(path)
            for item in _normalize_receipt_list(payload.get("skipped_handoff_paths"))
            if (path := _receipt_path_value(item))
        ],
        "failed_handoff_paths": [
            str(path)
            for item in _normalize_receipt_list(payload.get("failed_handoff_paths"))
            if (path := _receipt_path_value(item))
        ],
        "malformed_handoff_paths": [
            str(path)
            for item in _normalize_receipt_list(payload.get("malformed_handoff_paths"))
            if (path := _receipt_path_value(item))
        ],
        "unreachable_sources": [
            str(item)
            for item in _normalize_receipt_list(payload.get("unreachable_sources"))
            if isinstance(item, str) and item.strip()
        ],
        "no_reply": bool(payload.get("no_reply")),
        "warning_events": [
            item
            for item in _normalize_receipt_list(payload.get("warning_events"))
            if isinstance(item, dict)
        ],
        "warning_count": int(payload.get("warning_count") or 0),
        "safe_summary": str(payload.get("safe_summary") or ""),
        "log_path": str(payload.get("log_path") or ""),
    }
    normalized["outcomes"] = _ingest_receipt_outcomes(target, normalized)
    return normalized


def _load_ingest_receipt(target: Path, path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return _normalize_ingest_receipt(target, raw, source_path=path)
    except ValueError:
        return None


def _ingest_receipts(target: Path) -> list[dict[str, Any]]:
    root = _handoff_ingest_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [
        receipt
        for path in sorted(root.glob("*.json"))
        if (receipt := _load_ingest_receipt(target, path)) is not None
    ]
    receipts.sort(key=lambda item: str(item.get("completed_at") or item.get("started_at") or item.get("run_id")), reverse=True)
    return receipts


def _ingest_receipt_outcomes(target: Path, receipt: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}

    def add(path_value: str, status: str, *, target_card: str | None = None, target_document: str | None = None) -> None:
        entry = {
            "path": path_value,
            "status": status,
            "run_id": receipt.get("run_id"),
            "completed_at": receipt.get("completed_at"),
            "log_path": receipt.get("log_path"),
            "target_card": target_card,
            "target_document": target_document,
        }
        for key in _path_match_keys(target, path_value):
            existing = outcomes.get(key, {})
            merged = {**existing, **{k: v for k, v in entry.items() if v is not None}}
            outcomes[key] = merged

    for path_value in receipt.get("processed_handoff_paths") or []:
        if isinstance(path_value, str):
            add(path_value, "ingested")
    for item in receipt.get("promoted_card_targets") or []:
        if isinstance(item, dict) and isinstance(item.get("handoff_path"), str):
            add(item["handoff_path"], "ingested", target_card=_receipt_target_value(item))
    for item in receipt.get("routed_document_targets") or []:
        if isinstance(item, dict) and isinstance(item.get("handoff_path"), str):
            add(item["handoff_path"], "ingested", target_document=_receipt_target_value(item))
    for path_value in receipt.get("skipped_handoff_paths") or []:
        if isinstance(path_value, str):
            add(path_value, "skipped")
    for path_value in receipt.get("failed_handoff_paths") or []:
        if isinstance(path_value, str):
            add(path_value, "failed")

    unique: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for outcome in outcomes.values():
        unique[(outcome.get("path"), outcome.get("status"))] = outcome
    return list(unique.values())


def _ingest_outcomes_by_path(target: Path) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for receipt in _ingest_receipts(target):
        for outcome in receipt.get("outcomes") or []:
            path_value = outcome.get("path")
            if not isinstance(path_value, str):
                continue
            for key in _path_match_keys(target, path_value):
                mapped.setdefault(key, outcome)
    return mapped


def _latest_ingest_outcome_for_path(target: Path, path: Path, outcomes: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    outcomes = outcomes if outcomes is not None else _ingest_outcomes_by_path(target)
    for key in _path_match_keys(target, path):
        outcome = outcomes.get(key)
        if outcome is not None:
            return outcome
    return None


def _receipt_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    outcomes = receipt.get("outcomes") if isinstance(receipt.get("outcomes"), list) else []
    counts: dict[str, int] = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        status = str(outcome.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "run_id": receipt.get("run_id"),
        "started_at": receipt.get("started_at"),
        "completed_at": receipt.get("completed_at"),
        "source_root": receipt.get("source_root"),
        "inbox_paths": receipt.get("inbox_paths") or [],
        "warning_count": receipt.get("warning_count") or 0,
        "warning_events": receipt.get("warning_events") or [],
        "malformed": len(receipt.get("malformed_handoff_paths") or []),
        "unreachable_sources": len(receipt.get("unreachable_sources") or []),
        "no_reply": bool(receipt.get("no_reply")),
        "safe_summary": receipt.get("safe_summary") or "",
        "log_path": receipt.get("log_path") or "",
        "outcome_counts": counts,
        "processed": len(receipt.get("processed_handoff_paths") or []),
        "skipped": len(receipt.get("skipped_handoff_paths") or []),
        "failed": len(receipt.get("failed_handoff_paths") or []),
    }


def _extract_handoff_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip().strip("`").casefold().replace(" ", "_")
        value = value.strip().strip("`")
        if key and value and key not in values:
            values[key] = value
    return values


def _draft_summary(
    path: Path,
    *,
    target: Path,
    inbox: str,
    watched: bool,
    ingest_outcomes: dict[str, dict[str, Any]] | None = None,
) -> HandoffDraft:
    path = path.expanduser().resolve()
    try:
        text = path.read_text(errors="replace")
    except OSError:
        text = ""
    sections = _parse_markdown_sections(text)
    lint_result = lint_file(path)
    action = lint_result.action
    target_card = _section_value(sections, "Target card").splitlines()[0].strip() if _section_value(sections, "Target card") else None
    target_document = _section_value(sections, "Target document").splitlines()[0].strip() if _section_value(sections, "Target document") else None
    key_values = _extract_handoff_key_values(text)
    source_import_id = key_values.get("import") or key_values.get("import_id") or key_values.get("source_import_id")
    source_fingerprint = key_values.get("source_fingerprint") or key_values.get("handoff_source_fingerprint")
    scanner_keys = (
        "scanner_id",
        "scanner_source",
        "scanner_run_id",
        "scanner_receipt_path",
        "scanner_output_path_snapshot",
        "scanner_import_path",
        "sweep_id",
        "sweep_issue_id",
    )
    scanner_provenance = {key: key_values[key] for key in scanner_keys if key_values.get(key)}
    created_at, _ = _path_timestamp(path, "created")
    modified_at, modified_seconds = _path_timestamp(path, "modified")
    age_hours = None
    if modified_seconds is not None:
        age_hours = round((time.time() - modified_seconds) / 3600, 2)
    stale = bool(age_hours is not None and age_hours > HANDOFF_DRAFT_STALE_HOURS)
    status = "reviewed" if lint_result.valid else "pending"
    ingest_outcome = _latest_ingest_outcome_for_path(target, path, ingest_outcomes)
    return HandoffDraft(
        id=path.stem,
        path=path,
        inbox=inbox,
        created_at=created_at,
        modified_at=modified_at,
        age_hours=age_hours,
        stale=stale,
        lint=lint_result,
        action=action,
        target_card=target_card,
        target_document=target_document,
        source_import_id=source_import_id,
        source_fingerprint=source_fingerprint,
        scanner_provenance=scanner_provenance,
        status=status,
        watched=watched,
        ingestion_status=str(ingest_outcome.get("status")) if ingest_outcome and ingest_outcome.get("status") else None,
        ingest_run_id=str(ingest_outcome.get("run_id")) if ingest_outcome and ingest_outcome.get("run_id") else None,
        ingest_log_path=str(ingest_outcome.get("log_path")) if ingest_outcome and ingest_outcome.get("log_path") else None,
    )


def _drafts(target: Path, sources: Path | None = None) -> tuple[list[HandoffDraft], list[str], bool]:
    target = target.expanduser().resolve()
    paths, errors, loaded = _draft_paths(target, sources=sources)
    ingest_outcomes = _ingest_outcomes_by_path(target)
    drafts = [
        _draft_summary(path, target=target, inbox=inbox, watched=watched, ingest_outcomes=ingest_outcomes)
        for path, inbox, watched in paths
    ]
    drafts.sort(key=lambda item: str(item.modified_at or item.id), reverse=True)
    return drafts, errors, loaded


def _archive_records(target: Path) -> list[dict[str, Any]]:
    path = _handoff_archive_records_path(target)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _write_archive_records(target: Path, records: list[dict[str, Any]]) -> None:
    path = _handoff_archive_records_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _append_archive_record(target: Path, record: dict[str, Any]) -> None:
    path = _handoff_archive_records_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _archive_record_with_ingest_outcome(target: Path, record: dict[str, Any], outcomes: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    outcomes = outcomes if outcomes is not None else _ingest_outcomes_by_path(target)
    outcome = None
    for key in ("archive_path", "path"):
        value = record.get(key)
        if isinstance(value, str) and value:
            outcome = _latest_ingest_outcome_for_path(target, Path(value), outcomes)
            if outcome is not None:
                break
    if outcome is None:
        return record
    updated = dict(record)
    updated["ingestion_status"] = outcome.get("status")
    updated["ingest_run_id"] = outcome.get("run_id")
    updated["ingest_log_path"] = outcome.get("log_path")
    if outcome.get("target_card") and not updated.get("target_card"):
        updated["target_card"] = outcome.get("target_card")
    if outcome.get("target_document") and not updated.get("target_document"):
        updated["target_document"] = outcome.get("target_document")
    return updated


def _refresh_archive_ingest_outcomes(target: Path) -> list[dict[str, Any]]:
    records = _archive_records(target)
    if not records:
        return []
    outcomes = _ingest_outcomes_by_path(target)
    refreshed = [_archive_record_with_ingest_outcome(target, record, outcomes) for record in records]
    if refreshed != records:
        _write_archive_records(target, refreshed)
    return refreshed


def _draft_source_import_issues(target: Path, drafts: list[HandoffDraft]) -> tuple[list[str], list[str]]:
    from . import work_cmd

    imports_by_id = {
        str(item.get("id")): item
        for item in work_cmd._read_imports(target)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    missing: list[str] = []
    changed: list[str] = []
    for draft in drafts:
        if not draft.source_import_id:
            continue
        item = imports_by_id.get(draft.source_import_id)
        if item is None:
            missing.append(draft.id)
            continue
        if draft.source_fingerprint:
            current = work_cmd._import_fingerprint(item)
            if current and current != draft.source_fingerprint:
                changed.append(draft.id)
    return missing, changed


def draft_queue_payload(target: Path, sources: Path | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    drafts, errors, sources_loaded = _drafts(target, sources=sources)
    archives = _refresh_archive_ingest_outcomes(target)
    receipts = _ingest_receipts(target)
    stale = [draft.id for draft in drafts if draft.stale and draft.status != "archived"]
    unreconciled = [
        draft.id
        for draft in drafts
        if draft.stale and draft.ingestion_status is None and draft.status != "archived"
    ]
    invalid = [draft.id for draft in drafts if not draft.lint.valid]
    uncovered = [draft.id for draft in drafts if not draft.watched]
    missing_imports, changed_fingerprints = _draft_source_import_issues(target, drafts)
    checks = [
        {
            "status": WARN if errors else OK,
            "name": "handoff_draft_sources",
            "detail": "; ".join(errors) if errors else ("configured" if sources_loaded else "default writer inboxes"),
            "items": errors,
        },
        {
            "status": WARN if stale else OK,
            "name": "handoff_draft_stale",
            "detail": f"{len(stale)} stale pending handoff draft(s)" if stale else "none",
            "items": stale[:10],
        },
        {
            "status": WARN if unreconciled else OK,
            "name": "handoff_draft_unreconciled",
            "detail": f"{len(unreconciled)} stale handoff draft(s) not represented in recent ingest receipts" if unreconciled else "none",
            "items": unreconciled[:10],
        },
        {
            "status": WARN if invalid else OK,
            "name": "handoff_draft_invalid",
            "detail": f"{len(invalid)} invalid handoff draft(s)" if invalid else "none",
            "items": invalid[:10],
        },
        {
            "status": WARN if missing_imports else OK,
            "name": "handoff_draft_missing_source_import",
            "detail": f"{len(missing_imports)} handoff draft(s) reference missing source imports" if missing_imports else "none",
            "items": missing_imports[:10],
        },
        {
            "status": WARN if changed_fingerprints else OK,
            "name": "handoff_draft_changed_source_fingerprint",
            "detail": f"{len(changed_fingerprints)} handoff draft(s) have changed source fingerprints" if changed_fingerprints else "none",
            "items": changed_fingerprints[:10],
        },
        {
            "status": WARN if uncovered else OK,
            "name": "handoff_draft_uncovered_inbox",
            "detail": f"{len(uncovered)} handoff draft(s) are in inboxes not covered by source config" if uncovered else "none",
            "items": uncovered[:10],
        },
    ]
    issues = [check for check in checks if check["status"] != OK]
    return {
        "target": str(target),
        "handoff_root": str(_handoff_state_root(target)),
        "drafts": [draft.as_dict() for draft in drafts],
        "archives": archives,
        "ingest_runs_root": str(_handoff_ingest_runs_root(target)),
        "latest_ingest_run": _receipt_summary(receipts[0]) if receipts else None,
        "counts": {
            "pending": len([draft for draft in drafts if draft.status == "pending"]),
            "reviewed": len([draft for draft in drafts if draft.status == "reviewed"]),
            "archived": len(archives),
            "ingested": len([draft for draft in drafts if draft.ingestion_status == "ingested"]),
            "skipped": len([draft for draft in drafts if draft.ingestion_status == "skipped"]),
            "failed": len([draft for draft in drafts if draft.ingestion_status == "failed"]),
            "total": len(drafts),
        },
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _find_draft(target: Path, draft_id_or_path: str, sources: Path | None = None) -> tuple[HandoffDraft | None, str | None]:
    target = target.expanduser().resolve()
    raw_path = Path(draft_id_or_path).expanduser()
    candidates: list[HandoffDraft] = []
    drafts, errors, _ = _drafts(target, sources=sources)
    if errors:
        return None, "; ".join(errors)
    if raw_path.is_absolute() or len(raw_path.parts) > 1:
        path = raw_path if raw_path.is_absolute() else target / raw_path
        resolved = path.resolve()
        candidates = [draft for draft in drafts if draft.path == resolved]
    else:
        candidates = [
            draft
            for draft in drafts
            if draft.id == draft_id_or_path
            or draft.path.name == draft_id_or_path
            or draft.id.startswith(draft_id_or_path)
        ]
    if not candidates:
        return None, f"handoff draft not found: {draft_id_or_path}"
    if len(candidates) > 1:
        return None, f"handoff draft id is ambiguous: {draft_id_or_path}"
    return candidates[0], None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:48].strip("-") or "handoff"


def _read_suggested_content(content: str | None, content_file: Path | None) -> str:
    if content and content_file:
        raise ValueError("pass --content or --content-file, not both")
    if content is not None:
        return content.strip()
    if content_file is None:
        raise ValueError("--content or --content-file is required")
    try:
        return content_file.expanduser().read_text(errors="replace").strip()
    except OSError as exc:
        raise ValueError(f"cannot read --content-file: {exc}") from exc


def _draft_inbox_path(target: Path, inbox: str) -> tuple[Path, str]:
    inbox = inbox.strip()
    if not inbox:
        inbox = DEFAULT_DRAFT_INBOX
    inbox = _WRITER_INBOX_MAP.get(inbox, inbox)
    path = Path(inbox).expanduser()
    if not path.is_absolute():
        path = target / path
    return path.resolve(), inbox


def _render_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item.strip()}" for item in items if item.strip())


def _render_handoff_draft(
    *,
    handoff_type: str,
    title: str,
    summary: str,
    facts: list[str],
    evidence: list[str],
    action: str,
    target_card: str | None,
    target_document: str | None,
    suggested_content: str,
) -> str:
    parts = [
        "# Memory Handoff",
        "",
        "## Type",
        "",
        handoff_type.strip(),
        "",
        "## Title",
        "",
        title.strip(),
        "",
        "## Summary",
        "",
        summary.strip(),
    ]
    if any(item.strip() for item in facts):
        parts.extend(["", "## Durable facts", "", _render_bullets(facts)])
    if any(item.strip() for item in evidence):
        parts.extend(["", "## Evidence", "", _render_bullets(evidence)])
    parts.extend(["", "## Recommended memory action", "", action])
    if action in CARD_ACTIONS:
        parts.extend(["", "## Target card", "", str(target_card or "").strip()])
        parts.extend(["", "## Suggested card content", "", suggested_content.strip()])
    else:
        parts.extend(["", "## Target document", "", str(target_document or "").strip()])
        parts.extend(["", "## Suggested document content", "", suggested_content.strip()])
    return "\n".join(parts).rstrip() + "\n"


def draft(
    *,
    target: Path,
    title: str,
    summary: str,
    content: str | None = None,
    content_file: Path | None = None,
    handoff_type: str = "workflow",
    action: str = NO_CARD_ACTION,
    target_card: str | None = None,
    target_document: str | None = DEFAULT_DRAFT_DOCUMENT,
    fact: list[str] | None = None,
    evidence: list[str] | None = None,
    inbox: str = DEFAULT_DRAFT_INBOX,
    draft_id: str | None = None,
    force: bool = False,
    guard: bool = False,
    guard_policy: str = "personal",
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    action = action.strip().casefold()
    if action not in HANDOFF_ACTIONS:
        print("error: --action must be one of: " + ", ".join(HANDOFF_ACTIONS), file=sys.stderr)
        return 2
    if not title.strip():
        print("error: --title is required", file=sys.stderr)
        return 2
    if not summary.strip():
        print("error: --summary is required", file=sys.stderr)
        return 2
    if action in CARD_ACTIONS:
        if not target_card:
            print("error: --target-card is required for card handoffs", file=sys.stderr)
            return 2
        target_document = None
    else:
        if not target_document:
            print("error: --target-document is required for no-card handoffs", file=sys.stderr)
            return 2
        target_card = None
    try:
        suggested_content = _read_suggested_content(content, content_file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not suggested_content:
        print("error: suggested content is empty", file=sys.stderr)
        return 2
    inbox_path, inbox_label = _draft_inbox_path(target, inbox)
    created_at = _iso_from_timestamp()
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    safe_id = _slugify(draft_id or title)
    filename = f"{timestamp}-{safe_id}.md"
    path = inbox_path / filename
    if path.exists() and not force:
        print(f"error: handoff draft already exists: {path}", file=sys.stderr)
        return 2
    text = _render_handoff_draft(
        handoff_type=handoff_type,
        title=title,
        summary=summary,
        facts=fact or [],
        evidence=evidence or [],
        action=action,
        target_card=target_card,
        target_document=target_document,
        suggested_content=suggested_content,
    )
    inbox_path.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    lint_result = lint_file(path)
    guard_result = _guard_handoff_path(path, target=target, policy=guard_policy) if guard else None
    payload = {
        "target": str(target),
        "created_at": created_at,
        "path": str(path),
        "id": path.stem,
        "inbox": inbox_label,
        "action": action,
        "target_card": target_card,
        "target_document": target_document,
        "lint": lint_result.as_dict(),
        "content_guard": guard_result,
        "valid": lint_result.valid and (guard_result is None or guard_result.get("exit_code") == 0),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"handoff draft: {path}")
    print(f"id: {payload['id']}")
    print(f"inbox: {inbox_label}")
    print(f"action: {action}")
    if target_card:
        print(f"target_card: {target_card}")
    if target_document:
        print(f"target_document: {target_document}")
    print(f"lint: {'ok' if lint_result.valid else 'fail'}")
    if guard_result is not None:
        guard_ok = guard_result.get("exit_code") == 0
        print(f"content_guard: {'ok' if guard_ok else 'fail'} ({guard_policy})")
        if not guard_ok:
            print(f"content_guard_detail: {guard_result.get('detail')}")
    for error in lint_result.errors:
        print(f"error: {error}")
    return 0 if payload["valid"] else 1


def list_drafts(*, target: Path, sources: Path | None = None, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = draft_queue_payload(target, sources=sources)
    payload["drafts"] = payload["drafts"][:limit]
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff drafts: {target}")
    print(f"handoff_root: {payload['handoff_root']}")
    counts = payload["counts"]
    print(f"drafts: {counts['total']}")
    print(f"pending: {counts['pending']}")
    print(f"reviewed: {counts['reviewed']}")
    print(f"archived: {counts['archived']}")
    if payload.get("latest_ingest_run"):
        latest = payload["latest_ingest_run"]
        print(f"latest_ingest_run: {latest.get('run_id')} completed={latest.get('completed_at')}")
    for draft in payload["drafts"]:
        target_value = draft.get("target_document") or draft.get("target_card") or ""
        ingest = draft.get("ingestion_status") or "unreconciled"
        print(
            f"- {draft.get('id')} [{draft.get('status')}] "
            f"lint={'ok' if draft.get('lint', {}).get('valid') else 'fail'} "
            f"ingest={ingest} target={target_value}: {draft.get('path')}"
        )
    return 0


def show_draft(*, target: Path, draft_id: str, sources: Path | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    draft, error = _find_draft(target, draft_id, sources=sources)
    if draft is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = {"target": str(target), "draft": draft.as_dict()}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff: {draft.id}")
    print(f"status: {draft.status}")
    print(f"path: {draft.path}")
    print(f"inbox: {draft.inbox}")
    print(f"modified_at: {draft.modified_at}")
    print(f"age_hours: {draft.age_hours}")
    print(f"stale: {draft.stale}")
    print(f"lint: {'ok' if draft.lint.valid else 'fail'}")
    print(f"ingestion_status: {draft.ingestion_status or 'unreconciled'}")
    if draft.ingest_run_id:
        print(f"ingest_run_id: {draft.ingest_run_id}")
    if draft.ingest_log_path:
        print(f"ingest_log_path: {draft.ingest_log_path}")
    print(f"action: {draft.action}")
    if draft.target_card:
        print(f"target_card: {draft.target_card}")
    if draft.target_document:
        print(f"target_document: {draft.target_document}")
    if draft.source_import_id:
        print(f"source_import_id: {draft.source_import_id}")
    if draft.source_fingerprint:
        print(f"source_fingerprint: {draft.source_fingerprint}")
    if draft.scanner_provenance:
        print("scanner_provenance:")
        for key in sorted(draft.scanner_provenance):
            print(f"  {key}: {draft.scanner_provenance[key]}")
    for error in draft.lint.errors:
        print(f"error: {error}")
    return 0


def _archive_one(target: Path, draft: HandoffDraft, *, reason: str | None = None) -> dict[str, Any]:
    archived_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    archive_dir = _handoff_archive_root(target) / archived_at[:10]
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / draft.path.name
    if destination.exists():
        destination = archive_dir / f"{draft.path.stem}-{hashlib.sha1(str(draft.path).encode()).hexdigest()[:8]}{draft.path.suffix}"
    shutil.move(str(draft.path), str(destination))
    record = {
        "id": draft.id,
        "status": "archived",
        "previous_status": draft.status,
        "path": str(draft.path),
        "archive_path": str(destination),
        "archived_at": archived_at,
        "review_reason": reason or "reviewed handoff draft archived",
        "reviewed_at": archived_at,
        "source_import_id": draft.source_import_id,
        "source_fingerprint": draft.source_fingerprint,
        "target_card": draft.target_card,
        "target_document": draft.target_document,
        "ingestion_status": draft.ingestion_status,
        "ingest_run_id": draft.ingest_run_id,
        "ingest_log_path": draft.ingest_log_path,
    }
    _append_archive_record(target, record)
    return record


def archive_draft(
    *,
    target: Path,
    draft_id: str | None = None,
    all_reviewed: bool = False,
    reason: str | None = None,
    sources: Path | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if all_reviewed and draft_id:
        print("error: pass a handoff id/path or --all-reviewed, not both", file=sys.stderr)
        return 2
    archived: list[dict[str, Any]] = []
    if all_reviewed:
        drafts, errors, _ = _drafts(target, sources=sources)
        if errors:
            print(f"error: {'; '.join(errors)}", file=sys.stderr)
            return 2
        for draft in drafts:
            if draft.lint.valid:
                archived.append(_archive_one(target, draft, reason=reason))
    else:
        if not draft_id:
            print("error: handoff id/path is required unless --all-reviewed is passed", file=sys.stderr)
            return 2
        draft, error = _find_draft(target, draft_id, sources=sources)
        if draft is None:
            print(f"error: {error}", file=sys.stderr)
            return 1 if error and "not found" in error else 2
        archived.append(_archive_one(target, draft, reason=reason))
    payload = {
        "target": str(target),
        "archive_path": str(_handoff_archive_records_path(target)),
        "archived": len(archived),
        "records": archived,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff archive: {target}")
    print(f"archived: {len(archived)}")
    for record in archived:
        print(f"- {record['id']} -> {record['archive_path']}")
    return 0


def _draft_closeout_fingerprint(draft: HandoffDraft) -> str:
    if draft.source_fingerprint:
        return draft.source_fingerprint
    stable = {
        "id": draft.id,
        "path": str(draft.path),
        "modified_at": draft.modified_at,
        "target_card": draft.target_card,
        "target_document": draft.target_document,
        "ingestion_status": draft.ingestion_status,
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def closeout(
    *,
    target: Path,
    draft_id: str | None = None,
    all_pending: bool = False,
    reason: str | None = None,
    defer: bool = False,
    sources: Path | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if draft_id and all_pending:
        print("error: pass a handoff id/path or --all, not both", file=sys.stderr)
        return 2
    if not draft_id and not all_pending:
        all_pending = True
    if all_pending:
        drafts, errors, _ = _drafts(target, sources=sources)
        if errors:
            print(f"error: {'; '.join(errors)}", file=sys.stderr)
            return 2
        selected = [draft for draft in drafts if draft.status != "archived"]
    else:
        draft, error = _find_draft(target, draft_id or "", sources=sources)
        if draft is None:
            print(f"error: {error}", file=sys.stderr)
            return 1 if error and "not found" in error else 2
        selected = [draft]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    closeout_id = f"{created_at.replace(':', '').replace('+', 'Z')}-handoff-closeout"
    status = "deferred" if defer else "reviewed"
    records = []
    for draft in selected:
        records.append(
            {
                "id": draft.id,
                "path": str(draft.path),
                "status": draft.status,
                "lint_valid": draft.lint.valid,
                "ingestion_status": draft.ingestion_status,
                "target_card": draft.target_card,
                "target_document": draft.target_document,
                "source_import_id": draft.source_import_id,
                "source_fingerprint": draft.source_fingerprint,
                "closeout_fingerprint": _draft_closeout_fingerprint(draft),
            }
        )
    payload = {
        "target": str(target),
        "closeout_id": closeout_id,
        "created_at": created_at,
        "status": status,
        "reason": reason or ("handoff drafts deferred" if defer else "handoff drafts reviewed"),
        "draft_count": len(records),
        "drafts": records,
        "source_fingerprints": [item["closeout_fingerprint"] for item in records],
        "path": str(_handoff_closeouts_root(target) / closeout_id / "closeout.json"),
    }
    _write_json(Path(payload["path"]), payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff closeout: {closeout_id}")
    print(f"status: {status}")
    print(f"drafts: {len(records)}")
    print(f"path: {payload['path']}")
    for record in records[:20]:
        print(f"- {record['id']} [{record['status']}] ingest={record.get('ingestion_status') or 'unreconciled'}")
    return 0


def runs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipts = _ingest_receipts(target)
    payload = {
        "target": str(target),
        "runs_root": str(_handoff_ingest_runs_root(target)),
        "count": len(receipts),
        "runs": [_receipt_summary(receipt) for receipt in receipts[:limit]],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff ingest runs: {target}")
    print(f"runs_root: {payload['runs_root']}")
    print(f"runs: {payload['count']}")
    for item in payload["runs"]:
        print(
            f"- {item.get('run_id')} completed={item.get('completed_at')} "
            f"processed={item.get('processed')} skipped={item.get('skipped')} "
            f"failed={item.get('failed')} warnings={item.get('warning_count')}"
        )
    return 0


def run_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    matches = [receipt for receipt in _ingest_receipts(target) if str(receipt.get("run_id")) == run_id or str(receipt.get("run_id", "")).startswith(run_id)]
    if not matches:
        print(f"error: handoff ingest run not found: {run_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: handoff ingest run id is ambiguous: {run_id}", file=sys.stderr)
        return 2
    receipt = matches[0]
    payload = {"target": str(target), "run": receipt}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff ingest run: {receipt.get('run_id')}")
    print(f"started_at: {receipt.get('started_at')}")
    print(f"completed_at: {receipt.get('completed_at')}")
    print(f"source_root: {receipt.get('source_root')}")
    print(f"warning_count: {receipt.get('warning_count')}")
    if receipt.get("safe_summary"):
        print(f"safe_summary: {receipt.get('safe_summary')}")
    if receipt.get("log_path"):
        print(f"log_path: {receipt.get('log_path')}")
    print(f"processed: {len(receipt.get('processed_handoff_paths') or [])}")
    print(f"skipped: {len(receipt.get('skipped_handoff_paths') or [])}")
    print(f"failed: {len(receipt.get('failed_handoff_paths') or [])}")
    print(f"malformed: {len(receipt.get('malformed_handoff_paths') or [])}")
    print(f"unreachable_sources: {len(receipt.get('unreachable_sources') or [])}")
    print(f"no_reply: {'yes' if receipt.get('no_reply') else 'no'}")
    for outcome in receipt.get("outcomes") or []:
        if isinstance(outcome, dict):
            print(f"- {outcome.get('status')} {outcome.get('path')}")
    return 0


def reconcile(*, target: Path, sources: Path | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    config, sources_path, errors, _ = _load_source_config_for_drafts(target, sources=sources)
    if errors:
        print(f"error: {'; '.join(errors)}", file=sys.stderr)
        return 2
    if config.ingestor is None:
        print("error: ingestor.last_run_log is not configured", file=sys.stderr)
        return 2
    log_path = config.ingestor.log_path
    if not log_path.is_file():
        print(f"error: ingestor last_run_log not found: {log_path}", file=sys.stderr)
        return 1
    try:
        text = log_path.read_text(errors="replace")
    except OSError as exc:
        print(f"error: cannot read ingestor log: {exc}", file=sys.stderr)
        return 1
    receipt = _parse_ingestor_log_receipt(target, config, log_path, text)
    path = _ingest_receipt_path(target, str(receipt["run_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    archives = _refresh_archive_ingest_outcomes(target)
    payload = {
        "target": str(target),
        "sources_path": str(sources_path) if sources_path else None,
        "receipt_path": str(path),
        "run": receipt,
        "archive_records_refreshed": len(archives),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff reconcile: {target}")
    print(f"receipt: {path}")
    print(f"run_id: {receipt['run_id']}")
    print(f"processed: {len(receipt['processed_handoff_paths'])}")
    print(f"skipped: {len(receipt['skipped_handoff_paths'])}")
    print(f"failed: {len(receipt['failed_handoff_paths'])}")
    print(f"warnings: {receipt['warning_count']}")
    return 0


def issues(
    *,
    target: Path,
    sources: Path | None = None,
    json_output: bool = False,
    limit: int = 20,
    categories: list[str] | None = None,
) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    found = collect_issues(target, sources=sources, categories=categories)
    payload = _issues_payload(target, found)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff issues: {target}")
    print(f"issues: {payload['count']}")
    if not found:
        return 0
    print("groups:")
    for category, count in payload["by_category"].items():
        print(f"- {category}: {count}")
    print("items:")
    for issue in found[:limit]:
        print(f"- {issue.id} [{issue.category}] {issue.kind}: {_short(issue.text)}")
        print(f"  repair: {_short(issue.repair, 140)}")
        print(f"  evidence: {_short(issue.evidence, 160)}")
    if len(found) > limit:
        print(f"... {len(found) - limit} more")
    return 0


def import_issues(
    *,
    target: Path,
    sources: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
    categories: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    found = collect_issues(target, sources=sources, categories=categories)
    records = [issue.as_import_record() for issue in found]
    from . import work_cmd

    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "imports_path": str(work_cmd._imports_path(target)),
        "dry_run": dry_run,
        "issues": len(found),
        "imported": len(imported),
        "skipped_duplicates": len(skipped),
        "skipped_dismissed": len(skipped_dismissed),
        "by_category": _issue_counts(found),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff issue imports: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"dry_run: {dry_run}")
    print(f"issues: {len(found)}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {_short(str(item.get('text', '')))}")
    return 0


def sync_issues(
    *,
    target: Path,
    sources: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
    categories: list[str] | None = None,
    close_stale: bool = True,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    found = collect_issues(target, sources=sources, categories=categories)
    current_ids = {issue.id for issue in found}
    known_ids = _known_local_issue_ids(target)
    covered_summary_ids = _covered_warning_summary_ids(found, known_ids)
    new_issues = [
        issue
        for issue in found
        if issue.id not in known_ids and issue.id not in covered_summary_ids
    ]
    records = [issue.as_import_record() for issue in new_issues]
    from . import work_cmd

    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    stale = (
        _close_stale_local_issue_work(
            target,
            current_ids=current_ids,
            close_current_ids=covered_summary_ids,
            categories=categories,
            dry_run=dry_run,
        )
        if close_stale
        else {"imports": [], "tasks": []}
    )
    payload = {
        "target": str(target),
        "imports_path": str(work_cmd._imports_path(target)),
        "dry_run": dry_run,
        "close_stale": close_stale,
        "issues": len(found),
        "known_issues": len(known_ids.intersection(current_ids)),
        "covered_summary_issues": len(covered_summary_ids),
        "new_issues": len(new_issues),
        "imported": len(imported),
        "skipped_duplicates": len(skipped),
        "skipped_dismissed": len(skipped_dismissed),
        "stale_imports_closed": len(stale["imports"]),
        "stale_tasks_closed": len(stale["tasks"]),
        "by_category": _issue_counts(found),
        "imports": imported,
        "stale_imports": stale["imports"],
        "stale_tasks": stale["tasks"],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff issue sync: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"dry_run: {dry_run}")
    print(f"close_stale: {close_stale}")
    print(f"issues: {len(found)}")
    print(f"known: {payload['known_issues']}")
    print(f"covered_summary: {payload['covered_summary_issues']}")
    print(f"new: {len(new_issues)}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    print(f"stale_imports_closed: {len(stale['imports'])}")
    print(f"stale_tasks_closed: {len(stale['tasks'])}")
    for item in imported:
        print(f"- imported {item.get('id')} [{item.get('kind')}] {_short(str(item.get('text', '')))}")
    for item in stale["imports"]:
        print(f"- closed import {item.get('id')} {_short(str(item.get('text', '')))}")
    for task in stale["tasks"]:
        print(f"- closed task {task.get('id')} {_short(str(task.get('text', '')))}")
    return 0


def doctor(*, target: Path, sources: Path | None = None, json_output: bool = False) -> int:
    if not target.expanduser().exists():
        print(f"error: target does not exist: {target}", file=sys.stderr)
        return 2
    health = inspect(target, sources=sources)
    if json_output:
        print(json.dumps(health.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"handoff doctor: {health.target}")
        print(f"sources: {health.sources_path if health.sources_path else '(not configured)'}")
        for status, name, detail in doctor_checks(health.target, sources=health.sources_path):
            print(f"[{status}] {name}: {detail}")
    return 1 if health.failures else 0


def sources_init(*, target: Path, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = default_sources_path(target)
    if path.exists() and not force:
        print(f"error: handoff source config already exists: {path}", file=sys.stderr)
        return 2
    payload = {
        "_description": "Local handoff source coverage. Relative roots resolve from this repo or workspace target.",
        "canonical_owner": "openclaw",
        "ingestor": {
            "last_run_log": ".brigade/handoff-ingest/latest.log",
            "stale_after_minutes": DEFAULT_STALE_AFTER_MINUTES,
            "warning_patterns": list(DEFAULT_WARNING_PATTERNS),
        },
        "sources": [
            {
                "root": ".",
                "inboxes": list(WRITER_INBOXES),
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    output = {
        "target": str(target),
        "path": str(path),
        "written": True,
        "inboxes": list(WRITER_INBOXES),
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"handoff_sources: {path}")
    print(f"inboxes: {', '.join(WRITER_INBOXES)}")
    print("next_command: brigade handoff doctor")
    return 0


def _resolve_lint_path(target: Path, path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        path = target / path
    return path.resolve()


def _pending_handoff_paths(target: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for rel in WRITER_INBOXES:
        inbox = target / rel
        if not inbox.is_dir():
            continue
        for candidate in inbox.glob("*.md"):
            if not candidate.is_file():
                continue
            if candidate.name.startswith(".") or candidate.name in IGNORED_HANDOFF_NAMES:
                continue
            paths.append(candidate.resolve())
    return tuple(sorted(paths))


def _parse_markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _section_value(sections: dict[str, str], name: str) -> str:
    raw = sections.get(name, "")
    lines: list[str] = []
    in_comment = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--"):
            in_comment = not stripped.endswith("-->")
            continue
        if in_comment:
            if stripped.endswith("-->"):
                in_comment = False
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _lint_card_action(
    sections: dict[str, str],
    errors: list[str],
    warnings: list[str],
) -> None:
    target_card = _section_value(sections, "Target card")
    if not target_card:
        errors.append("card handoffs require Target card")
    elif not CARD_TARGET_PATTERN.fullmatch(target_card.splitlines()[0].strip()):
        errors.append("Target card must be a filename like project-context.md with no path separators")

    suggested_card = _section_value(sections, "Suggested card content")
    if not suggested_card:
        errors.append("card handoffs require Suggested card content")
    elif not suggested_card.startswith("---"):
        errors.append("Suggested card content must start with YAML frontmatter")

    for prohibited in ("Target document", "Suggested document content"):
        if prohibited in sections:
            errors.append(f"card handoffs must omit the {prohibited} section entirely")

    if any(line.startswith("## ") for line in suggested_card.splitlines()):
        warnings.append("Suggested card content contains level-2 markdown headings, which may be parsed as handoff sections")


def _lint_no_card_action(sections: dict[str, str], errors: list[str]) -> None:
    target_document = _section_value(sections, "Target document")
    if not target_document:
        errors.append("no-card handoffs require Target document")
    elif not _valid_document_target(target_document.splitlines()[0].strip()):
        errors.append("Target document must be TOOLS.md, USER.md, rules/*.md, or .learnings/*.md")

    suggested_document = _section_value(sections, "Suggested document content")
    if not suggested_document:
        errors.append("no-card handoffs require Suggested document content")
    elif any(line.startswith("## ") for line in suggested_document.splitlines()):
        errors.append("Suggested document content must not contain level-2 markdown headings")

    for prohibited in ("Target card", "Suggested card content"):
        if prohibited in sections:
            errors.append(f"no-card handoffs must omit the {prohibited} section entirely")


def _valid_document_target(value: str) -> bool:
    if value.startswith("/") or ".." in Path(value).parts:
        return False
    if value in DOCUMENT_TARGETS:
        return True
    return any(value.startswith(prefix) and value.endswith(".md") for prefix in DOCUMENT_TARGET_PREFIXES)


def _lint_repair_for_result(result: HandoffLintResult) -> str:
    if result.action in CARD_ACTIONS:
        return (
            "Keep only the card branch in the handoff: Recommended memory action "
            f"{result.action}, Target card, and Suggested card content. "
            "Delete Target document and Suggested document content sections entirely."
        )
    if result.action == NO_CARD_ACTION:
        return (
            "Keep only the document branch in the handoff: Recommended memory action no-card, "
            "Target document, and Suggested document content. Delete Target card and "
            "Suggested card content sections entirely."
        )
    return "Rewrite the handoff with exactly one valid action branch before rerunning the ingestor."


def _known_local_issue_ids(target: Path) -> set[str]:
    from . import work_cmd

    known: set[str] = set()
    for item in work_cmd._read_imports(target):
        issue_id = _handoff_issue_id(item)
        if issue_id:
            known.add(issue_id)
    ledger = work_cmd._read_task_ledger(target)
    for task in ledger.get("tasks", []):
        issue_id = _handoff_issue_id(task) if isinstance(task, dict) else None
        if issue_id:
            known.add(issue_id)
    return known


def _close_stale_local_issue_work(
    target: Path,
    *,
    current_ids: set[str],
    close_current_ids: set[str],
    categories: list[str] | None,
    dry_run: bool,
) -> dict[str, list[dict[str, Any]]]:
    from . import work_cmd

    wanted_categories = {category for category in categories or [] if category}
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    closed_imports: list[dict[str, Any]] = []
    imports = work_cmd._read_imports(target)
    for item in imports:
        if not isinstance(item, dict) or item.get("status", "pending") != "pending":
            continue
        if item.get("source") != ISSUE_SOURCE:
            continue
        issue_id = _handoff_issue_id(item)
        if not issue_id:
            continue
        if issue_id in current_ids and issue_id not in close_current_ids:
            continue
        if wanted_categories and _handoff_issue_category(item) not in wanted_categories:
            continue
        updated = dict(item)
        updated["status"] = "dismissed"
        updated["updated_at"] = now
        updated["dismissed_at"] = now
        updated["dismiss_reason"] = _stale_close_reason(issue_id, close_current_ids)
        item.update(updated)
        closed_imports.append(updated)
    if closed_imports and not dry_run:
        work_cmd._write_imports(target, imports)

    closed_tasks: list[dict[str, Any]] = []
    ledger = work_cmd._read_task_ledger(target)
    for task in ledger.get("tasks", []):
        if not isinstance(task, dict) or task.get("status", "pending") != "pending":
            continue
        if task.get("source") != f"import:{ISSUE_SOURCE}":
            continue
        issue_id = _handoff_issue_id(task)
        if not issue_id:
            continue
        if issue_id in current_ids and issue_id not in close_current_ids:
            continue
        if wanted_categories and _handoff_issue_category(task) not in wanted_categories:
            continue
        updated = dict(task)
        updated["status"] = "done"
        updated["updated_at"] = now
        updated["completed_at"] = now
        updated["completion_reason"] = _stale_close_reason(issue_id, close_current_ids)
        task.update(updated)
        closed_tasks.append(updated)
    if closed_tasks and not dry_run:
        work_cmd._write_task_ledger(target, ledger)

    return {
        "imports": closed_imports,
        "tasks": closed_tasks,
    }


def _handoff_issue_id(item: dict[str, Any]) -> str | None:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    issue_id = metadata.get("handoff_issue_id")
    return issue_id if isinstance(issue_id, str) and issue_id else None


def _handoff_issue_category(item: dict[str, Any]) -> str | None:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    category = metadata.get("handoff_issue_category")
    return category if isinstance(category, str) and category else None


def _covered_warning_summary_ids(found: list[HandoffIssue], known_ids: set[str]) -> set[str]:
    concrete = [issue for issue in found if issue.category not in {"warning-summary", "hidden-warning"}]
    if not concrete:
        return set()
    if any(issue.id not in known_ids for issue in concrete):
        return set()
    return {issue.id for issue in found if issue.category == "warning-summary"}


def _stale_close_reason(issue_id: str, close_current_ids: set[str]) -> str:
    if issue_id in close_current_ids:
        return "covered by known concrete handoff issue lines"
    return "resolved or absent from latest handoff issue scan"


def _issues_payload(target: Path, found: list[HandoffIssue]) -> dict[str, Any]:
    return {
        "target": str(target),
        "count": len(found),
        "by_category": _issue_counts(found),
        "issues": [issue.as_dict() for issue in found],
    }


def _issue_counts(found: list[HandoffIssue]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in found:
        counts[issue.category] = counts.get(issue.category, 0) + 1
    return dict(sorted(counts.items()))


def _dedupe_issues(issues: list[HandoffIssue]) -> list[HandoffIssue]:
    seen: set[str] = set()
    deduped: list[HandoffIssue] = []
    for issue in issues:
        if issue.id in seen:
            continue
        seen.add(issue.id)
        deduped.append(issue)
    return deduped


def _filter_issues_by_category(
    issues: list[HandoffIssue],
    categories: list[str] | None,
) -> list[HandoffIssue]:
    wanted = {category for category in categories or [] if category}
    if not wanted:
        return issues
    return [issue for issue in issues if issue.category in wanted]


def _make_issue(
    *,
    category: str,
    kind: str,
    text: str,
    repair: str,
    evidence: str,
    metadata: dict[str, Any] | None = None,
) -> HandoffIssue:
    raw_id = f"{category}|{text}|{evidence}"
    digest = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:10]
    return HandoffIssue(
        id=f"handoff-{category}-{digest}",
        category=category,
        kind=kind,
        text=text,
        repair=repair,
        evidence=evidence,
        metadata=metadata or {},
    )


def _handoff_issue_source_key(issue: HandoffIssue) -> str:
    value = issue.metadata.get("source_item_key")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"handoff-ingest:{issue.category}:{issue.id}"


def _handoff_issue_fingerprint(issue: HandoffIssue, metadata: dict[str, Any]) -> str:
    payload = {
        "category": issue.category,
        "kind": issue.kind,
        "text": issue.text,
        "repair": issue.repair,
        "evidence": issue.evidence,
        "metadata": {
            key: value
            for key, value in metadata.items()
            if key not in {"source_fingerprint", "handoff_issue_id"}
        },
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _source_config_for_checks(target: Path, sources_path: Path | None) -> SourceConfig | None:
    if sources_path is None or not sources_path.is_file():
        return None
    try:
        return _load_sources(target, sources_path)
    except ValueError:
        return None


def _parse_ingestor_log_issues(log_path: Path) -> list[HandoffIssue]:
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return [
            _make_issue(
                category="missing-log",
                kind="incident",
                text=f"Read handoff ingestor log at {log_path}",
                repair="Fix file permissions or update ingestor.last_run_log to a readable latest-run log.",
                evidence=str(exc),
                metadata={"log_path": str(log_path)},
            )
        ]
    issues: list[HandoffIssue] = []
    has_warning_summary = False
    has_no_reply_or_update = False
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.casefold()
        if lower.startswith(("skip ", "skipped ")):
            issues.append(_issue_from_log_line("skip", "task", stripped, line_number, log_path))
        elif lower.startswith("promote-skip "):
            issues.append(_issue_from_log_line("promote-skip", "task", stripped, line_number, log_path))
        elif lower.startswith("route-skip "):
            issues.append(_issue_from_log_line("route-skip", "task", stripped, line_number, log_path))
        elif lower.startswith(("fail ", "failed ", "error ")):
            issues.append(_issue_from_log_line("failed", "incident", stripped, line_number, log_path))
        elif _looks_malformed(stripped):
            issues.append(_issue_from_log_line("malformed", "task", stripped, line_number, log_path))
        elif lower.startswith(("warnings:", "warning:")):
            has_warning_summary = True
            issues.append(_issue_from_log_line("warning-summary", "incident", stripped, line_number, log_path))
        if _looks_no_reply(stripped):
            has_no_reply_or_update = True
            issues.append(_issue_from_log_line("no-reply", "incident", stripped, line_number, log_path))
        if _looks_unreachable(stripped):
            issues.append(_issue_from_log_line("source-unreachable", "incident", stripped, line_number, log_path))
    if has_warning_summary and has_no_reply_or_update:
        issues.append(
            _make_issue(
                category="hidden-warning",
                kind="incident",
                text="Fix handoff ingestor no-reply output that can hide warnings",
                repair="Adjust the scheduler or wrapper so warning output is delivered even when the run also emits NO_REPLY or NO_UPDATES.",
                evidence=str(log_path),
                metadata={"log_path": str(log_path)},
            )
        )
    return issues


def _parse_ingestor_log_receipt(target: Path, config: SourceConfig, log_path: Path, text: str) -> dict[str, Any]:
    try:
        stat = log_path.stat()
        timestamp = stat.st_mtime
    except OSError:
        timestamp = time.time()
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:10]
    run_id = f"handoff-ingest-{time.strftime('%Y%m%d%H%M%S', time.gmtime(timestamp))}-{digest}"
    processed: list[str] = []
    promoted: list[dict[str, str | None]] = []
    routed: list[dict[str, str | None]] = []
    skipped: list[str] = []
    failed: list[str] = []
    malformed: list[str] = []
    unreachable: list[str] = []
    warning_events: list[dict[str, Any]] = []
    warning_count = 0
    no_reply = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.casefold()
        if lower.startswith(("warnings:", "warning:")):
            match = re.search(r"Warnings?:\s*(\d+)", stripped, flags=re.IGNORECASE)
            warning_count += int(match.group(1)) if match else 1
            warning_events.append(_warning_event("warning-summary", stripped, line_number))
            continue
        if _looks_no_reply(stripped):
            no_reply = True
            warning_events.append(_warning_event("no-reply", stripped, line_number))
        if lower.startswith(("promoted ", "promote ")):
            path_value, target_value = _split_outcome_line(stripped.split(" ", 1)[1])
            if path_value:
                promoted.append({"handoff_path": path_value, "target": target_value})
                if path_value not in processed:
                    processed.append(path_value)
            continue
        if lower.startswith(("routed ", "route ")):
            path_value, target_value = _split_outcome_line(stripped.split(" ", 1)[1])
            if path_value:
                routed.append({"handoff_path": path_value, "target": target_value})
                if path_value not in processed:
                    processed.append(path_value)
            continue
        if lower.startswith(("processed ", "ingested ")):
            remainder = stripped.split(" ", 1)[1]
            path_value, _ = _split_outcome_line(remainder)
            if path_value and path_value.endswith(".md") and path_value not in processed:
                processed.append(path_value)
            continue
        if lower.startswith(("skip ", "skipped ", "promote-skip ", "route-skip ")):
            path_value, _ = _split_outcome_line(stripped.split(" ", 1)[1])
            if path_value:
                skipped.append(path_value)
            warning_events.append(_warning_event("skip", stripped, line_number))
            continue
        if lower.startswith(("fail ", "failed ", "error ")):
            path_value, _ = _split_outcome_line(stripped.split(" ", 1)[1])
            if path_value:
                failed.append(path_value)
            warning_events.append(_warning_event("failed", stripped, line_number))
            continue
        if _looks_malformed(stripped):
            path_value, _ = _split_outcome_line(stripped.split(" ", 1)[1] if " " in stripped else stripped)
            if path_value:
                malformed.append(path_value)
            warning_events.append(_warning_event("malformed", stripped, line_number))
            continue
        if _looks_unreachable(stripped):
            unreachable.append(_safe_log_subject(stripped))
            warning_events.append(_warning_event("source-unreachable", stripped, line_number))
    if no_reply and warning_count == 0:
        warning_count = 1
    if warning_events and warning_count < len([event for event in warning_events if event.get("category") != "warning-summary"]):
        warning_count = len([event for event in warning_events if event.get("category") != "warning-summary"])
    inbox_paths = [str(watched.root / watched.inbox) for watched in config.watched]
    safe_summary = (
        f"processed={len(processed)}, skipped={len(skipped)}, "
        f"failed={len(failed)}, malformed={len(malformed)}, "
        f"unreachable={len(unreachable)}, warnings={warning_count}"
    )
    receipt = {
        "run_id": run_id,
        "started_at": _iso_from_timestamp(timestamp),
        "completed_at": _iso_from_timestamp(timestamp),
        "source_root": str(target),
        "inbox_paths": inbox_paths,
        "processed_handoff_paths": processed,
        "promoted_card_targets": promoted,
        "routed_document_targets": routed,
        "skipped_handoff_paths": skipped,
        "failed_handoff_paths": failed,
        "malformed_handoff_paths": malformed,
        "unreachable_sources": unreachable,
        "no_reply": no_reply,
        "warning_events": warning_events,
        "warning_count": warning_count,
        "safe_summary": safe_summary,
        "log_path": str(log_path),
    }
    return _normalize_ingest_receipt(target, receipt)


def _split_outcome_line(value: str) -> tuple[str | None, str | None]:
    value = value.strip()
    if not value:
        return None, None
    target_value = None
    if " -> " in value:
        value, target_value = value.split(" -> ", 1)
    if ":" in value:
        value = value.split(":", 1)[0]
    value = value.strip().strip("`")
    if value.startswith("[") and "]" in value:
        value = value.split("]", 1)[1].strip()
    return (value or None, target_value.strip() if isinstance(target_value, str) and target_value.strip() else None)


def _issue_from_log_line(category: str, kind: str, line: str, line_number: int, log_path: Path) -> HandoffIssue:
    subject, detail = _split_issue_line(line)
    repair = _repair_for_issue(category, line)
    text = _text_for_issue(category, subject, detail)
    return _make_issue(
        category=category,
        kind=kind,
        text=text,
        repair=repair,
        evidence=line,
        metadata={
            "log_path": str(log_path),
            "line_number": line_number,
            "subject": subject,
        },
    )


def _split_issue_line(line: str) -> tuple[str, str]:
    if ": " not in line:
        return line, ""
    subject, detail = line.split(": ", 1)
    return subject, detail


def _text_for_issue(category: str, subject: str, detail: str) -> str:
    item = Path(subject.split()[-1]).name if subject else "handoff ingest issue"
    if category == "skip":
        return f"Repair malformed handoff {item}: {detail or 'not parsed'}"
    if category == "promote-skip":
        return f"Fix handoff promotion target for {item}: {detail or 'promotion skipped'}"
    if category == "route-skip":
        return f"Fix handoff routing fields for {item}: {detail or 'route skipped'}"
    if category == "warning-summary":
        return f"Review handoff ingestor warning summary: {subject}"
    if category == "failed":
        return f"Investigate failed handoff ingest for {item}: {detail or 'failed'}"
    if category == "malformed":
        return f"Repair malformed handoff log item {item}: {detail or 'malformed'}"
    if category == "no-reply":
        return "Review handoff ingestor no-reply output"
    if category == "source-unreachable":
        return f"Investigate unreachable handoff source: {subject}"
    return f"Review handoff ingest issue: {subject}"


def _repair_for_issue(category: str, line: str) -> str:
    if category == "skip":
        return "Rewrite the handoff with the standard markdown sections, especially Type, Title, Summary, Recommended memory action, and the matching target section."
    if category == "promote-skip" and "target card does not exist" in line:
        return "Either create the target memory card first, change Recommended memory action to create-card, or correct Target card to an existing card."
    if category == "promote-skip":
        return "Align Recommended memory action, Target card, and Suggested card content so card promotion can succeed."
    if category == "route-skip" and "action is not no-card" in line:
        return "Use Recommended memory action no-card when routing to Target document, or remove Target document and provide a valid card target."
    if category == "route-skip":
        return "Align Recommended memory action, Target document, and Suggested document content so document routing can succeed."
    if category == "warning-summary":
        return "Inspect the latest ingestor log and clear the concrete warning lines before treating the run as clean."
    if category == "failed":
        return "Inspect the failed handoff, fix the underlying ingest error, then rerun the handoff ingestor."
    if category == "malformed":
        return "Rewrite the malformed handoff or adjust the ingestor parser so the handoff is either processed or explicitly skipped with a reason."
    if category == "no-reply":
        return "Adjust the scheduler or wrapper so no-reply output cannot hide warning, failed, skipped, malformed, or unreachable-source states."
    if category == "source-unreachable":
        return "Check network, SSH, mount, or source-path availability, then rerun the handoff ingestor."
    return "Review the latest handoff ingestor log and fix the underlying source or scheduler issue."


def _looks_unreachable(line: str) -> bool:
    lowered = line.casefold()
    return any(token in lowered for token in ("unreachable", "unavailable", "cannot reach", "could not reach", "timed out", "timeout", "no route"))


def _looks_no_reply(line: str) -> bool:
    upper = line.upper()
    return "NO_REPLY" in upper or "NO_UPDATES" in upper


def _looks_malformed(line: str) -> bool:
    lowered = line.casefold()
    return lowered.startswith(("malformed ", "invalid ", "parse-error ", "parse error ")) or "malformed handoff" in lowered or "invalid handoff" in lowered


def _warning_event(category: str, line: str, line_number: int) -> dict[str, Any]:
    return {
        "category": category,
        "line_number": line_number,
        "summary": line[:220],
    }


def _safe_log_subject(line: str) -> str:
    return line[:220]


def _load_sources(target: Path, sources_path: Path) -> SourceConfig:
    try:
        payload = json.loads(sources_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("root must be a JSON object")
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources must be a list")

    watched: list[WatchedInbox] = []
    for index, entry in enumerate(sources):
        if isinstance(entry, str):
            root_value = entry
            inbox_values = list(WRITER_INBOXES)
        elif isinstance(entry, dict):
            root_value = entry.get("root", ".")
            inbox_values = entry.get("inboxes", list(WRITER_INBOXES))
        else:
            raise ValueError(f"sources[{index}] must be an object or string")
        if not isinstance(root_value, str) or not root_value.strip():
            raise ValueError(f"sources[{index}].root must be a non-empty string")
        if not isinstance(inbox_values, list) or not all(isinstance(item, str) for item in inbox_values):
            raise ValueError(f"sources[{index}].inboxes must be a list of strings")
        root = _resolve_source_root(target, root_value)
        for inbox in inbox_values:
            normalized = _normalize_inbox(inbox)
            if normalized:
                watched.append(WatchedInbox(root=root, inbox=normalized))
    return SourceConfig(
        watched=tuple(watched),
        ingestor=_parse_ingestor_config(target, payload),
    )


def _parse_ingestor_config(target: Path, payload: dict[str, Any]) -> IngestorConfig | None:
    ingestor = payload.get("ingestor")
    if ingestor is None:
        return None
    if not isinstance(ingestor, dict):
        raise ValueError("ingestor must be an object")
    log_value = ingestor.get("last_run_log") or ingestor.get("log_path") or ingestor.get("latest_log")
    if log_value is None:
        return None
    if not isinstance(log_value, str) or not log_value.strip():
        raise ValueError("ingestor.last_run_log must be a non-empty string")
    stale_value = ingestor.get("stale_after_minutes", DEFAULT_STALE_AFTER_MINUTES)
    if not isinstance(stale_value, int) or stale_value < 1:
        raise ValueError("ingestor.stale_after_minutes must be a positive integer")
    patterns_value = ingestor.get("warning_patterns", list(DEFAULT_WARNING_PATTERNS))
    if not isinstance(patterns_value, list) or not all(isinstance(item, str) for item in patterns_value):
        raise ValueError("ingestor.warning_patterns must be a list of strings")
    patterns = tuple(item for item in patterns_value if item)
    return IngestorConfig(
        log_path=_resolve_source_root(target, log_value),
        stale_after_minutes=stale_value,
        warning_patterns=patterns,
    )


def _inspect_ingestor(config: IngestorConfig | None) -> IngestorHealth:
    if config is None:
        return IngestorHealth(
            configured=False,
            log_path=None,
            exists=False,
            age_seconds=None,
            stale_after_seconds=None,
            stale=False,
            warnings=(),
        )
    if not config.log_path.is_file():
        return IngestorHealth(
            configured=True,
            log_path=config.log_path,
            exists=False,
            age_seconds=None,
            stale_after_seconds=config.stale_after_minutes * 60,
            stale=False,
            warnings=(f"handoff ingestor log is configured but missing at {config.log_path}",),
        )
    try:
        text = config.log_path.read_text(errors="replace")
        mtime = config.log_path.stat().st_mtime
    except OSError as exc:
        return IngestorHealth(
            configured=True,
            log_path=config.log_path,
            exists=False,
            age_seconds=None,
            stale_after_seconds=config.stale_after_minutes * 60,
            stale=False,
            warnings=(f"handoff ingestor log is unreadable at {config.log_path}: {exc}",),
        )
    age_seconds = max(0, int(time.time() - mtime))
    stale_after_seconds = config.stale_after_minutes * 60
    warnings = _ingestor_warning_lines(text, config.warning_patterns)
    stale = age_seconds > stale_after_seconds
    if stale:
        warnings = (
            f"handoff ingestor log is stale: age={_format_seconds(age_seconds)}, stale_after={_format_seconds(stale_after_seconds)}",
            *warnings,
        )
    return IngestorHealth(
        configured=True,
        log_path=config.log_path,
        exists=True,
        age_seconds=age_seconds,
        stale_after_seconds=stale_after_seconds,
        stale=stale,
        warnings=warnings,
    )


def _ingestor_warning_lines(text: str, patterns: tuple[str, ...]) -> tuple[str, ...]:
    signals: list[str] = []
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern in stripped for pattern in patterns):
            signals.append(f"handoff ingestor warning signal: {stripped[:220]}")
    has_warnings = any("Warnings:" in line for line in lines)
    hidden_no_reply = any(token in text for token in ("NO_REPLY", "NO_UPDATES")) and has_warnings
    if hidden_no_reply:
        signals.append("handoff ingestor warning summary may be hidden behind NO_REPLY or NO_UPDATES")
    unique = tuple(dict.fromkeys(signals))
    if len(unique) <= MAX_INGESTOR_WARNING_SIGNALS:
        return unique
    return (
        *unique[:MAX_INGESTOR_WARNING_SIGNALS],
        f"handoff ingestor warning signal: {len(unique) - MAX_INGESTOR_WARNING_SIGNALS} more warning signals omitted",
    )


def _resolve_source_root(target: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = target / path
    return path.resolve()


def _normalize_inbox(value: str) -> str:
    normalized = value.strip().replace("\\", "/").strip("/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _short(text: str, limit: int = 96) -> str:
    rendered = " ".join(text.split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _inspect_inbox(target: Path, rel: str, watched: tuple[WatchedInbox, ...]) -> InboxHealth:
    path = target / rel
    return InboxHealth(
        inbox=rel,
        path=path,
        exists=path.is_dir(),
        pending=_count_pending(path),
        processed=_count_processed(path),
        watched=_is_watched(target, rel, watched),
        oldest_pending_age_seconds=_oldest_pending_age_seconds(path),
    )


def _oldest_pending_age_seconds(path: Path) -> int | None:
    """Age in seconds of the oldest pending handoff, or None if the inbox is empty."""
    if not path.is_dir():
        return None
    oldest_mtime: float | None = None
    for candidate in path.glob("*.md"):
        if not candidate.is_file():
            continue
        if candidate.name.startswith(".") or candidate.name in IGNORED_HANDOFF_NAMES:
            continue
        mtime = candidate.stat().st_mtime
        if oldest_mtime is None or mtime < oldest_mtime:
            oldest_mtime = mtime
    if oldest_mtime is None:
        return None
    return max(0, int(time.time() - oldest_mtime))


def _count_pending(path: Path) -> int:
    if not path.is_dir():
        return 0
    count = 0
    for candidate in path.glob("*.md"):
        if not candidate.is_file():
            continue
        if candidate.name.startswith(".") or candidate.name in IGNORED_HANDOFF_NAMES:
            continue
        count += 1
    return count


def _count_processed(path: Path) -> int:
    processed = path / "processed"
    if not processed.is_dir():
        return 0
    return len([candidate for candidate in processed.glob("*.md") if candidate.is_file()])


def _is_watched(target: Path, rel: str, watched: tuple[WatchedInbox, ...]) -> bool:
    resolved_target = target.resolve()
    normalized = _normalize_inbox(rel)
    return any(item.root == resolved_target and item.inbox == normalized for item in watched)


def _format_seconds(value: int | None) -> str:
    if value is None:
        return "unknown"
    minutes, seconds = divmod(value, 60)
    if minutes < 1:
        return f"{seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 1:
        return f"{minutes}m"
    days, hours = divmod(hours, 24)
    if days < 1:
        return f"{hours}h{minutes:02d}m"
    return f"{days}d{hours:02d}h"
