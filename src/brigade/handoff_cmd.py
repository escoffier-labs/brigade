"""Handoff health checks shared by CLI doctors."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OK = "ok"
WARN = "warn"
FAIL = "fail"

WRITER_INBOXES = (".claude/memory-handoffs", ".codex/memory-handoffs")
IGNORED_HANDOFF_NAMES = {"TEMPLATE.md"}
DEFAULT_STALE_AFTER_MINUTES = 90
MAX_INGESTOR_WARNING_SIGNALS = 5
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "inbox": self.inbox,
            "path": str(self.path),
            "exists": self.exists,
            "pending": self.pending,
            "processed": self.processed,
            "watched": self.watched,
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
class HandoffHealth:
    target: Path
    sources_path: Path | None
    sources_loaded: bool
    inboxes: tuple[InboxHealth, ...]
    ingestor: IngestorHealth
    warnings: tuple[str, ...]
    failures: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": str(self.target),
            "sources_path": str(self.sources_path) if self.sources_path else None,
            "sources_loaded": self.sources_loaded,
            "inboxes": [inbox.as_dict() for inbox in self.inboxes],
            "ingestor": self.ingestor.as_dict(),
            "warnings": list(self.warnings),
            "failures": list(self.failures),
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
    warnings.extend(ingestor.warnings)

    return HandoffHealth(
        target=target,
        sources_path=sources_path,
        sources_loaded=sources_loaded,
        inboxes=inboxes,
        ingestor=ingestor,
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

    for warning in health.warnings:
        checks.append((WARN, "handoff_warning", warning))
    return checks


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


def _inspect_inbox(target: Path, rel: str, watched: tuple[WatchedInbox, ...]) -> InboxHealth:
    path = target / rel
    return InboxHealth(
        inbox=rel,
        path=path,
        exists=path.is_dir(),
        pending=_count_pending(path),
        processed=_count_processed(path),
        watched=_is_watched(target, rel, watched),
    )


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
