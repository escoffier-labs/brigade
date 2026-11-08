"""Refuse a fleet policy that lists a memory verifier as a reflect principal.

A verifier principal (docs/phase-memory-verify.md, D4) is never a member of
``memory.reflect_principals``. The flag lives in the Rust hub's memory file,
which Python never opens. After each committed flag change, the memory writer
replaces ``fleet-hub-memory-verifiers.json`` beside the hub database while it
still holds the hub write lock, and it rewrites the file at every start.

Every policy revision is written inside ``BEGIN IMMEDIATE`` on the hub
database, and this module reads the file inside that transaction. A flag change
that committed first is therefore visible here, and a policy revision that
committed first is visible to the memory writer, which refuses to flag a listed
principal. A missing file means no memory store has published a list, so no
principal is flagged. A file that cannot be read or parsed refuses any policy
that lists a reflect principal.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .fleet_hub import FleetHubConflict

SIDECAR = "fleet-hub-memory-verifiers.json"
SCHEMA = "brigade.memory-verifiers.v1"
LISTED = "memory_policy_verifier_listed"
UNREADABLE = "memory_policy_verifier_list_unreadable"
MAX_BYTES = 1024 * 1024


class VerifierListed(FleetHubConflict):
    """The policy names a flagged verifier in ``memory.reflect_principals`` (HTTP 409)."""

    def __init__(self, message: str, *, code: str = LISTED) -> None:
        super().__init__(message)
        self.code = code


def sidecar_path(conn: sqlite3.Connection) -> Path | None:
    """The published list beside the main database, or None for an in-memory database."""
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file).with_name(SIDECAR)
    return None


def flagged(conn: sqlite3.Connection) -> frozenset[str]:
    """Principal ids the memory hub last published as verifiers."""
    path = sidecar_path(conn)
    if path is None:
        return frozenset()
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise VerifierListed(
            f"{UNREADABLE}: the memory verifier list could not be read ({type(exc).__name__})",
            code=UNREADABLE,
        ) from exc
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("too large")
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            raise ValueError("schema")
        verifiers = document.get("verifiers")
        if not isinstance(verifiers, list) or not all(isinstance(item, str) for item in verifiers):
            raise ValueError("verifiers")
    except ValueError as exc:
        raise VerifierListed(f"{UNREADABLE}: the memory verifier list is malformed", code=UNREADABLE) from exc
    return frozenset(verifiers)


def refuse_listed(conn: sqlite3.Connection, document: Mapping[str, Any]) -> None:
    """Raise VerifierListed when ``document`` lists a flagged verifier as a reflect principal."""
    memory = document.get("memory")
    listed = memory.get("reflect_principals") if isinstance(memory, Mapping) else None
    if not listed:
        return
    names = sorted({name for name in listed if isinstance(name, str)} & flagged(conn))
    if names:
        raise VerifierListed(
            f"{LISTED}: memory.reflect_principals lists verifier principal(s) {', '.join(names)}. "
            "A verifier is never a reflect principal. Remove it from the policy, or have an admin clear its flag first"
        )
