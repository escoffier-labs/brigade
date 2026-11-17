"""Text and JSON rendering for ``brigade care`` payloads."""

from __future__ import annotations

import json
from typing import Any


def print_payload(payload: dict[str, Any], *, json_output: bool) -> None:
    from .care_cmd import REPEATED_FAILURE_THRESHOLD

    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key, value in payload.items():
        if key == "repeated_failures" and isinstance(value, list):
            print(f"repeated_failures: {len(value)}")
            for rec in value:
                if not isinstance(rec, dict):
                    continue
                print(f"  - {rec.get('id')}: {rec.get('consecutive_failures')} consecutive failed receipts")
            continue
        if key == "adopted" and isinstance(value, list):
            print(f"adopted: {len(value)}")
            for rec in value:
                if not isinstance(rec, dict):
                    continue
                print(
                    f"  - {rec.get('requested')} -> {rec.get('using')} "
                    f"(runbook {rec.get('runbook_id') or rec.get('runbook')})"
                )
            continue
        if key == "entries" and isinstance(value, list):
            print(f"entries: {len(value)}")
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                receipt = entry.get("last_receipt")
                receipt_bit = "none"
                if isinstance(receipt, dict):
                    receipt_bit = f"{receipt.get('status')}@{receipt.get('started_at')}"
                present = entry.get("runbook_present")
                present_bit = ""
                if present is False:
                    present_bit = " runbook=missing"
                elif present is True:
                    present_bit = " runbook=present"
                fail_bit = ""
                consecutive = entry.get("consecutive_failures")
                if isinstance(consecutive, int) and consecutive >= REPEATED_FAILURE_THRESHOLD:
                    fail_bit = f" repeated_failure={consecutive}"
                print(
                    f"  - {entry.get('id')}: schedule={entry.get('schedule')} "
                    f"last_receipt={receipt_bit}{present_bit}{fail_bit}"
                    + (" paused=true" if entry.get("paused") else "")
                )
            continue
        if key == "units" and isinstance(value, list):
            print(f"units: {len(value)}")
            for unit in value:
                if isinstance(unit, dict):
                    print(f"  - {unit.get('name')}: {unit.get('path')} ({unit.get('status')})")
            continue
        if key == "tasks" and isinstance(value, list):
            print(f"tasks: {len(value)}")
            for task in value:
                if not isinstance(task, dict):
                    continue
                if "exists" in task:
                    last_run = task.get("last_run") or "none"
                    print(
                        f"  - {task.get('id')}: exists={task.get('exists')} "
                        f"last_run={last_run} schedule={task.get('schedule')}"
                    )
                    continue
                print(f"  - {task.get('id')}: {task.get('command')}")
            continue
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, sort_keys=True)}")
        else:
            print(f"{key}: {value}")
