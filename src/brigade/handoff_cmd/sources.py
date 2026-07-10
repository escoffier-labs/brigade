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
    if warning_events and warning_count < len(
        [event for event in warning_events if event.get("category") != "warning-summary"]
    ):
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
    return any(
        token in lowered
        for token in (
            "unreachable",
            "unavailable",
            "cannot reach",
            "could not reach",
            "timed out",
            "timeout",
            "no route",
        )
    )


def _looks_no_reply(line: str) -> bool:
    upper = line.upper()
    return "NO_REPLY" in upper or "NO_UPDATES" in upper


def _looks_malformed(line: str) -> bool:
    lowered = line.casefold()
    return (
        lowered.startswith(("malformed ", "invalid ", "parse-error ", "parse error "))
        or "malformed handoff" in lowered
        or "invalid handoff" in lowered
    )


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
