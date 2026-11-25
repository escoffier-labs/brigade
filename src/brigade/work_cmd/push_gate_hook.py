"""Evaluate pre-push verification receipts and record advisory outcomes."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from .. import __version__
from ..verify_manifest_git import _git
from . import push_gate_log, verify_push
from .verify_push_input import branch_updates

_WOULD_BLOCK = {"missing_receipt", "rejected"}
_REASONS = {
    "ok": "receipt_eligible",
    "missing_receipt": "receipt_missing",
    "rejected": "receipt_ineligible",
    "not_applicable": "no_required_check",
    "base_predates_policy": "base_predates_required_check",
    "indeterminate": "verdict_unavailable",
}


def run() -> int:
    started = monotonic()
    deadline = started + verify_push._BUDGET_SECONDS
    target = Path.cwd()
    try:
        updates = branch_updates(verify_push._read_stdin(deadline))
        if not updates:
            return 0
        common = _git(target, "rev-parse", "--path-format=absolute", "--git-common-dir", deadline=deadline)
        if common.returncode:
            raise OSError("could not locate the common Git directory")
    except Exception:
        print("push gate: could not read Git's pre-push updates or common directory", file=sys.stderr)
        return 0

    log = push_gate_log.log_path(Path(os.fsdecode(common.stdout.rstrip(b"\r\n"))))
    try:
        results = verify_push._check(target, updates, None, deadline, details=True)
    except Exception:
        results = [
            {"ref": ref, "tip": tip, "tree": None, "status": "indeterminate", "reason_code": "check_failed"}
            for ref, tip in updates
        ]

    stamp = datetime.now(UTC).isoformat()
    duration = round((monotonic() - started) * 1000)
    rows = []
    for item in results:
        verdict = item["status"]
        code = item["reason_code"]
        failed = code in {"check_timeout", "check_failed"}
        would_block = verdict in _WOULD_BLOCK
        if would_block:
            print(f"push gate: {item['ref']} ({verdict})\n{item['remedy']}", file=sys.stderr)
        rows.append(
            {
                "schema": push_gate_log.SCHEMA,
                "time": stamp,
                "mode": "warn",
                "ref": item["ref"],
                "tip": item["tip"],
                "tree": item["tree"],
                "result": "gate_error" if failed else verdict,
                "reason": code if failed else _REASONS[verdict],
                "would_block": would_block,
                "duration_ms": duration,
                "brigade_version": __version__,
                "worktree": str(target),
            }
        )
    try:
        push_gate_log.append(log, rows)
    except OSError:
        print("push gate: could not write the observation log", file=sys.stderr)
    return 0
