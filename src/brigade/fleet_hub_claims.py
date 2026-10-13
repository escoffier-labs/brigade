"""Legacy claim arbitration and durable per-target generation authority.

All current claim rows are legacy. V2 tagging and origin provenance require
another migration. These fences do not make legacy requests tenure-specific.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import fleet_hub as hub

# Fences are never evicted. At capacity, existing targets remain usable but
# an unseen target must fail closed, including a missing legacy release.
MAX_CLAIM_TARGETS = 100_000
MAX_CLAIM_TARGET_BYTES = 1024
MAX_GENERATION = 2**63 - 1
CLAIM_COLUMNS = (
    "target, owner_node, owner_conductor, harness, role, job, session, acquired_at, renewed_at, ttl_seconds, expires_at"
)
_FENCE_SCHEMA = """
CREATE TABLE claim_fences (
    target TEXT NOT NULL PRIMARY KEY,
    generation INTEGER NOT NULL
        CHECK (typeof(generation) = 'integer' AND generation >= 1 AND generation <= 9223372036854775807)
)
"""
_FENCE_DEFINITION = " ".join(_FENCE_SCHEMA.split())


class ClaimFenceError(hub.FleetHubError):
    """Authoritative fencing storage cannot safely serve this operation."""


class ClaimTargetError(hub.FleetHubUnprocessable):
    """A target cannot be represented within new fence storage bounds."""

    def __init__(self, message: str, *, limit: int | None = None) -> None:
        super().__init__(message)
        self.limit = limit

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": "invalid-field", "field": "target", "message": str(self)}
        if self.limit is not None:
            error.update(limit=self.limit, unit="utf8-bytes")
        return {"error": error}


def _target_bytes(target: str) -> bytes:
    try:
        return target.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ClaimTargetError("claim target must be valid UTF-8") from exc


def _refuse_missing_schema(exc: sqlite3.OperationalError) -> None:
    # Lock contention and I/O failures retain SQLite's error and retry policy.
    if str(exc).startswith(("no such table:", "no such column:")):
        raise ClaimFenceError("claim generation fence storage is missing or corrupt") from exc


def _checked_generation(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_GENERATION:
        raise ClaimFenceError("claim generation fence is missing or corrupt")
    return value


def _validate_structure(conn: sqlite3.Connection) -> None:
    """Admit only the exact table this slice creates, without writing.

    A hand-built look-alike can lack the primary key or range CHECK, or use
    NOCASE equality, even when its current rows look valid. Refuse rather than
    repair: rewriting it could change which targets share a fence.
    """
    # NOCASE: SQLite stores a trigger's tbl_name as written in its ON clause.
    entries = conn.execute(
        "SELECT type, sql FROM sqlite_master WHERE tbl_name = 'claim_fences' COLLATE NOCASE"
    ).fetchall()
    if not entries:
        raise ClaimFenceError("claim generation fence storage is missing or corrupt")
    tables = [sql for kind, sql in entries if kind == "table"]
    # The primary key's automatic index has no SQL. Any other index, trigger, or
    # view could alter fence semantics.
    others = [entry for entry in entries if entry[0] != "table" and not (entry[0] == "index" and entry[1] is None)]
    if len(tables) != 1 or others or " ".join(tables[0].split()) != _FENCE_DEFINITION:
        raise ClaimFenceError("claim generation fence storage has an unsupported definition")


def validate_schema(conn: sqlite3.Connection) -> None:
    """Read-only integrity check. Never repair current-schema fence history."""
    try:
        _validate_structure(conn)
        invalid = conn.execute(
            "SELECT 1 FROM claim_fences WHERE typeof(generation) != 'integer' OR generation < 1 "
            "OR generation > ? LIMIT 1",
            (MAX_GENERATION,),
        ).fetchone()
        missing = conn.execute(
            "SELECT 1 FROM claims c LEFT JOIN claim_fences f ON f.target = c.target WHERE f.target IS NULL LIMIT 1"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM claim_fences").fetchone()[0]
    except sqlite3.OperationalError as exc:
        _refuse_missing_schema(exc)
        raise
    if invalid or missing:
        raise ClaimFenceError("claim generation fence is missing or corrupt")
    if count > MAX_CLAIM_TARGETS:
        raise ClaimFenceError("claim generation fence capacity exceeded")


def migrate_schema(conn: sqlite3.Connection) -> None:
    """One-time migration; caller owns schema transaction and version update."""
    # NOCASE: SQLite treats CLAIM_FENCES and claim_fences as one name, so an existing
    # differently-cased object must reach validation rather than collide with CREATE.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'claim_fences' COLLATE NOCASE").fetchone():
        # Existing history is authoritative even when user_version is old.
        # Validate its definition and rows without resetting generations or
        # backfilling missing rows.
        validate_schema(conn)
        return
    targets = conn.execute("SELECT target FROM claims ORDER BY target LIMIT ?", (MAX_CLAIM_TARGETS + 1,)).fetchall()
    if len(targets) > MAX_CLAIM_TARGETS:
        raise ClaimFenceError("claim generation fence capacity exceeded during migration")
    conn.execute(_FENCE_SCHEMA)
    conn.executemany("INSERT INTO claim_fences (target, generation) VALUES (?, 1)", targets)


def generation(conn: sqlite3.Connection, target: str, *, has_claim: bool) -> int:
    try:
        row = conn.execute("SELECT generation FROM claim_fences WHERE target = ?", (target,)).fetchone()
    except sqlite3.OperationalError as exc:
        _refuse_missing_schema(exc)
        raise
    if row is None:
        if has_claim:
            raise ClaimFenceError("claim generation fence is missing or corrupt")
        return 0
    return _checked_generation(row[0])


def _advance(conn: sqlite3.Connection, target: str, current: int) -> int:
    if current == MAX_GENERATION:
        raise ClaimFenceError("claim generation fence exhausted")
    if current == 0:
        if len(_target_bytes(target)) > MAX_CLAIM_TARGET_BYTES:
            raise ClaimTargetError("claim target exceeds UTF-8 byte limit", limit=MAX_CLAIM_TARGET_BYTES)
        count = conn.execute("SELECT COUNT(*) FROM claim_fences").fetchone()[0]
        if count >= MAX_CLAIM_TARGETS:
            raise ClaimFenceError("claim generation fence capacity exceeded")
        conn.execute("INSERT INTO claim_fences (target, generation) VALUES (?, 1)", (target,))
    else:
        conn.execute("UPDATE claim_fences SET generation = ? WHERE target = ?", (current + 1, target))
    return current + 1


def payload(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "target": row[0],
        "owner_node": row[1],
        "owner_conductor": row[2],
        "harness": row[3],
        "role": row[4],
        "job": row[5],
        "session": row[6],
        "acquired_at": row[7],
        "renewed_at": row[8],
        "ttl_seconds": row[9],
        "expires_at": hub._epoch_to_iso(row[10]),
        "generation": _checked_generation(row[15]),
        "protocol": "legacy",
    }


def fetch_claim(conn: sqlite3.Connection, target: str) -> tuple[Any, ...] | None:
    """Private row: payload columns, holder/lock fields, then generation."""
    row = conn.execute(
        f"SELECT {CLAIM_COLUMNS}, holder_token, lock_token, lock_acquired_at, lock_run_dir FROM claims WHERE target = ?",
        (target,),
    ).fetchone()
    current = generation(conn, target, has_claim=row is not None)
    return (*row, current) if row is not None else None


def _prune(conn: sqlite3.Connection, now: float) -> None:
    targets = conn.execute(
        "SELECT target FROM claims WHERE expires_at <= ? LIMIT ?", (now, MAX_CLAIM_TARGETS + 1)
    ).fetchall()
    if len(targets) > MAX_CLAIM_TARGETS:
        raise ClaimFenceError("claim generation fence capacity exceeded")
    for (target,) in targets:
        current = generation(conn, target, has_claim=True)
        _advance(conn, target, current)
        conn.execute("DELETE FROM claims WHERE target = ?", (target,))


def _acquire(conn: sqlite3.Connection, request: dict[str, Any], now: float) -> tuple[int, dict[str, Any]]:
    _prune(conn, now)
    target, node, holder = request["target"], request["node_id"], request["holder"]
    prior = hub._fetch_claim(conn, target)
    current = generation(conn, target, has_claim=prior is not None)
    retry = prior is not None and prior[1] == node and prior[11] == holder
    supersede = request["supersede"]
    replace = (
        prior is not None
        and supersede is not None
        and prior[1] == node
        and prior[12] == supersede["token"]
        and (supersede["acquired_at"] is None or prior[13] is None or prior[13] <= supersede["acquired_at"])
    )
    if prior is not None and not retry and not replace:
        error = f"target {target!r} is held by {prior[1]}"
        if request["scope"] == "node" and prior[1] == node:
            error += " under another lease than the reconciled dead run's (not superseded)"
        return 409, {"granted": False, "error": error, "owner": hub._claim_payload(prior)}
    if not retry:
        _advance(conn, target, current)
    lock = request["lock"] or {"token": None, "acquired_at": None, "run_dir": None}
    labels = request["labels"]
    now_iso = hub._epoch_to_iso(now)
    conn.execute(
        "INSERT INTO claims "
        "(target, owner_node, owner_conductor, harness, role, job, session, holder_token, "
        "lock_token, lock_acquired_at, lock_run_dir, acquired_at, renewed_at, ttl_seconds, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(target) DO UPDATE SET "
        "owner_node=excluded.owner_node, owner_conductor=excluded.owner_conductor, harness=excluded.harness, "
        "role=excluded.role, job=excluded.job, session=excluded.session, holder_token=excluded.holder_token, "
        "lock_token=excluded.lock_token, lock_acquired_at=excluded.lock_acquired_at, lock_run_dir=excluded.lock_run_dir, "
        "acquired_at=excluded.acquired_at, renewed_at=excluded.renewed_at, ttl_seconds=excluded.ttl_seconds, "
        "expires_at=excluded.expires_at",
        (
            target,
            node,
            request["conductor"],
            labels["harness"],
            labels["role"],
            labels["job"],
            labels["session"],
            holder,
            lock["token"],
            lock["acquired_at"],
            lock["run_dir"],
            prior[7] if retry and prior else now_iso,
            now_iso,
            request["ttl_seconds"],
            now + request["ttl_seconds"],
        ),
    )
    row = hub._fetch_claim(conn, target)
    assert row is not None
    response: dict[str, Any] = {"granted": True, "claim": hub._claim_payload(row)}
    if request["scope"] == "node" and prior is not None and prior[11] != holder:
        response["superseded"] = hub._claim_payload(prior)
    return 200, response


def _decide(conn: sqlite3.Connection, request: dict[str, Any], now: float) -> tuple[int, dict[str, Any]]:
    if request["action"] == "acquire":
        return _acquire(conn, request, now)
    target, node, holder = request["target"], request["node_id"], request["holder"]
    row = hub._fetch_claim(conn, target)
    current = generation(conn, target, has_claim=row is not None)
    if request["action"] == "inspect":
        if row is None or row[10] <= now:
            free: dict[str, Any] = {"inspected": True, "claim": None, "owned": False}
            if current:
                free["generation"] = current
            return 200, free
        inspected: dict[str, Any] = {
            "inspected": True,
            "claim": hub._claim_payload(row),
            "owned": row[1] == node,
            "generation": current,
        }
        if row[1] == node:
            inspected["lock_run_dir"] = row[14]
        return 200, inspected
    if request["action"] == "renew":
        if row is not None and row[10] > now and row[11] == holder and row[1] == node:
            conn.execute(
                "UPDATE claims SET renewed_at = ?, ttl_seconds = ?, expires_at = ? WHERE target = ?",
                (hub._epoch_to_iso(now), request["ttl_seconds"], now + request["ttl_seconds"], target),
            )
            renewed = hub._fetch_claim(conn, target)
            assert renewed is not None
            return 200, {"renewed": True, "claim": hub._claim_payload(renewed)}
        if row is not None and row[10] > now:
            return 409, {
                "renewed": False,
                "error": f"target {target!r} is held by {row[1]}",
                "owner": hub._claim_payload(row),
            }
        return 409, {"renewed": False, "error": f"claim on {target!r} is expired or missing", "owner": None}
    scope = request["scope"]
    expected = request["acquired_at"]
    accepted = row is None or (
        (row[11] == holder and row[1] == node)
        if scope == "holder"
        else ((scope == "force" or row[1] == node) and (expected is None or row[7] == expected))
    )
    if accepted:
        _advance(conn, target, current)
        conn.execute("DELETE FROM claims WHERE target = ?", (target,))
        released: dict[str, Any] = {"released": row is not None}
        if scope != "holder" and row is not None:
            released["claim"] = hub._claim_payload(row)
        return 200, released
    assert row is not None
    if scope == "holder":
        error = f"target {target!r} is held by {row[1]}"
    elif scope != "force" and row[1] != node:
        error = f"target {target!r} is held by {row[1]}, not {node}"
    else:
        error = f"target {target!r} was re-acquired since it was inspected (now acquired {row[7]})"
    return 409, {"released": False, "error": error, "owner": hub._claim_payload(row)}


def _refuse_newer_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] > hub.SCHEMA_VERSION:
        raise hub.FleetHubUnsupportedSchema(hub.FleetHubUnsupportedSchema.public_message)


def handle_claim(conn: sqlite3.Connection, raw: Any, *, caller_node: str | None = None) -> tuple[int, dict[str, Any]]:
    request = hub._validate_claim_request(raw)
    if caller_node is not None and request["node_id"] != caller_node:
        raise hub.FleetHubForbidden(
            f"claim node_id {request['node_id']!r} does not match the caller's node token ({caller_node})"
        )
    _target_bytes(request["target"])
    # Early refusal avoids taking the write lock on a known-newer database. A
    # newer hub can still migrate before BEGIN returns, so check again inside.
    _refuse_newer_schema(conn)
    conn.execute("BEGIN" if request["action"] == "inspect" else "BEGIN IMMEDIATE")
    try:
        _refuse_newer_schema(conn)
        # Read time only after acquiring the transaction. Never re-read an
        # owner after commit: the response belongs to this decision snapshot.
        response = _decide(conn, request, hub._now_epoch())
        conn.commit()
        return response
    except BaseException:
        conn.rollback()
        raise


def list_claims(conn: sqlite3.Connection, *, include_all: bool = False) -> list[dict[str, Any]]:
    now = hub._now_epoch()
    # One SELECT snapshot includes each row's fence, including expired rows
    # hidden from ordinary listings. Missing state must not appear healthy.
    columns = ", ".join("c." + name.strip() for name in CLAIM_COLUMNS.split(","))
    try:
        rows = conn.execute(
            f"SELECT {columns}, c.holder_token, c.lock_token, c.lock_acquired_at, c.lock_run_dir, f.generation "
            "FROM claims c LEFT JOIN claim_fences f ON f.target = c.target ORDER BY c.target"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        _refuse_missing_schema(exc)
        raise
    result = []
    for row in rows:
        _checked_generation(row[15])
        expired = row[10] <= now
        if expired and not include_all:
            continue
        claim = hub._claim_payload(row)
        claim["expired"] = expired
        result.append(claim)
    return result
