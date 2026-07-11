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

OK = "ok"

WARN = "warn"

FAIL = "fail"

RELEASE_CANDIDATE_STALE_HOURS = 168

PHASE_REPORT_STALE_HOURS = 24

RELEASE_PRIVATE_VALUE_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|token|password|passwd|pwd)[A-Za-z0-9_]*)\b\s*[:=]\s*['\"]?([A-Za-z0-9_./+=:-]{8,})"
)

RELEASE_PRIVATE_PATH_RE = re.compile(r"(?<!`)/(?:home|Users|private|mnt|Volumes)/[^\s`)]+")

SCHEMA_MANIFEST_VERSION = 1

INSTALL_SMOKE_STALE_HOURS = 168

INSTALL_SMOKE_STATUSES = {"passed", "failed", "skipped", "blocked"}

INSTALL_SMOKE_MATRIX = (
    {"matrix_id": "repo-none", "depth": "repo", "harnesses": []},
    {"matrix_id": "repo-claude", "depth": "repo", "harnesses": ["claude"]},
    {"matrix_id": "repo-codex", "depth": "repo", "harnesses": ["codex"]},
    {"matrix_id": "workspace-claude", "depth": "workspace", "harnesses": ["claude"]},
    {"matrix_id": "workspace-claude-openclaw", "depth": "workspace", "harnesses": ["claude", "openclaw"]},
    {"matrix_id": "workspace-codex-openclaw", "depth": "workspace", "harnesses": ["codex", "openclaw"]},
    {
        "matrix_id": "workspace-claude-codex-openclaw",
        "depth": "workspace",
        "harnesses": ["claude", "codex", "openclaw"],
    },
)

CI_DEPRECATION_PATTERNS = (
    re.compile(r"(?i)\bnode(?:\.js)?\s*(12|16)\b.*\b(deprecat|unsupported|retired)"),
    re.compile(r"(?i)\b(deprecat|unsupported|retired)\b.*\bnode(?:\.js)?\s*(12|16)\b"),
    re.compile(r"(?i)\bgithub actions?\b.*\b(deprecat|unsupported|retired)\b"),
)

CI_DEPRECATED_ACTION_MAJORS = {
    "actions/cache": {"1", "2", "3"},
    "actions/checkout": {"1", "2", "3"},
    "actions/download-artifact": {"1", "2", "3"},
    "actions/github-script": {"1", "2", "3", "4", "5", "6"},
    "actions/setup-go": {"1", "2", "3", "4"},
    "actions/setup-java": {"1", "2", "3"},
    "actions/setup-node": {"1", "2", "3"},
    "actions/setup-python": {"1", "2", "3", "4"},
    "actions/upload-artifact": {"1", "2", "3"},
}

CI_ACTION_REF_RE = re.compile(r"uses:\s*['\"]?([^@\s'\"]+)@([^@\s'\"]+)")


def _field(name: str, field_type: str, detail: str) -> dict[str, str]:
    return {"name": name, "type": field_type, "detail": detail}


def _release_root(target: Path) -> Path:
    return target / ".brigade" / "release"


def _release_runs_root(target: Path) -> Path:
    return _release_root(target) / "runs"


def _release_candidates_root(target: Path) -> Path:
    return _release_root(target) / "candidates"


def _release_candidates_archive_root(target: Path) -> Path:
    return _release_candidates_root(target) / "archive"


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(target), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _git_value(target: Path, *args: str) -> str | None:
    result = _git(target, *args)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _git_state(target: Path) -> dict[str, Any]:
    snapshot = work_cmd._git_snapshot(target)
    status_result = _git(target, "status", "--porcelain=v1")
    status = status_result.stdout if status_result.returncode == 0 else ""
    tracked_dirty = [line for line in status.splitlines() if line and not line.startswith("??")]
    upstream = _git_value(target, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ahead = behind = None
    if upstream:
        counts = _git_value(target, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts:
            parts = counts.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
    snapshot.update(
        {
            "head": _git_value(target, "rev-parse", "HEAD"),
            "short_head": _git_value(target, "rev-parse", "--short", "HEAD"),
            "tracked_dirty_files": tracked_dirty,
            "tracked_dirty_count": len(tracked_dirty),
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
        }
    )
    return snapshot


def _latest_work_closeout(target: Path) -> dict[str, Any] | None:
    root = target / ".brigade" / "work" / "closeouts"
    if not root.is_dir():
        return None
    closeouts: list[dict[str, Any]] = []
    for child in root.iterdir():
        payload = _read_json(child / "closeout.json") if child.is_dir() else None
        if payload is not None:
            payload.setdefault("path", str(child / "closeout.json"))
            closeouts.append(payload)
    closeouts.sort(key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)
    return closeouts[0] if closeouts else None


def _latest_review_closeout(target: Path) -> dict[str, Any] | None:
    for receipt in work_cmd._review_receipts(target):
        closeout = receipt.get("closeout")
        if isinstance(closeout, dict):
            return {"run_id": receipt.get("run_id"), **closeout}
    return None


def _latest_closeout_json(root: Path) -> dict[str, Any] | None:
    if not root.is_dir():
        return None
    candidates = sorted(root.glob("*/closeout.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        payload = _read_json(path)
        if payload is not None:
            payload.setdefault("path", str(path))
            return payload
    return None


def _security_summary(target: Path) -> dict[str, Any]:
    health = security_cmd.health(target)
    return {
        "valid": health.get("valid"),
        "issue_count": health.get("issue_count"),
        "top_issue": health.get("top_issue"),
        "top_finding": health.get("top_finding"),
        "evidence": health.get("evidence"),
        "template_privacy": health.get("template_privacy"),
        "latest_closeout": health.get("latest_closeout"),
    }


def _changed_files(target: Path, base_ref: str | None) -> list[str]:
    files: set[str] = set()
    status_result = _git(target, "status", "--porcelain=v1")
    status = status_result.stdout if status_result.returncode == 0 else ""
    for line in status.splitlines():
        if not line:
            continue
        files.add(line[3:] if len(line) > 3 else line)
    if base_ref:
        result = _git(target, "diff", "--name-only", f"{base_ref}...HEAD")
        if result.returncode == 0:
            files.update(line for line in result.stdout.splitlines() if line.strip())
    return sorted(files)


def _docs_warnings(target: Path, base_ref: str | None) -> list[str]:
    changed = _changed_files(target, base_ref)
    user_facing = [
        path
        for path in changed
        if path.startswith("src/brigade/") and not path.startswith("src/brigade/templates/") and path.endswith(".py")
    ]
    if not user_facing:
        return []
    warnings: list[str] = []
    for required in ("README.md", "CHANGELOG.md", "ROADMAP.md"):
        if required not in changed:
            warnings.append(f"user-facing changes detected but {required} was not changed")
    return warnings
