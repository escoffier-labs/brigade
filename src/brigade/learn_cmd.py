"""Bounded local learning candidate aggregation."""

from __future__ import annotations

import json
import re
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from . import work_cmd
from .untrusted import PROMPT_INJECTION_RE, scan_untrusted
from .localio import read_json_dict as _read_json, slugify, utc_now as _now, write_json as _write_json

OK = "ok"
WARN = "warn"
LEARNING_CLOSEOUT_STATUSES = {"accepted-risk", "dismissed", "archived", "deferred"}
LEARNING_IMPORT_SOURCES = {
    "backup-health",
    "code-review",
    "handoff-ingest",
    "learnings-import",
    "memory-care",
    "repo-fleet-release",
    "scanner-health",
    "security-scan",
    "tool-catalog",
}

# Importer for the structured `.learnings/` markdown log format that some
# operator workflows already keep on disk. Each entry is a level-two heading
# carrying a typed id (ERR/LRN/FEAT), followed by labelled fields and prose
# sections. The importer reads those entries and proposes one local work-import
# per entry so historical logs are not stranded. It never edits the log files
# and never writes canonical memory.
LEARNINGS_IMPORT_SOURCE = "learnings-import"
LEARNINGS_DEFAULT_FILES = (
    ".learnings/ERRORS.md",
    ".learnings/LEARNINGS.md",
    ".learnings/FEATURE_REQUESTS.md",
)
# Maps the typed id prefix to the proposed work-import kind. ERR entries are
# failures (incident), LRN entries are durable findings, FEAT entries are
# actionable feature work.
LEARNINGS_ENTRY_KINDS = {
    "ERR": "incident",
    "LRN": "finding",
    "FEAT": "task",
}
LEARNINGS_PROMOTED_STATUSES = {"promoted", "resolved"}
_LEARNINGS_HEADING_RE = re.compile(
    r"^##\s+\[?(?P<id>(?P<prefix>ERR|LRN|FEAT)-\d{8}-\d+)\]?\s*[:\-]?\s*(?P<title>.*?)\s*$"
)
_LEARNINGS_FIELD_RE = re.compile(r"^\*\*(?P<key>[A-Za-z][A-Za-z _-]*)\*\*\s*:\s*(?P<value>.*?)\s*$")


def _learning_root(target: Path) -> Path:
    return target / ".brigade" / "learn"


def _replays_root(target: Path) -> Path:
    return _learning_root(target) / "replays"


def _replay_compares_root(target: Path) -> Path:
    return _learning_root(target) / "replay-compares"


def _skill_workshop_root(target: Path) -> Path:
    return _learning_root(target) / "skill-workshop"


def _closeouts_root(target: Path) -> Path:
    return _learning_root(target) / "closeouts"


def _redact_text(value: str) -> str:
    rendered = re.sub(
        r"(?i)\b([A-Z0-9_]*(?:token|secret|password|api[_-]?key)[A-Z0-9_]*)\s*[:=]\s*['\"]?[^'\"\s]+",
        lambda match: f"{match.group(1)}=<redacted>",
        value,
    )
    rendered = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+", "bearer <redacted>", rendered)
    rendered = re.sub(r"https?://[^\s]+", "<redacted-url>", rendered)
    return rendered


def _safe_learning_text(value: Any, *, limit: int | None = None) -> str:
    rendered = _redact_text(str(value or ""))
    rendered = PROMPT_INJECTION_RE.sub("<prompt-injection-marker>", rendered)
    rendered = " ".join(rendered.split())
    if limit is not None:
        return _short(rendered, limit)
    return rendered


def _learning_text_had_guard_signal(value: Any) -> bool:
    text = str(value or "")
    return _redact_text(text) != text or scan_untrusted(text).flagged


def _safe_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_learning_text(value)
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?i)(token|secret|password|api[_-]?key|credential)", key_text):
                safe[key_text] = "<redacted>"
            else:
                safe[key_text] = _safe_payload(item)
        return safe
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    return value


def _slug(value: str) -> str:
    return slugify(value, fallback="skill")


