"""Bounded read-only inspection of recorded local action/import/task lineage."""

from __future__ import annotations

import json
import stat
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import fleet_campaign as campaign
from .repos_cmd import actions_dispatch
from .repos_cmd.constants import RepoEntry
from .work_cmd import edges, helpers
from .work_cmd.ledger import import_model, locking

INSPECT_KIND = "fleet-campaign-inspect"
MAX_LEDGER_BYTES = 4 * 1024 * 1024


def parse_references(raw: object) -> list[dict[str, str]]:
    """Use storage fingerprint grammar, independently of preview bindings."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= campaign.MAX_MEMBERS:
        raise campaign.PreviewError("references_refused")
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"repo_id", "action_id", "source_fingerprint"}:
            raise campaign.PreviewError("reference_refused")
        repo_id = campaign.opaque_key(item["repo_id"])
        action_id = campaign.opaque_key(item["action_id"])
        fingerprint = import_model._safe_stable_source_fingerprint(item["source_fingerprint"])
        if fingerprint is None:
            raise campaign.PreviewError("fingerprint_required")
        if repo_id in seen:
            raise campaign.PreviewError("duplicate_repository_reference")
        seen.add(repo_id)
        result.append({"repo_id": repo_id, "action_id": action_id, "source_fingerprint": fingerprint})
    return result


def _snapshot(path: Path) -> tuple[bytes, tuple[int, int, int, int] | None]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return b"", None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LEDGER_BYTES:
        raise campaign.PreviewError("ledger_refused")
    with path.open("rb") as stream:
        raw = stream.read(MAX_LEDGER_BYTES + 1)
    if len(raw) > MAX_LEDGER_BYTES:
        raise campaign.PreviewError("ledger_refused")
    return raw, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise campaign.PreviewError("ledger_refused")
        result[key] = value
    return result


def _read_rows(
    path: Path,
    reader: Callable[[], list[dict[str, Any]]],
    field: str | None,
    snapshots: dict[Path, tuple[bytes, tuple[int, int, int, int] | None]],
) -> list[dict[str, Any]]:
    before = _snapshot(path)
    snapshots[path] = before
    if before[1] is None:
        return []
    try:
        rows: object
        if field is None:
            rows = [
                json.loads(line, object_pairs_hook=_json_object)
                for line in before[0].decode("utf-8").splitlines()
                if line.strip()
            ]
        else:
            payload = json.loads(before[0], object_pairs_hook=_json_object)
            rows = payload.get(field) if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise campaign.PreviewError("ledger_refused")
        observed = reader()
        if observed != rows or _snapshot(path) != before:
            raise campaign.PreviewError("lineage_changed")
        return observed
    except campaign.PreviewError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, RuntimeError):
        raise campaign.PreviewError("ledger_refused") from None


def _exact(rows: list[dict[str, Any]], field: str, key: str, kind: str) -> dict[str, Any]:
    matches = [row for row in rows if row.get(field) == key]
    if not matches:
        raise campaign.PreviewError(f"{kind}_missing")
    if len(matches) != 1:
        raise campaign.PreviewError(f"{kind}_duplicate")
    return matches[0]


def _resolve(
    member: dict[str, Any],
    entry: RepoEntry,
    actions: list[dict[str, Any]],
    snapshots: dict[Path, tuple[bytes, tuple[int, int, int, int] | None]],
) -> None:
    action = _exact(actions, "fleet_action_id", member["action_id"], "action")
    fingerprint = member["source_fingerprint"]
    if action.get("repo_id") != member["repo_id"] or action.get("source_fingerprint") != fingerprint:
        raise campaign.PreviewError("action_mismatch")
    if action.get("status") in {"superseded", "archived"}:
        raise campaign.PreviewError("action_superseded")
    reviewed = action.get("reviewed_at")
    try:
        if not isinstance(reviewed, str) or datetime.fromisoformat(reviewed.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except ValueError:
        raise campaign.PreviewError("action_unreviewed") from None
    dispatch = action.get("dispatch")
    if not isinstance(dispatch, dict) or dispatch.get("source_fingerprint") != fingerprint:
        raise campaign.PreviewError("dispatch_mismatch")
    import_id = campaign.opaque_key(dispatch.get("target_import_id"))
    member["import_id"] = import_id
    imports = _read_rows(
        helpers._imports_path(entry.path), lambda: import_model._read_imports(entry.path), None, snapshots
    )
    item = _exact(imports, "id", import_id, "import")
    metadata = item.get("metadata")
    if (
        item.get("source") != "repo-fleet"
        or item.get("kind") != "task"
        or not isinstance(metadata, dict)
        or metadata.get("fleet_action_id") != member["action_id"]
        or metadata.get("source_fingerprint") != fingerprint
        or metadata.get("repo_id") != member["repo_id"]
    ):
        raise campaign.PreviewError("import_mismatch")
    if item.get("status") == "superseded" or item.get("superseded_at") or item.get("superseded_by"):
        raise campaign.PreviewError("import_superseded")
    if item.get("status") != "promoted":
        raise campaign.PreviewError("import_not_promoted")
    task_id = campaign.opaque_key(item.get("task_id"))
    member["task_id"] = task_id
    tasks = _read_rows(
        helpers._tasks_path(entry.path), lambda: locking._read_task_ledger(entry.path)["tasks"], "tasks", snapshots
    )
    task = _exact(tasks, "id", task_id, "task")
    status = task.get("status")
    if not isinstance(status, str) or status not in {"pending", *edges.READY_EXCLUDED_STATUSES}:
        raise campaign.PreviewError("task_status_invalid")
    member["local_task_status"] = status


def inspect(
    *,
    target: Path,
    campaign_id: str,
    entries: Sequence[RepoEntry],
    references: list[dict[str, str]],
) -> dict[str, Any]:
    """Inspect exact references as local observations, never specification proof."""
    campaign.opaque_key(campaign_id)
    selected = {campaign.opaque_key(entry.repo_id): entry for entry in entries}
    if not 1 <= len(entries) <= campaign.MAX_MEMBERS or len(selected) != len(entries):
        raise campaign.PreviewError("membership_refused")
    if {ref["repo_id"] for ref in references} != set(selected):
        raise campaign.PreviewError("membership_reference_mismatch")
    action_path = actions_dispatch._actions_path(target)
    action_snapshots: dict[Path, tuple[bytes, tuple[int, int, int, int] | None]] = {}
    action_error = None
    actions: list[dict[str, Any]] = []
    try:
        actions = _read_rows(action_path, lambda: actions_dispatch._read_actions(target), "actions", action_snapshots)
    except campaign.PreviewError as exc:
        action_error = str(exc)
    except (OSError, ValueError, RuntimeError):
        action_error = "ledger_refused"
    members = []
    for reference in sorted(references, key=lambda ref: ref["repo_id"]):
        member: dict[str, Any] = {
            **reference,
            "import_id": None,
            "task_id": None,
            "action_binding": "unknown",
            "reason": None,
            "local_task_status": "unknown",
            "specification_verification": "unknown",
            "launch_safety": "unknown",
        }
        snapshots = dict(action_snapshots)
        try:
            if action_error:
                raise campaign.PreviewError(action_error)
            _resolve(member, selected[reference["repo_id"]], actions, snapshots)
            if any(_snapshot(path) != before for path, before in snapshots.items()):
                raise campaign.PreviewError("lineage_changed")
            member["action_binding"] = "matched"
        except campaign.PreviewError as exc:
            member["reason"] = str(exc)
            member["local_task_status"] = "unknown"
        except (OSError, ValueError, TypeError, IndexError, RecursionError, RuntimeError):
            member["reason"] = "ledger_refused"
            member["local_task_status"] = "unknown"
        members.append(member)
    matched = sum(member["action_binding"] == "matched" for member in members)
    return {
        "kind": INSPECT_KIND,
        "read_only": True,
        "campaign_id": campaign_id,
        "members": members,
        "summary": {"inspected": len(members), "matched": matched, "unknown": len(members) - matched, "done": False},
    }
