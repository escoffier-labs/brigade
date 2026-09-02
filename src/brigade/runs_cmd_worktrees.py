"""Worktree helpers for the `brigade runs` command family."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import runguard

_PRUNE_WORKTREES_SCHEMA = "brigade.runs-prune-worktrees.v1"


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _classify_worktree_for_removal(
    path: Path,
    repo_root: Path,
    threshold: datetime,
    older_than_days: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    clean = False
    try:
        clean = runguard.worktree_is_clean(path)
    except runguard.RunGuardError as exc:
        reasons.append(f"unreadable worktree state: {exc}")
    if not clean and not reasons:
        reasons.append("dirty")

    branch_backed = False
    try:
        branch_backed = runguard.worktree_head_is_branch_backed(path)
    except runguard.RunGuardError as exc:
        reasons.append(f"unreadable HEAD state: {exc}")
    if not branch_backed and not any(r.startswith("unreadable HEAD state") for r in reasons):
        reasons.append("detached HEAD with unreachable commits")

    try:
        mtime = runguard.worktree_mtime(path)
        age_seconds = (datetime.now(timezone.utc) - mtime).total_seconds()
        age_days = age_seconds / 86400
        old_enough = age_seconds >= 0 and mtime <= threshold
    except OSError as exc:
        age_days = 0.0
        old_enough = False
        reasons.append(f"unreadable directory age: {exc}")
    if not old_enough and not any(r.startswith("unreadable directory age") for r in reasons):
        reasons.append(f"younger than {older_than_days} days")

    return {
        "path": str(path),
        "removable": clean and branch_backed and old_enough,
        "reasons": reasons,
        "age_days": round(age_days, 2),
        "clean": clean,
        "branch_backed": branch_backed,
    }


def prune_worktrees(
    cwd: Path,
    *,
    older_than_days: int = 14,
    apply: bool = False,
    json_output: bool = False,
) -> int:
    """List or remove Brigade-created worktrees that are safe to delete.

    A worktree is removable only when it is clean, its HEAD is reachable from
    a branch, and it is older than the threshold. Non-Brigade worktrees are
    never touched.
    """
    target = cwd.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        repo_root = runguard.git_root(target)
    except runguard.RunGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    root = runguard.brigade_worktree_root()
    if not root.is_dir():
        if json_output:
            _emit_json(
                {
                    "schema": _PRUNE_WORKTREES_SCHEMA,
                    "target": str(repo_root),
                    "older_than_days": older_than_days,
                    "apply": apply,
                    "removed": [],
                    "worktrees": [],
                }
            )
        else:
            print(f"no Brigade worktrees found under {root}")
        return 0

    threshold = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    entries: list[dict[str, Any]] = []
    removed: list[Path] = []
    for path in runguard.brigade_worktrees_for_repo(repo_root):
        classification = _classify_worktree_for_removal(path, repo_root, threshold, older_than_days)
        if classification["removable"]:
            if apply:
                try:
                    runguard.remove_worktree(repo_root, path, force=False)
                    removed.append(path)
                except (runguard.RunGuardError, OSError) as exc:
                    classification["removable"] = False
                    classification["reasons"].append(f"removal failed: {exc}")
        entries.append(classification)

    if json_output:
        _emit_json(
            {
                "schema": _PRUNE_WORKTREES_SCHEMA,
                "target": str(repo_root),
                "older_than_days": older_than_days,
                "apply": apply,
                "removed": [str(p) for p in removed],
                "worktrees": entries,
            }
        )
        return 0

    if not entries:
        print(f"no Brigade worktrees found for {repo_root}")
        return 0

    for item in entries:
        path_str = str(item["path"])
        age_days = float(item["age_days"])
        if item["removable"]:
            if apply:
                print(f"removed: {path_str} ({age_days:g} days old)")
            else:
                print(f"removable: {path_str} ({age_days:g} days old)")
        else:
            print(f"kept: {path_str} ({', '.join(item['reasons'])})")
    return 0
