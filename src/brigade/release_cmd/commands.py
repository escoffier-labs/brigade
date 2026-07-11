"""Local release readiness receipts."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import (
    context_cmd,
    handoff_cmd,
    learn_cmd,
    memory_cmd,
    phases_cmd,
    projects_cmd,
    repos_cmd,
    reportstore,
    research_cmd,
    roadmap_cmd,
    scrub,
    security_cmd,
    tools_cmd,
    work_cmd,
)
from ..selection import KNOWN_HARNESSES
from ..localio import (
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)

from . import paths as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def run(*, target: Path, base_ref: str | None = "origin/main", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    started = _now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-release-{uuid4().hex[:6]}"
    payload = _payload(target, base_ref=base_ref, run_checks=True)
    completed = _now()
    receipt = {
        **payload,
        "run_id": run_id,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": (completed - started).total_seconds(),
        "path": str(_release_runs_root(target) / run_id),
    }
    receipt_path = _release_runs_root(target) / run_id / "receipt.json"
    _write_json(receipt_path, receipt)
    _write_release_markdown(receipt_path, receipt)
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0 if receipt["ready"] else 1
    print(f"release run: {run_id}")
    print(f"status: {receipt['status']}")
    print(f"ready: {receipt['ready']}")
    print(f"blockers: {len(receipt['blockers'])}")
    print(f"warnings: {len(receipt['warnings'])}")
    print(f"receipt: {receipt_path}")
    return 0 if receipt["ready"] else 1


def runs(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    items = _release_receipts(target)[:limit]
    payload = {"target": str(target), "release_runs_root": str(_release_runs_root(target)), "runs": items}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release runs: {target}")
    print(f"release_runs_root: {payload['release_runs_root']}")
    if not items:
        print("runs: none")
        return 0
    for item in items:
        print(
            f"- {item.get('run_id')} [{item.get('status')}] blockers={len(item.get('blockers') or [])} {item.get('started_at')}"
        )
    return 0


def show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipt, error = _resolve_release_receipt(target, run_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    print(f"release run: {receipt.get('run_id')}")
    print(f"status: {receipt.get('status')}")
    print(f"ready: {receipt.get('ready')}")
    print(f"blockers: {len(receipt.get('blockers') or [])}")
    for blocker in receipt.get("blockers") or []:
        print(f"- {blocker}")
    print(f"warnings: {len(receipt.get('warnings') or [])}")
    return 0
