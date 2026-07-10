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
    from .. import work_cmd

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
    new_issues = [issue for issue in found if issue.id not in known_ids and issue.id not in covered_summary_ids]
    records = [issue.as_import_record() for issue in new_issues]
    from .. import work_cmd

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


def sources_init(
    *, target: Path, force: bool = False, inboxes: list[str] | None = None, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = default_sources_path(target)
    if path.exists() and not force:
        print(f"error: handoff source config already exists: {path}", file=sys.stderr)
        return 2
    if inboxes is not None:
        inbox_values = list(inboxes)
    else:
        inbox_values = list(WRITER_INBOXES)
        try:
            config = load_brigade_config(target)
        except Exception:
            config = None
        if config is not None and config.selection.harnesses:
            selected = [_WRITER_INBOX_MAP[h] for h in config.selection.harnesses if h in _WRITER_INBOX_MAP]
            if selected:
                inbox_values = selected
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
                "inboxes": inbox_values,
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    output = {
        "target": str(target),
        "path": str(path),
        "written": True,
        "inboxes": inbox_values,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"handoff_sources: {path}")
    print(f"inboxes: {', '.join(inbox_values) if inbox_values else '(none)'}")
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
        warnings.append(
            "Suggested card content contains level-2 markdown headings, which may be parsed as handoff sections"
        )


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
    from .. import work_cmd

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
    from .. import work_cmd

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
            key: value for key, value in metadata.items() if key not in {"source_fingerprint", "handoff_issue_id"}
        },
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
