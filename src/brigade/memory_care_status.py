"""Memory care status rendering, including the scheduled reflect pause gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import memory_cmd
from .memory_care_plan import format_block_reasons


def status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    from .memory_reflect_hub import care_status_payload

    payload = care_status_payload(target, memory_cmd.health(target))
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"memory care status: {target}\nreflect: {json.dumps(payload['reflect'], sort_keys=True)}")
    print(f"config_path: {payload['config_path']}")
    print(f"scan_path: {payload['scan_path']}")
    print(f"queue_path: {payload['queue_path']}")
    if payload.get("read_only_paths"):
        print(f"read_only_paths: {', '.join(payload['read_only_paths'])}")
    issue_count = payload["issue_count"]
    print(f"health: {'ok' if issue_count == 0 else str(issue_count) + ' issue(s)'}")
    evidence_count = payload["counts"].get("unconsolidated-evidence", 0)
    if "unconsolidated-evidence" not in payload["enabled_checks"]:
        evidence_count = "disabled"
    print(f"unconsolidated-evidence: {evidence_count}")
    metadata_value = payload.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    reviewed_value = metadata.get("reviewed_dates")
    reviewed = reviewed_value if isinstance(reviewed_value, dict) else None
    freshness_value = metadata.get("freshness_dates")
    freshness = freshness_value if isinstance(freshness_value, dict) else None
    evidence_value = metadata.get("evidence")
    evidence = evidence_value if isinstance(evidence_value, dict) else None
    evidence_refs_value = metadata.get("evidence_refs")
    evidence_refs = evidence_refs_value if isinstance(evidence_refs_value, dict) else None
    confidence_value = metadata.get("confidence")
    confidence = confidence_value if isinstance(confidence_value, dict) else None
    if reviewed:
        print(
            f"reviewed_dates: present={reviewed.get('present', 0)} missing={reviewed.get('missing', 0)} stale={reviewed.get('stale', 0)}"
        )
    if freshness:
        print(
            f"freshness_dates: present={freshness.get('present', 0)} missing={freshness.get('missing', 0)} expired={freshness.get('expired', 0)}"
        )
    if evidence:
        print(f"evidence_metadata: present={evidence.get('present', 0)} missing={evidence.get('missing', 0)}")
    if evidence_refs:
        print(f"evidence_refs: present={evidence_refs.get('present', 0)} missing={evidence_refs.get('missing', 0)}")
    if confidence:
        rendered = ", ".join(f"{key}={confidence[key]}" for key in sorted(confidence))
        print(f"confidence_metadata: {rendered}")
    autofix_plan_value = payload.get("autofix_plan")
    autofix_plan = autofix_plan_value if isinstance(autofix_plan_value, dict) else {}
    if autofix_plan.get("plan_count"):
        print(
            f"autofix_plan: planned={autofix_plan.get('plan_count')} blocked={autofix_plan.get('blocked_count')} would_write=false"
        )
        print(f"autofix_plan_block_reasons: {format_block_reasons(autofix_plan.get('block_reasons'))}")
        if autofix_plan.get("suggested_next_command"):
            print(f"autofix_plan_command: {autofix_plan.get('suggested_next_command')}")
    top = payload.get("top_issue") if isinstance(payload.get("top_issue"), dict) else None
    if top:
        print(f"top_issue: {top.get('issue_type') or top.get('name')} {top.get('file') or top.get('detail')}")
    archive_value = payload.get("archive_candidates")
    archive = archive_value if isinstance(archive_value, dict) else {}
    print(
        "archive_candidates: "
        f"count={archive.get('candidate_count', 0)} "
        f"approval_required={archive.get('approval_required', False)} "
        f"would_archive={archive.get('would_archive', False)}"
    )
    for candidate in archive.get("candidates", []):
        print(f"  archive_candidate: {candidate.get('file')} signal={candidate.get('signal')}")
    recall_value = payload.get("search_recall")
    recall = recall_value if isinstance(recall_value, dict) else {}
    print(
        "search_recall: "
        f"searches={recall.get('searches', 0)} "
        f"followup_rate={recall.get('followup_rate', 0.0)} "
        f"(second-class; informs, never gates)"
    )
    return 0 if payload["valid"] else 1