def _short(value: str, limit: int = 160) -> str:
    rendered = " ".join(str(value).split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _candidate(
    candidate_id: str,
    subsystem: str,
    status: str,
    summary: str,
    command: str,
    *,
    severity: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = {
        "id": candidate_id,
        "subsystem": subsystem,
        "status": status,
        "severity": severity,
        "safe_summary": summary,
        "suggested_next_command": command,
        "metadata": metadata or {},
    }
    candidate["source_fingerprint"] = _candidate_fingerprint(candidate)
    return candidate


def _candidate_fingerprint(candidate: dict[str, Any]) -> str:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    explicit = metadata.get("source_fingerprint")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return work_cmd._stable_hash(
        {
            "id": candidate.get("id"),
            "subsystem": candidate.get("subsystem"),
            "summary": candidate.get("safe_summary"),
            "status": candidate.get("status"),
        }
    )


def _read_closeouts(target: Path) -> list[dict[str, Any]]:
    root = _closeouts_root(target.expanduser().resolve())
    receipts: list[dict[str, Any]] = []
    if not root.is_dir():
        return receipts
    for path in sorted(root.glob("*/closeout.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("closeout_id", path.parent.name)
        payload["path"] = str(path.parent)
        receipts.append(payload)
    return sorted(receipts, key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)


def _closeout_key(candidate: dict[str, Any]) -> str:
    return f"{candidate.get('subsystem')}:{candidate.get('id')}"


def _latest_closeout_by_candidate(target: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for closeout in _read_closeouts(target):
        key = str(closeout.get("candidate_key") or f"{closeout.get('subsystem')}:{closeout.get('candidate_id')}")
        if key and key not in latest:
            latest[key] = closeout
    return latest


def _import_learning_summary(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    for key in ("safe_summary", "safe_detail", "evidence_summary"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return _safe_learning_text(value)
    source = str(item.get("source") or "producer")
    kind = str(item.get("kind") or "import")
    return f"{source} {kind} import requires review"


def _raw_candidates(target: Path) -> list[dict[str, Any]]:
    target = target.expanduser().resolve()
    results: list[dict[str, Any]] = []
    for item in work_cmd._read_imports(target):
        if item.get("status", "pending") != "pending":
            continue
        source = str(item.get("source") or "manual")
        if source in LEARNING_IMPORT_SOURCES:
            import_id = str(item.get("id") or "")
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            candidate_metadata = {
                "import_id": import_id,
                "source": source,
                "source_fingerprint": metadata.get("source_fingerprint"),
            }
            for key in (
                "rule_id",
                "issue_type",
                "template",
                "category",
                "area",
                "entry_prefix",
                "severity",
                "surface",
                "confidence",
                "path",
                "line",
                "finding_id",
                "response_options",
                "remediation_hint",
                "local_evidence_path",
            ):
                if key in metadata:
                    candidate_metadata[key] = _safe_payload(metadata.get(key))
            if item.get("template") and "template" not in candidate_metadata:
                candidate_metadata["template"] = item.get("template")
            guarded_input = any(
                _learning_text_had_guard_signal(metadata.get(key))
                for key in ("safe_summary", "safe_detail", "evidence_summary", "response_options", "remediation_hint")
            )
            if guarded_input:
                candidate_metadata["guarded_input"] = True
            results.append(
                _candidate(
                    import_id,
                    source,
                    "pending",
                    _import_learning_summary(item),
                    f"brigade work import plan {import_id}",
                    severity=item.get("priority") if isinstance(item.get("priority"), str) else None,
                    metadata=candidate_metadata,
                )
            )
    for receipt in work_cmd._review_receipts(target):
        if receipt.get("status") == "failed":
            run_id = str(receipt.get("run_id") or "")
            results.append(
                _candidate(run_id, "code-review", "failed", "failed review run", f"brigade work review show {run_id}")
            )
    tool_runs = target / ".brigade" / "tools" / "runs"
    if tool_runs.is_dir():
        for path in sorted(tool_runs.glob("*/receipt.json")):
            payload = _read_json(path)
            if isinstance(payload, dict) and payload.get("status") == "failed":
                run_id = str(payload.get("run_id") or path.parent.name)
                results.append(
                    _candidate(
                        run_id, "tool-run", "failed", "failed portable tool run", f"brigade tools run show {run_id}"
                    )
                )
    return results


def candidates(target: Path, *, include_quieted: bool = False) -> list[dict[str, Any]]:
    target = target.expanduser().resolve()
    closeout_by_candidate = _latest_closeout_by_candidate(target)
    results: list[dict[str, Any]] = []
    for item in _raw_candidates(target):
        closeout = closeout_by_candidate.get(_closeout_key(item))
        if (
            closeout
            and closeout.get("source_fingerprint") == item.get("source_fingerprint")
            and closeout.get("status") in LEARNING_CLOSEOUT_STATUSES
        ):
            if include_quieted:
                item = {**item, "quieted_by": closeout.get("closeout_id"), "closeout_status": closeout.get("status")}
                results.append(item)
            continue
        if closeout and closeout.get("source_fingerprint") != item.get("source_fingerprint"):
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            item["metadata"] = {
                **metadata,
                "changed_closeout_id": closeout.get("closeout_id"),
                "previous_fingerprint": closeout.get("source_fingerprint"),
            }
            item["closeout_status"] = "changed-fingerprint"
        results.append(item)
    return results


def plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    items = candidates(target)
    raw = _raw_candidates(target)
    quieted = candidates(target, include_quieted=True)
    quieted_count = len([item for item in quieted if item.get("quieted_by")])
    changed_count = len([item for item in items if item.get("closeout_status") == "changed-fingerprint"])
    checks = [
        {
            "status": WARN if items else OK,
            "name": "learning_candidates",
            "detail": f"{len(items)} candidate(s)" if items else "none",
        }
    ]
    return {
        "target": str(target),
        "candidate_count": len(items),
        "raw_candidate_count": len(raw),
        "quieted_candidate_count": quieted_count,
        "changed_fingerprint_count": changed_count,
        "candidates": items,
        "checks": checks,
        "issues": [check for check in checks if check["status"] != OK],
        "issue_count": 1 if items else 0,
        "top_issue": checks[0] if items else None,
        "replay_policy": "safe local summaries only, no private raw evidence",
    }


def plan(*, target: Path, json_output: bool = False) -> int:
    payload = plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"learn plan: {payload['target']}")
    print(f"candidates: {payload['candidate_count']}")
    for item in payload["candidates"][:20]:
        print(f"- {item['id']} [{item['subsystem']}] {item['safe_summary']}")
    return 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    payload = plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["issue_count"] == 0 else 1
    print(f"learn doctor: {payload['target']}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0 if payload["issue_count"] == 0 else 1


def import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    payload = plan_payload(target)
    records: list[dict[str, Any]] = []
    for item in payload["candidates"]:
        fingerprint = str(
            item.get("source_fingerprint")
            or work_cmd._stable_hash(
                {"id": item["id"], "subsystem": item["subsystem"], "summary": item["safe_summary"]}
            )
        )
        records.append(
            {
                "text": f"Review learning candidate: {item['safe_summary']}",
                "kind": "task",
                "source": "learning-loop",
                "type": "research",
                "priority": "normal",
                "template": "docs",
                "acceptance": [
                    "The candidate is routed to a task, handoff, suppression, accepted risk, archive, or dismissal.",
                    "No canonical memory, source, policy, or tool config is edited automatically.",
                ],
                "metadata": {
                    "candidate_id": item["id"],
                    "subsystem": item["subsystem"],
                    "source_item_key": f"{item['subsystem']}:{item['id']}",
                    "source_fingerprint": fingerprint,
                    "safe_summary": item["safe_summary"],
                },
            }
        )
    imported, skipped, dismissed = work_cmd._append_import_records(
        target.expanduser().resolve(), records, dry_run=dry_run
    )
    output = {
        "target": payload["target"],
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(dismissed),
        "dry_run": dry_run,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"learning_imports: {payload['target']}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(dismissed)}")
    return 0


def _parse_learnings_entries(text: str, *, source_file: str) -> list[dict[str, Any]]:
    """Parse structured `.learnings/` markdown entries into safe dictionaries.

    Each entry is a level-two heading carrying a typed id such as
    ``ERR-20260311-001``, optional ``**Label**: value`` fields, and free-form
    prose sections. Fields and prose are redacted and prompt-injection-guarded
    before they leave this function.
    """
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    body_lines: list[str] = []

    def _flush() -> None:
        if current is None:
            return
        body = "\n".join(body_lines).strip()
        current["safe_detail"] = _safe_learning_text(body, limit=600) if body else ""
        entries.append(current)

    for raw_line in text.splitlines():
        heading = _LEARNINGS_HEADING_RE.match(raw_line)
        if heading:
            _flush()
            body_lines = []
            current = {
                "entry_id": heading.group("id"),
                "prefix": heading.group("prefix"),
                "title": _safe_learning_text(heading.group("title") or "", limit=160),
                "fields": {},
                "source_file": source_file,
            }
            continue
        if current is None:
            continue
        field = _LEARNINGS_FIELD_RE.match(raw_line)
        if field:
            key = field.group("key").strip().lower().replace(" ", "_")
            current["fields"][key] = _safe_learning_text(field.group("value"), limit=120)
            continue
        body_lines.append(raw_line)
    _flush()
    return entries


def _learnings_record(entry: dict[str, Any]) -> dict[str, Any]:
    prefix = str(entry.get("prefix") or "")
    kind = LEARNINGS_ENTRY_KINDS.get(prefix, "finding")
    fields = entry.get("fields") if isinstance(entry.get("fields"), dict) else {}
    entry_id = str(entry.get("entry_id") or "")
    title = str(entry.get("title") or "").strip()
    summary = title or _short(str(entry.get("safe_detail") or "logged learning entry"), 120)
    area = fields.get("area")
    status = str(fields.get("status") or "").strip().lower()
    metadata: dict[str, Any] = {
        "entry_id": entry_id,
        "entry_prefix": prefix,
        "source_file": entry.get("source_file"),
        "safe_summary": summary,
        "source_item_key": f"{LEARNINGS_IMPORT_SOURCE}:{entry_id}",
    }
    if isinstance(entry.get("safe_detail"), str) and entry["safe_detail"]:
        metadata["safe_detail"] = entry["safe_detail"]
    for key in ("priority", "status", "area", "logged"):
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            metadata[key] = value.strip()
    if status in LEARNINGS_PROMOTED_STATUSES:
        metadata["log_status"] = status
    record: dict[str, Any] = {
        "text": f"Review .learnings entry {entry_id}: {summary}",
        "kind": kind,
        "source": LEARNINGS_IMPORT_SOURCE,
        "metadata": metadata,
    }
    if kind == "task":
        record["type"] = "feature"
        record["template"] = "vertical-slice"
        record["acceptance"] = [
            "The logged entry is routed to a task, handoff, suppression, accepted risk, archive, or dismissal.",
            "No canonical memory, source, policy, or log file is edited automatically.",
        ]
        priority = (fields.get("priority") or "").strip().lower()
        if priority in {"low", "high"}:
            record["priority"] = priority
        elif priority == "critical":
            record["priority"] = "urgent"
        elif priority == "medium":
            record["priority"] = "normal"
    fingerprint_basis = {
        "entry_id": entry_id,
        "title": title,
        "kind": kind,
        "priority": fields.get("priority"),
        "status": fields.get("status"),
        "area": area,
        "detail": metadata.get("safe_detail"),
    }
    metadata["source_fingerprint"] = work_cmd._stable_hash(fingerprint_basis)
    return record


def _resolve_learnings_files(target: Path, files: list[str] | None) -> list[Path]:
    names = files if files else list(LEARNINGS_DEFAULT_FILES)
    resolved: list[Path] = []
    for name in names:
        candidate = (target / name).expanduser().resolve()
        if candidate.is_file():
            resolved.append(candidate)
    return resolved


def import_learnings(
    *,
    target: Path,
    files: list[str] | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    paths = _resolve_learnings_files(target, files)
    records: list[dict[str, Any]] = []
    parsed_entries = 0
    scanned_files: list[str] = []
    for path in paths:
        try:
            text = path.read_text()
        except OSError as exc:
            print(f"error: cannot read learnings file: {exc}", file=sys.stderr)
            return 2
        try:
            rel = str(path.relative_to(target))
        except ValueError:
            rel = path.name
        scanned_files.append(rel)
        for entry in _parse_learnings_entries(text, source_file=rel):
            parsed_entries += 1
            records.append(_learnings_record(entry))
    imported, skipped, dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    output = {
        "target": str(target),
        "imports_path": str(work_cmd._imports_path(target)),
        "scanned_files": scanned_files,
        "parsed_entries": parsed_entries,
        "dry_run": dry_run,
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"learnings_import: {target}")
    print(f"scanned_files: {len(scanned_files)}")
    print(f"parsed_entries: {parsed_entries}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} {_short(str(item.get('text', '')))}")
    return 0


def _skill_pattern_key(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    # Imported .learnings entries group by their typed prefix and logged area so
    # repeated failures or learnings in the same area cluster into one
    # promote-candidate.
    if str(item.get("subsystem") or "") == "learnings-import":
        prefix = metadata.get("entry_prefix")
        area = metadata.get("area")
        if isinstance(prefix, str) and prefix.strip() and isinstance(area, str) and area.strip():
            return f"learnings:{_slug(prefix)}:{_slug(area)}"
    for key in ("rule_id", "issue_type", "template", "subsystem"):
        value = metadata.get(key) if key != "subsystem" else item.get("subsystem")
        if isinstance(value, str) and value.strip():
            return f"{key}:{_slug(value)}"
    summary = str(item.get("safe_summary") or "")
    summary = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", summary, flags=re.IGNORECASE)
    summary = re.sub(r"\b\d+\b", "<n>", summary)
    return f"summary:{_slug(summary)[:80]}"


def _skill_candidate_from_group(target: Path, key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    first = items[0]
    first_metadata = first.get("metadata") if isinstance(first.get("metadata"), dict) else {}
    subsystem = str(first.get("subsystem") or "learning")
    candidate_id = f"skill-{work_cmd._stable_hash({'key': key, 'subsystem': subsystem})[:12]}"
    summary = _safe_learning_text(first.get("safe_summary") or "repeatable learning pattern", limit=160)
    skill_id = _slug(f"{subsystem}-{summary}")[:64].strip("._-") or "learning-skill"
    evidence = [
        {
            "candidate_id": item.get("id"),
            "subsystem": item.get("subsystem"),
            "safe_summary": _safe_learning_text(item.get("safe_summary") or "learning evidence", limit=180),
            "source_fingerprint": item.get("source_fingerprint"),
        }
        for item in items
    ]
    response_options: list[str] = []
    guarded_input = any(
        bool((item.get("metadata") if isinstance(item.get("metadata"), dict) else {}).get("guarded_input"))
        or _learning_text_had_guard_signal(item.get("safe_summary"))
        for item in items
    )
    for item in items:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        for option in metadata.get("response_options") or []:
            if isinstance(option, str):
                guarded_input = guarded_input or _learning_text_had_guard_signal(option)
                safe_option = _safe_learning_text(option, limit=220)
                if safe_option and safe_option not in response_options:
                    response_options.append(safe_option)
    review_risk = (
        "high" if any(str(item.get("severity") or "").lower() in {"high", "critical"} for item in items) else "normal"
    )
    if subsystem == "security-scan" or first_metadata.get("category") == "secrets":
        review_risk = "high"
    if guarded_input and review_risk == "normal":
        review_risk = "high"
    payload = {
        "id": candidate_id,
        "pattern_key": key,
        "subsystem": subsystem,
        "occurrence_count": len(items),
        "safe_summary": f"Repeatable {subsystem} pattern: {summary}",
        "suggested_skill_id": skill_id,
        "suggested_title": " ".join(part.capitalize() for part in skill_id.replace("_", "-").split("-")[:8]),
        "grouping_reason": f"{len(items)} learning candidate(s) share {key}.",
        "review_risk": review_risk,
        "response_options": response_options,
        "evidence": evidence,
        "source_fingerprint": work_cmd._stable_hash({"pattern_key": key, "evidence": evidence}),
        "suggested_next_command": f"brigade learn propose-skill {candidate_id} --target {target}",
        "manual_only": True,
        "auto_install": False,
        "guarded_input": guarded_input,
    }
    return payload


def skill_candidates_payload(target: Path, *, min_count: int = 2, source: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    minimum = max(1, int(min_count))
    source_filter = source.strip() if isinstance(source, str) and source.strip() else None
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in candidates(target):
        if source_filter and str(item.get("subsystem") or "") != source_filter:
            continue
        groups.setdefault(_skill_pattern_key(item), []).append(item)
    skill_candidates = [
        _skill_candidate_from_group(target, key, items)
        for key, items in sorted(groups.items())
        if len(items) >= minimum
    ]
    skill_candidates.sort(key=lambda item: (-int(item.get("occurrence_count") or 0), str(item.get("id") or "")))
    return {
        "target": str(target),
        "min_count": minimum,
        "source": source_filter,
        "candidate_count": len(skill_candidates),
        "candidates": skill_candidates,
        "manual_only": True,
        "auto_install": False,
        "next_command": "brigade learn propose-skill <candidate-id>",
    }


def skill_candidates(*, target: Path, min_count: int = 2, source: str | None = None, json_output: bool = False) -> int:
    if min_count < 1:
        print("error: --min-count must be at least 1", file=sys.stderr)
        return 2
    payload = skill_candidates_payload(target, min_count=min_count, source=source)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"learn skill-candidates: {payload['target']}")
    print(f"min_count: {payload['min_count']}")
    if payload.get("source"):
        print(f"source: {payload['source']}")
    print(f"candidates: {payload['candidate_count']}")
    for item in payload["candidates"][:20]:
        print(f"- {item['id']} x{item['occurrence_count']} {item['suggested_skill_id']}: {item['safe_summary']}")
        print(f"  reason: {item['grouping_reason']}")
        print(f"  risk: {item['review_risk']}")
    return 0


def _resolve_skill_candidate(
    target: Path, candidate_id: str, *, min_count: int, source: str | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    payload = skill_candidates_payload(target, min_count=min_count, source=source)
    needle = candidate_id.strip()
    matches = [
        item
        for item in payload["candidates"]
        if needle and (str(item.get("id") or "") == needle or str(item.get("id") or "").startswith(needle))
    ]
    if not matches:
        return None, f"skill candidate not found: {candidate_id}"
    if len(matches) > 1:
        return None, f"skill candidate id is ambiguous: {candidate_id}"
    return matches[0], None


def _render_skill_candidate_markdown(candidate: dict[str, Any]) -> str:
    skill_id = _slug(str(candidate.get("suggested_skill_id") or "learning-skill"))
    title = _safe_learning_text(candidate.get("suggested_title") or skill_id, limit=120)
    summary = _safe_learning_text(candidate.get("safe_summary") or "Repeatable learning pattern.", limit=220)
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
    evidence_lines = "\n".join(
        f"- {_safe_learning_text(item.get('subsystem') or 'learning', limit=48)}:{_safe_learning_text(item.get('candidate_id') or 'candidate', limit=80)} - {_safe_learning_text(item.get('safe_summary') or 'learning evidence', limit=120)}"
        for item in evidence[:10]
        if isinstance(item, dict)
    )
    if not evidence_lines:
        evidence_lines = "- No linked evidence."
    response_options = candidate.get("response_options") if isinstance(candidate.get("response_options"), list) else []
    response_lines = "\n".join(
        f"- {_safe_learning_text(option, limit=220)}" for option in response_options[:10] if isinstance(option, str)
    )
    if not response_lines:
        response_lines = "- Choose and document the response path during review."
    grouping_reason = _safe_learning_text(candidate.get("grouping_reason") or "Repeated learning evidence.", limit=220)
    review_risk = _safe_learning_text(candidate.get("review_risk") or "normal", limit=40)
    return "\n".join(
        [
            "---",
            f"name: {json.dumps(skill_id)}",
            f"description: {json.dumps('Use for repeatable Brigade learning pattern: ' + _short(summary, 140))}",
            "---",
            "",
            f"# {title}",
            "",
            "## Use When",
            f"- The workspace shows this repeatable pattern: {summary}",
            "- The operator wants a reviewed workflow instead of another one-off fix.",
            "- The relevant evidence has been reviewed in Brigade before using this skill.",
            "",
            "## Workflow",
            "1. Inspect the linked Brigade evidence and confirm the pattern still applies.",
            "2. State the concrete problem, affected subsystem, and expected safe outcome.",
            "3. Apply the smallest repeatable workflow that resolves the pattern.",
            "4. Run the smallest meaningful verification step.",
            "5. Record a Memory Handoff or learning closeout when the workflow teaches a durable lesson.",
            "",
            "## Review Context",
            f"- Grouping reason: {grouping_reason}",
            f"- Review risk: {review_risk}",
            f"- Guarded input: {'yes' if candidate.get('guarded_input') else 'no'}",
            "",
            "## Response Options",
            response_lines,
            "",
            "## Evidence To Review",
            evidence_lines,
            "",
            "## Boundaries",
            "- Do not install or modify skills automatically.",
            "- Do not edit canonical memory directly.",
            "- Do not copy private evidence values into public docs or prompts.",
            "- Treat this generated skill as unreviewed until the skills inbox accepts it.",
            "",
        ]
    )


def _write_skill_candidate_source(target: Path, candidate: dict[str, Any], *, force: bool) -> Path:
    skill_id = _slug(str(candidate.get("suggested_skill_id") or candidate.get("id") or "learning-skill"))
    root = _skill_workshop_root(target) / str(candidate.get("id")) / "skill"
    if root.exists() and not force:
        raise FileExistsError(root)
    if root.exists():
        for path in sorted([p for p in root.rglob("*") if p.is_file()], reverse=True):
            path.unlink()
        for path in sorted([p for p in root.rglob("*") if p.is_dir()], reverse=True):
            path.rmdir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(_render_skill_candidate_markdown(candidate))
    metadata = {
        "id": skill_id,
        "title": _safe_learning_text(candidate.get("suggested_title") or skill_id, limit=120),
        "description": _safe_learning_text(candidate.get("safe_summary") or "", limit=240),
        "version": "0.1.0",
        "trust_level": "unreviewed",
        "required_tools": [],
        "required_mcp_servers": [],
        "supported_harnesses": [
            "codex",
            "claude",
            "opencode",
            "antigravity",
            "pi",
            "cursor",
            "aider",
            "goose",
            "continue",
            "copilot",
            "qwen",
            "kimi",
            "adal",
            "openhands",
            "openclaw",
            "hermes",
            "mcp",
        ],
        "tests": [],
        "source": "brigade-learn-skill-candidate",
        "learning_candidate_id": candidate.get("id"),
        "learning_pattern_key": candidate.get("pattern_key"),
        "learning_source_fingerprint": candidate.get("source_fingerprint"),
        "evidence_count": candidate.get("occurrence_count"),
        "guarded_input": bool(candidate.get("guarded_input")),
    }
    _write_json(root / "skill.json", metadata)
    return root


def propose_skill(
    *,
    target: Path,
    candidate_id: str,
    min_count: int = 2,
    source: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    if min_count < 1:
        print("error: --min-count must be at least 1", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_skill_candidate(target, candidate_id, min_count=min_count, source=source)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    planned_skill_source = _skill_workshop_root(target) / str(candidate.get("id")) / "skill"
    if dry_run:
        payload = {
            "target": str(target),
            "candidate": candidate,
            "dry_run": True,
            "skill_source": str(planned_skill_source),
            "proposal": {
                "status": "planned",
                "skill_id": candidate.get("suggested_skill_id"),
                "summary": candidate.get("safe_summary"),
            },
            "preview": {
                "skill_md": _render_skill_candidate_markdown(candidate).splitlines(),
                "skill_json": {
                    "id": candidate.get("suggested_skill_id"),
                    "trust_level": "unreviewed",
                    "learning_candidate_id": candidate.get("id"),
                    "evidence_count": candidate.get("occurrence_count"),
                },
            },
            "manual_only": True,
            "auto_install": False,
            "would_write": [
                str(planned_skill_source / "SKILL.md"),
                str(planned_skill_source / "skill.json"),
                str(target / ".brigade" / "skills" / "inbox" / "<proposal-id>"),
            ],
            "next_commands": [
                f"brigade learn propose-skill {candidate.get('id')} --target {target}",
                "brigade skills inbox list --target .",
            ],
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"learn propose-skill dry-run: {candidate.get('id')}")
        print(f"would_write: {planned_skill_source / 'SKILL.md'}")
        print(f"would_write: {planned_skill_source / 'skill.json'}")
        print("would_create: skill inbox proposal")
        return 0
    try:
        skill_source = _write_skill_candidate_source(target, candidate, force=force)
    except FileExistsError as exc:
        print(f"error: generated skill source already exists: {exc.args[0]} (use --force to refresh)", file=sys.stderr)
        return 2
    from . import skills_cmd

    output = StringIO()
    with redirect_stdout(output):
        rc = skills_cmd.inbox_add(
            target=target,
            source=skill_source,
            skill_id=str(candidate.get("suggested_skill_id") or ""),
            summary=str(candidate.get("safe_summary") or ""),
            force=force,
            json_output=True,
        )
    try:
        proposal = json.loads(output.getvalue() or "{}")
    except json.JSONDecodeError:
        proposal = {"error": "skills inbox add returned invalid JSON", "output": output.getvalue().strip().splitlines()}
        rc = 1
    diff_preview: list[str] = []
    proposal_id = proposal.get("proposal_id")
    if proposal_id:
        diff_output = StringIO()
        with redirect_stdout(diff_output):
            diff_rc = skills_cmd.inbox_diff(target=target, proposal_id=str(proposal_id), json_output=True)
        if diff_rc == 0:
            try:
                diff_payload = json.loads(diff_output.getvalue() or "{}")
                diff_preview = [str(line) for line in (diff_payload.get("diff") or [])[:80]]
            except json.JSONDecodeError:
                diff_preview = []
    payload = {
        "target": str(target),
        "candidate": candidate,
        "dry_run": False,
        "skill_source": str(skill_source),
        "proposal": proposal,
        "diff_preview": diff_preview,
        "manual_only": True,
        "auto_install": False,
        "next_commands": [
            f"brigade skills inbox show {proposal.get('proposal_id', '<proposal-id>')} --target {target}",
            f"brigade skills inbox diff {proposal.get('proposal_id', '<proposal-id>')} --target {target}",
            f"brigade skills inbox accept {proposal.get('proposal_id', '<proposal-id>')} --target {target}",
        ],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"learn propose-skill: {candidate.get('id')}")
    print(f"skill_source: {skill_source}")
    print(f"proposal: {proposal.get('proposal_id')}")
    print("status: pending review")
    if diff_preview:
        print("diff_preview:")
        for line in diff_preview[:20]:
            print(line)
    return rc


def closeout(
    *,
    target: Path,
    candidate_id: str,
    status: str,
    reason: str,
    subsystem: str | None = None,
    json_output: bool = False,
) -> int:
    if status not in LEARNING_CLOSEOUT_STATUSES:
        print(f"error: status must be one of: {', '.join(sorted(LEARNING_CLOSEOUT_STATUSES))}", file=sys.stderr)
        return 1
    target = target.expanduser().resolve()
    matches = [
        item
        for item in _raw_candidates(target)
        if str(item.get("id") or "") == candidate_id and (subsystem is None or item.get("subsystem") == subsystem)
    ]
    if not matches:
        print(f"error: learning candidate not found: {candidate_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: learning candidate id is ambiguous: {candidate_id}", file=sys.stderr)
        return 1
    candidate = matches[0]
    closeout_id = f"{_now().strftime('%Y%m%d-%H%M%S-%f')}-learning-closeout"
    payload = {
        "target": str(target),
        "closeout_id": closeout_id,
        "candidate_id": candidate.get("id"),
        "candidate_key": _closeout_key(candidate),
        "subsystem": candidate.get("subsystem"),
        "status": status,
        "reason": reason,
        "safe_summary": candidate.get("safe_summary"),
        "source_fingerprint": candidate.get("source_fingerprint"),
        "created_at": _now().isoformat(),
        "manual_only": True,
        "remote_mutation": False,
        "receipt_fingerprint": work_cmd._stable_hash(
            {
                "candidate_key": _closeout_key(candidate),
                "status": status,
                "reason": reason,
                "source_fingerprint": candidate.get("source_fingerprint"),
            }
        ),
    }
    root = _closeouts_root(target) / closeout_id
    _write_json(root / "closeout.json", payload)
    payload["path"] = str(root)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"learning_closeout: {closeout_id}")
    print(f"candidate: {candidate.get('id')}")
    print(f"subsystem: {candidate.get('subsystem')}")
    print(f"status: {status}")
    print(f"path: {root}")
    return 0


def closeouts(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipts = _read_closeouts(target)
    payload = {"target": str(target), "closeouts": receipts, "closeout_count": len(receipts)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"learning_closeouts: {target}")
    print(f"closeouts: {len(receipts)}")
    for receipt in receipts:
        print(f"- {receipt.get('closeout_id')} status={receipt.get('status')} candidate={receipt.get('candidate_key')}")
    return 0


def closeout_show(*, target: Path, closeout_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipts = _read_closeouts(target)
    if closeout_id == "latest":
        matches = receipts[:1]
    else:
        matches = [receipt for receipt in receipts if str(receipt.get("closeout_id") or "").startswith(closeout_id)]
    if not matches:
        print(f"error: learning closeout not found: {closeout_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: learning closeout id is ambiguous: {closeout_id}", file=sys.stderr)
        return 1
    payload = {"target": str(target), "closeout": matches[0]}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"learning_closeout: {matches[0].get('closeout_id')}")
    print(f"status: {matches[0].get('status')}")
    print(f"candidate: {matches[0].get('candidate_key')}")
    return 0


def write_replay(target: Path, *, scenario_id: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    target = target.expanduser().resolve()
    replay_id = f"{_now().strftime('%Y%m%d-%H%M%S-%f')}-learning-replay-{scenario_id}"
    payload = {
        "replay_id": replay_id,
        "scenario_id": scenario_id,
        "created_at": _now().isoformat(),
        "before": _safe_payload(before),
        "after": _safe_payload(after),
        "privacy": "safe summaries only",
        "manual_only": True,
        "remote_mutation": False,
    }
    _write_json(_replays_root(target) / replay_id / "replay.json", payload)
    return payload


def _read_replays(target: Path) -> list[dict[str, Any]]:
    root = _replays_root(target.expanduser().resolve())
    receipts: list[dict[str, Any]] = []
    if not root.is_dir():
        return receipts
    for path in sorted(root.glob("*/replay.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("replay_id", path.parent.name)
        payload["path"] = str(path.parent)
        receipts.append(payload)
    return sorted(receipts, key=lambda item: str(item.get("created_at") or item.get("replay_id") or ""), reverse=True)


def _read_replay_compares(target: Path) -> list[dict[str, Any]]:
    root = _replay_compares_root(target.expanduser().resolve())
    receipts: list[dict[str, Any]] = []
    if not root.is_dir():
        return receipts
    for path in sorted(root.glob("*/compare.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("compare_id", path.parent.name)
        payload["path"] = str(path.parent)
        receipts.append(payload)
    return sorted(receipts, key=lambda item: str(item.get("created_at") or item.get("compare_id") or ""), reverse=True)


def _find_replay(target: Path, replay_id: str) -> tuple[dict[str, Any] | None, str | None]:
    replays = _read_replays(target)
    if replay_id == "latest":
        if not replays:
            return None, "learning replay not found: latest"
        return replays[0], None
    matches = [replay for replay in replays if str(replay.get("replay_id") or "").startswith(replay_id)]
    if not matches:
        return None, f"learning replay not found: {replay_id}"
    if len(matches) > 1:
        return None, f"learning replay id is ambiguous: {replay_id}"
    return matches[0], None


def _metric(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _compare_replay_payload(target: Path, replay: dict[str, Any]) -> dict[str, Any]:
    before = replay.get("before") if isinstance(replay.get("before"), dict) else {}
    after = replay.get("after") if isinstance(replay.get("after"), dict) else {}
    before_count = _metric(before, "candidate_count")
    after_count = _metric(after, "candidate_count")
    if before_count is None:
        before_count = _metric(before, "issue_count")
    if after_count is None:
        after_count = _metric(after, "issue_count")
    if before_count is None or after_count is None:
        outcome = "unknown"
        delta = None
    else:
        delta = after_count - before_count
        outcome = "improved" if delta < 0 else "regressed" if delta > 0 else "unchanged"
    compare_id = f"{_now().strftime('%Y%m%d-%H%M%S-%f')}-learning-replay-compare"
    return {
        "target": str(target),
        "compare_id": compare_id,
        "replay_id": replay.get("replay_id"),
        "scenario_id": replay.get("scenario_id"),
        "created_at": _now().isoformat(),
        "outcome": outcome,
        "candidate_delta": delta,
        "before_count": before_count,
        "after_count": after_count,
        "before_summary": _safe_payload(before.get("summary") or before.get("safe_summary") or ""),
        "after_summary": _safe_payload(after.get("summary") or after.get("safe_summary") or ""),
        "manual_only": True,
        "remote_mutation": False,
        "suggested_next_command": "brigade learn import-issues",
    }


def replay_export(
    *,
    target: Path,
    scenario_id: str,
    before_summary: str,
    after_summary: str,
    before_count: int | None = None,
    after_count: int | None = None,
    json_output: bool = False,
) -> int:
    before: dict[str, Any] = {"summary": before_summary}
    after: dict[str, Any] = {"summary": after_summary}
    if before_count is not None:
        before["candidate_count"] = before_count
    if after_count is not None:
        after["candidate_count"] = after_count
    payload = write_replay(target, scenario_id=scenario_id, before=before, after=after)
    payload["path"] = str(_replays_root(target.expanduser().resolve()) / str(payload["replay_id"]))
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"learning_replay: {payload['replay_id']}")
    print(f"path: {payload['path']}")
    print("remote_mutation: false")
    return 0


def replay_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    replays = _read_replays(target)
    payload = {"target": str(target), "replays": replays, "replay_count": len(replays)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"learning_replays: {target}")
    print(f"replays: {len(replays)}")
    for replay in replays:
        print(f"- {replay.get('replay_id')} scenario={replay.get('scenario_id')}")
    return 0


def replay_show(*, target: Path, replay_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    replay, error = _find_replay(target, replay_id)
    if replay is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = {"target": str(target), "replay": replay}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"learning_replay: {replay.get('replay_id')}")
    print(f"scenario: {replay.get('scenario_id')}")
    return 0


def replay_compare(*, target: Path, replay_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    replay, error = _find_replay(target, replay_id)
    if replay is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = _compare_replay_payload(target, replay)
    root = _replay_compares_root(target) / str(payload["compare_id"])
    _write_json(root / "compare.json", payload)
    payload["path"] = str(root)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"learning_replay_compare: {payload['compare_id']}")
    print(f"outcome: {payload['outcome']}")
    print(f"path: {root}")
    return 0


def health(target: Path) -> dict[str, Any]:
    payload = plan_payload(target)
    compares = _read_replay_compares(target)
    replay_issue = None
    latest_compare = compares[0] if compares else None
    if isinstance(latest_compare, dict) and latest_compare.get("outcome") == "regressed":
        replay_issue = {
            "status": WARN,
            "name": "learning_replay_regressed",
            "detail": f"{latest_compare.get('replay_id')} regressed by {latest_compare.get('candidate_delta')}",
            "compare_id": latest_compare.get("compare_id"),
        }
    issue_count = payload["issue_count"] + (1 if replay_issue else 0)
    return {
        "target": payload["target"],
        "candidate_count": payload["candidate_count"],
        "raw_candidate_count": payload["raw_candidate_count"],
        "quieted_candidate_count": payload["quieted_candidate_count"],
        "changed_fingerprint_count": payload["changed_fingerprint_count"],
        "issue_count": issue_count,
        "top_issue": payload["top_issue"] or replay_issue,
        "candidates": payload["candidates"],
        "latest_closeout": _read_closeouts(target)[0] if _read_closeouts(target) else None,
        "replay": {
            "latest": _read_replays(target)[0] if _read_replays(target) else None,
            "latest_compare": latest_compare,
            "top_issue": replay_issue,
            "compare_count": len(compares),
        },
    }
