"""Issue helpers for the tools command family."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..localio import stable_hash as _stable_hash
from . import constants, helpers, paths


def _projection_issue(tool: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "projection")
    harness = str(item.get("harness") or "")
    detail = str(item.get("detail") or "")
    issue = _tool_issue(
        tool,
        f"{status}_projection" if status not in {"missing"} else "missing_projection",
        f"{harness}: {detail}",
        harness=harness,
        target=str(item.get("projection_path") or ""),
    )
    issue.update(
        {
            "projection_status": status,
            "tool_source_fingerprint": item.get("source_fingerprint"),
            "expected_projection_fingerprint": item.get("expected_projection_fingerprint")
            or item.get("expected_fingerprint"),
            "actual_projection_fingerprint": item.get("actual_projection_fingerprint"),
        }
    )
    return issue


def _tool_issue(
    tool: dict[str, Any], issue_type: str, detail: str, *, harness: str | None = None, target: str | None = None
) -> dict[str, Any]:
    return {
        "status": constants.WARN,
        "name": f"tool_{issue_type}",
        "tool_id": tool.get("id"),
        "family": tool.get("family"),
        "issue_type": issue_type,
        "harness": harness,
        "projection_target": target,
        "description": tool.get("description"),
        "detail": detail,
    }


def _is_parity_issue(issue: dict[str, Any]) -> bool:
    return str(issue.get("issue_type") or issue.get("name") or "") in constants.PARITY_ISSUE_TYPES


def _parity_issue_fingerprint(issue: dict[str, Any]) -> str:
    return _stable_hash(
        {
            "tool_id": issue.get("tool_id"),
            "family": issue.get("family"),
            "issue_type": issue.get("issue_type") or issue.get("name"),
            "harness": issue.get("harness"),
            "projection_target": issue.get("projection_target"),
            "projection_status": issue.get("projection_status"),
            "tool_source_fingerprint": issue.get("tool_source_fingerprint"),
            "expected_projection_fingerprint": issue.get("expected_projection_fingerprint"),
            "actual_projection_fingerprint": issue.get("actual_projection_fingerprint"),
            "detail": issue.get("detail"),
        }
    )


def _latest_parity_closeout(target: Path) -> dict[str, Any] | None:
    root = paths.parity_closeouts_path(target)
    if not root.is_dir():
        return None
    closeouts: list[dict[str, Any]] = []
    for path in root.glob("*/closeout.json"):
        payload, error = helpers._read_json(path)
        if error is None and isinstance(payload, dict):
            payload.setdefault("path", str(path))
            closeouts.append(payload)
    closeouts.sort(key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)
    return closeouts[0] if closeouts else None


def _apply_parity_closeout(target: Path, issues: list[dict[str, Any]]) -> dict[str, Any]:
    closeout = _latest_parity_closeout(target)
    closed = set(closeout.get("source_fingerprints", [])) if isinstance(closeout, dict) else set()
    active: list[dict[str, Any]] = []
    quieted: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for issue in issues:
        if not _is_parity_issue(issue):
            active.append(issue)
            continue
        fingerprint = _parity_issue_fingerprint(issue)
        issue["parity_fingerprint"] = fingerprint
        if fingerprint in closed:
            quieted.append(issue)
        else:
            active.append(issue)
            if closed:
                changed.append(issue)
    return {
        "issues": active,
        "quieted_issues": quieted,
        "changed_issues": changed,
        "latest_closeout": closeout,
    }
