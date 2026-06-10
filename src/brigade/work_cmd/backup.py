"""Backup bootstrap and health operations."""

from __future__ import annotations
import json
import sys
from pathlib import Path
from .. import dogfood_cmd
from ..install import apply_gitignore
from . import constants, helpers, ledger as ledger_mod, config as config_mod


def backup_init(*, target: Path, force: bool = False, update_gitignore: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = helpers._backup_config_path(target)
    if path.exists() and not force:
        print(f"error: backup config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod._format_backup_toml())
    print(f"backup_config: {path}")
    print(f"destinations: {len(constants.BACKUP_DEFAULTS)}")
    if update_gitignore:
        result = apply_gitignore(target, helpers._work_selection(target, dogfood_cmd.default_handoff_inbox(target)))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade work backup status")
    return 0


def backup_contract(*, target: Path, destination_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = config_mod._backup_contract_payload(target, destination_id=destination_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"work backup contract: {target}")
    print(f"config_path: {payload['config_path']}")
    print(f"config_loaded: {payload['config_loaded']}")
    if payload.get("config_errors"):
        for error in payload["config_errors"]:
            print(f"config_warning: {error}")
    for error in payload.get("errors", []):
        print(f"error: {error}", file=sys.stderr)
    print(f"destinations: {payload['destination_count']}")
    for destination in payload.get("destinations", []):
        print(f"- {destination.get('id')} [{destination.get('kind')}]")
        print(f"  summary_path: {destination.get('summary_path')}")
        print(f"  command_label: {destination.get('command_label')}")
        print(f"  required_fields: {', '.join(destination.get('required_fields', []))}")
        print(f"  accepted_success_results: {', '.join(destination.get('accepted_success_results', []))}")
    print("would_write: false")
    return rc


def backup_status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    health = config_mod._backup_health(target)
    if json_output:
        print(json.dumps(health, indent=2, sort_keys=True))
        return 0 if health["valid"] else 1
    print(f"work backup status: {target}")
    print(f"config_path: {health['config_path']}")
    if not health["valid"]:
        for check in health["checks"]:
            if check.get("name") == "backup_config":
                print(f"error: {check.get('detail')}")
        return 1
    destinations = health.get("destinations") if isinstance(health.get("destinations"), list) else []
    print(f"destinations: {len(destinations)}")
    print(f"operator_summary: {health.get('operator_summary')}")
    for destination in destinations:
        if not isinstance(destination, dict):
            continue
        status = "enabled" if destination.get("enabled", True) else "disabled"
        destination_issues = [issue for issue in health["issues"] if issue.get("destination") == destination.get("id")]
        print(f"- {destination.get('id')} [{status}] {destination.get('kind')} issues={len(destination_issues)}")
        print(f"  summary: {destination.get('summary_path')}")
    top_issue = health.get("top_issue")
    if isinstance(top_issue, dict):
        print(f"top_issue: {top_issue.get('destination')}/{top_issue.get('issue_type')} {top_issue.get('detail')}")
    else:
        print("top_issue: none")
    print(f"raw_issues: {health.get('raw_issue_count')}")
    print(f"quieted_issues: {health.get('quieted_issue_count')}")
    print(f"restore_rehearsal_issues: {health.get('restore_rehearsal_issue_count')}")
    return 0


def backup_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    health = config_mod._backup_health(target)
    if json_output:
        print(json.dumps(health, indent=2, sort_keys=True))
        return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1
    print(f"work backup doctor: {target}")
    print(f"config_path: {health['config_path']}")
    for check in health.get("active_checks", health["checks"]):
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    print(f"backup_issues: {health['issue_count']}")
    return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1


def backup_import_issues(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    records = config_mod._backup_issue_records(target)
    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records)
    payload = {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "issues": len(records),
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"backup issue imports: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"issues: {len(records)}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {helpers._short(str(item.get('text', '')))}")
    return 0


def backup_closeout(*, target: Path, reason: str | None = None, defer: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    raw_health = config_mod._backup_health(target)
    source_issues = (
        raw_health.get("raw_issues") if isinstance(raw_health.get("raw_issues"), list) else raw_health["issues"]
    )
    fingerprints = [config_mod._backup_issue_fingerprint(issue) for issue in source_issues if isinstance(issue, dict)]
    closeout_id = f"{helpers._now().strftime('%Y%m%d-%H%M%S')}-backup-closeout"
    payload = {
        "closeout_id": closeout_id,
        "created_at": helpers._now().isoformat(),
        "status": "deferred" if defer else "reviewed",
        "reason": reason or "",
        "issue_count": len(source_issues),
        "source_fingerprints": fingerprints,
        "restore_rehearsal_issue_count": raw_health.get("restore_rehearsal_issue_count", 0),
        "safe_summary": f"{len(fingerprints)} backup issue(s) {'deferred' if defer else 'reviewed'}",
    }
    helpers._write_json(config_mod._backup_closeouts_root(target) / closeout_id / "closeout.json", payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"backup_closeout: {closeout_id}")
    print(f"status: {payload['status']}")
    print(f"issues: {payload['issue_count']}")
    return 0
