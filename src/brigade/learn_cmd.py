"""Bounded local learning candidate aggregation."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import work_cmd

OK = "ok"
WARN = "warn"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _learning_root(target: Path) -> Path:
    return target / ".brigade" / "learn"


def _replays_root(target: Path) -> Path:
    return _learning_root(target) / "replays"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _candidate(candidate_id: str, subsystem: str, status: str, summary: str, command: str, *, severity: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": candidate_id,
        "subsystem": subsystem,
        "status": status,
        "severity": severity,
        "safe_summary": summary,
        "suggested_next_command": command,
        "metadata": metadata or {},
    }


def candidates(target: Path) -> list[dict[str, Any]]:
    target = target.expanduser().resolve()
    results: list[dict[str, Any]] = []
    for item in work_cmd._read_imports(target):
        if item.get("status", "pending") != "pending":
            continue
        source = str(item.get("source") or "manual")
        if source in {"code-review", "security-scan", "memory-care", "backup-health", "scanner-health"}:
            import_id = str(item.get("id") or "")
            results.append(
                _candidate(
                    import_id,
                    source,
                    "pending",
                    str(item.get("text") or ""),
                    f"brigade work import plan {import_id}",
                    severity=item.get("priority") if isinstance(item.get("priority"), str) else None,
                    metadata={"import_id": import_id, "source": source},
                )
            )
    for receipt in work_cmd._review_receipts(target):
        if receipt.get("status") == "failed":
            run_id = str(receipt.get("run_id") or "")
            results.append(_candidate(run_id, "code-review", "failed", "failed review run", f"brigade work review show {run_id}"))
    tool_runs = target / ".brigade" / "tools" / "runs"
    if tool_runs.is_dir():
        for path in sorted(tool_runs.glob("*/receipt.json")):
            payload = _read_json(path)
            if isinstance(payload, dict) and payload.get("status") == "failed":
                run_id = str(payload.get("run_id") or path.parent.name)
                results.append(_candidate(run_id, "tool-run", "failed", "failed portable tool run", f"brigade tools run show {run_id}"))
    return results


def plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    items = candidates(target)
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
        fingerprint = work_cmd._stable_hash({"id": item["id"], "subsystem": item["subsystem"], "summary": item["safe_summary"]})
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
    imported, skipped, dismissed = work_cmd._append_import_records(target.expanduser().resolve(), records, dry_run=dry_run)
    output = {"target": payload["target"], "created": len(imported), "skipped": len(skipped), "dismissed": len(dismissed), "dry_run": dry_run}
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"learning_imports: {payload['target']}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(dismissed)}")
    return 0


def write_replay(target: Path, *, scenario_id: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    target = target.expanduser().resolve()
    replay_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-learning-replay-{scenario_id}"
    payload = {
        "replay_id": replay_id,
        "scenario_id": scenario_id,
        "created_at": _now().isoformat(),
        "before": before,
        "after": after,
        "privacy": "safe summaries only",
    }
    _write_json(_replays_root(target) / replay_id / "replay.json", payload)
    return payload


def health(target: Path) -> dict[str, Any]:
    payload = plan_payload(target)
    return {
        "target": payload["target"],
        "candidate_count": payload["candidate_count"],
        "issue_count": payload["issue_count"],
        "top_issue": payload["top_issue"],
        "candidates": payload["candidates"],
    }
