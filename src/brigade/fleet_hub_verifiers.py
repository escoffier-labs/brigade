"""Refuse a fleet policy that lists a memory verifier as a reflect principal.

A verifier principal (docs/phase-memory-verify.md, D4) is never a member of
``memory.reflect_principals``. The flag lives in the Rust hub's memory file,
which Python never opens. The memory writer replaces
``fleet-hub-memory-verifiers.json`` beside the hub database inside each flag
change's own transaction, while it holds the hub write lock and before it
commits, and refuses the change if publication fails. A clear keeps the
principal listed until the clear commits. The writer republishes the
committed set after the commit and at every start. The file can therefore
over-report flagged principals but never under-report them.

The list is bound to the flag state by a generation. Before a flag change
reaches the memory writer, the Rust hub writer durably records the change's
generation in ``hub_meta`` under ``memory_verifier_generation``, and every list
the memory writer publishes carries the generation it reflects. A list whose
generation is below the hub's is one a crash or a failed startup republish left
behind, and it refuses any policy that lists a reflect principal, whatever
principals it names.

Every policy revision is written inside ``BEGIN IMMEDIATE`` on the hub
database, and this module reads the file inside that transaction. A flag change
that committed first is therefore visible here, and a policy revision that
committed first is visible to the memory writer, which refuses to flag a listed
principal. Without a memory file beside the hub database, native memory was
never initialized and no principal is flagged. Once the memory file exists
(this module checks only that it exists and never opens it), a missing,
unreadable or malformed list refuses any policy that lists a reflect principal.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .fleet_hub import FleetHubConflict

SIDECAR = "fleet-hub-memory-verifiers.json"
MEMORY_FILE = "fleet-hub-memory.db"
SCHEMA = "brigade.memory-verifiers.v1"
LISTED = "memory_policy_verifier_listed"
UNREADABLE = "memory_policy_verifier_list_unreadable"
STALE = "memory_policy_verifier_list_stale"
HUB_GENERATION_KEY = "memory_verifier_generation"
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


def hub_generation(conn: sqlite3.Connection) -> int:
    """The latest verifier generation the hub announced, 0 when none was."""
    try:
        row = conn.execute("SELECT value FROM hub_meta WHERE key = ?", (HUB_GENERATION_KEY,)).fetchone()
    except sqlite3.OperationalError:
        return 0
    try:
        return int(row[0]) if row is not None else 0
    except (TypeError, ValueError) as exc:
        raise VerifierListed(f"{STALE}: the hub's verifier generation is not a number", code=STALE) from exc


def flagged(conn: sqlite3.Connection) -> frozenset[str]:
    """Principal ids the memory hub last published as verifiers, at a current generation."""
    path = sidecar_path(conn)
    if path is None:
        return frozenset()
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        if not path.with_name(MEMORY_FILE).exists():
            return frozenset()
        raise VerifierListed(
            f"{UNREADABLE}: hub memory is initialized but its verifier list is missing",
            code=UNREADABLE,
        ) from None
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
        generation = document.get("generation")
        if type(generation) is not int or generation < 0:
            raise ValueError("generation")
    except ValueError as exc:
        raise VerifierListed(f"{UNREADABLE}: the memory verifier list is malformed", code=UNREADABLE) from exc
    expected = hub_generation(conn)
    if generation < expected:
        raise VerifierListed(
            f"{STALE}: the memory verifier list is at generation {generation} but the hub announced {expected}. "
            "Restart hub memory so it republishes the list",
            code=STALE,
        )
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
