"""Bounded path admission for checkpoint declarations, under the caller's write fence."""

from __future__ import annotations

import base64
import hashlib
import json
import re
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
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
# Lookahead also finds capabilities embedded inside longer identifiers/segments.
_NONCE_CANDIDATES = re.compile(r"(?=([A-Za-z0-9_+/-]{43}))")
_HEX_CANDIDATES = re.compile(r"(?=([0-9A-Fa-f]{64}))")


class ScopeAdmissionError(store.WorkloreConflict):
    """A refusal containing only validated identities/paths and fixed resolution codes."""

    def __init__(self, code: str, *, conflicts: list[dict[str, str]] | None = None, truncated: bool = False) -> None:
        super().__init__(_MESSAGES[code], code=code)
        self.details: dict[str, Any] = {"resolution": _RESOLUTIONS[code]}
        if conflicts is not None:
            self.details.update(conflicts=conflicts, truncated=truncated)


def _reject_reflection(digests: set[str], texts: list[str]) -> None:
    """Scan bounded output once against every active holder's private fingerprints."""
    for text in set(texts):
        for match in _NONCE_CANDIDATES.finditer(text):
            candidate = match[1]
            decoded = base64.b64decode(candidate + "=", altchars=b"-_", validate=True)
            if candidate not in {
                base64.urlsafe_b64encode(decoded).decode().rstrip("="),
                base64.b64encode(decoded).decode().rstrip("="),
            }:
                continue
            if len(decoded) == 32 and hashlib.sha256(decoded).hexdigest() in digests:
                raise ValueError("private reservation metadata")
        for match in _HEX_CANDIDATES.finditer(text):
            if match[1].lower() in digests or hashlib.sha256(bytes.fromhex(match[1])).hexdigest() in digests:
                raise ValueError("private reservation metadata")


def admit_checkpoint(
    conn: sqlite3.Connection, work_id: str, body: Mapping[str, Any], *, current_holder_digest: str
) -> None:
    """Check latest declarations without presence/status inference or modifying holders.

    The ownership caller holds BEGIN IMMEDIATE and has already fenced authority,
    CAS, replay, event capacity and serialized byte size. LIMIT bounds snapshot processing, not SQLite's
    underlying index traversal. Empty scopes have no writer to admit. The trusted
    current holder digest covers the item excluded from the peer query.
    """
    # Reuse the ownership contract without a module-initialization import cycle.
    from . import worklore_ownership as ownership

    requested = body["write_scope"]
    if not requested:
        return
    reservations = {
        ownership.EVENT_TYPES[action]: (action, state)
        for action, state in (
            ("accept", "owned"),
            ("take", "owned"),
            ("checkpoint", "owned"),
            ("report", "owned"),
            ("handoff", "handoff-pending"),
        )
    }
    # Group all ownership events first: a later reset ends the old reservation.
    # Filter resets before LIMIT so immutable departed histories consume no slot.
    rows = conn.execute(
        "SELECT e.work_id, e.event_type, substr(e.node_id, 1, ?), "
        "substr(CAST(e.detail_json AS BLOB), 1, ?) FROM work_events e "
        "JOIN (SELECT work_id, MAX(seq) AS seq FROM work_events WHERE work_id != ? AND "
        + ownership._EVENT_PREDICATE
        + " GROUP BY work_id) latest ON e.work_id=latest.work_id AND e.seq=latest.seq "
        "WHERE e.event_type IN (" + ",".join("?" for _ in reservations) + ") ORDER BY e.work_id LIMIT ?",
        (
            ownership.NODE_ID_MAX + 1,
            ownership.OWNERSHIP_EVENT_MAX_BYTES + 1,
            work_id,
            *ownership.EVENT_TYPES.values(),
            *reservations,
            MAX_SCOPE_SNAPSHOTS + 1,
        ),
    )
    conflicts: list[dict[str, str]] = []
    digests = {current_holder_digest}
    truncated = False
    conflicted = False
    conflict_bytes = 2  # JSON list brackets
    for index, (identifier, event_type, principal, raw) in enumerate(rows):
        if index == MAX_SCOPE_SNAPSHOTS:
            raise ScopeAdmissionError("path-scope-capacity-exhausted")
        try:
            if len(raw) > ownership.OWNERSHIP_EVENT_MAX_BYTES:
                raise ValueError("oversized snapshot")
            detail = json.loads(raw)
            snapshot = detail["ownership"]
            action, state = reservations[event_type]
            if (
                ownership._text(detail["action"], "action", ownership.ACTION_MAX) != action
                or snapshot["state"] != state
            ):
                raise ValueError("inconsistent reservation event")
            identifier = ownership._reference(identifier)
            owner = ownership._node(snapshot["owner_node"])
            if owner != ownership._node(principal):
                raise ValueError("inconsistent reservation principal")
            digest = snapshot["holder_hash"]
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
                raise ValueError("invalid reservation digest")
            digests.add(digest)
            raw_paths = ownership._array(snapshot["write_scope"], ownership.SCOPE_MAX_ITEMS)
            if not raw_paths:
                continue
            repo = ownership._path(snapshot["repo_identity"], "repo_identity", ownership.REPO_IDENTITY_MAX)
            if not ownership._REPO_RE.fullmatch(repo):
                raise ValueError("invalid repository")
            if repo.casefold() != body["repo_identity"].casefold():
                continue
            paths = [ownership._path(path) for path in raw_paths]
            for path in requested:
                for held in paths:
                    if path != held and not path.startswith(held + "/") and not held.startswith(path + "/"):
                        continue
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
        try:
            # A protected holder can follow its carrier in the ordered scan, or
            # have no paths in this repository. Check only the bounded output.
            _reject_reflection(
                digests,
                [
                    record[field]
                    for record in conflicts
                    for field in ("work_id", "owner_node", "requested_path", "held_path")
                ],
            )
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, WorkloreValidationError):
            raise ScopeAdmissionError("path-scope-unavailable") from None
        raise ScopeAdmissionError("path-scope-conflict", conflicts=conflicts, truncated=truncated)
