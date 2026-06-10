"""Shared primitives for the single-file action-queue lifecycle.

These helpers were extracted from near-identical private copies behind
`center actions` (center_cmd), `repos actions` (repos_cmd), and
`repos release actions` (repos_cmd). Each station keeps its own paths,
payload builders, write envelope, and output text; this module owns the
store read, id-prefix lookup, status stamping, fingerprint-deduped merge,
and the archive split/append.

The phase-ledger queue in phases_cmd stays local: it stores one JSON file
per action and stamps `reviewed_at`/`review_reason` instead of the
`started_at`/`completed_at`/`deferred_at` lifecycle fields below.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .localio import read_json_dict


def read_actions(path: Path) -> list[dict[str, Any]]:
    """Read the "actions" list from a single-file store; [] when missing or invalid."""
    payload = read_json_dict(path)
    actions = payload.get("actions") if isinstance(payload, dict) else None
    return [item for item in actions if isinstance(item, dict)] if isinstance(actions, list) else []


def find_action(
    actions: list[dict[str, Any]],
    action_id: str,
    *,
    id_field: str,
    label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Find the unique action whose id_field value starts with action_id.

    Returns (action, None) on a unique match, otherwise (None, error) where the
    error is a "{label} not found" or "{label} id is ambiguous" message.
    """
    matches = [action for action in actions if str(action.get(id_field) or "").startswith(action_id)]
    if not matches:
        return None, f"{label} not found: {action_id}"
    if len(matches) > 1:
        return None, f"{label} id is ambiguous: {action_id}"
    return matches[0], None


def stamp_status(action: dict[str, Any], status: str, *, now: str, reason: str | None = None) -> None:
    """Apply a lifecycle status transition in place with the shared timestamp fields."""
    action["status"] = status
    action["updated_at"] = now
    if status == "active":
        action["started_at"] = now
    elif status == "done":
        action["completed_at"] = now
    elif status == "deferred":
        action["deferred_at"] = now
        action["defer_reason"] = reason or "deferred"


def merge_planned(
    existing: list[dict[str, Any]],
    archived: list[dict[str, Any]],
    planned: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Append planned actions with new source_fingerprint values to existing in place.

    Fingerprints already present in existing or archived are skipped.
    Returns (created, skipped).
    """
    fingerprints = {str(action.get("source_fingerprint")) for action in existing}
    fingerprints.update(str(action.get("source_fingerprint")) for action in archived)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for action in planned:
        if str(action.get("source_fingerprint")) in fingerprints:
            skipped.append(action)
            continue
        created.append(action)
        existing.append(action)
        fingerprints.add(str(action.get("source_fingerprint")))
    return created, skipped


def split_archived_completed(
    actions: list[dict[str, Any]],
    *,
    now: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split actions into (archived copies of done actions, remaining actions).

    Done actions are copied with status "archived" and archived_at/updated_at
    set to now; all other actions pass through unchanged.
    """
    archived: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for action in actions:
        if action.get("status") == "done":
            archived_action = dict(action)
            archived_action["status"] = "archived"
            archived_action["archived_at"] = now
            archived_action["updated_at"] = now
            archived.append(archived_action)
        else:
            remaining.append(action)
    return archived, remaining


def append_archive(path: Path, actions: list[dict[str, Any]]) -> None:
    """Append actions to a JSONL archive, creating parent directories; no-op when empty."""
    if not actions:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for action in actions:
            handle.write(json.dumps(action, sort_keys=True) + "\n")
