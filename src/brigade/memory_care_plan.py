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


def derived_fresh_until(reviewed: date, stale_after_days: int) -> str | None:
    """Derive the freshness date, or None when it falls outside ``date``'s range.

    A card may carry a near-limit ``last_reviewed`` such as ``9999-12-31``, and
    ``stale_after_days`` is operator-configured. Either can push the sum past
    ``date.max`` or below ``date.min``, so the caller treats a non-representable
    derivation as a blocker instead of letting the arithmetic escape.
    """
    try:
        return (reviewed + timedelta(days=stale_after_days)).isoformat()
    except (OverflowError, ValueError):
        return None


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


def plan_item(
    issue: dict[str, Any],
    *,
    target: Path,
    scan_date: str,
    config: MemoryCareConfig,
    context: tuple[set[str], bool] | None = None,
) -> dict[str, Any]:
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
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                text = handle.read()
        except OSError:
            blockers.append("card-file-unreadable")
        else:
            meta, has_frontmatter = care._parse_frontmatter(text.replace("\r\n", "\n"))
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
            derived = derived_fresh_until(reviewed, config.stale_after_days)
            if derived is None:
                blockers.append("freshness-date-not-representable")
            else:
                candidate_fields["fresh_until"] = derived
        else:
            candidate_fields["fresh_until"] = "<operator-selected-date>"
    blockers.extend(metadata_plan_blockers(issue_type, reviewed=reviewed))
    from .memory_care_tend import load_config
    from .memory_care_fixes import AUTO_FIXES, prepare, FixRefused

    automatic = load_config(target).enabled and issue_type in AUTO_FIXES
    derived_auto: dict[str, Any] = {}
    if automatic:
        try:
            derived_auto = prepare(
                target, config, {"card_file": card_file, "issue_type": issue_type}, care._today(), context=context
            )
            candidate_fields = derived_auto["candidate_fields"]
            blockers = []
        except (FixRefused, OSError, ValueError) as exc:
            candidate_fields = {}
            blockers = [str(exc) if isinstance(exc, FixRefused) else "unreadable"]
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
        "safe_to_apply_automatically": automatic and unblocked,
        "content_digest": derived_auto.get("content_digest"),
        "would_write": False,
        "blockers": blockers,
        "suggested_next_command": (
            "brigade memory care backfill" if unblocked else "brigade memory care import-issues"
        ),
    }
    if issue_type == "missing-freshness" and reviewed is not None and "fresh_until" in candidate_fields:
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
    # The shared scanner does not recognize BOM frontmatter. Interpret it only
    # here, using the fixer's strict grammar, without changing other consumers.
    from .memory_care_fixes import plain_frontmatter, read, FixRefused

    issues = list(issues)
    for issue in list(issues):
        if not isinstance(issue, dict) or issue.get("issue_type") != "missing-frontmatter":
            continue
        try:
            raw = read(target, str(issue.get("file", "")))
            if not raw.startswith(b"\xef\xbb\xbf"):
                continue
            meta = plain_frontmatter(raw.decode("utf-8"))
        except (FixRefused, OSError, ValueError):
            continue
        for kind, keys in (
            ("missing-reviewed", ("last_reviewed", "last_reviewed_at", "reviewed_at")),
            ("missing-freshness", ("fresh_until", "freshness_until", "expires", "expires_at")),
        ):
            if kind in config.enabled_checks and not any(key in meta for key in keys):
                issues.append({**issue, "issue_type": kind})

    from .memory_care_fixes import AUTO_FIXES, plan_context
    from .memory_care_tend import load_config

    needs_context = load_config(target).enabled and any(
        isinstance(issue, dict)
        and issue.get("issue_type") in AUTO_FIXES
        and care.read_only_pattern(str(issue.get("file") or issue.get("card_file") or ""), config.read_only_paths)
        is None
        for issue in issues
    )
    context = plan_context(target, config) if needs_context else None
    items = [
        plan_item(issue, target=target, scan_date=scan_date, config=config, context=context)
        for issue in issues
        if isinstance(issue, dict)
        and str(issue.get("issue_type") or "") in (PLANABLE_METADATA_FIXES | {"missing-index-link", "orphaned-card"})
        # A saved scan can predate a read_only_paths entry; never plan edits for those cards.
        and care.read_only_pattern(str(issue.get("file") or issue.get("card_file") or ""), config.read_only_paths)
        is None
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


def plan_fixes(*, target: Path, json_output: bool = False, apply: bool = False) -> int:
    from . import memory_cmd as care

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        config = care._config_or_default(target)
        scan_payload = care._load_scan_payload(target, config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        if apply:
            print("memory care fixes: applied=0 skipped=1 reasons=scan-unreadable=1", file=sys.stderr)
            return 2
        print(f"error: cannot read memory-care scan: {exc}", file=sys.stderr)
        return 2
    if scan_payload is None:
        if apply:
            print("memory care fixes: applied=0 skipped=1 reasons=scan-missing=1", file=sys.stderr)
            return 2
        print(f"error: memory-care scan not found: {care._scan_path(target, config)}", file=sys.stderr)
        return 2
    payload = plan_payload(target, config, scan_payload)
    if apply:
        from .memory_care_fixes import apply_fixes, summary

        from .memory_care_journal import JournalConflict

        try:
            result = apply_fixes(target, config, today=care._today(), plan=payload)
        except JournalConflict:
            print("memory care busy", file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True) if json_output else summary(result))
        return 0
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
