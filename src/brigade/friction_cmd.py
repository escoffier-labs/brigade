"""Friction-log scanner and backlog helpers."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import work_cmd

TEXT_SUFFIXES = {
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".out",
    ".txt",
    ".yaml",
    ".yml",
}
DEFAULT_SOURCE_DIRS = (
    ".brigade/work",
    ".brigade/runs",
    ".codex/memory-handoffs",
    ".claude/memory-handoffs",
    ".learnings",
    "memory",
    "notes",
)
DEFAULT_AGENT_LOG_DIRS = (
    "~/.codex",
    "~/.claude/projects",
)
PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    (
        "auth",
        "high",
        "auth or permission friction",
        r"\b(api error:\s*(401|403)|(401|403)\s+(invalid|unauthorized|forbidden|auth)|auth(?:entication)? error|permission denied|eacces|eperm|logged out|sso|session expired)\b",
    ),
    (
        "quota",
        "high",
        "quota or rate-limit friction",
        r"\b(429\s+(rate|quota)|rate limit|quota exceeded|creditsdepleted|credits depleted|usage limit)\b",
    ),
    (
        "tool_failure",
        "high",
        "tool or command failure",
        r"\b(exit code [1-9]|command not found|no such file or directory|syntaxerror|typeerror|referenceerror|importerror|modulenotfounderror|npm err!|fatal|panic|segfault|core dumped)\b",
    ),
    (
        "network_timeout",
        "medium",
        "network or timeout friction",
        r"\b(econnrefused|etimedout|connection refused|timeout|timed out|hung|stalled)\b",
    ),
    (
        "blocked",
        "high",
        "blocked workflow",
        r"\b(blocked by|blocked because|blocker|could not|couldn't|failed|failure|cannot|can't)\b",
    ),
    (
        "workaround",
        "medium",
        "workaround required",
        r"\b(workaround|fallback|route around|manual fix|recovered by|had to)\b",
    ),
    (
        "missing_context",
        "medium",
        "missing or stale context",
        r"\b(missing|not found|stale|outdated|wrong docs|unclear|ambiguous|no reply|not covered)\b",
    ),
    (
        "workflow_correction",
        "medium",
        "workflow correction",
        r"\b(correction captured|future behavior|next time|should have|learned|gotcha|do differently)\b",
    ),
    (
        "latency",
        "low",
        "latency or effort friction",
        r"\b(slow|latency|took ages|too long|long-running|expensive)\b",
    ),
)
SECRET_RE = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization|bearer)\b\s*[:=]\s*['\"]?[^'\"\s]+")
IGNORED_ATTACHMENT_TYPES = {"hook_success", "hook_additional_context", "skill_listing"}
# A JSON field whose value is a bare number is configuration or a counter
# ('"timeout": 900', '"failed": 0'), never friction evidence in itself. Real
# friction always carries textual evidence (an error string, a status word).
JSON_NUMERIC_FIELD_RE = re.compile(r'^"[^"\n]+"\s*:\s*-?\d+(?:\.\d+)?\s*,?$')


@dataclass(frozen=True)
class Match:
    path: Path
    line_number: int
    line: str
    friction_type: str
    severity: str
    title: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _redact(text: str) -> str:
    return SECRET_RE.sub(lambda m: m.group(0).split(m.group(1), 1)[0] + f"{m.group(1)}=[REDACTED]", text)


def _short(text: str, limit: int = 180) -> str:
    rendered = " ".join(_redact(text).split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _parse_since(days: int) -> datetime:
    if days < 1:
        raise ValueError("--days must be a positive integer")
    return _now() - timedelta(days=days)


def _candidate_id(source: str, friction_type: str, text: str) -> str:
    digest = hashlib.sha256(f"{source}\0{friction_type}\0{text}".encode("utf-8")).hexdigest()[:12]
    return f"friction-{digest}"


def _iter_source_roots(target: Path, *, include_agent_logs: bool) -> list[Path]:
    roots = [target / item for item in DEFAULT_SOURCE_DIRS]
    if include_agent_logs:
        roots.extend(Path(item).expanduser() for item in DEFAULT_AGENT_LOG_DIRS)
    seen: set[Path] = set()
    resolved: list[Path] = []
    for root in roots:
        try:
            path = root.expanduser().resolve()
        except OSError:
            continue
        if path in seen or not path.exists():
            continue
        seen.add(path)
        resolved.append(path)
    return resolved


def _iter_files(roots: list[Path], *, since: datetime, max_files: int) -> tuple[list[Path], int]:
    files: list[Path] = []
    skipped = 0
    cutoff = since.timestamp()
    for root in roots:
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if len(files) >= max_files:
                skipped += 1
                continue
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if ".brigade/work/imports/" in str(path):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                skipped += 1
                continue
            files.append(path)
    return files, skipped


def _line_text(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        fragments: list[str] = []
        if not isinstance(payload, dict):
            return ""
        attachment = payload.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") in IGNORED_ATTACHMENT_TYPES:
            return ""
        message = payload.get("message")
        if isinstance(message, dict):
            fragments.extend(_extract_text(message.get("content")))
        fragments.extend(_extract_text(payload.get("toolUseResult")))
        for key in ("content", "text", "result", "summary", "error", "details"):
            value = payload.get(key)
            if isinstance(value, str):
                fragments.append(value)
            elif isinstance(value, dict):
                nested = value.get("content") or value.get("text")
                fragments.extend(_extract_text(nested))
        return " ".join(_dedupe_fragments(fragments))
    return stripped


def _dedupe_fragments(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _extract_text(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        fragments: list[str] = []
        for item in value:
            fragments.extend(_extract_text(item))
        return fragments
    if isinstance(value, dict):
        object_fragments: list[str] = []
        if isinstance(value.get("text"), str):
            object_fragments.append(value["text"])
        if isinstance(value.get("content"), (str, list, dict)):
            object_fragments.extend(_extract_text(value["content"]))
        return object_fragments
    return []


def _scan_file(path: Path, *, max_line_length: int = 5000) -> list[Match]:
    matches: list[Match] = []
    try:
        handle = path.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        return matches
    with handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = _line_text(raw_line)
            if not line:
                continue
            line = line[:max_line_length]
            if JSON_NUMERIC_FIELD_RE.match(line):
                continue
            lowered = line.lower()
            for friction_type, severity, title, pattern in PATTERNS:
                if re.search(pattern, lowered):
                    matches.append(
                        Match(
                            path=path,
                            line_number=line_number,
                            line=line,
                            friction_type=friction_type,
                            severity=severity,
                            title=title,
                        )
                    )
                    break
    return matches


def _is_verify_receipt(path: Path) -> bool:
    return path.name == "receipt.json" and "verify-runs" in path.parts


def _scan_receipt(path: Path) -> list[Match]:
    """Parse a verify-run receipt structurally instead of keyword-matching it.

    Receipts are machine-written JSON where words like "failed" and "timeout"
    appear as field names and configured budgets; keyword scanning them yields
    false positives on passing runs. Only real failure evidence counts: a
    non-zero command exit code, a command that did not complete, or a run
    whose top-level status is not completed.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    matches: list[Match] = []
    commands = payload.get("commands")
    for command in commands if isinstance(commands, list) else []:
        if not isinstance(command, dict):
            continue
        exit_code = command.get("exit_code")
        status = str(command.get("status") or "")
        failed = (isinstance(exit_code, int) and exit_code != 0) or status not in ("", "completed")
        if not failed:
            continue
        rendered = str(command.get("command") or "verification command")
        stderr_summary = " ".join(str(command.get("stderr_summary") or "").split())
        detail = f"verify command failed: {rendered} (exit={exit_code}, status={status or 'unknown'})"
        if stderr_summary:
            detail += f" stderr: {stderr_summary}"
        timed_out = "timeout" in status or "timed out" in stderr_summary.lower()
        matches.append(
            Match(
                path=path,
                line_number=1,
                line=detail,
                friction_type="network_timeout" if timed_out else "tool_failure",
                severity="medium" if timed_out else "high",
                title="network or timeout friction" if timed_out else "tool or command failure",
            )
        )
    run_status = str(payload.get("status") or "completed")
    if not matches and run_status != "completed":
        matches.append(
            Match(
                path=path,
                line_number=1,
                line=f"verify run did not complete: status={run_status}",
                friction_type="blocked",
                severity="high",
                title="blocked workflow",
            )
        )
    return matches


