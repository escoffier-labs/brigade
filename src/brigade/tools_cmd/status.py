"""Status, doctor, import, and parity commands for the tools command family."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..render import emit
from . import catalog_health, constants, helpers, issues, packs as packs_mod, paths


def health(target: Path) -> dict[str, Any]:
    payload = catalog_health._catalog_payload(target)
    packs = packs_mod._tool_pack_health(target)
    sync_plan = packs_mod._sync_plan_summary(target)
    return {
        "config_path": payload["config_path"],
        "valid": payload["valid"],
        "tool_count": payload["tool_count"],
        "raw_issue_count": payload["raw_issue_count"],
        "issue_count": payload["issue_count"],
        "top_issue": payload["top_issue"],
        "issues": payload["issues"],
        "parity": payload["parity"],
        "packs": packs,
        "sync_plan": sync_plan,
        "call_queue": payload["call_queue"],
        "run_history": payload["run_history"],
        "checkpoints": payload["checkpoints"],
        "runtimes": payload["runtimes"],
        "policy": payload["policy"],
    }


def _issue_records(target: Path) -> list[dict[str, Any]]:
    payload = catalog_health._catalog_payload(target)
    records: list[dict[str, Any]] = []
    for issue in payload["issues"]:
        issue_type = str(issue.get("issue_type") or issue.get("name") or "tool_issue")
        tool_id = str(issue.get("tool_id") or "catalog")
        detail = str(issue.get("detail") or "")
        source_fingerprint = str(
            issue.get("parity_fingerprint")
            or helpers._stable_hash(
                {
                    "tool_id": tool_id,
                    "issue_type": issue_type,
                    "detail": detail,
                    "harness": issue.get("harness"),
                    "call_id": issue.get("call_id"),
                    "run_id": issue.get("run_id"),
                    "checkpoint_id": issue.get("checkpoint_id"),
                    "projection_target": issue.get("projection_target"),
                    "projection_status": issue.get("projection_status"),
                    "tool_source_fingerprint": issue.get("tool_source_fingerprint"),
                    "expected_projection_fingerprint": issue.get("expected_projection_fingerprint"),
                    "actual_projection_fingerprint": issue.get("actual_projection_fingerprint"),
                }
            )
        )
        metadata = {
            "tool_id": tool_id,
            "tool_family": issue.get("family"),
            "tool_issue_type": issue_type,
            "tool_harness": issue.get("harness"),
            "tool_call_id": issue.get("call_id"),
            "tool_run_id": issue.get("run_id"),
            "tool_checkpoint_id": issue.get("checkpoint_id"),
            "projection_target": issue.get("projection_target"),
            "tool_issue_detail": detail,
            "source_item_key": f"tool-catalog:{tool_id}:{issue_type}:{issue.get('harness') or ''}:{issue.get('call_id') or ''}:{issue.get('run_id') or ''}:{issue.get('checkpoint_id') or ''}",
            "source_fingerprint": source_fingerprint,
        }
        records.append(
            {
                "text": f"Repair tool catalog issue {tool_id}/{issue_type}: {detail}",
                "kind": "task",
                "source": "tool-catalog",
                "type": "workflow",
                "priority": "normal",
                "template": "bugfix",
                "acceptance": [f"`brigade tools doctor` no longer reports {tool_id}/{issue_type}."],
                "metadata": metadata,
            }
        )
    return records


def doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = catalog_health._catalog_payload(target)
    text_lines = [f"tools doctor: {target}", f"config_path: {payload['config_path']}"]
    if payload["errors"]:
        for error in payload["errors"]:
            text_lines.append(f"[warn] tool_config: {error}")
    else:
        text_lines.append(f"[ok] tool_config: {payload['config_path']}")
    if payload["issues"]:
        for issue in payload["issues"]:
            text_lines.append(f"[{issue.get('status', constants.WARN)}] {issue.get('name')}: {issue.get('detail')}")
    else:
        text_lines.append("[ok] tool_catalog: no issues")
    text_lines.append(f"tool_issues: {payload['issue_count']}")
    return emit(payload, json_output, text_lines, 0 if payload["valid"] else 1)


def import_issues(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    records = _issue_records(target)
    from .. import work_cmd

    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records)
    payload = {
        "target": str(target),
        "imports_path": str(work_cmd._imports_path(target)),
        "issues": len(records),
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    text_lines = [
        f"tool issue imports: {target}",
        f"imports_path: {payload['imports_path']}",
        f"issues: {len(records)}",
        f"created: {len(imported)}",
        f"skipped: {len(skipped)}",
        f"dismissed: {len(skipped_dismissed)}",
    ]
    for item in imported:
        text_lines.append(f"- {item.get('id')} [{item.get('kind')}] {helpers._short(str(item.get('text', '')))}")
    return emit(payload, json_output, text_lines, 0)


def parity_status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = catalog_health._catalog_payload(target)
    raw_parity = payload.get("parity")
    parity = raw_parity if isinstance(raw_parity, dict) else {}
    projection_issues = [issue for issue in payload["issues"] if issues._is_parity_issue(issue)]
    response = {
        "target": str(target),
        "closeouts_path": str(paths.parity_closeouts_path(target)),
        "latest_closeout": parity.get("latest_closeout"),
        "projection_issue_count": len(projection_issues),
        "quieted_issue_count": parity.get("quieted_issue_count", 0),
        "changed_issue_count": parity.get("changed_issue_count", 0),
        "issues": projection_issues,
        "quieted_issues": parity.get("quieted_issues", []),
        "changed_issues": parity.get("changed_issues", []),
    }
    if json_output:
        print(json.dumps(response, indent=2, sort_keys=True))
        return 0
    print(f"tools parity: {target}")
    print(f"projection_issues: {response['projection_issue_count']}")
    print(f"quieted_issues: {response['quieted_issue_count']}")
    print(f"changed_issues: {response['changed_issue_count']}")
    latest = response.get("latest_closeout")
    if isinstance(latest, dict):
        print(f"latest_closeout: {latest.get('closeout_id')} [{latest.get('status')}]")
    else:
        print("latest_closeout: none")
    return 0


def parity_closeout(*, target: Path, reason: str | None = None, defer: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = catalog_health._catalog_payload(target)
    source_issues = [
        issue for issue in payload.get("raw_issues", []) if isinstance(issue, dict) and issues._is_parity_issue(issue)
    ]
    fingerprints = [issues._parity_issue_fingerprint(issue) for issue in source_issues]
    closeout_id = f"{helpers._now().strftime('%Y%m%d-%H%M%S')}-tool-parity-closeout"
    closeout = {
        "closeout_id": closeout_id,
        "created_at": helpers._now().isoformat(),
        "status": "deferred" if defer else "reviewed",
        "reason": reason or "",
        "issue_count": len(source_issues),
        "source_fingerprints": fingerprints,
        "safe_summary": f"{len(source_issues)} tool projection parity issue(s) {'deferred' if defer else 'reviewed'}",
    }
    helpers._write_json(paths.parity_closeouts_path(target) / closeout_id / "closeout.json", closeout)
    if json_output:
        print(json.dumps(closeout, indent=2, sort_keys=True))
        return 0
    print(f"tool_parity_closeout: {closeout_id}")
    print(f"status: {closeout['status']}")
    print(f"issues: {closeout['issue_count']}")
    return 0
