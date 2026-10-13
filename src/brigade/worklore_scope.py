"""Bounded path admission for checkpoint declarations, under the caller's write fence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from . import worklore_store as store
from .worklore_validate import WorkloreValidationError

MAX_SCOPE_SNAPSHOTS = 1024
MAX_SCOPE_CONFLICTS = 8
# Escaped JSON bytes for the list, leaving room under 8 KiB for the fixed envelope.
MAX_SCOPE_CONFLICT_BYTES = 7168
_RESOLUTIONS = {
    "path-scope-conflict": "replace-scope-or-release-holder",
    "path-scope-capacity-exhausted": "reduce-snapshot-set-or-retry-after-scope-review",
    "path-scope-unavailable": "review-stored-scope-declarations",
}
_MESSAGES = {
    "path-scope-conflict": "checkpoint paths overlap a reserved declaration. Ownership unchanged.",
    "path-scope-capacity-exhausted": "checkpoint admission exceeds snapshot scan bound. Ownership unchanged.",
    "path-scope-unavailable": "stored scope cannot be safely assessed. Ownership unchanged.",
}


class ScopeAdmissionError(store.WorkloreConflict):
    """A refusal containing only validated identities/paths and fixed resolution codes."""

    def __init__(self, code: str, *, conflicts: list[dict[str, str]] | None = None, truncated: bool = False) -> None:
        super().__init__(_MESSAGES[code], code=code)
        self.details: dict[str, Any] = {"resolution": _RESOLUTIONS[code]}
        if conflicts is not None:
            self.details.update(conflicts=conflicts, truncated=truncated)


def admit_checkpoint(conn: sqlite3.Connection, work_id: str, body: Mapping[str, Any]) -> None:
    """Check latest declarations without presence/status inference or modifying holders.

    The ownership caller holds BEGIN IMMEDIATE and has already fenced authority,
    CAS, replay and event capacity. LIMIT bounds snapshot processing, not SQLite's
    underlying index traversal. Empty scopes have no writer to admit.
    """
    # Reuse the ownership contract without a module-initialization import cycle.
    from . import worklore_ownership as ownership

    requested = body["write_scope"]
    if not requested:
        return
    rows = conn.execute(
        "SELECT e.work_id, substr(CAST(e.detail_json AS BLOB), 1, ?) FROM work_events e "
        "JOIN (SELECT work_id, MAX(seq) AS seq FROM work_events WHERE work_id != ? AND "
        + ownership._EVENT_PREDICATE
        + " GROUP BY work_id ORDER BY work_id LIMIT ?) latest ON e.work_id=latest.work_id AND e.seq=latest.seq ORDER BY e.work_id",
        (ownership.OWNERSHIP_EVENT_MAX_BYTES + 1, work_id, *ownership.EVENT_TYPES.values(), MAX_SCOPE_SNAPSHOTS + 1),
    )
    conflicts: list[dict[str, str]] = []
    truncated = False
    conflicted = False
    conflict_bytes = 2  # JSON list brackets
    for index, (identifier, raw) in enumerate(rows):
        if index == MAX_SCOPE_SNAPSHOTS:
            raise ScopeAdmissionError("path-scope-capacity-exhausted")
        try:
            if len(raw) > ownership.OWNERSHIP_EVENT_MAX_BYTES:
                raise ValueError("oversized snapshot")
            snapshot = json.loads(raw)["ownership"]
            if snapshot["state"] in {"unowned", "offered"}:
                continue
            if snapshot["state"] not in {"owned", "handoff-pending"}:
                raise ValueError("unknown reservation state")
            paths = [
                ownership._path(path) for path in ownership._array(snapshot["write_scope"], ownership.SCOPE_MAX_ITEMS)
            ]
            if not paths:
                continue
            repo = ownership._path(snapshot["repo_identity"], "repo_identity", ownership.REPO_IDENTITY_MAX)
            if not ownership._REPO_RE.fullmatch(repo):
                raise ValueError("invalid repository")
            if repo.casefold() != body["repo_identity"].casefold():
                continue
            identifier = ownership._reference(identifier)
            owner = ownership._node(snapshot["owner_node"])
            for path in requested:
                for held in paths:
                    if path != held and not path.startswith(held + "/") and not held.startswith(path + "/"):
                        continue
                    # Imported snapshots must not reflect even a digest through
                    # an otherwise syntactically valid identity or path.
                    digest = snapshot.get("holder_hash")
                    if (
                        isinstance(digest, str)
                        and digest
                        and any(digest in text for text in (identifier, owner, path, held))
                    ):
                        raise ValueError("private reservation metadata")
                    conflicted = True
                    record = {
                        "work_id": identifier,
                        "owner_node": owner,
                        "requested_path": path,
                        "held_path": held,
                        "resolution": _RESOLUTIONS["path-scope-conflict"],
                    }
                    record_bytes = len(json.dumps(record).encode("utf-8")) + (2 if conflicts else 0)
                    if (
                        len(conflicts) == MAX_SCOPE_CONFLICTS
                        or conflict_bytes + record_bytes > MAX_SCOPE_CONFLICT_BYTES
                    ):
                        truncated = True
                        continue
                    conflicts.append(record)
                    conflict_bytes += record_bytes
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, WorkloreValidationError):
            raise ScopeAdmissionError("path-scope-unavailable") from None
    if conflicted:
        raise ScopeAdmissionError("path-scope-conflict", conflicts=conflicts, truncated=truncated)
