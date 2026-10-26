"""Legacy claim arbitration and durable per-target generation authority.

All current claim rows are legacy. Generation-less retired holders cannot regain authority within the retirement horizon.
Optional generations fence callers that deliberately reuse a holder identity.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from . import fleet_hub as hub
from . import fleet_hub_claim_cardinality, fleet_hub_events

# Fences are never evicted. At capacity, existing targets remain usable but
# an unseen target must fail closed, including a missing legacy release.
MAX_CLAIM_TARGETS = 100_000
MAX_RETIRED_HOLDERS = 1_000_000
MAX_RETIRED_HOLDERS_PER_NODE = 100_000
# Seven days includes the maximum one-day TTL plus six days of queueing.
RETIRED_HOLDER_HORIZON_SECONDS = 7 * 86400
RETIRED_HOLDER_EVICTION_BATCH = 256
EXPIRED_CLAIM_PRUNE_BATCH = 32
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
_RETIRED_V33_SCHEMA = """
CREATE TABLE claim_retired_holders (
    target TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    holder_token TEXT NOT NULL,
    generation INTEGER NOT NULL
        CHECK (typeof(generation) = 'integer' AND generation >= 1 AND generation <= 9223372036854775807),
    PRIMARY KEY (target, owner_node, holder_token)
)
"""
_RETIRED_SCHEMA = _RETIRED_V33_SCHEMA.replace(
    "    PRIMARY KEY",
    "    retired_at REAL NOT NULL CHECK (typeof(retired_at) IN ('integer', 'real') "
    "AND retired_at >= 0 AND retired_at <= 1.7976931348623157e308),\n    PRIMARY KEY",
)
_RETIRED_INDEXES = (
    "CREATE INDEX claim_retired_holders_age ON claim_retired_holders (retired_at)",
    "CREATE INDEX claim_retired_holders_node_age ON claim_retired_holders (owner_node, retired_at)",
)


class ClaimFenceError(hub.FleetHubError):
    """Authoritative fencing storage cannot safely serve this operation."""

    reason = "claim-fence-refused"


class _RetirementCapacityError(ClaimFenceError):
    """A retirement budget is full, without implying corrupt target fences."""


class ClaimTargetError(hub.FleetHubUnprocessable):
    """A target cannot be represented within new fence storage bounds."""

    status = 422
    reason: str | None = None

    def __init__(self, message: str, *, limit: int | None = None) -> None:
        super().__init__(message)
        self.limit = limit

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": "invalid-field", "field": "target", "message": str(self)}
        if self.limit is not None:
            error.update(limit=self.limit, unit="utf8-bytes")
        payload: dict[str, Any] = {"error": error}
        if self.reason is not None:
            payload.update(reason=self.reason, owner={})
        return payload


class ClaimTargetTooLongError(ClaimTargetError, ClaimFenceError):
    """A new target exceeds the fence byte bound and must fail closed.

    Despite the ``FleetHubUnprocessable`` base (HTTP 422), this reports 409 with
    an ``owner``. That only holds while the ``ClaimTargetError`` handler runs
    before the generic ``FleetHubUnprocessable`` handler in ``fleet_hub_http``;
    a 422 without an owner is treated as non-terminal by deployed base clients,
    which would fall back to the local lock. A regression test pins the order.
    """

    status = 409
    reason = "claim-target-too-long"


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


def _validate_structure(
    conn: sqlite3.Connection,
    table: str = "claim_fences",
    schema: str = _FENCE_SCHEMA,
    indexes: tuple[str, ...] = (),
) -> None:
    """Admit exactly the declared table, primary key, and named indexes."""
    label = "claim retired-holder fence" if table == "claim_retired_holders" else "claim generation fence"
    entries = conn.execute("SELECT type, sql FROM sqlite_master WHERE tbl_name = ? COLLATE NOCASE", (table,)).fetchall()
    if not entries:
        raise ClaimFenceError(f"{label} storage is missing or corrupt")
    tables = [sql for kind, sql in entries if kind == "table"]
    others = [entry for entry in entries if entry[0] != "table" and not (entry[0] == "index" and entry[1] is None)]
    declared = sorted(("index", " ".join(sql.split())) for sql in indexes)
    actual = sorted((kind, " ".join(sql.split())) for kind, sql in others)
    if len(tables) != 1 or actual != declared or " ".join(tables[0].split()) != " ".join(schema.split()):
        raise ClaimFenceError(f"{label} storage has an unsupported definition")


def _validate_fences(conn: sqlite3.Connection) -> None:
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


def _validate_retired(conn: sqlite3.Connection, *, legacy: bool = False) -> None:
    try:
        _validate_structure(
            conn,
            "claim_retired_holders",
            _RETIRED_V33_SCHEMA if legacy else _RETIRED_SCHEMA,
            () if legacy else _RETIRED_INDEXES,
        )
        timestamp_check = (
            ""
            if legacy
            else (
                " OR typeof(r.retired_at) NOT IN ('integer', 'real') OR r.retired_at < 0 "
                "OR r.retired_at > 1.7976931348623157e308"
            )
        )
        invalid = conn.execute(
            "SELECT 1 FROM claim_retired_holders r LEFT JOIN claim_fences f ON f.target = r.target "
            "WHERE f.target IS NULL OR typeof(r.generation) != 'integer' OR r.generation < 1 "
            "OR r.generation > f.generation OR typeof(r.owner_node) != 'text' "
            "OR length(r.owner_node) NOT BETWEEN 1 AND 128 OR typeof(r.holder_token) != 'text' "
            "OR length(r.holder_token) NOT BETWEEN 1 AND 128" + timestamp_check + " LIMIT 1"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM claim_retired_holders").fetchone()[0]
    except sqlite3.OperationalError as exc:
        if str(exc).startswith(("no such table:", "no such column:")):
            raise ClaimFenceError("claim retired-holder fence storage is missing or corrupt") from exc
        raise
    if invalid:
        raise ClaimFenceError("claim retired-holder fence is missing or corrupt")
    if count > MAX_RETIRED_HOLDERS:
        raise ClaimFenceError("claim retired-holder fence capacity exceeded")


def validate_schema(conn: sqlite3.Connection) -> None:
    """Read-only validation, including export of an unmigrated v32 snapshot."""
    _validate_fences(conn)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 33:
        _validate_retired(conn, legacy=version == 33)


def migrate_schema(conn: sqlite3.Connection) -> None:
    """One-time migration; caller owns schema transaction and version update."""
    # NOCASE: SQLite treats CLAIM_FENCES and claim_fences as one name, so an existing
    # differently-cased object must reach validation rather than collide with CREATE.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'claim_fences' COLLATE NOCASE").fetchone():
        # Existing history is authoritative even when user_version is old.
        # Validate its definition and rows without resetting generations or
        # backfilling missing rows.
        _validate_fences(conn)
    else:
        targets = conn.execute("SELECT target FROM claims ORDER BY target LIMIT ?", (MAX_CLAIM_TARGETS + 1,)).fetchall()
        if len(targets) > MAX_CLAIM_TARGETS:
            raise ClaimFenceError("claim generation fence capacity exceeded during migration")
        conn.execute(_FENCE_SCHEMA)
        conn.executemany("INSERT INTO claim_fences (target, generation) VALUES (?, 1)", targets)
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'claim_retired_holders' COLLATE NOCASE").fetchone()
    if exists:
        # A v33 draft has no timestamps. Validate before migrating and retain
        # every retirement for a full horizon from this upgrade, conservatively.
        legacy = "retired_at" not in {row[1] for row in conn.execute("PRAGMA table_info(claim_retired_holders)")}
        _validate_retired(conn, legacy=legacy)
        if legacy:
            conn.execute("ALTER TABLE claim_retired_holders RENAME TO claim_retired_holders_v33")
            conn.execute(_RETIRED_SCHEMA)
            conn.execute(
                "INSERT INTO claim_retired_holders SELECT target, owner_node, holder_token, generation, ? "
                "FROM claim_retired_holders_v33",
                (hub._now_epoch(),),
            )
            conn.execute("DROP TABLE claim_retired_holders_v33")
    else:
        conn.execute(_RETIRED_SCHEMA)
        legacy = True
    if legacy:
        for sql in _RETIRED_INDEXES:
            conn.execute(sql)
    _validate_retired(conn)


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
            raise ClaimTargetTooLongError("claim target exceeds UTF-8 byte limit", limit=MAX_CLAIM_TARGET_BYTES)
        count = conn.execute("SELECT COUNT(*) FROM claim_fences").fetchone()[0]
        if count >= MAX_CLAIM_TARGETS:
            raise ClaimFenceError("claim generation fence capacity exceeded")
        conn.execute("INSERT INTO claim_fences (target, generation) VALUES (?, 1)", (target,))
    else:
        conn.execute("UPDATE claim_fences SET generation = ? WHERE target = ?", (current + 1, target))
    return current + 1


def _checked_retired_at(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ClaimFenceError("claim retired-holder fence timestamp is missing or corrupt")
    return float(value)


def _retired(conn: sqlite3.Connection, target: str, node: str, holder: str, current: int, now: float) -> int:
    try:
        row = conn.execute(
            "SELECT generation, retired_at FROM claim_retired_holders "
            "WHERE target = ? AND owner_node = ? AND holder_token = ?",
            (target, node, holder),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        _refuse_missing_schema(exc)
        raise
    if row is None:
        return 0
    value = _checked_generation(row[0])
    if value > current:
        raise ClaimFenceError("claim retired-holder fence is missing or corrupt")
    return value if _checked_retired_at(row[1]) >= now - RETIRED_HOLDER_HORIZON_SECONDS else 0


class _RetirementBudget:
    """Transaction-local counts. Age and node/age indexes bound reclamation."""

    def __init__(self, conn: sqlite3.Connection, now: float):
        self.conn, self.now = conn, now
        self.cutoff = now - RETIRED_HOLDER_HORIZON_SECONDS
        self.nodes: dict[str, int] = {}
        _validate_structure(conn, "claim_retired_holders", _RETIRED_SCHEMA, _RETIRED_INDEXES)
        self.count = conn.execute("SELECT COUNT(*) FROM claim_retired_holders").fetchone()[0]
        if self.count > MAX_RETIRED_HOLDERS:
            raise ClaimFenceError("claim retired-holder fence capacity exceeded")
        self._evict()

    def _evict(self) -> None:
        try:
            rows = self.conn.execute(
                "SELECT r.rowid, r.generation, r.retired_at, f.generation FROM claim_retired_holders r "
                "LEFT JOIN claim_fences f ON f.target = r.target "
                "WHERE r.retired_at < ? ORDER BY r.retired_at LIMIT ?",
                (self.cutoff, RETIRED_HOLDER_EVICTION_BATCH),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _refuse_missing_schema(exc)
            raise
        for _, value, retired_at, current in rows:
            if _checked_generation(value) > _checked_generation(current):
                raise ClaimFenceError("claim retired-holder fence is missing or corrupt")
            _checked_retired_at(retired_at)
        self.conn.executemany("DELETE FROM claim_retired_holders WHERE rowid = ?", [(row[0],) for row in rows])
        self.count -= len(rows)
        # Only out-of-horizon rows were removed, so cached in-horizon node
        # counts remain valid. Target generation fences are never touched.

    def check(self, target: str, node: str, holder: str, *, enforce_node_quota: bool = False) -> tuple[bool, bool]:
        old = self.conn.execute(
            "SELECT generation, retired_at FROM claim_retired_holders "
            "WHERE target = ? AND owner_node = ? AND holder_token = ?",
            (target, node, holder),
        ).fetchone()
        if old is not None and _checked_generation(old[0]) > generation(self.conn, target, has_claim=False):
            raise ClaimFenceError("claim retired-holder fence is missing or corrupt")
        active = old is not None and _checked_retired_at(old[1]) >= self.cutoff
        if active:
            return False, False
        if enforce_node_quota and node not in self.nodes:
            self.nodes[node] = self.conn.execute(
                "SELECT COUNT(*) FROM claim_retired_holders WHERE owner_node = ? AND retired_at >= ?",
                (node, self.cutoff),
            ).fetchone()[0]
        # Reclaim a bounded batch before every capacity refusal, including a
        # node refusal. Re-read the key because eviction may have removed it.
        node_full = enforce_node_quota and self.nodes[node] >= MAX_RETIRED_HOLDERS_PER_NODE
        if node_full or (old is None and self.count >= MAX_RETIRED_HOLDERS):
            self._evict()
            old = self.conn.execute(
                "SELECT 1 FROM claim_retired_holders WHERE target = ? AND owner_node = ? AND holder_token = ?",
                (target, node, holder),
            ).fetchone()
        if node_full:
            raise _RetirementCapacityError("claim retired-holder fence per-node quota exceeded")
        if old is None and self.count >= MAX_RETIRED_HOLDERS:
            raise _RetirementCapacityError("claim retired-holder fence capacity exceeded")
        return old is None, True

    def retire(self, target: str, node: str, holder: str, current: int, *, enforce_node_quota: bool = False) -> None:
        new_row, new_active = self.check(target, node, holder, enforce_node_quota=enforce_node_quota)
        self.conn.execute(
            "INSERT INTO claim_retired_holders (target, owner_node, holder_token, generation, retired_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(target, owner_node, holder_token) DO UPDATE SET "
            "generation = excluded.generation, retired_at = excluded.retired_at",
            (target, node, holder, current, self.now),
        )
        self.count += int(new_row)
        if new_active and node in self.nodes:
            self.nodes[node] += 1


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    _validate_fences(conn)
    _validate_retired(conn)
    return {
        "retired_holder_rows": conn.execute("SELECT COUNT(*) FROM claim_retired_holders").fetchone()[0],
        "retired_holder_horizon_seconds": RETIRED_HOLDER_HORIZON_SECONDS,
    }


def _stale(
    conn: sqlite3.Connection, request: dict[str, Any], row: tuple[Any, ...] | None, current: int, now: float
) -> tuple[int, dict[str, Any]] | None:
    action, holder = request["action"], request["holder"]
    retired = 0
    if holder is not None and (action != "release" or request["scope"] == "holder"):
        retired = _retired(conn, request["target"], request["node_id"], holder, current, now)
    expected = request["generation"]
    # Acquire retries may carry the pre-acquire fence. Only the same live
    # holder can retry that transition; renew/release require its tenure fence.
    retry = (
        action == "acquire"
        and row is not None
        and row[1] == request["node_id"]
        and row[11] == holder
        and expected == current - 1
        and expected >= retired
    )
    reason = None
    if expected is not None and expected != current and not retry:
        reason = "stale-generation"
    elif expected is None and retired:
        reason = "stale-holder"
    if reason is None:
        return None
    if action == "release" and row is None and reason == "stale-holder":
        return 200, {"released": False, "reason": reason}
    key = {"acquire": "granted", "renew": "renewed", "release": "released"}[action]
    return 409, {
        key: False,
        "reason": reason,
        "error": f"claim operation refused: {reason}",
        "owner": hub._claim_payload(row) if row is not None else {},
    }


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


def _prune(conn: sqlite3.Connection, now: float, budget: _RetirementBudget, requested_target: str) -> None:
    # Always process the requested target first, even beyond the unrelated
    # batch. Keep retirement work under the write lock independent of backlog.
    targets = conn.execute(
        "SELECT target, owner_node, holder_token, expires_at FROM claims WHERE target = ? AND expires_at <= ?",
        (requested_target, now),
    ).fetchall()
    targets += conn.execute(
        "SELECT target, owner_node, holder_token, expires_at FROM claims WHERE expires_at <= ? AND target != ? LIMIT ?",
        (now, requested_target, EXPIRED_CLAIM_PRUNE_BATCH),
    ).fetchall()
    for target, node, holder, expires_at in targets:
        current = generation(conn, target, has_claim=True)
        if current == MAX_GENERATION:
            raise ClaimFenceError("claim generation fence exhausted")
        try:
            # A refused unrelated retirement must leave both its claim and
            # target fence untouched. Check and write the marker only once.
            budget.retire(target, node, holder, current + 1)
        except _RetirementCapacityError:
            if target == requested_target:
                raise
            continue
        _advance(conn, target, current)
        conn.execute("DELETE FROM claims WHERE target = ?", (target,))
        fleet_hub_events.record_claim_expired(conn, target, node, holder, expires_at)


def _acquire(
    conn: sqlite3.Connection, request: dict[str, Any], now: float, budget: _RetirementBudget
) -> tuple[int, dict[str, Any]]:
    _prune(conn, now, budget, request["target"])
    target, node, holder = request["target"], request["node_id"], request["holder"]
    prior = hub._fetch_claim(conn, target)
    current = generation(conn, target, has_claim=prior is not None)
    stale = _stale(conn, request, prior, current, now)
    if stale is not None:
        return stale
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
    # Existing-target and stale-holder decisions must precede quota checks;
    # retries and replacements do not consume another claim slot.
    if prior is None:
        fleet_hub_claim_cardinality.check_new_claim(conn, node, now)
    if not retry:
        # Every new holder, including a superseding replacement, needs
        # retirement capacity. Only the old holder's retirement is exempt
        # from the node quota, so replacements cannot chain past it.
        budget.check(target, node, holder, enforce_node_quota=True)
    if not retry:
        started = _advance(conn, target, current)
        if prior is not None:
            budget.retire(target, prior[1], prior[11], started)
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
    budget = _RetirementBudget(conn, now) if request["action"] != "inspect" else None
    if request["action"] == "acquire":
        assert budget is not None
        return _acquire(conn, request, now, budget)
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
    stale = _stale(conn, request, row, current, now)
    if stale is not None:
        return stale
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
        assert budget is not None
        ended = _advance(conn, target, current)
        if row is not None:
            budget.retire(target, row[1], row[11], ended)
        elif scope == "holder":
            budget.retire(target, node, holder, ended, enforce_node_quota=True)
        conn.execute("DELETE FROM claims WHERE target = ?", (target,))
        if row is not None and row[10] <= now:
            # Released after it lapsed but before a prune or sweep saw it. The event id is
            # deterministic, so a later sweep cannot record a second one.
            fleet_hub_events.record_claim_expired(conn, target, row[1], row[11], row[10])
        released: dict[str, Any] = {"released": row is not None}
        if scope == "holder" and row is None:
            released["reason"] = "stale-holder"
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
    expected = raw.get("generation")
    if "generation" in raw and (type(expected) is not int or not 0 <= expected <= MAX_GENERATION):
        raise hub.FleetHubError("claim field 'generation' must be an integer in [0, 9223372036854775807]")
    request["generation"] = expected
    if caller_node is not None and request["node_id"] != caller_node:
        raise hub.FleetHubForbidden(
            f"claim node_id {request['node_id']!r} does not match the caller's node token ({caller_node})"
        )
    _target_bytes(request["target"])
    # Early refusal avoids taking the write lock on a known-newer database. A
    # newer hub can still migrate before BEGIN returns, so check again inside.
    _refuse_newer_schema(conn)
    if request["action"] == "release" and request["scope"] != "holder" and expected is None:
        # Legacy operator requests have no holder incarnation. Bind them to
        # the ingress snapshot before waiting for the write lock. New clients
        # send the generation they inspected before putting the POST on wire.
        try:
            observed = conn.execute(
                "SELECT f.generation, EXISTS(SELECT 1 FROM claims WHERE target = ?) "
                "FROM (SELECT 1) LEFT JOIN claim_fences f ON f.target = ?",
                (request["target"], request["target"]),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            _refuse_missing_schema(exc)
            raise
        assert observed is not None
        if observed[0] is None and observed[1]:
            raise ClaimFenceError("claim generation fence is missing or corrupt")
        request["generation"] = _checked_generation(observed[0]) if observed[0] is not None else 0
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


def list_active_claims(conn: sqlite3.Connection, *, now_epoch: float, limit: int) -> list[dict[str, Any]]:
    """Read a bounded active page, validating each returned claim's generation fence."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("active claim limit must be a positive integer")
    columns = ", ".join("c." + name.strip() for name in CLAIM_COLUMNS.split(","))
    try:
        rows = conn.execute(
            f"SELECT {columns}, c.holder_token, c.lock_token, c.lock_acquired_at, c.lock_run_dir, f.generation "
            "FROM claims c LEFT JOIN claim_fences f ON f.target = c.target "
            "WHERE c.expires_at > ? ORDER BY c.target LIMIT ?",
            (now_epoch, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        _refuse_missing_schema(exc)
        raise
    result = []
    for row in rows:
        _checked_generation(row[15])
        claim = hub._claim_payload(row)
        claim["expired"] = False
        result.append(claim)
    return result
