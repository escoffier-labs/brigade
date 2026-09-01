"""SQLite projection and append-only events for Worklore items."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import sys
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence, TypeVar

from .worklore_validate import (
    ACCEPTANCE_ITEM_MAX,
    ACCEPTANCE_MAX_ITEMS,
    CREATE_FIELDS,
    DEPENDENCIES_MAX_ITEMS,
    DESCRIPTION_MAX,
    EVIDENCE_REFS_MAX_ITEMS,
    KINDS,  # noqa: F401
    TITLE_MAX,
    TRANSITIONS,  # noqa: F401
    WorkloreValidationError,
    as_datetime,
    assert_ready,
    can_transition,
    parse_create,
    safe_https_url,
    safe_optional_text,
    safe_text,
)

PATCH_FIELDS = CREATE_FIELDS - {"kind"}
EXCLUSION_BUCKETS = (
    "acceptance-required",
    "not-ready",
    "not-eligible",
    "manual-mode",
    "blocker",
    "attempt-limit",
    "review-after",
    "source-policy",
)
OBSERVATION_FIELDS = frozenset(
    {
        "external_key",
        "link_type",
        "title",
        "source_policy",
        "description",
        "acceptance",
        "external_state",
        "external_updated_at",
        "url",
        "display_ref",
        "proposed_status",
        "priority",
        "dependencies",
        "evidence_refs",
        "stale",
    }
)
IMPORT_LINK_TYPES = frozenset({"github", "brigade"})
ADMIN_LINK_TYPES = frozenset({"url", "github", "brigade"})
SOURCE_TYPES = frozenset({"github", "brigade"})
SOURCE_POLICIES = frozenset({"eligible", "label-removed", "closed", "completed"})
EVIDENCE_REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
IMPORT_MAX_OBSERVATIONS = 500
# Total storage ceilings for imported work. One batch is already capped at
# IMPORT_MAX_OBSERVATIONS; these bound what an adapter, and the node that owns it, can
# accumulate across many batches. Both sit far above a normal GitHub org or Brigade
# workspace sync, and they refuse only new identities, never an update to work the
# adapter already imported.
ADAPTER_IMPORT_MAX_LINKS = 20000
OWNER_IMPORT_MAX_LINKS = 100000
# One work item may carry at most ITEM_MAX_LINKS links of every type combined. Operator
# links, adapter imports, and fleet-run links all claim from the same ceiling inside the
# writing transaction, so no caller can grow one item's link set without limit and make
# every read that touches it expensive. The ceiling binds new writes only. A database
# written before it existed may hold an item over it, and that item is served, not
# refused: every read path is cut by SQL rather than by trusting the ceiling, and an
# operator repairs the item by unlinking through the ordinary route on a running hub.
ITEM_MAX_LINKS = 200
# Fleet-run links are the one link class ordinary execution grows on its own, so each item
# retains the most recent FLEET_RUN_LINK_RETENTION of them and older ones expire when a new
# run is linked. Without this a busy item reaches ITEM_MAX_LINKS and can never be linked to
# another run again. Retention is silent, the way adapter observation events are: it writes
# no `unlinked` event, because it is storage policy rather than an operator decision.
FLEET_RUN_LINK_RETENTION = 50
# A list page projects at most this many links per item and says so with
# ``links_truncated``. The full set is read through the paged link route.
ITEM_LINK_PROJECTION_MAX = 50
LINK_PAGE_DEFAULT = 50
# Import keys are retained, not kept forever: the most recent keys per adapter and per
# owning node stay exact, older ones expire. A replay inside the window returns the stored
# counts unchanged; a replay of an expired key is re-applied as a fresh batch, and the
# per-identity source high-water below is what stops that re-apply from rolling a newer
# projection back to the older observations the expired batch carried.
ADAPTER_IMPORT_KEY_RETENTION = 500
OWNER_IMPORT_KEY_RETENTION = 5000
# The rollback guard for expired-key replay. ``work_links.source_version`` holds the
# highest ``external_updated_at`` ever accepted for one external identity, so an
# observation is applied only when the source can prove it is at least that recent. One
# nullable column on a row that already exists keeps the guard bounded by the imported
# identities themselves: no side table, and nothing an adapter can grow past the link
# ceilings. A source that never sends a usable timestamp never gets a projection at all:
# a revision is what buys the right to write, so such an observation is refused rather
# than applied, and there is no identity anywhere in the ledger without a guard.
_LINK_SOURCE_VERSION_COLUMN = "source_version"
# Adapter observation events are the only events an external source can grow without
# limit, so each item retains the most recent ADAPTER_SYNC_EVENT_RETENTION of them.
# Lifecycle events (created, linked, transitioned, attempt-*, unlinked) are never trimmed.
ADAPTER_SYNC_EVENT_RETENTION = 200
ADAPTER_SYNC_EVENT_TYPES = ("sync-observed", "sync-stale")
# Reconciling existing rows against the quotas, and backfilling a newly added column, is a
# per-database migration and not a per-start cost. ``work_schema_meta`` records the highest
# reconciliation this database has completed, so an ordinary hub start pays for idempotent
# DDL only and never re-walks the whole event log. Bump this whenever a new migration must
# touch rows that were written before it.
WORKLORE_RECONCILE_VERSION = 2
_RECONCILED_VERSION_KEY = "reconciled_version"
# Why one imported observation was not applied. Every reason is per identity: the rest of
# the batch is imported normally, and the count and a bounded list of reasons come back on
# the import result so an operator sees a refusal instead of inferring it from a silence.
REFUSED_OPERATOR_MANAGED = "operator-managed-identity"
REFUSED_ADAPTER_IDENTITY_CONFLICT = "adapter-identity-conflict"
REFUSED_OWNER_NODE_MISMATCH = "owner-node-mismatch"
REFUSED_REVISION_STALE = "source-revision-stale"
REFUSED_REVISION_MISSING = "source-revision-missing"
REFUSED_REVISION_NOT_ADVANCED = "source-revision-not-advanced"
# One import result carries at most this many refusal details. The count is always exact.
REFUSED_DETAILS_MAX = 20
# One burn read returns at most BURN_PAGE_DEFAULT items and examines at most
# BURN_SCAN_MAX rows. Past the scan budget the page stops and hands back a cursor rather
# than walking a ledger that has grown without limit.
BURN_PAGE_DEFAULT = 50
BURN_SCAN_MAX = 2000
# Filtered fleet-wide listings walk their global keyset in bounded chunks. Filters that do
# not share an order-compatible index, especially source existence, are tested per candidate.
LIST_SCAN_MAX = 2000
DETAIL_RECENT_EVENTS = 20
_BURN_SCAN_CHUNK = 200
# SQLite defaults to 999 bound variables per statement, so page-scoped IN () lookups are
# split well under that.
_SQL_VARIABLE_CHUNK = 200
ADAPTER_ID_MAX = 256
ADAPTER_SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$")
IDEMPOTENCY_KEY_MAX = 256
EXTERNAL_KEY_MAX = 256
DISPLAY_REF_MAX = 256
EXTERNAL_STATE_MAX = 64
# Attempt run ids are public correlation labels, not receipt bodies or paths. 128 characters
# covers Brigade's generated run ids while keeping every indexed attempt identity small.
ATTEMPT_RUN_ID_MAX = 128
# Every attempt action consumes a durable event. This ceiling applies to new writes, including
# privileged ones, so a stream of distinct run ids cannot make one item's event log unbounded.
ITEM_MAX_ATTEMPT_EVENTS = 100

_ITEM_COLUMNS = (
    "work_id",
    "title",
    "description",
    "kind",
    "scope",
    "status",
    "priority",
    "burn_eligible",
    "burn_rank",
    "token_appetite",
    "execution_mode",
    "acceptance_json",
    "blocker",
    "review_after",
    "spend_by",
    "ready_at",
    "attempt_count",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)
_EVENT_SEQ_SQL = "(SELECT COALESCE(MAX(seq), 0) + 1 FROM work_events)"
_EVENT_COLUMNS = (
    "work_id",
    "event_id",
    "event_type",
    "from_status",
    "to_status",
    "actor_type",
    "actor_id",
    "node_id",
    "run_id",
    "detail_json",
    "occurred_at",
    "received_at",
)
_EVENT_INSERT_COLUMNS = (*_EVENT_COLUMNS, "seq")
_EVENT_PAGE_COLUMNS = (*_EVENT_COLUMNS, "seq")
# Adapter ownership is stored on the link, not re-derived from the item's event history,
# so an import checks it with one indexed read and storage bounds can count it. The
# source high-water rides the same row and the same read, and stays out of _LINK_COLUMNS
# so it never reaches an API response as if it were source-reported data.
_LINK_OWNER_COLUMNS = ("adapter_id", "owner_node")
_LINK_CLAIM_COLUMNS = (*_LINK_OWNER_COLUMNS, _LINK_SOURCE_VERSION_COLUMN)
_LINK_COLUMNS = (
    "link_id",
    "work_id",
    "link_type",
    "external_key",
    "display_ref",
    "url",
    "external_state",
    "external_updated_at",
    "source_policy",
    "source_acceptance_json",
    "synced_at",
    "stale_at",
)

# The burn order from the spec: smallest burn_rank, then earliest spend_by with "no
# deadline" last, then oldest ready_at, then work_id. COALESCE keeps NULL and empty
# indistinguishable so a keyset cursor never drops a NULL-valued row, and the same
# expressions back an index, the ORDER BY, and the cursor comparison.
_BURN_KEY_SQL = (
    "burn_rank, (spend_by IS NULL OR spend_by = ''), (COALESCE(spend_by, '')), (COALESCE(ready_at, '')), work_id"
)
_BURN_ORDER_SQL = (
    "burn_rank ASC, (spend_by IS NULL OR spend_by = '') ASC, COALESCE(spend_by, '') ASC, "
    "COALESCE(ready_at, '') ASC, work_id ASC"
)
_BURN_CURSOR_FIELDS = ("burn_rank", "spend_by", "ready_at", "work_id")

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS work_items (
        work_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        kind TEXT NOT NULL,
        scope TEXT,
        status TEXT NOT NULL,
        priority TEXT NOT NULL,
        burn_eligible INTEGER NOT NULL,
        burn_rank INTEGER NOT NULL,
        token_appetite TEXT NOT NULL,
        execution_mode TEXT NOT NULL,
        acceptance_json TEXT NOT NULL,
        blocker TEXT,
        review_after TEXT,
        spend_by TEXT,
        ready_at TEXT,
        attempt_count INTEGER NOT NULL,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_links (
        link_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL,
        link_type TEXT NOT NULL,
        external_key TEXT NOT NULL,
        display_ref TEXT,
        url TEXT,
        external_state TEXT,
        external_updated_at TEXT,
        source_policy TEXT NOT NULL,
        source_acceptance_json TEXT NOT NULL,
        synced_at TEXT NOT NULL,
        stale_at TEXT,
        adapter_id TEXT,
        owner_node TEXT,
        source_version TEXT,
        FOREIGN KEY (work_id) REFERENCES work_items(work_id) ON DELETE RESTRICT,
        UNIQUE (link_type, external_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_events (
        work_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        actor_type TEXT NOT NULL,
        actor_id TEXT,
        node_id TEXT,
        run_id TEXT,
        detail_json TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        seq INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (work_id, event_id),
        FOREIGN KEY (work_id) REFERENCES work_items(work_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_sync_cursors (
        adapter_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        owner_node TEXT NOT NULL,
        cursor TEXT,
        last_success_at TEXT,
        last_error_code TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_import_keys (
        adapter_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        created INTEGER NOT NULL,
        updated INTEGER NOT NULL,
        unchanged INTEGER NOT NULL DEFAULT 0,
        refused INTEGER NOT NULL DEFAULT 0,
        received_at TEXT NOT NULL,
        owner_node TEXT,
        PRIMARY KEY (adapter_id, idempotency_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_create_keys (
        actor_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        work_id TEXT NOT NULL,
        received_at TEXT NOT NULL,
        PRIMARY KEY (actor_id, idempotency_key),
        FOREIGN KEY (work_id) REFERENCES work_items(work_id) ON DELETE RESTRICT
    )
    """,
)

