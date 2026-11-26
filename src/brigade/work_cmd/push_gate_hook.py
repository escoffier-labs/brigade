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
from .push_gate import ENFORCE_V1_NOTICE, _mode, _normalized_mode
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


def run(*, hook_v2: bool = False) -> int:
    started = monotonic()
    deadline = started + verify_push._BUDGET_SECONDS
    target = Path.cwd()
    try:
        configured = _mode(target)
        mode = _normalized_mode(configured)
    except Exception:
        print(
            "push gate: could not read brigade.pushGate from Git config. Run brigade work verify push-gate status.",
            file=sys.stderr,
        )
        return 2 if hook_v2 else 0
    if mode not in {"warn", "enforce"}:
        if mode not in {"", "off"}:
            print(
                f"push gate: unrecognised brigade.pushGate value {configured!r}; the gate is not running",
                file=sys.stderr,
            )
        return 0
    legacy_enforce = mode == "enforce" and not hook_v2
    enforce = mode == "enforce" and hook_v2
    try:
        updates = branch_updates(verify_push._read_stdin(deadline))
        if not updates:
            return 0
        if legacy_enforce:
            print(ENFORCE_V1_NOTICE, file=sys.stderr)
        common = _git(target, "rev-parse", "--path-format=absolute", "--git-common-dir", deadline=deadline)
        if common.returncode:
            raise OSError("could not locate the common Git directory")
    except Exception:
        print(
            "push gate: could not read Git's pre-push updates or common directory. "
            "Run brigade work verify push-gate status and check the repository.",
            file=sys.stderr,
        )
        return 2 if enforce else 0

    log = push_gate_log.log_path(Path(os.fsdecode(common.stdout.rstrip(b"\r\n"))))
    try:
        # Give the shared evaluator its full budget, as the advisory command
        # does. Git config and telemetry-path probes have a separate deadline.
        results = verify_push._check(target, updates, None, monotonic() + verify_push._BUDGET_SECONDS, details=True)
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
        if not legacy_enforce and (would_block or verdict == "indeterminate"):
            remedy = item["remedy"]
            tip = item["tip"]
            if not remedy:
                remedy = f"brigade work verify push-check --target {shlex.quote(str(target))} --ref {tip}"
            label = f"push gate: {item['ref']} ({verdict})"
            if not refused and verdict == "indeterminate":
                print(f"{label} {item['reason']} Run: {' '.join(remedy.splitlines())}", file=sys.stderr)
            else:
                print(label, file=sys.stderr)
                if refused or code == verify_push._MASKED_PASS:
                    print(item["reason"], file=sys.stderr)
                if refused and remedy.startswith("brigade work verify run "):
                    print(f"Verify commit {tip} in this worktree, then retry the push.", file=sys.stderr)
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
                "reason": "hook_too_old" if legacy_enforce else code if failed else _REASONS[verdict],
                "would_block": would_block,
                "duration_ms": duration,
                "brigade_version": __version__,
                "worktree": str(target),
            }
        )
    if mode == "enforce":
        for row in rows:
            row["blocked"] = blocked
    try:
        push_gate_log.append(log, rows)
    except OSError:
        print("push gate: could not write the observation log", file=sys.stderr)
    # Reserve 1 for an unexpected process failure, which the shell diagnoses.
    return 3 if blocked else 0
