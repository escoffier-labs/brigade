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

from . import models as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


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
        warnings.append("pending handoffs exist but no .brigade/handoff-sources.json is configured")
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
        checks.append(
            (level, "handoff_sources", "not configured; no pending handoffs" if not pending_total else "not configured")
        )

    quiet_inboxes = [
        inbox
        for inbox in health.inboxes
        if not inbox.exists and not inbox.watched and not inbox.pending and not inbox.processed
    ]
    for inbox in health.inboxes:
        if inbox in quiet_inboxes:
            continue
        if inbox.pending and not inbox.watched:
            level = WARN
        else:
            level = OK
        watched = "yes" if inbox.watched else "no"
        exists = "yes" if inbox.exists else "no"
        detail = (
            f"{inbox.path} (exists={exists}, pending={inbox.pending}, processed={inbox.processed}, watched={watched})"
        )
        checks.append((level, f"handoff_watch: {inbox.inbox}", detail))
    if quiet_inboxes:
        checks.append(
            (
                OK,
                "handoff_watch: other inboxes",
                f"{len(quiet_inboxes)} writer inbox(es) absent and unwatched",
            )
        )

    stale_backlog = [
        inbox
        for inbox in health.inboxes
        if inbox.pending
        and inbox.oldest_pending_age_seconds is not None
        and inbox.oldest_pending_age_seconds >= BACKLOG_STALE_SECONDS
    ]
    if stale_backlog:
        pending_total = sum(inbox.pending for inbox in stale_backlog)
        oldest = max(inbox.oldest_pending_age_seconds or 0 for inbox in stale_backlog)
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
        checks.append(
            (WARN, "handoff_lint", f"{len(invalid_lint)} invalid of {len(health.lint)} pending handoff files")
        )
        for result in invalid_lint:
            first_error = result.errors[0] if result.errors else "invalid handoff"
            checks.append((WARN, "handoff_lint", f"{result.path}: {first_error}"))
    else:
        checks.append(
            (OK, "handoff_lint", f"{len(health.lint)} pending handoff file{'s' if len(health.lint) != 1 else ''} valid")
        )

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
