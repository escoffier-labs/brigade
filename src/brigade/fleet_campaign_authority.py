"""Bounded read-only inspection of recorded local action/import/task lineage."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import dirfd, fleet_campaign as campaign
from .repos_cmd import actions_dispatch
from .repos_cmd.constants import ACTION_STATUSES, RepoEntry
from .work_cmd import edges, helpers
from .work_cmd.ledger import import_model

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


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode


def _generation(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _bounded_read(descriptor: int, before: os.stat_result) -> bytes:
    if not _regular(before) or before.st_size > MAX_LEDGER_BYTES:
        raise campaign.PreviewError("ledger_refused")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    total = 0
    while total <= MAX_LEDGER_BYTES:
        chunk = os.read(descriptor, min(64 * 1024, MAX_LEDGER_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        chunks.append(chunk)
    after = os.fstat(descriptor)
    if not _regular(after) or _generation(before) != _generation(after) or total != before.st_size:
        raise campaign.PreviewError("lineage_changed")
    if total > MAX_LEDGER_BYTES:
        raise campaign.PreviewError("ledger_refused")
    return b"".join(chunks)


@dataclass
class _Snapshot:
    root: Path
    root_descriptor: int
    directories: list[tuple[int, str, int]]
    parent: int
    name: str
    descriptor: int | None = None
    info: os.stat_result | None = None
    raw: bytes = b""

    def check(self) -> None:
        """Check retained bytes and their names, including missing children."""
        try:
            current_root = dirfd.open_directory_nofollow(self.root, writable=False)
            try:
                if _identity(os.fstat(current_root)) != _identity(os.fstat(self.root_descriptor)):
                    raise campaign.PreviewError("lineage_changed")
            finally:
                os.close(current_root)
            for parent, name, child in self.directories:
                if _identity(dirfd.stat_child(parent, name)) != _identity(os.fstat(child)):
                    raise campaign.PreviewError("lineage_changed")
            if self.descriptor is None:
                try:
                    dirfd.stat_child(self.parent, self.name)
                except FileNotFoundError:
                    return
                raise campaign.PreviewError("lineage_changed")
            assert self.info is not None
            current = os.fstat(self.descriptor)
            if (
                _generation(current) != _generation(self.info)
                or _identity(dirfd.stat_child(self.parent, self.name)) != _identity(self.info)
                or _bounded_read(self.descriptor, current) != self.raw
            ):
                raise campaign.PreviewError("lineage_changed")
        except OSError:
            raise campaign.PreviewError("lineage_changed") from None


def _snapshot(root: Path, path: Path, stack: ExitStack) -> _Snapshot:
    root_descriptor = dirfd.open_directory_nofollow(root, writable=False)
    stack.callback(os.close, root_descriptor)
    parts = path.relative_to(root).parts
    parent = root_descriptor
    directories: list[tuple[int, str, int]] = []
    for name in parts[:-1]:
        try:
            child = dirfd.open_child_directory(parent, name, writable=False)
        except FileNotFoundError:
            return _Snapshot(root, root_descriptor, directories, parent, name)
        stack.callback(os.close, child)
        directories.append((parent, name, child))
        parent = child
    snapshot = _Snapshot(root, root_descriptor, directories, parent, parts[-1])
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        snapshot.descriptor = dirfd.open_child_file(parent, snapshot.name, flags)
    except FileNotFoundError:
        return snapshot
    stack.callback(os.close, snapshot.descriptor)
    snapshot.info = os.fstat(snapshot.descriptor)
    snapshot.raw = _bounded_read(snapshot.descriptor, snapshot.info)
    snapshot.check()
    return snapshot


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise campaign.PreviewError("ledger_refused")
        result[key] = value
    return result


def _read_rows(
    root: Path,
    path: Path,
    field: str | None,
    snapshots: dict[Path, _Snapshot],
    stack: ExitStack,
) -> list[dict[str, Any]]:
    snapshot = _snapshot(root, path, stack)
    snapshots[path] = snapshot
    if snapshot.descriptor is None:
        return []
    try:
        rows: object
        if field is None:
            # The inspection contract refuses malformed/duplicate-key JSONL;
            # the canonical parser supplies the rows from those same bytes.
            strict_rows = [
                json.loads(line, object_pairs_hook=_json_object)
                for line in snapshot.raw.decode("utf-8").splitlines()
                if line.strip()
            ]
            rows = import_model._parse_import_inbox_bytes(snapshot.raw)
            if rows != strict_rows:
                raise campaign.PreviewError("ledger_refused")
        else:
            payload = json.loads(snapshot.raw.decode("utf-8"), object_pairs_hook=_json_object)
            rows = payload.get(field) if isinstance(payload, dict) else None
            if field == "tasks" and isinstance(payload, dict):
                version = payload.get("version", 1)
                if (
                    isinstance(version, bool)
                    or not isinstance(version, int)
                    or not 1 <= version <= edges.TASK_LEDGER_VERSION
                ):
                    raise campaign.PreviewError("ledger_refused")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise campaign.PreviewError("ledger_refused")
        snapshot.check()
        return rows
    except (campaign.PreviewError, OSError, ValueError, TypeError, RecursionError, RuntimeError) as exc:
        snapshot.check()
        if isinstance(exc, campaign.PreviewError):
            raise
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
    snapshots: dict[Path, _Snapshot],
    stack: ExitStack,
) -> None:
    action = _exact(actions, "fleet_action_id", member["action_id"], "action")
    fingerprint = member["source_fingerprint"]
    if action.get("repo_id") != member["repo_id"] or action.get("source_fingerprint") != fingerprint:
        raise campaign.PreviewError("action_mismatch")
    action_status = action.get("status")
    if isinstance(action_status, str) and action_status in {"superseded", "archived"}:
        raise campaign.PreviewError("action_superseded")
    if not isinstance(action_status, str) or action_status not in ACTION_STATUSES:
        raise campaign.PreviewError("action_status_invalid")
    reviewed = action.get("reviewed_at")
    try:
        if not isinstance(reviewed, str) or datetime.fromisoformat(reviewed.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except ValueError:
        raise campaign.PreviewError("action_unreviewed") from None
    dispatch = action.get("dispatch")
    if not isinstance(dispatch, dict) or dispatch.get("source_fingerprint") != fingerprint:
        raise campaign.PreviewError("dispatch_mismatch")
    for record, field, known in (
        (action, "resolution_status", {"dispatched", "in-progress", "completed"}),
        (dispatch, "status", {"dispatched"}),
    ):
        if field in record and (not isinstance(record[field], str) or record[field] not in known):
            raise campaign.PreviewError("lineage_mismatch")
    import_id = campaign.opaque_key(dispatch.get("target_import_id"))
    if "target_import_id" in action and action["target_import_id"] != import_id:
        raise campaign.PreviewError("lineage_mismatch")
    imports = _read_rows(entry.path, helpers._imports_path(entry.path), None, snapshots, stack)
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
    if item.get("status") == "dismissed":
        raise campaign.PreviewError("import_dismissed")
    if item.get("status") == "pending":
        raise campaign.PreviewError("import_not_promoted")
    if item.get("status") != "promoted":
        raise campaign.PreviewError("import_status_invalid")
    task_id = campaign.opaque_key(item.get("task_id"))
    for record in (action, dispatch):
        if "target_task_id" in record and record["target_task_id"] != task_id:
            raise campaign.PreviewError("lineage_mismatch")
    tasks = _read_rows(entry.path, helpers._tasks_path(entry.path), "tasks", snapshots, stack)
    task = _exact(tasks, "id", task_id, "task")
    task_metadata = task.get("metadata")
    expected = {
        "import_id": import_id,
        "import_kind": "task",
        "import_source": "repo-fleet",
        "fleet_action_id": member["action_id"],
        "repo_id": member["repo_id"],
        "source_fingerprint": fingerprint,
    }
    if (
        task.get("source") != "import:repo-fleet"
        or not isinstance(task_metadata, dict)
        or any(task_metadata.get(key) != value for key, value in expected.items())
    ):
        raise campaign.PreviewError("task_mismatch")
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
    with ExitStack() as action_stack:
        action_path = actions_dispatch._actions_path(target)
        action_snapshots: dict[Path, _Snapshot] = {}
        action_error = None
        actions: list[dict[str, Any]] = []
        try:
            actions = _read_rows(target, action_path, "actions", action_snapshots, action_stack)
        except campaign.PreviewError as exc:
            action_error = str(exc)
        except (OSError, ValueError, RuntimeError):
            action_error = "ledger_refused"
        members = []
        for reference in sorted(references, key=lambda ref: ref["repo_id"]):
            member: dict[str, Any] = {
                **reference,
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
                with ExitStack() as member_stack:
                    try:
                        _resolve(member, selected[reference["repo_id"]], actions, snapshots, member_stack)
                    finally:
                        for snapshot in snapshots.values():
                            snapshot.check()
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