def _workflow_from_path(target: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(target)
    except ValueError:
        path_text = str(path)
        if "/.claude/" in path_text:
            return "agent-logs/claude"
        if "/.codex/" in path_text:
            return "agent-logs/codex"
        relative = path
    parts = relative.parts
    if not parts:
        return "unknown"
    if parts[0] == ".brigade" and len(parts) > 1:
        return f"brigade/{parts[1]}"
    return parts[0]


def _make_candidate(target: Path, match: Match) -> dict[str, Any]:
    try:
        source = str(match.path.resolve().relative_to(target))
    except ValueError:
        source = str(match.path)
    snippet = _short(match.line)
    text = f"{match.title}: {snippet}"
    return {
        "id": _candidate_id(source, match.friction_type, snippet),
        "title": match.title,
        "text": text,
        "status": "candidate",
        "kind": "finding",
        "source": "friction-scan",
        "friction_type": match.friction_type,
        "severity": match.severity,
        "workflow": _workflow_from_path(target, match.path),
        "evidence": {
            "path": source,
            "line": match.line_number,
            "snippet": snippet,
        },
        "suggested_fix": "Review the evidence, decide whether this is actionable, then promote to a task, note, memory card, rule, or tool fix.",
    }


def scan_payload(
    *,
    target: Path,
    days: int = 30,
    include_agent_logs: bool = False,
    max_files: int = 5000,
    max_candidates: int = 200,
) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return None, 2
    try:
        since = _parse_since(days)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 2
    if max_files < 1:
        print("error: --max-files must be a positive integer", file=sys.stderr)
        return None, 2
    if max_candidates < 1:
        print("error: --max-candidates must be a positive integer", file=sys.stderr)
        return None, 2

    roots = _iter_source_roots(target, include_agent_logs=include_agent_logs)
    files, skipped_files = _iter_files(roots, since=since, max_files=max_files)
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    type_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    workflow_counts: dict[str, int] = {}
    for path in files:
        matches = _scan_receipt(path) if _is_verify_receipt(path) else _scan_file(path)
        for match in matches:
            candidate = _make_candidate(target, match)
            if candidate["id"] in seen_ids:
                continue
            seen_ids.add(str(candidate["id"]))
            candidates.append(candidate)
            type_counts[str(candidate["friction_type"])] = type_counts.get(str(candidate["friction_type"]), 0) + 1
            severity_counts[str(candidate["severity"])] = severity_counts.get(str(candidate["severity"]), 0) + 1
            workflow_counts[str(candidate["workflow"])] = workflow_counts.get(str(candidate["workflow"]), 0) + 1
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break

    payload = {
        "version": 1,
        "generated_at": _now().isoformat(),
        "target": str(target),
        "days": days,
        "since": since.isoformat(),
        "include_agent_logs": include_agent_logs,
        "source_roots": [str(root) for root in roots],
        "files_scanned": len(files),
        "files_skipped": skipped_files,
        "candidate_count": len(candidates),
        "truncated": len(candidates) >= max_candidates,
        "counts": {
            "by_type": dict(sorted(type_counts.items())),
            "by_severity": dict(sorted(severity_counts.items())),
            "by_workflow": dict(sorted(workflow_counts.items())),
        },
        "candidates": candidates,
    }
    return payload, 0


def _default_json_path(target: Path) -> Path:
    return target / ".brigade" / "friction" / "latest.json"


def _default_markdown_path(target: Path) -> Path:
    return target / ".brigade" / "friction" / "latest.md"


def show(*, target: Path, severity: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path = _default_json_path(target)
    data = work_cmd._read_json(path)
    if data is None:
        print(f"error: no friction scan found at {path}; run `brigade friction scan` first", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print(f"error: invalid friction scan at {path}; expected JSON object", file=sys.stderr)
        return 2
    candidates_value = data.get("candidates")
    candidates = candidates_value if isinstance(candidates_value, list) else []
    if severity:
        candidates = [c for c in candidates if isinstance(c, dict) and c.get("severity") == severity]
    payload = {
        "source": str(path),
        "generated_at": data.get("generated_at"),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"friction show: {path}")
    print(f"candidates: {len(candidates)}")
    for candidate in candidates:
        if isinstance(candidate, dict):
            print(f"- [{candidate.get('severity', '?')}] {candidate.get('workflow', '?')}: {candidate.get('text', '')}")
    return 0


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Brigade Friction Scan",
        "",
        f"- Generated: {payload.get('generated_at')}",
        f"- Target: `{payload.get('target')}`",
        f"- Window: {payload.get('days')} days since `{payload.get('since')}`",
        f"- Files scanned: {payload.get('files_scanned')}",
        f"- Candidates: {payload.get('candidate_count')}",
        "",
        "## Counts",
        "",
    ]
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    for label, key in (("Severity", "by_severity"), ("Type", "by_type"), ("Workflow", "by_workflow")):
        values = counts.get(key) if isinstance(counts, dict) else {}
        lines.append(f"### {label}")
        if isinstance(values, dict) and values:
            for name, count in values.items():
                lines.append(f"- {name}: {count}")
        else:
            lines.append("- none")
        lines.append("")
    lines.append("## Candidate Friction")
    lines.append("")
    candidates_value = payload.get("candidates")
    candidates = candidates_value if isinstance(candidates_value, list) else []
    if not candidates:
        lines.append("No candidate friction found.")
    for item in candidates:
        if not isinstance(item, dict):
            continue
        evidence_value = item.get("evidence")
        evidence = evidence_value if isinstance(evidence_value, dict) else {}
        lines.extend(
            [
                f"### {item.get('id')}",
                "",
                f"- Title: {item.get('title')}",
                f"- Severity: {item.get('severity')}",
                f"- Type: {item.get('friction_type')}",
                f"- Workflow: {item.get('workflow')}",
                f"- Evidence: `{evidence.get('path')}`:{evidence.get('line')}",
                f"- Snippet: {evidence.get('snippet')}",
                f"- Suggested fix: {item.get('suggested_fix')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_markdown(payload))


def _candidate_fingerprint(item: dict[str, Any]) -> str:
    # Fingerprint only the meaning-bearing fields. Evidence position (path/line/
    # snippet placement) drifts as scanned logs grow, and a drifted position must
    # not re-import an otherwise unchanged friction.
    stable = {
        "id": item.get("id"),
        "title": item.get("title"),
        "text": item.get("text"),
        "friction_type": item.get("friction_type"),
        "severity": item.get("severity"),
        "workflow": item.get("workflow"),
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _import_candidates(target: Path, candidates: list[dict[str, Any]], *, dry_run: bool) -> tuple[int, int, int]:
    records: list[dict[str, Any]] = []
    for item in candidates:
        evidence_value = item.get("evidence")
        evidence = evidence_value if isinstance(evidence_value, dict) else {}
        records.append(
            {
                "kind": "finding",
                "source": "friction-scan",
                "text": str(item.get("text") or item.get("title") or "").strip(),
                "metadata": {
                    "source_item_key": item.get("id"),
                    "source_fingerprint": _candidate_fingerprint(item),
                    "friction_id": item.get("id"),
                    "friction_type": item.get("friction_type"),
                    "severity": item.get("severity"),
                    "workflow": item.get("workflow"),
                    "evidence_path": evidence.get("path"),
                    "evidence_line": evidence.get("line"),
                },
            }
        )
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    return len(imported), len(skipped), len(skipped_dismissed)


def scan(
    *,
    target: Path,
    days: int = 30,
    include_agent_logs: bool = False,
    max_files: int = 5000,
    max_candidates: int = 200,
    output: Path | None = None,
    markdown: Path | None = None,
    import_candidates: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    payload, code = scan_payload(
        target=target,
        days=days,
        include_agent_logs=include_agent_logs,
        max_files=max_files,
        max_candidates=max_candidates,
    )
    if payload is None:
        return code
    target = target.expanduser().resolve()
    json_path = (output or _default_json_path(target)).expanduser()
    markdown_path = (markdown or _default_markdown_path(target)).expanduser()
    if not dry_run:
        _write_payload(json_path, payload)
        _write_markdown(markdown_path, payload)
    imported = 0
    skipped = 0
    skipped_dismissed = 0
    if import_candidates:
        imported, skipped, skipped_dismissed = _import_candidates(
            target,
            payload.get("candidates", []) if isinstance(payload.get("candidates"), list) else [],
            dry_run=dry_run,
        )
    payload["output"] = {
        "json": str(json_path),
        "markdown": str(markdown_path),
        "imports_added": imported,
        "imports_skipped": skipped,
        "imports_skipped_dismissed": skipped_dismissed,
        "dry_run": dry_run,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("friction scan:")
    print(f"  target: {payload['target']}")
    print(f"  days: {payload['days']}")
    print(f"  files_scanned: {payload['files_scanned']}")
    print(f"  candidates: {payload['candidate_count']}")
    print(f"  json: {json_path if not dry_run else '(dry-run) ' + str(json_path)}")
    print(f"  markdown: {markdown_path if not dry_run else '(dry-run) ' + str(markdown_path)}")
    if import_candidates:
        print(f"  imports_added: {imported}")
        print(f"  imports_skipped: {skipped}")
        print(f"  imports_skipped_dismissed: {skipped_dismissed}")
    counts = payload.get("counts", {})
    by_type = counts.get("by_type") if isinstance(counts, dict) else {}
    if by_type:
        print("  by_type:")
        for name, count in sorted(by_type.items()):
            print(f"    {name}: {count}")
    return 0


def add(
    *,
    target: Path,
    text: str,
    friction_type: str = "manual",
    severity: str = "medium",
    workflow: str = "manual",
    evidence: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    rendered = text.strip()
    if not rendered:
        print("error: friction text is required", file=sys.stderr)
        return 2
    friction_id = f"manual-{uuid4().hex[:12]}"
    record: dict[str, Any] = {
        "kind": "finding",
        "source": "friction-manual",
        "text": rendered,
        "metadata": {
            "source_item_key": friction_id,
            "source_fingerprint": hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16],
            "friction_id": friction_id,
            "friction_type": friction_type,
            "severity": severity,
            "workflow": workflow,
        },
    }
    if evidence:
        record["metadata"]["evidence"] = evidence
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, [record])
    payload = {
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_dismissed": len(skipped_dismissed),
        "record": imported[0] if imported else record,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if imported:
        print(f"friction: {imported[0]['id']}")
        print(f"imports: {work_cmd._imports_path(target)}")
    else:
        print("friction: duplicate pending import skipped")
    return 0
