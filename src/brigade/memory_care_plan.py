"""Planning-only memory-care metadata repairs. Writes no card files."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .memory_cmd import MemoryCareConfig

PLANABLE_METADATA_FIXES = {"missing-reviewed", "missing-freshness"}
FRESHNESS_DERIVATION = "last_reviewed+stale_after_days"


def metadata_plan_blockers(issue_type: str, *, reviewed: date | None) -> list[str]:
    if issue_type == "missing-reviewed":
        return ["requires-current-evidence-review"]
    if issue_type == "missing-freshness":
        return [] if reviewed is not None else ["requires-operator-freshness-date"]
    return ["issue-type-not-supported-for-metadata-plan"]


def derived_fresh_until(reviewed: date, stale_after_days: int) -> str:
    return (reviewed + timedelta(days=stale_after_days)).isoformat()


def block_reason_counts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        blockers = item.get("blockers")
        if not isinstance(blockers, list):
            continue
        for blocker in blockers:
            if isinstance(blocker, str) and blocker:
                counts[blocker] = counts.get(blocker, 0) + 1
    return [
        {"reason": reason, "count": counts[reason]} for reason in sorted(counts, key=lambda name: (-counts[name], name))
    ]


def format_block_reasons(reasons: object) -> str:
    if not isinstance(reasons, list) or not reasons:
        return "none"
    parts: list[str] = []
    for item in reasons:
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        count = item.get("count")
        if isinstance(reason, str) and reason and isinstance(count, int):
            parts.append(f"{reason}={count}")
    return ", ".join(parts) if parts else "none"


def plan_item(issue: dict[str, Any], *, target: Path, scan_date: str, config: MemoryCareConfig) -> dict[str, Any]:
    from . import memory_cmd as care

    card_file = str(issue.get("file") or issue.get("card_file") or "")
    issue_type = str(issue.get("issue_type") or "")
    source_fingerprint = str(issue.get("source_fingerprint") or "")
    path = target / card_file if card_file else target
    blockers: list[str] = []
    candidate_fields: dict[str, str] = {}
    reviewed: date | None = None
    if not card_file:
        blockers.append("missing-card-path")
    elif not path.is_file():
        blockers.append("card-file-missing")
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            blockers.append("card-file-unreadable")
        else:
            meta, has_frontmatter = care._parse_frontmatter(text)
            if not has_frontmatter:
                blockers.append("card-frontmatter-missing")
            else:
                reviewed = care._parse_date(
                    care._frontmatter_value(meta, "last_reviewed", "last_reviewed_at", "reviewed_at")
                )
    if issue_type == "missing-reviewed":
        candidate_fields["last_reviewed"] = scan_date
    elif issue_type == "missing-freshness":
        if reviewed is not None:
            candidate_fields["fresh_until"] = derived_fresh_until(reviewed, config.stale_after_days)
        else:
            candidate_fields["fresh_until"] = "<operator-selected-date>"
    blockers.extend(metadata_plan_blockers(issue_type, reviewed=reviewed))
    unblocked = not blockers
    item = {
        "id": f"memory-care-fix-{source_fingerprint or care._stable_hash({'file': card_file, 'issue_type': issue_type})}",
        "card_file": card_file,
        "card_id": issue.get("card_id"),
        "issue_type": issue_type,
        "source_fingerprint": source_fingerprint,
        "safe_summary": issue.get("safe_summary") or issue.get("summary") or "",
        "candidate_fields": candidate_fields,
        "status": "blocked" if blockers else "planned",
        "safe_to_apply_automatically": False,
        "would_write": False,
        "blockers": blockers,
        "suggested_next_command": (
            "brigade memory care backfill" if unblocked else "brigade memory care import-issues"
        ),
    }
    if issue_type == "missing-freshness" and reviewed is not None:
        item["derivation"] = FRESHNESS_DERIVATION
    return item


def plan_payload(target: Path, config: MemoryCareConfig, scan_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from . import memory_cmd as care

    target = target.expanduser().resolve()
    if scan_payload is None:
        scan_payload = care._load_scan_payload(target, config)
    scan_date = (
        str(scan_payload.get("scan_date") or care._today().isoformat())
        if isinstance(scan_payload, dict)
        else care._today().isoformat()
    )
    issues_value = scan_payload.get("issues") if isinstance(scan_payload, dict) else None
    issues = issues_value if isinstance(issues_value, list) else []
    items = [
        plan_item(issue, target=target, scan_date=scan_date, config=config)
        for issue in issues
        if isinstance(issue, dict) and str(issue.get("issue_type") or "") in PLANABLE_METADATA_FIXES
    ]
    blocked = [item for item in items if item.get("blockers")]
    unblocked = [item for item in items if not item.get("blockers")]
    if unblocked:
        next_command = "brigade memory care backfill"
    elif items:
        next_command = "brigade memory care import-issues"
    else:
        next_command = "brigade memory care scan"
    return {
        "target": str(target),
        "scan_path": str(care._scan_path(target, config)),
        "queue_path": str(care._queue_path(target, config)),
        "generated_at": care._utc_iso(),
        "valid": scan_payload is not None,
        "would_write": False,
        "plan_count": len(items),
        "blocked_count": len(blocked),
        "block_reasons": block_reason_counts(items),
        "items": items,
        "suggested_next_command": next_command,
    }


def plan_fixes(*, target: Path, json_output: bool = False) -> int:
    from . import memory_cmd as care

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        config = care._config_or_default(target)
        scan_payload = care._load_scan_payload(target, config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: cannot read memory-care scan: {exc}", file=sys.stderr)
        return 2
    if scan_payload is None:
        print(f"error: memory-care scan not found: {care._scan_path(target, config)}", file=sys.stderr)
        return 2
    payload = plan_payload(target, config, scan_payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"memory care fix plan: {target}")
    print(f"scan_path: {payload['scan_path']}")
    print("would_write: false")
    print(f"planned: {payload['plan_count']}")
    print(f"blocked: {payload['blocked_count']}")
    print(f"block_reasons: {format_block_reasons(payload.get('block_reasons'))}")
    for item in payload["items"]:
        blockers = ",".join(item.get("blockers", [])) or "none"
        print(f"- {item['card_file']} {item['issue_type']} {item['status']} blockers={blockers}")
    print(f"next_command: {payload['suggested_next_command']}")
    return 0