_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS work_items_status ON work_items (status)",
    "CREATE INDEX IF NOT EXISTS work_items_burn ON work_items (burn_eligible)",
    "CREATE INDEX IF NOT EXISTS work_items_ready_at ON work_items (ready_at)",
    "CREATE INDEX IF NOT EXISTS work_events_work_id ON work_events (work_id)",
    "CREATE INDEX IF NOT EXISTS work_events_seq ON work_events (seq)",
    "CREATE INDEX IF NOT EXISTS work_events_work_seq ON work_events (work_id, seq)",
    "CREATE INDEX IF NOT EXISTS work_links_external ON work_links (link_type, external_key)",
    "CREATE INDEX IF NOT EXISTS work_links_work_id ON work_links (work_id)",
    # Backs the per-item ORDER BY that the list projection's window function cuts on, so a
    # page reads its per-item link budget out of the index instead of sorting a legacy
    # item's whole link set.
    "CREATE INDEX IF NOT EXISTS work_links_work_order ON work_links (work_id, synced_at, link_id)",
    # The bounded item and burn summaries use exact source-policy probes. These indexes
    # let every probe stop after its first matching link, even for pre-ceiling items.
    "CREATE INDEX IF NOT EXISTS work_links_work_policy_order ON work_links (work_id, source_policy, synced_at, link_id)",
    "CREATE INDEX IF NOT EXISTS work_links_work_policy_type_stale "
    "ON work_links (work_id, source_policy, link_type, stale_at)",
    "CREATE INDEX IF NOT EXISTS work_links_work_policy_type_order "
    "ON work_links (work_id, source_policy, link_type, synced_at, link_id)",
    "CREATE INDEX IF NOT EXISTS work_links_adapter ON work_links (adapter_id)",
    "CREATE INDEX IF NOT EXISTS work_links_owner ON work_links (owner_node)",
    "CREATE INDEX IF NOT EXISTS work_import_keys_adapter ON work_import_keys (adapter_id, received_at)",
    "CREATE INDEX IF NOT EXISTS work_import_keys_owner ON work_import_keys (owner_node, received_at)",
    "CREATE INDEX IF NOT EXISTS work_events_adapter_retention ON work_events (work_id, actor_type, event_type, seq)",
    # The retention sweep filters adapter observation event types before it discovers the
    # affected item partitions. Keep that filter first and retain work_id for the per-item
    # deletes that follow.
    "CREATE INDEX IF NOT EXISTS work_events_adapter_event_work_seq "
    "ON work_events (actor_type, event_type, work_id, seq)",
    # Attempt writes count a single item's three event types through this index before
    # appending. A separate identity index makes authenticated retry probes constant-time.
    "CREATE INDEX IF NOT EXISTS work_events_attempt_work_seq ON work_events (work_id, event_type, seq)",
    "CREATE INDEX IF NOT EXISTS work_events_attempt_identity ON work_events (work_id, event_type, node_id, run_id)",
    # The item page orders by (created_at, work_id) and the sprint log by (occurred_at DESC,
    # seq DESC). Both are the keyset the cursor walks, so an index in exactly that order lets
    # a page read its rows straight out of the index instead of sorting the whole table.
    "CREATE INDEX IF NOT EXISTS work_items_created_order ON work_items (created_at, work_id)",
    "CREATE INDEX IF NOT EXISTS work_events_recent ON work_events (occurred_at DESC, seq DESC)",
    # Ownership and the source high-water are keyed by the full external identity,
    # (work_id, link_type, external_key). No extra index is needed for it: the
    # UNIQUE (link_type, external_key) constraint already resolves that lookup to one row.
    # Fleet-run retention deletes the oldest of one item's run links.
    "CREATE INDEX IF NOT EXISTS work_links_work_type ON work_links (work_id, link_type, synced_at)",
    f"CREATE INDEX IF NOT EXISTS work_items_burn_order ON work_items ({_BURN_KEY_SQL})",
)

T = TypeVar("T")


class WorkloreConflict(Exception):
    """A Worklore optimistic-concurrency or import conflict."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class WorkloreSchemaError(Exception):
    """A stored Worklore database cannot be brought up to the current storage contract.

    Reserved for a migration that genuinely cannot proceed. Nothing raises it today: rows
    written before a quota existed are reconciled or served under SQL-bounded reads rather
    than refused, because a hub that will not start is a hub an operator cannot repair
    through. Kept so callers that already catch it stay valid.
    """


class WorkloreNotFound(Exception):
    """A Worklore item was not found."""

    def __init__(self, message: str = "work item not found", *, code: str = "not-found") -> None:
        super().__init__(message)
        self.code = code


class WorkloreForbidden(Exception):
    """A Worklore caller is not allowed to perform this mutation."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mint_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(12)}"


