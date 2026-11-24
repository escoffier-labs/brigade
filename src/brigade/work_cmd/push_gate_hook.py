"""Run the advisory part of the pre-push hook and record its observations."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic
from typing import Any

from .. import __version__
from . import push_gate_log
from .verify_push_input import INPUT_BYTES, branch_updates

_CHECK_TIMEOUT = 2.25
_OUTPUT_LIMIT = 4 * 1024 * 1024
_WOULD_BLOCK = {"missing_receipt", "rejected"}
_REASONS = {
    "ok": "receipt_eligible",
    "missing_receipt": "receipt_missing",
    "rejected": "receipt_ineligible",
    "not_applicable": "no_required_check",
    "base_predates_policy": "base_predates_required_check",
    "indeterminate": "verdict_unavailable",
}


def _check(data: bytes, updates: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], str | None]:
    # Use a real file for stdout so a descendant holding the descriptor open
    # cannot extend the timeout. Never persist child output or stderr.
    try:
        with TemporaryFile() as output:
            child = subprocess.run(
                ["brigade", "work", "verify", "push-check", "--json"],
                input=data,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "BRIGADE_NO_UPDATE_CHECK": "1"},
                timeout=_CHECK_TIMEOUT,
            )
            if child.returncode not in (0, 1, 3):
                return [], "check_failed"
            output.seek(0)
            raw = output.read(_OUTPUT_LIMIT + 1)
        if len(raw) > _OUTPUT_LIMIT:
            return [], "invalid_output"
        results = json.loads(raw)["refs"]
        if not isinstance(results, list) or len(results) != len(updates):
            return [], "invalid_output"
        for item, (ref, _) in zip(results, updates, strict=True):
            if not isinstance(item, dict) or item["ref"] != ref or item["status"] not in _REASONS:
                return [], "invalid_output"
            if item["status"] in _WOULD_BLOCK and not isinstance(item.get("remedy"), str):
                return [], "invalid_output"
        return results, None
    except subprocess.TimeoutExpired:
        return [], "check_timeout"
    except FileNotFoundError:
        return [], "brigade_missing"
    except OSError:
        return [], "check_failed"
    except (ValueError, TypeError, KeyError, RecursionError):
        return [], "invalid_output"


def _trees(updates: list[tuple[str, str]]) -> dict[str, str]:
    commits = list(dict.fromkeys(commit for _, commit in updates))
    try:
        completed = subprocess.run(
            ["git", "--no-replace-objects", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input="".join(f"{commit}^{{commit}}^{{tree}}\n" for commit in commits),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""},
            timeout=0.1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    trees = {}
    for commit, line in zip(commits, completed.stdout.splitlines(), strict=False):
        tree, _, kind = line.partition(" ")
        if kind == "tree" and len(tree) == len(commit) and all(c in "0123456789abcdef" for c in tree):
            trees[commit] = tree
    return trees


def run(common: Path, data: bytes) -> None:
    started = monotonic()
    time = datetime.now(UTC).isoformat()
    try:
        updates = branch_updates(os.fsdecode(data))
    except ValueError:
        print("push gate: could not parse Git's pre-push updates", file=sys.stderr)
        return
    if not updates:
        return

    results, error = _check(data, updates)
    duration = round((monotonic() - started) * 1000)
    trees = _trees(updates)
    rows = []
    for index, (ref, commit) in enumerate(updates):
        item = results[index] if results else {}
        status = item.get("status", "gate_error")
        reason = error or _REASONS[status]
        if status == "indeterminate" and any(
            phrase in str(item.get("reason", "")).lower() for phrase in ("time limit", "timed out", "deadline")
        ):
            status, reason = "gate_error", "check_timeout"
        would_block = status in _WOULD_BLOCK
        if would_block:
            print(f"push gate: {ref} ({status})\n{item['remedy']}", file=sys.stderr)
        rows.append(
            {
                "schema": push_gate_log.SCHEMA,
                "time": time,
                "mode": "warn",
                "ref": ref,
                "tip": commit,
                "tree": trees.get(commit),
                "result": status,
                "reason": reason,
                "would_block": would_block,
                "duration_ms": duration,
                "brigade_version": __version__,
                "worktree": str(Path.cwd()),
            }
        )
    try:
        push_gate_log.append(push_gate_log.log_path(common), rows)
    except OSError:
        print("push gate: could not write the observation log", file=sys.stderr)


if __name__ == "__main__":
    if hasattr(signal, "setitimer"):
        signal.setitimer(signal.ITIMER_REAL, 2.65)
    run(Path(sys.argv[1]), sys.stdin.buffer.read(INPUT_BYTES + 1))
