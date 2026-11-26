"""Evaluate and, in enforce mode, require pre-push verification receipts."""

from __future__ import annotations

import os
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from .. import __version__
from ..verify_manifest_git import _git
from . import push_gate_log, verify_push
from .push_gate import _mode
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
        mode = _mode(target)
    except Exception:
        print("push gate: could not read brigade.pushGate from Git config", file=sys.stderr)
        return 2
    if mode not in {"warn", "enforce"}:
        return 0
    enforce = mode == "enforce"
    try:
        updates = branch_updates(verify_push._read_stdin(deadline))
        if not updates:
            return 0
        common = _git(target, "rev-parse", "--path-format=absolute", "--git-common-dir", deadline=deadline)
        if common.returncode:
            raise OSError("could not locate the common Git directory")
    except Exception:
        print("push gate: could not read Git's pre-push updates or common directory", file=sys.stderr)
        return 2 if enforce else 0

    log = push_gate_log.log_path(Path(os.fsdecode(common.stdout.rstrip(b"\r\n"))))
    try:
        results = verify_push._check(target, updates, None, deadline, details=True)
    except Exception:
        results = [
            {
                "ref": ref,
                "tip": tip,
                "tree": None,
                "status": "indeterminate",
                "reason_code": "check_failed",
                "reason": "Brigade could not evaluate verification receipts.",
                "remedy": None,
            }
            for ref, tip in updates
        ]

    stamp = datetime.now(UTC).isoformat()
    duration = round((monotonic() - started) * 1000)
    rows = []
    blocked = False
    for item in results:
        verdict = item["status"]
        code = item["reason_code"]
        failed = code in {"check_timeout", "check_failed"}
        would_block = verdict in _WOULD_BLOCK
        refused = enforce and (would_block or verdict == "indeterminate")
        blocked |= refused
        if refused or would_block:
            print(f"push gate: {item['ref']} ({verdict})", file=sys.stderr)
            if refused or code == verify_push._MASKED_PASS:
                print(item["reason"], file=sys.stderr)
            remedy = item["remedy"]
            if refused:
                tip = item["tip"]
                if remedy:
                    print(f"Verify commit {tip} in this worktree, then retry the push.", file=sys.stderr)
                else:
                    print(
                        "Run this command to diagnose the pushed commit, then retry the push after the check works:",
                        file=sys.stderr,
                    )
                    remedy = f"brigade work verify push-check --target {shlex.quote(str(target))} --ref {tip}"
            print(remedy, file=sys.stderr)
        rows.append(
            {
                "schema": push_gate_log.SCHEMA,
                "time": stamp,
                "mode": mode,
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
    if enforce:
        for row in rows:
            row["blocked"] = blocked
    try:
        push_gate_log.append(log, rows)
    except OSError:
        print("push gate: could not write the observation log", file=sys.stderr)
    # Reserve 1 for an unexpected process failure, which the shell diagnoses.
    return 3 if blocked else 0