def _pragma_foreign_keys(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")


def _in_transaction(conn: sqlite3.Connection, body: Callable[[], T]) -> T:
    _pragma_foreign_keys(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = body()
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return result


def _load_json_list(raw: object) -> list[Any]:
    if not raw:
        return []
    parsed = json.loads(str(raw))
    return parsed if isinstance(parsed, list) else []


def _review_after_future(value: object) -> bool:
    if value is None or value == "":
        return False
    if not isinstance(value, str):
        return True
    try:
        parsed = as_datetime(value)
    except ValueError:
        return True
    return parsed > datetime.now(timezone.utc)


def _empty_link_summary() -> dict[str, Any]:
    return {
        "blocking_policy": None,
        "source_acceptance": [],
        "has_source_link": False,
        "has_eligible_non_stale_source_link": False,
    }


def _source_policies(summary: Mapping[str, Any]) -> list[str]:
    """Return the bounded source-policy sequence ``exclusion_bucket`` reads.

    Native work has no imported source link and remains eligible. Imported work needs at
    least one eligible non-stale source link; a stale source is no longer authority for burn.
    """
    if summary.get("has_source_link") and not summary.get("has_eligible_non_stale_source_link"):
        return ["stale-source"]
    policy = summary.get("blocking_policy")
    return [] if policy is None else [str(policy)]


def _row_to_item(row: Any, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Own acceptance wins; otherwise the oldest eligible source link is authoritative, empty or not."""
    values = dict(zip(_ITEM_COLUMNS, row, strict=True))
    acceptance = _load_json_list(values.pop("acceptance_json"))
    return {
        **values,
        "burn_eligible": bool(values["burn_eligible"]),
        "attempt_count": int(values["attempt_count"]),
        "version": int(values["version"]),
        "acceptance": acceptance,
        "effective_acceptance": list(acceptance) if acceptance else list(summary["source_acceptance"]),
    }


def _select_item_row(conn: sqlite3.Connection, work_id: str) -> Any:
    row = conn.execute(
        f"SELECT {', '.join(_ITEM_COLUMNS)} FROM work_items WHERE work_id = ?",
        (work_id,),
    ).fetchone()
    if row is None:
        raise WorkloreNotFound()
    return row


def _require_version(item: Mapping[str, Any], expected_version: int) -> None:
    if int(item["version"]) != int(expected_version):
        raise WorkloreConflict("work item version does not match", code="version-conflict")


def _append_event(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    actor_type: str,
    actor_id: str | None,
    now: str,
    node_id: str | None = None,
    run_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    placeholders = ", ".join(["?"] * len(_EVENT_COLUMNS) + [_EVENT_SEQ_SQL])
    conn.execute(
        f"INSERT INTO work_events ({', '.join(_EVENT_INSERT_COLUMNS)}) VALUES ({placeholders})",
        (
            work_id,
            _mint_id("evt-"),
            event_type,
            from_status,
            to_status,
            actor_type,
            actor_id,
            node_id,
            run_id,
            json.dumps(dict(detail) if detail is not None else {}),
            now,
            now,
        ),
    )


def _insert_item(conn: sqlite3.Connection, item: Mapping[str, Any]) -> None:
    conn.execute(
        f"INSERT INTO work_items ({', '.join(_ITEM_COLUMNS)}) VALUES ({', '.join('?' for _ in _ITEM_COLUMNS)})",
        (
            item["work_id"],
            item["title"],
            item["description"],
            item["kind"],
            item["scope"],
            item["status"],
            item["priority"],
            1 if item["burn_eligible"] else 0,
            item["burn_rank"],
            item["token_appetite"],
            item["execution_mode"],
            json.dumps(item["acceptance"]),
            item["blocker"],
            item["review_after"],
            item["spend_by"],
            item["ready_at"],
            item["attempt_count"],
            item["version"],
            item["created_at"],
            item["updated_at"],
            item["archived_at"],
        ),
    )


def _update_item(conn: sqlite3.Connection, item: Mapping[str, Any]) -> None:
    assignments = [name for name in _ITEM_COLUMNS if name != "work_id"]
    conn.execute(
        f"UPDATE work_items SET {', '.join(f'{name} = ?' for name in assignments)} WHERE work_id = ?",
        (
            item["title"],
            item["description"],
            item["kind"],
            item["scope"],
            item["status"],
            item["priority"],
            1 if item["burn_eligible"] else 0,
            item["burn_rank"],
            item["token_appetite"],
            item["execution_mode"],
            json.dumps(item["acceptance"]),
            item["blocker"],
            item["review_after"],
            item["spend_by"],
            item["ready_at"],
            item["attempt_count"],
            item["version"],
            item["created_at"],
            item["updated_at"],
            item["archived_at"],
            item["work_id"],
        ),
    )


def _item_from_create(parsed: Mapping[str, Any], *, work_id: str, now: str) -> dict[str, Any]:
    return {
        "work_id": work_id,
        "title": parsed["title"],
        "description": parsed["description"],
        "kind": parsed["kind"],
        "scope": parsed["scope"],
        "status": parsed["status"],
        "priority": parsed["priority"],
        "burn_eligible": bool(parsed["burn_eligible"]),
        "burn_rank": parsed["burn_rank"],
        "token_appetite": parsed["token_appetite"],
        "execution_mode": parsed["execution_mode"],
        "acceptance": list(parsed["acceptance"]),
        "effective_acceptance": list(parsed["acceptance"]),
        "blocker": parsed["blocker"],
        "review_after": parsed["review_after"],
        "spend_by": parsed["spend_by"],
        "ready_at": None,
        "attempt_count": 0,
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
    }


def _parse_patch(item: Mapping[str, Any], raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkloreValidationError("patch body must be an object", code="field-bound")
    unknown = [name for name in raw if name not in PATCH_FIELDS]
    if unknown:
        raise WorkloreValidationError(f"unknown field: {unknown[0]}", code="unknown-field")
    merged = {
        "title": item["title"],
        "kind": item["kind"],
        "description": item["description"],
        "scope": item["scope"],
        "priority": item["priority"],
        "burn_eligible": item["burn_eligible"],
        "burn_rank": item["burn_rank"],
        "token_appetite": item["token_appetite"],
        "execution_mode": item["execution_mode"],
        "acceptance": item["acceptance"],
        "blocker": item["blocker"],
        "review_after": item["review_after"],
        "spend_by": item["spend_by"],
    }
    merged.update(raw)
    return parse_create(merged)


def exclusion_bucket(item: Mapping[str, Any], source_policies: Sequence[str]) -> str | None:
    """Return the first burn-queue exclusion bucket an item falls into, or None when it is eligible."""
    if item["status"] != "ready":
        return "not-ready"
    if not item["burn_eligible"]:
        return "not-eligible"
    acceptance = item.get("effective_acceptance") or []
    if not isinstance(acceptance, list) or not acceptance or len(acceptance) > ACCEPTANCE_MAX_ITEMS:
        return "acceptance-required"
    if item["execution_mode"] == "manual":
        return "manual-mode"
    blocker = item.get("blocker")
    if isinstance(blocker, str) and blocker:
        return "blocker"
    if int(item["attempt_count"]) >= 2:
        return "attempt-limit"
    if _review_after_future(item.get("review_after")):
        return "review-after"
    if any(policy != "eligible" for policy in source_policies):
        return "source-policy"
    return None


def _loaded_item(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    return _row_to_item(_select_item_row(conn, work_id), _link_summary(conn, work_id))


def _field_bound(message: str) -> WorkloreValidationError:
    return WorkloreValidationError(message, code="field-bound")


def _require_text(value: object, field: str, *, max_len: int, min_len: int = 1) -> str:
    """One canonical bounded-string check for every value the hub durably stores."""
    return safe_text(value, field, max_len=max_len, min_len=min_len)


def _optional_text(value: object, field: str, *, max_len: int) -> str | None:
    return safe_optional_text(value, field, max_len=max_len)


def _https_url(value: object, field: str = "url") -> str | None:
    return safe_https_url(value, field)


def observation_fingerprint(observations: Sequence[Mapping[str, Any]]) -> str:
    """Return the canonical SHA-256 fingerprint used for import idempotency."""
    canonical = json.dumps(list(observations), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _observation_fingerprint(observations: Sequence[Mapping[str, Any]]) -> str:
    return observation_fingerprint(observations)


def _parse_observation(raw: object, *, source_type: str) -> dict[str, Any]:
    """The one canonical observation validator, run before every imported create and update.

    Every durable string is checked by the same primitives, so a batch that creates a link
    and a later batch that updates it are held to identical bounds and private-data rules.
    """
    if not isinstance(raw, Mapping):
        raise _field_bound("observation must be an object")
    unknown = [name for name in raw if name not in OBSERVATION_FIELDS]
    if unknown:
        raise WorkloreValidationError(f"unknown field: {unknown[0]}", code="unknown-field")
    for field in ("external_key", "link_type", "title", "source_policy"):
        if field not in raw:
            raise _field_bound(f"{field} is required")
    link_type = raw["link_type"]
    if link_type not in IMPORT_LINK_TYPES:
        raise _field_bound("link_type must be github or brigade")
    if link_type != source_type:
        raise _field_bound("link_type must match the batch source_type")
    source_policy = raw["source_policy"]
    if source_policy not in SOURCE_POLICIES:
        raise _field_bound("source_policy must be an allowed source policy")
    acceptance = raw.get("acceptance", [])
    if acceptance is None:
        acceptance = []
    if not isinstance(acceptance, list):
        raise _field_bound("acceptance must be an array of strings")
    if len(acceptance) > ACCEPTANCE_MAX_ITEMS:
        raise _field_bound(f"acceptance must have at most {ACCEPTANCE_MAX_ITEMS} items")
    acceptance = [
        _require_text(item, f"acceptance[{index}]", max_len=ACCEPTANCE_ITEM_MAX)
        for index, item in enumerate(acceptance)
    ]
    stale = raw.get("stale", False)
    if stale is None:
        stale = False
    if not isinstance(stale, bool):
        raise _field_bound("stale must be a boolean")
    proposed_status = raw.get("proposed_status")
    if proposed_status not in (None, "completed"):
        raise _field_bound("proposed_status must be completed when present")
    dependencies = raw.get("dependencies")
    if dependencies is not None:
        if not isinstance(dependencies, list):
            raise _field_bound("dependencies must be an array")
        if len(dependencies) > DEPENDENCIES_MAX_ITEMS:
            raise _field_bound(f"dependencies must have at most {DEPENDENCIES_MAX_ITEMS} items")
        parsed_deps: list[dict[str, str]] = []
        for item in dependencies:
            if not isinstance(item, Mapping) or "type" not in item or "id" not in item:
                raise _field_bound("dependencies items must have type and id")
            parsed_deps.append(
                {
                    "type": _require_text(item["type"], "dependencies.type", max_len=128),
                    "id": _require_text(item["id"], "dependencies.id", max_len=128),
                }
            )
        dependencies = parsed_deps
    evidence_refs = raw.get("evidence_refs")
    if evidence_refs is not None:
        if isinstance(evidence_refs, list) and len(evidence_refs) > EVIDENCE_REFS_MAX_ITEMS:
            raise _field_bound(f"evidence_refs must have at most {EVIDENCE_REFS_MAX_ITEMS} items")
        if not isinstance(evidence_refs, list) or any(
            not isinstance(item, str) or not EVIDENCE_REF_RE.fullmatch(item) for item in evidence_refs
        ):
            raise _field_bound("evidence_refs items must match the evidence token pattern")
    return {
        "external_key": _require_text(raw["external_key"], "external_key", max_len=EXTERNAL_KEY_MAX),
        "link_type": str(link_type),
        "title": _require_text(raw["title"], "title", max_len=TITLE_MAX),
        "source_policy": str(source_policy),
        "description": _optional_text(raw.get("description"), "description", max_len=DESCRIPTION_MAX),
        "acceptance": list(acceptance),
        "external_state": _optional_text(raw.get("external_state"), "external_state", max_len=EXTERNAL_STATE_MAX),
        "external_updated_at": _optional_text(raw.get("external_updated_at"), "external_updated_at", max_len=64),
        "url": _https_url(raw.get("url")),
        "display_ref": _optional_text(raw.get("display_ref"), "display_ref", max_len=DISPLAY_REF_MAX),
        "proposed_status": proposed_status,
        "priority": _optional_text(raw.get("priority"), "priority", max_len=64),
        "dependencies": dependencies,
        "evidence_refs": list(evidence_refs) if evidence_refs is not None else None,
        "stale": stale,
    }


def _event_from_page_row(row: Any) -> dict[str, Any]:
    """Project a paged row, dropping the private insertion sequence from the response."""
    return _event_from_row(tuple(row)[: len(_EVENT_COLUMNS)])


def _cursor_seq(values: Mapping[str, str]) -> int:
    raw = values["seq"]
    if not raw.isdigit():
        raise _field_bound("cursor is invalid")
    return int(raw)


def _event_from_row(row: Any) -> dict[str, Any]:
    values = dict(zip(_EVENT_COLUMNS, row, strict=True))
    detail = json.loads(str(values.pop("detail_json") or "{}"))
    return {**values, "detail": detail if isinstance(detail, dict) else {}}


def _row_to_link(row: Any) -> dict[str, Any]:
    values = dict(zip(_LINK_COLUMNS, row, strict=True))
    return {**values, "source_acceptance": _load_json_list(values.pop("source_acceptance_json"))}


def _full_link_rows(conn: sqlite3.Connection, work_id: str) -> list[Any]:
    return list(
        conn.execute(
            f"SELECT {', '.join(_LINK_COLUMNS)} FROM work_links WHERE work_id = ? ORDER BY synced_at ASC, link_id ASC",
            (work_id,),
        )
    )


def _links_by_work_id(
    conn: sqlite3.Connection,
    work_ids: Sequence[str],
    columns: tuple[str, ...],
    *,
    per_item_limit: int,
) -> tuple[dict[str, list[Any]], set[str]]:
    """Group up to ``per_item_limit`` link rows per item, in synced-at order.

    The cut happens in SQL, not in Python: every item's indexed query returns at most
    ``per_item_limit + 1`` rows, the extra one being the only evidence the projection needs
    that the set was truncated. A page therefore costs its own budget even against a legacy
    item that accumulated links before ``ITEM_MAX_LINKS`` was enforced on write, which is
    exactly the row set Python must never be handed.

    The second return value names the items whose link set was cut, so the caller can
    report the truncation instead of silently shortening it.
    """
    grouped: dict[str, list[Any]] = {work_id: [] for work_id in work_ids}
    truncated: set[str] = set()
    if per_item_limit <= 0:
        return grouped, set(work_ids)
    for work_id in work_ids:
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM work_links WHERE work_id = ? "
            "ORDER BY synced_at ASC, link_id ASC LIMIT ?",
            (work_id, per_item_limit + 1),
        ).fetchall()
        grouped[work_id] = rows[:per_item_limit]
        if len(rows) > per_item_limit:
            truncated.add(work_id)
    return grouped, truncated


def _claim_item_link(conn: sqlite3.Connection, work_id: str, *, code: str) -> None:
    """Refuse a new link once one item is carrying ``ITEM_MAX_LINKS`` of them.

    Read inside the writing transaction, so two concurrent writers cannot both pass the
    ceiling. Every link path claims from this one count: operator links, adapter imports,
    and fleet-run links.
    """
    count = int(conn.execute("SELECT COUNT(*) FROM work_links WHERE work_id = ?", (work_id,)).fetchone()[0])
    if count >= ITEM_MAX_LINKS:
        raise WorkloreConflict(f"work item has reached its {ITEM_MAX_LINKS} link ceiling", code=code)


def _has_source_link(conn: sqlite3.Connection, work_id: str) -> bool:
    placeholders = ", ".join("?" for _ in IMPORT_LINK_TYPES)
    row = conn.execute(
        f"SELECT 1 FROM work_links WHERE work_id = ? AND link_type IN ({placeholders}) LIMIT 1",
        (work_id, *sorted(IMPORT_LINK_TYPES)),
    ).fetchone()
    return row is not None


def _find_link(conn: sqlite3.Connection, link_type: str, external_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(_LINK_COLUMNS)} FROM work_links WHERE link_type = ? AND external_key = ?",
        (link_type, external_key),
    ).fetchone()
    return _row_to_link(row) if row is not None else None


def _link_claim(
    conn: sqlite3.Connection, work_id: str, link_type: str, external_key: str
) -> tuple[str, str | None, str | None] | None:
    """Return ``(adapter_id, owner_node, source_version)`` for a claimed identity, or None.

    The identity is ``(work_id, link_type, external_key)``, never the external key alone.
    One item may carry the same key under two link types, an operator ``url`` link and an
    adapter-owned ``github`` link, say, and those are two separate identities: reading the
    key alone would let either one answer for the other, which is how an adapter could
    reach across types to claim an operator link and how an eligible adapter link could
    block the operator from deleting their own url.

    None means this identity is operator managed: no adapter has ever claimed it, so no
    adapter may adopt or mutate it. Ownership and the source high-water are read from the
    same link row in one indexed lookup, no matter how long the item's event history grows.
    """
    row = conn.execute(
        f"SELECT {', '.join(_LINK_CLAIM_COLUMNS)} FROM work_links "
        "WHERE work_id = ? AND link_type = ? AND external_key = ?",
        (work_id, link_type, external_key),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0]), None if row[1] is None else str(row[1]), None if row[2] is None else str(row[2])


def _owning_adapter(
    conn: sqlite3.Connection, work_id: str, link_type: str, external_key: str
) -> tuple[str, str | None] | None:
    """Return the ``(adapter_id, owner_node)`` that first imported this identity, or None."""
    claim = _link_claim(conn, work_id, link_type, external_key)
    return None if claim is None else (claim[0], claim[1])


def _source_version(value: object) -> datetime | None:
    """Parse a source-reported timestamp into a comparable instant, or None.

    ``external_updated_at`` is stored as bounded free text because sources disagree about
    format, so an unparseable value is treated exactly like an absent one: it never
    establishes a high-water and never satisfies one. Refusing to compare is the safe
    direction, because the only thing a bad parse could otherwise authorize is a rollback.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_datetime(value)
    except ValueError:
        return None


def _revision_refusal(stored: object, incoming: object, *, changed: bool, stale_transition: bool = False) -> str | None:
    """Return why ``incoming`` must not be applied over the stored high-water, or None.

    An observation may write the projection only when the source proves which revision it
    is speaking for. A parseable ``external_updated_at`` is that proof, and it is required
    before any mutation, not merely used to break ties between two proofs:

    - No usable incoming revision: the source proved nothing, so it may not create the
      identity and it may not change one. Refusing is what makes the guard total. If an
      unproven change were applied whenever no high-water existed yet, then A -> expire ->
      replay A would roll the projection back for exactly the identities that never had a
      guard, which is the rollback this function exists to prevent. The refusal is
      reported per identity rather than folded into the unchanged count, because "we
      silently kept the old projection" and "nothing was different" are not the same
      answer to an operator.
    - Older than the high-water: an expired-key replay or an out-of-order delivery.
    - Equal to the high-water but carrying a different payload: two different payloads
      cannot both be the state at one revision, so the one already projected stands. This
      is what stops A -> expire -> B -> replay A from rolling back when a source stamps
      both observations with the same timestamp. A direct GitHub 404 is the exception:
      moving an identity from live to stale is monotonic, so it may add that one-way marker
      at the stored provider revision. An equal revision can never clear the marker.

    An observation that changes nothing is never refused, with or without a revision:
    applying nothing cannot roll anything back, so there is no state for a revision to
    protect, and refusing it would make every re-poll of a settled identity noisy. That is
    the only case a missing revision survives, and it is safe precisely because it writes
    no projection. ``stored`` is ``None`` on creation, which is a change like any other:
    an unproven observation does not get to mint an item.
    """
    if not changed:
        return None
    candidate = _source_version(incoming)
    if candidate is None:
        return REFUSED_REVISION_MISSING
    high_water = _source_version(stored)
    if high_water is None:
        return None
    if candidate < high_water:
        return REFUSED_REVISION_STALE
    if candidate == high_water and not stale_transition:
        return REFUSED_REVISION_NOT_ADVANCED
    return None


def _insert_link(
    conn: sqlite3.Connection,
    link: Mapping[str, Any],
    *,
    adapter_id: str | None = None,
    owner_node: str | None = None,
    source_version: str | None = None,
) -> None:
    """Insert one link, stamping adapter ownership and the first source high-water on it.

    Operator and fleet-run links carry neither: no adapter owns them, so no adapter may
    later mutate them, and there is nothing for a replay to roll back.
    """
    columns = (*_LINK_COLUMNS, *_LINK_CLAIM_COLUMNS)
    conn.execute(
        f"INSERT INTO work_links ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        (
            link["link_id"],
            link["work_id"],
            link["link_type"],
            link["external_key"],
            link.get("display_ref"),
            link.get("url"),
            link.get("external_state"),
            link.get("external_updated_at"),
            link["source_policy"],
            json.dumps(list(link.get("source_acceptance") or [])),
            link["synced_at"],
            link.get("stale_at"),
            adapter_id,
            owner_node,
            source_version,
        ),
    )


def _import_headroom(conn: sqlite3.Connection, *, adapter_id: str, owner_node: str) -> Callable[[], None]:
    """Return a claim function that refuses a new imported identity past the storage ceilings.

    The two counts are taken once per batch and then tracked in memory, so a 500
    observation batch costs two indexed counts rather than one per observation.
    """
    counts = [
        int(conn.execute("SELECT COUNT(*) FROM work_links WHERE adapter_id = ?", (adapter_id,)).fetchone()[0]),
        int(conn.execute("SELECT COUNT(*) FROM work_links WHERE owner_node = ?", (owner_node,)).fetchone()[0]),
    ]

    def _claim() -> None:
        if counts[0] >= ADAPTER_IMPORT_MAX_LINKS:
            raise WorkloreConflict(
                f"adapter has reached its {ADAPTER_IMPORT_MAX_LINKS} imported item ceiling",
                code="import-conflict",
            )
        if counts[1] >= OWNER_IMPORT_MAX_LINKS:
            raise WorkloreConflict(
                f"owner node has reached its {OWNER_IMPORT_MAX_LINKS} imported item ceiling",
                code="import-conflict",
            )
        counts[0] += 1
        counts[1] += 1

    return _claim


def _update_link(conn: sqlite3.Connection, link: Mapping[str, Any], *, source_version: str | None = None) -> None:
    """Apply an accepted observation to a link and advance its source high-water.

    ``COALESCE`` keeps the stored high-water when this observation carried no usable
    timestamp, so a source that goes quiet about ``external_updated_at`` can never erase
    the guard that an earlier, timestamped observation established.
    """
    conn.execute(
        "UPDATE work_links SET display_ref = ?, url = ?, external_state = "
        "CASE WHEN ? = 1 AND ? IS NULL THEN external_state ELSE ? END, external_updated_at = ?, "
        "source_policy = ?, source_acceptance_json = ?, synced_at = ?, stale_at = ?, "
        "source_version = COALESCE(?, source_version) WHERE link_id = ?",
        (
            link.get("display_ref"),
            link.get("url"),
            1 if link.get("stale_at") is not None else 0,
            link.get("external_state"),
            link.get("external_state"),
            link.get("external_updated_at"),
            link["source_policy"],
            json.dumps(list(link.get("source_acceptance") or [])),
            link["synced_at"],
            link.get("stale_at"),
            source_version,
            link["link_id"],
        ),
    )


def _touch_link_synced_at(conn: sqlite3.Connection, link_id: str, now: str) -> None:
    """Record that an unchanged observation was seen without adding events or bumping the item."""
    conn.execute("UPDATE work_links SET synced_at = ? WHERE link_id = ?", (now, link_id))


def _observation_detail(adapter_id: str, parsed: Mapping[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "adapter_id": adapter_id,
        "external_key": parsed["external_key"],
        "link_type": parsed["link_type"],
    }
    if parsed.get("proposed_status") is not None:
        detail["proposed_status"] = parsed["proposed_status"]
    if parsed.get("priority") is not None:
        detail["priority"] = parsed["priority"]
    if parsed.get("dependencies") is not None:
        detail["dependencies"] = parsed["dependencies"]
    if parsed.get("evidence_refs") is not None:
        detail["evidence_refs"] = parsed["evidence_refs"]
    return detail


def _link_from_observation(parsed: Mapping[str, Any], *, work_id: str, link_id: str, now: str) -> dict[str, Any]:
    return {
        "link_id": link_id,
        "work_id": work_id,
        "link_type": parsed["link_type"],
        "external_key": parsed["external_key"],
        "display_ref": parsed.get("display_ref") or parsed["external_key"],
        "url": parsed.get("url"),
        "external_state": parsed.get("external_state"),
        "external_updated_at": parsed.get("external_updated_at"),
        "source_policy": parsed["source_policy"],
        "source_acceptance": list(parsed.get("acceptance") or []),
        "synced_at": now,
        "stale_at": now if parsed.get("stale") else None,
    }


def _observed_source_version(parsed: Mapping[str, Any]) -> str | None:
    """The high-water an accepted observation establishes, or None when it proves nothing.

    Only a parseable timestamp is stored, so the column holds a value ``_source_version``
    can always compare and a source cannot poison the guard with unparseable text.
    """
    raw = parsed.get("external_updated_at")
    return str(raw) if _source_version(raw) is not None else None


def _observation_link_projection(current_link: Mapping[str, Any], parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Return the source-link projection an observation is allowed to change.

    A direct 404 tombstone has no current external state. Retaining that existing value
    lets its equal-revision transition add only the durable stale marker.
    """
    return {
        "display_ref": parsed.get("display_ref") or parsed["external_key"],
        "url": parsed.get("url"),
        "external_state": (
            current_link["external_state"]
            if parsed.get("stale") and parsed.get("external_state") is None
            else parsed.get("external_state")
        ),
        "external_updated_at": parsed.get("external_updated_at"),
        "source_policy": parsed["source_policy"],
        "source_acceptance": list(parsed.get("acceptance") or []),
    }


def _observation_changed(
    current_item: Mapping[str, Any], current_link: Mapping[str, Any], parsed: Mapping[str, Any]
) -> bool:
    """Return whether an observation changes its source-link projection.

    The item title is separately revision-gated by the importer. That keeps an equal
    source poll from clobbering an operator title edit while still allowing a newer source
    revision to advance the title projection.
    """
    expected = _observation_link_projection(current_link, parsed)
    return any(current_link[field] != value for field, value in expected.items()) or (
        current_link["stale_at"] is not None
    ) != bool(parsed.get("stale"))


def _one_way_stale_transition(
    current_item: Mapping[str, Any], current_link: Mapping[str, Any], parsed: Mapping[str, Any]
) -> bool:
    """Return whether this observation changes only a live source link to stale."""
    return (
        bool(parsed.get("stale"))
        and current_link["stale_at"] is None
        and all(
            current_link[field] == value for field, value in _observation_link_projection(current_link, parsed).items()
        )
    )


def _items_for_observations(
    conn: sqlite3.Connection, observations: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for observation in observations:
        link = _find_link(conn, str(observation["link_type"]), str(observation["external_key"]))
        if link is not None:
            items.append({"work_id": str(link["work_id"]), "external_key": str(link["external_key"])})
    return items


def _touch_cursor(
    conn: sqlite3.Connection,
    *,
    adapter_id: str,
    source_type: str,
    owner_node: str,
    now: str,
) -> None:
    existing = conn.execute(
        "SELECT adapter_id FROM work_sync_cursors WHERE adapter_id = ?",
        (adapter_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO work_sync_cursors (adapter_id, source_type, owner_node, cursor, "
            "last_success_at, last_error_code, updated_at) VALUES (?, ?, ?, NULL, ?, NULL, ?)",
            (adapter_id, source_type, owner_node, now, now),
        )
        return
    conn.execute(
        "UPDATE work_sync_cursors SET last_success_at = ?, updated_at = ? WHERE adapter_id = ?",
        (now, now, adapter_id),
    )


def _require_adapter_namespace(adapter_id: str, source_type: str) -> None:
    """Pin an adapter to its own ``<source_type>:`` namespace.

    Without this a ``brigade`` adapter could claim ``github:escoffier-labs`` and take the
    cursor ownership that keeps the real GitHub adapter's imports separate.
    """
    prefix = f"{source_type}:"
    if not adapter_id.startswith(prefix) or not ADAPTER_SUFFIX_RE.fullmatch(adapter_id[len(prefix) :]):
        raise _field_bound(f"adapter_id must be {prefix} followed by a bounded identifier")


def _cursor_owner(conn: sqlite3.Connection, adapter_id: str) -> str | None:
    row = conn.execute(
        "SELECT owner_node FROM work_sync_cursors WHERE adapter_id = ?",
        (adapter_id,),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _require_owned_fleet_run(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    node_id: str,
    run_id: str | None,
) -> None:
    if not run_id:
        raise WorkloreForbidden("node attempt requires an owned fleet-run", code="attempt-forbidden")
    row = conn.execute(
        "SELECT 1 FROM work_links WHERE work_id = ? AND link_type = 'fleet-run' AND external_key = ?",
        (work_id, f"{node_id}/{run_id}"),
    ).fetchone()
    if row is None:
        raise WorkloreForbidden("node attempt requires an owned fleet-run", code="attempt-forbidden")


def _hub_run_exists(conn: sqlite3.Connection, node_id: str, run_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE node_id = ? AND run_id = ? LIMIT 1",
        (node_id, run_id),
    ).fetchone()
    return row is not None


def _backfill_link_owners(conn: sqlite3.Connection) -> None:
    """Copy adapter ownership from the event log onto the link row it describes.

    Ownership used to be re-derived by walking an item's whole event history on every
    observation. This one-time pass keeps the same first-claim-wins answer while making
    every later lookup a single indexed read.
    """
    claimed: set[tuple[str, str, str]] = set()
    for work_id, detail_json, node_id in conn.execute(
        "SELECT work_id, detail_json, node_id FROM work_events ORDER BY seq ASC"
    ).fetchall():
        try:
            detail = json.loads(str(detail_json or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(detail, Mapping):
            continue
        external_key = detail.get("external_key")
        adapter_id = detail.get("adapter_id")
        link_type = detail.get("link_type")
        if not isinstance(external_key, str) or not isinstance(adapter_id, str) or not adapter_id:
            continue
        # An adapter can only ever have claimed an import link type, and the identity it
        # claimed is (work_id, link_type, external_key). An event that does not name its
        # link type cannot prove which identity it claimed, so it stamps nothing rather
        # than risk stamping an operator link that happens to share the key.
        if not isinstance(link_type, str) or link_type not in IMPORT_LINK_TYPES:
            continue
        claim = (str(work_id), link_type, external_key)
        if claim in claimed:
            continue
        claimed.add(claim)
        conn.execute(
            "UPDATE work_links SET adapter_id = ?, owner_node = ? "
            "WHERE work_id = ? AND link_type = ? AND external_key = ?",
            (adapter_id, None if node_id is None else str(node_id), str(work_id), link_type, external_key),
        )


def _backfill_import_key_owners(conn: sqlite3.Connection) -> None:
    """Stamp existing import keys with the node that owns their adapter.

    The owning node already lives on ``work_sync_cursors``, so the per-owner retention
    ceiling applies to keys written before the column existed instead of exempting them.
    """
    conn.execute(
        "UPDATE work_import_keys SET owner_node = "
        "(SELECT cursors.owner_node FROM work_sync_cursors cursors WHERE cursors.adapter_id = "
        "work_import_keys.adapter_id) WHERE owner_node IS NULL"
    )


def _trim_import_keys(conn: sqlite3.Connection, *, adapter_id: str, owner_node: str) -> None:
    """Expire import keys past the retention window for this adapter and its owning node.

    Newest first, so every key inside the window keeps its exact stored counts and an
    honest replay still short-circuits. Both deletes run in the import transaction.
    """
    for column, value, keep in (
        ("adapter_id", adapter_id, ADAPTER_IMPORT_KEY_RETENTION),
        ("owner_node", owner_node, OWNER_IMPORT_KEY_RETENTION),
    ):
        conn.execute(
            f"DELETE FROM work_import_keys WHERE {column} = ? AND rowid NOT IN ("
            f"SELECT rowid FROM work_import_keys WHERE {column} = ? "
            "ORDER BY received_at DESC, rowid DESC LIMIT ?)",
            (value, value, keep),
        )


def _trim_adapter_events(conn: sqlite3.Connection, work_id: str) -> None:
    """Keep only the most recent adapter observation events on one item.

    This is the whole adapter event-storage policy: ``sync-observed`` and ``sync-stale``
    written by an adapter expire past ``ADAPTER_SYNC_EVENT_RETENTION`` per item. Lifecycle
    events, and any event an operator or node wrote, are never removed.
    """
    placeholders = ", ".join("?" for _ in ADAPTER_SYNC_EVENT_TYPES)
    scope = f"work_id = ? AND actor_type = 'adapter' AND event_type IN ({placeholders})"
    conn.execute(
        f"DELETE FROM work_events WHERE {scope} AND seq NOT IN ("
        f"SELECT seq FROM work_events WHERE {scope} ORDER BY seq DESC LIMIT ?)",
        (work_id, *ADAPTER_SYNC_EVENT_TYPES, work_id, *ADAPTER_SYNC_EVENT_TYPES, ADAPTER_SYNC_EVENT_RETENTION),
    )


def _prune_import_keys_to_quota(conn: sqlite3.Connection) -> None:
    """Cut any adapter or owner whose import keys are over the retention window back to it.

    ``_trim_import_keys`` only runs for the adapter and owner an import touched, so keys a
    hub accumulated before retention existed, or under an adapter that has since gone
    quiet, would sit outside the quota forever. The over-quota partitions are found first
    with an indexed aggregate rather than by ranking every row: on a healthy hub that
    aggregate returns nothing and the pass writes nothing at all. Any key that survives
    keeps its exact stored counts, so an honest replay still short-circuits. NULL owners
    partition together, which bounds them too.
    """
    for column, keep in (
        ("adapter_id", ADAPTER_IMPORT_KEY_RETENTION),
        ("owner_node", OWNER_IMPORT_KEY_RETENTION),
    ):
        over_quota = conn.execute(
            f"SELECT {column} FROM work_import_keys GROUP BY {column} HAVING COUNT(*) > ?",
            (keep,),
        ).fetchall()
        for row in over_quota:
            match = "IS NULL" if row[0] is None else "= ?"
            values: tuple[object, ...] = () if row[0] is None else (row[0],)
            conn.execute(
                f"DELETE FROM work_import_keys WHERE {column} {match} AND rowid NOT IN ("
                f"SELECT rowid FROM work_import_keys WHERE {column} {match} "
                "ORDER BY received_at DESC, rowid DESC LIMIT ?)",
                (*values, *values, keep),
            )


def _prune_adapter_events_to_quota(conn: sqlite3.Connection) -> None:
    """Cut any item whose adapter observation events are over the per-item window back to it.

    Same reason and same shape as the import keys: ``_trim_adapter_events`` only runs for
    items an import touched, and the over-quota items are found with an aggregate the
    ``(work_id, actor_type, event_type, seq)`` index answers, so a healthy hub does not walk
    its event log. Lifecycle events, and any event an operator or node wrote, are never
    removed.
    """
    placeholders = ", ".join("?" for _ in ADAPTER_SYNC_EVENT_TYPES)
    over_quota = conn.execute(
        f"SELECT work_id FROM work_events WHERE actor_type = 'adapter' AND event_type IN ({placeholders}) "
        "GROUP BY work_id HAVING COUNT(*) > ?",
        (*ADAPTER_SYNC_EVENT_TYPES, ADAPTER_SYNC_EVENT_RETENTION),
    ).fetchall()
    for row in over_quota:
        _trim_adapter_events(conn, str(row[0]))


def _trim_fleet_run_links(conn: sqlite3.Connection, work_id: str) -> None:
    """Expire one item's oldest fleet-run links so a new run always has room to link.

    Import keys and adapter events expire for the same reason: they are the classes a
    running fleet grows on its own. Newest first by ``synced_at`` then insertion order, so
    the recent execution history an operator actually reads survives. This writes no
    ``unlinked`` event; retention is storage policy, not an operator decision, and an
    ``unlinked`` event per run would simply move the unbounded growth into the event log.
    """
    keep = max(FLEET_RUN_LINK_RETENTION - 1, 0)
    conn.execute(
        "DELETE FROM work_links WHERE work_id = ? AND link_type = 'fleet-run' AND rowid NOT IN ("
        "SELECT rowid FROM work_links WHERE work_id = ? AND link_type = 'fleet-run' "
        "ORDER BY synced_at DESC, rowid DESC LIMIT ?)",
        (work_id, work_id, keep),
    )


def _bootstrap_link_source_versions(conn: sqlite3.Connection) -> None:
    """Seed the per-identity high-water on imported links written before the column existed.

    v17 added ``source_version`` and deliberately left it NULL, on the reasoning that the
    stored ``external_updated_at`` is the last reported timestamp rather than the highest
    ever accepted. That reasoning left every already-imported identity with no guard at all
    until its source happened to send another timestamped observation, so an expired-key
    replay could roll a migrated identity straight back. The last reported timestamp is in
    fact the revision of the projection those rows currently hold, which is exactly what
    the guard compares against, so it is seeded here. Only parseable values are seeded, so
    the column keeps holding nothing the guard cannot compare.
    """
    rows = conn.execute(
        "SELECT link_id, external_updated_at FROM work_links "
        f"WHERE {_LINK_SOURCE_VERSION_COLUMN} IS NULL AND adapter_id IS NOT NULL AND external_updated_at IS NOT NULL"
    ).fetchall()
    seeded = [(str(row[0]), str(row[1])) for row in rows if _source_version(row[1]) is not None]
    for index in range(0, len(seeded), _SQL_VARIABLE_CHUNK):
        conn.executemany(
            f"UPDATE work_links SET {_LINK_SOURCE_VERSION_COLUMN} = ? WHERE link_id = ?",
            [(version, link_id) for link_id, version in seeded[index : index + _SQL_VARIABLE_CHUNK]],
        )


def _reconciled_version(conn: sqlite3.Connection) -> int:
    """The highest migration reconciliation this database has completed, 0 when none."""
    try:
        row = conn.execute(f"SELECT value FROM work_schema_meta WHERE key = '{_RECONCILED_VERSION_KEY}'").fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(str(row[0]))
    except ValueError:
        return 0


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the Worklore tables and named indexes, and reconcile pre-quota rows once.

    Migration is not only additive columns. A database written before a quota existed can
    hold rows that violate it, so import keys and adapter events are pruned to their
    retention windows and imported links are seeded with the source high-water they were
    upgraded without.

    The two reconciliations are paid for differently. Seeding the high-water reads every
    imported link, so it runs once: ``work_schema_meta`` records the version it completed
    and a hub that has already done the work skips straight past it. The quota passes stay
    on every start, because an adapter that goes quiet after a busy week leaves rows nothing
    else will ever revisit, but they no longer rank every row to find them: an indexed
    aggregate names the over-quota partitions first, and on a healthy hub it names none and
    the passes write nothing.

    Nothing here refuses a database. An item carrying more than ``ITEM_MAX_LINKS`` links is
    a legacy row, not a corrupt one: every read path cuts by SQL rather than trusting the
    ceiling, new writes still enforce it, and an operator repairs the item by unlinking
    through the ordinary route. Refusing at startup instead would take the hub down and
    leave no running route to repair it with.

    A caller that already owns a transaction, which is how Fleet Hub applies its schema,
    keeps it; otherwise the migration commits its own work and rolls back cleanly rather
    than stranding an open write.
    """
    owns_transaction = not conn.in_transaction
    try:
        _apply_worklore_schema(conn)
    except BaseException:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise
    if owns_transaction and conn.in_transaction:
        conn.commit()


def _apply_worklore_schema(conn: sqlite3.Connection) -> None:
    _pragma_foreign_keys(conn)
    reconciled = _reconciled_version(conn)
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(work_import_keys)")}
    if "unchanged" not in columns:
        conn.execute("ALTER TABLE work_import_keys ADD COLUMN unchanged INTEGER NOT NULL DEFAULT 0")
    if "owner_node" not in columns:
        conn.execute("ALTER TABLE work_import_keys ADD COLUMN owner_node TEXT")
        _backfill_import_key_owners(conn)
    event_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(work_events)")}
    if "seq" not in event_columns:
        conn.execute("ALTER TABLE work_events ADD COLUMN seq INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE work_events SET seq = rowid")
    link_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(work_links)")}
    if "adapter_id" not in link_columns:
        conn.execute("ALTER TABLE work_links ADD COLUMN adapter_id TEXT")
        conn.execute("ALTER TABLE work_links ADD COLUMN owner_node TEXT")
        _backfill_link_owners(conn)
    if _LINK_SOURCE_VERSION_COLUMN not in link_columns:
        conn.execute(f"ALTER TABLE work_links ADD COLUMN {_LINK_SOURCE_VERSION_COLUMN} TEXT")
    if "refused" not in columns:
        conn.execute("ALTER TABLE work_import_keys ADD COLUMN refused INTEGER NOT NULL DEFAULT 0")
    for statement in _INDEX_STATEMENTS:
        conn.execute(statement)
    if reconciled < WORKLORE_RECONCILE_VERSION:
        _bootstrap_link_source_versions(conn)
        conn.execute(
            "INSERT INTO work_schema_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_RECONCILED_VERSION_KEY, str(WORKLORE_RECONCILE_VERSION)),
        )
    _prune_import_keys_to_quota(conn)
    _prune_adapter_events_to_quota(conn)


def get_item(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    """Return one Worklore item projection."""
    _pragma_foreign_keys(conn)
    return _loaded_item(conn, work_id)


def list_events(conn: sqlite3.Connection, work_id: str) -> list[dict[str, Any]]:
    """Return events for one item in insertion order."""
    _pragma_foreign_keys(conn)
    rows = conn.execute(
        f"SELECT {', '.join(_EVENT_COLUMNS)} FROM work_events WHERE work_id = ? ORDER BY seq ASC",
        (work_id,),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def recent_events(conn: sqlite3.Connection, work_id: str, *, limit: int = DETAIL_RECENT_EVENTS) -> list[dict[str, Any]]:
    """Return the last ``limit`` events for one item, oldest first.

    The database does the ordering and the cut, so an item detail read costs the events it
    shows rather than every event the item has ever recorded.
    """
    _pragma_foreign_keys(conn)
    rows = conn.execute(
        f"SELECT {', '.join(_EVENT_COLUMNS)} FROM work_events WHERE work_id = ? ORDER BY seq DESC LIMIT ?",
        (work_id, max(int(limit), 0)),
    ).fetchall()
    return [_event_from_row(row) for row in reversed(rows)]


def create_item(
    conn: sqlite3.Connection,
    raw: object,
    *,
    actor_id: str,
    actor_type: str = "operator",
    node_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create a native Worklore item and its created event in one transaction."""
    parsed = parse_create(raw)
    key = None
    if idempotency_key is not None:
        key = _require_text(idempotency_key, "idempotency_key", max_len=IDEMPOTENCY_KEY_MAX)
    fingerprint = _create_fingerprint(parsed)

    def _write() -> dict[str, Any]:
        now = _utc_now()
        if key is not None:
            stored = conn.execute(
                "SELECT fingerprint, work_id FROM work_create_keys WHERE actor_id = ? AND idempotency_key = ?",
                (actor_id, key),
            ).fetchone()
            if stored is not None:
                if str(stored[0]) != fingerprint:
                    raise WorkloreConflict("idempotency key reused with a different payload", code="import-conflict")
                return _loaded_item(conn, str(stored[1]))
        item = _item_from_create(parsed, work_id=_mint_id("wl-"), now=now)
        _insert_item(conn, item)
        _append_event(
            conn,
            work_id=item["work_id"],
            event_type="created",
            from_status=None,
            to_status=item["status"],
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
            node_id=node_id,
        )
        if key is not None:
            conn.execute(
                "INSERT INTO work_create_keys (actor_id, idempotency_key, fingerprint, work_id, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (actor_id, key, fingerprint, item["work_id"], now),
            )
        return _loaded_item(conn, item["work_id"])

    return _in_transaction(conn, _write)


def _create_fingerprint(parsed: Mapping[str, Any]) -> str:
    payload = {name: parsed[name] for name in parsed if name != "status"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def patch_item(
    conn: sqlite3.Connection,
    work_id: str,
    raw: object,
    *,
    expected_version: int,
    actor_id: str,
    actor_type: str = "operator",
    node_id: str | None = None,
) -> dict[str, Any]:
    """Patch mutable fields and append an updated event in one transaction."""

    def _write() -> dict[str, Any]:
        current = _loaded_item(conn, work_id)
        _require_version(current, expected_version)
        parsed = _parse_patch(current, raw)
        now = _utc_now()
        updated = {
            **current,
            "title": parsed["title"],
            "description": parsed["description"],
            "scope": parsed["scope"],
            "priority": parsed["priority"],
            "burn_eligible": bool(parsed["burn_eligible"]),
            "burn_rank": parsed["burn_rank"],
            "token_appetite": parsed["token_appetite"],
            "execution_mode": parsed["execution_mode"],
            "acceptance": list(parsed["acceptance"]),
            "blocker": parsed["blocker"],
            "review_after": parsed["review_after"],
            "spend_by": parsed["spend_by"],
            "version": int(current["version"]) + 1,
            "updated_at": now,
        }
        _update_item(conn, updated)
        _append_event(
            conn,
            work_id=work_id,
            event_type="updated",
            from_status=current["status"],
            to_status=current["status"],
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
            node_id=node_id,
        )
        return _loaded_item(conn, work_id)

    return _in_transaction(conn, _write)


def transition(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    to_status: str,
    expected_version: int,
    actor_id: str,
    actor_type: str = "operator",
    node_id: str | None = None,
) -> dict[str, Any]:
    """Move one item along a legal lifecycle edge in one transaction."""

    def _write() -> dict[str, Any]:
        current = _loaded_item(conn, work_id)
        _require_version(current, expected_version)
        if not can_transition(str(current["status"]), to_status):
            raise WorkloreValidationError(
                f"cannot transition from {current['status']} to {to_status}",
                code="invalid-transition",
            )
        if to_status == "ready":
            assert_ready({"acceptance": current["effective_acceptance"]})
        now = _utc_now()
        ready_at = current["ready_at"]
        archived_at = current["archived_at"]
        if to_status == "ready" and current["status"] != "ready":
            ready_at = now
        if to_status == "archived":
            archived_at = now
        updated = {
            **current,
            "status": to_status,
            "ready_at": ready_at,
            "archived_at": archived_at,
            "version": int(current["version"]) + 1,
            "updated_at": now,
        }
        _update_item(conn, updated)
        _append_event(
            conn,
            work_id=work_id,
            event_type="transitioned",
            from_status=str(current["status"]),
            to_status=to_status,
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
            node_id=node_id,
        )
        return _loaded_item(conn, work_id)

    return _in_transaction(conn, _write)


def _burn_key(row: Any) -> tuple[Any, ...]:
    """The burn sort key for one item row, matching ``_BURN_KEY_SQL`` column for column."""
    work_id, spend_by, ready_at, burn_rank = str(row[0]), row[14], row[15], int(row[8])
    spend = "" if spend_by is None else str(spend_by)
    return (burn_rank, 1 if spend == "" else 0, spend, "" if ready_at is None else str(ready_at), work_id)


def _burn_cursor_key(values: Mapping[str, str]) -> tuple[Any, ...]:
    raw_rank = values["burn_rank"]
    if not raw_rank.isdigit():
        raise _field_bound("cursor is invalid")
    spend = values["spend_by"]
    return (int(raw_rank), 1 if spend == "" else 0, spend, values["ready_at"], values["work_id"])


def _burn_rows(conn: sqlite3.Connection, *, after: tuple[Any, ...] | None, limit: int) -> list[Any]:
    predicate = f" WHERE ({_BURN_KEY_SQL}) > (?, ?, ?, ?, ?)" if after is not None else ""
    params = (*(after or ()), limit)
    return list(
        conn.execute(
            f"SELECT {', '.join(_ITEM_COLUMNS)} FROM work_items{predicate} ORDER BY {_BURN_ORDER_SQL} LIMIT ?",
            params,
        )
    )


def burn_queue(
    conn: sqlite3.Connection,
    *,
    limit: object = BURN_PAGE_DEFAULT,
    cursor: object | None = None,
) -> dict[str, Any]:
    """Return one bounded page of eligible items in burn order plus first-match exclusion counts.

    The page holds at most ``limit`` items and examines at most ``BURN_SCAN_MAX`` rows, so a
    ledger that has grown without limit still answers in bounded time and memory. Exclusion
    counts cover the rows this page examined; walking ``next_cursor`` to the end sums them to
    the whole-ledger totals, and a ledger smaller than one page reports them in one read.
    """
    _pragma_foreign_keys(conn)
    page_size = _parse_limit(limit)
    after = _burn_cursor_key(_decode_cursor(cursor, "burn", _BURN_CURSOR_FIELDS)) if cursor is not None else None
    exclusions = {name: 0 for name in EXCLUSION_BUCKETS}
    eligible: list[dict[str, Any]] = []
    scanned = 0
    while len(eligible) < page_size and scanned < BURN_SCAN_MAX:
        rows = _burn_rows(conn, after=after, limit=min(_BURN_SCAN_CHUNK, BURN_SCAN_MAX - scanned))
        if not rows:
            break
        for row in rows:
            scanned += 1
            after = _burn_key(row)
            summary = _link_summary(conn, str(row[0]))
            item = _row_to_item(row, summary)
            bucket = exclusion_bucket(item, _source_policies(summary))
            if bucket is None:
                eligible.append(item)
            else:
                exclusions[bucket] += 1
            if len(eligible) >= page_size:
                break
    next_cursor = None
    if after is not None and _burn_rows(conn, after=after, limit=1):
        next_cursor = _encode_cursor(
            "burn",
            {
                "burn_rank": str(after[0]),
                "spend_by": str(after[2]),
                "ready_at": str(after[3]),
                "work_id": str(after[4]),
            },
        )
    return {"items": eligible, "exclusions": exclusions, "next_cursor": next_cursor}


def list_links(conn: sqlite3.Connection, work_id: str) -> list[dict[str, Any]]:
    """Return every link on one item in synced-at order.

    Bounded by ``ITEM_MAX_LINKS`` because that ceiling is enforced on write. Reads that
    answer a caller use ``list_links_page`` so the response size is a page, not a ceiling.
    """
    _pragma_foreign_keys(conn)
    return [_row_to_link(row) for row in _full_link_rows(conn, work_id)]


def list_links_page(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    limit: object = LINK_PAGE_DEFAULT,
    cursor: object | None = None,
) -> dict[str, Any]:
    """Return one deterministic page of an item's links with an explicit next cursor.

    Ordered and keyed by ``(synced_at, link_id)``, the same order ``list_links`` uses, so a
    caller that follows ``next_cursor`` to the end sees every link exactly once.
    """
    _pragma_foreign_keys(conn)
    _select_item_row(conn, work_id)
    page_size = _parse_limit(limit)
    cursor_values = _decode_cursor(cursor, "links", ("synced_at", "link_id")) if cursor is not None else None
    params: list[object] = [work_id]
    predicate = ""
    if cursor_values is not None:
        predicate = " AND (synced_at > ? OR (synced_at = ? AND link_id > ?))"
        params.extend([cursor_values["synced_at"], cursor_values["synced_at"], cursor_values["link_id"]])
    rows = conn.execute(
        f"SELECT {', '.join(_LINK_COLUMNS)} FROM work_links WHERE work_id = ?{predicate} "
        "ORDER BY synced_at ASC, link_id ASC LIMIT ?",
        (*params, page_size + 1),
    ).fetchall()
    rows, next_cursor = _page(
        list(rows),
        limit=page_size,
        kind="links",
        fields=("synced_at", "link_id"),
        row_values=lambda row: {"synced_at": str(row[10]), "link_id": str(row[0])},
    )
    return {"links": [_row_to_link(row) for row in rows], "next_cursor": next_cursor}


def _parse_limit(value: object) -> int:
    if isinstance(value, bool):
        raise _field_bound("limit must be an integer from 1 to 100")
    if isinstance(value, str):
        if not value.isdigit():
            raise _field_bound("limit must be an integer from 1 to 100")
        value = int(value)
    if not isinstance(value, int) or not 1 <= value <= 100:
        raise _field_bound("limit must be an integer from 1 to 100")
    return value


def _parse_bool(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise _field_bound(f"{field} must be a boolean")


def _encode_cursor(kind: str, values: Mapping[str, str]) -> str:
    payload = json.dumps({"kind": kind, "values": dict(values)}, sort_keys=True, separators=(",", ":"))
    return urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(value: object, kind: str, fields: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(value, str) or not value:
        raise _field_bound("cursor is invalid")
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8"))
    except (BinasciiError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise _field_bound("cursor is invalid") from None
    if (
        not isinstance(payload, Mapping)
        or payload.get("kind") != kind
        or not isinstance(payload.get("values"), Mapping)
    ):
        raise _field_bound("cursor is invalid")
    values = payload["values"]
    if set(values) != set(fields) or any(not isinstance(values[field], str) for field in fields):
        raise _field_bound("cursor is invalid")
    return {field: values[field] for field in fields}


def _page(
    rows: list[Any], *, limit: int, kind: str, fields: tuple[str, ...], row_values: Callable[[Any], dict[str, str]]
) -> tuple[list[Any], str | None]:
    visible = rows[:limit]
    if len(rows) <= limit or not visible:
        return visible, None
    return visible, _encode_cursor(kind, row_values(visible[-1]))


# Keep Worklore's established public import surface in this module.  The operations
# themselves live separately from the schema and low-level SQLite primitives above.
from . import worklore_store_operations as _worklore_store_operations  # noqa: E402

_worklore_store_operations.bind_core(sys.modules[__name__])

from .worklore_store_operations import (  # noqa: E402, F401
    add_link,
    delete_link,
    import_batch,
    link_execution,
    list_all_events,
    list_events_page,
    list_items,
    record_attempt,
    _link_summaries,
    _link_summary,
)
