"""Worklore query, link, attempt, and adapter-import operations.

The public functions are re-exported from :mod:`brigade.worklore_store` to preserve
its API while keeping the SQLite schema and primitive helpers in one core module.
"""

from __future__ import annotations

import sqlite3
from types import ModuleType
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .worklore_validate import parse_create, safe_optional_text

if TYPE_CHECKING:
    from .worklore_store import (  # noqa: F401
        ADAPTER_ID_MAX,
        ADMIN_LINK_TYPES,
        DISPLAY_REF_MAX,
        EXTERNAL_KEY_MAX,
        IDEMPOTENCY_KEY_MAX,
        IMPORT_LINK_TYPES,
        IMPORT_MAX_OBSERVATIONS,
        KINDS,
        REFUSED_ADAPTER_IDENTITY_CONFLICT,
        REFUSED_DETAILS_MAX,
        REFUSED_OPERATOR_MANAGED,
        REFUSED_OWNER_NODE_MISMATCH,
        SOURCE_POLICIES,
        SOURCE_TYPES,
        TRANSITIONS,
        WorkloreConflict,
        WorkloreForbidden,
        WorkloreNotFound,
        WorkloreValidationError,
        _EVENT_PAGE_COLUMNS,
        _ITEM_COLUMNS,
        _LINK_COLUMNS,
        _append_event,
        _claim_item_link,
        _cursor_owner,
        _cursor_seq,
        _decode_cursor,
        _empty_link_summary,
        _event_from_page_row,
        _field_bound,
        _find_link,
        _has_source_link,
        _https_url,
        _hub_run_exists,
        _import_headroom,
        _in_transaction,
        _insert_item,
        _insert_link,
        _item_from_create,
        _items_for_observations,
        _link_claim,
        _link_from_observation,
        _load_json_list,
        _links_by_work_id,
        _loaded_item,
        _mint_id,
        _observation_changed,
        _observation_detail,
        _observation_fingerprint,
        _one_way_stale_transition,
        _observed_source_version,
        _optional_text,
        _owning_adapter,
        _page,
        _parse_bool,
        _parse_limit,
        _parse_observation,
        _pragma_foreign_keys,
        _require_adapter_namespace,
        _require_owned_fleet_run,
        _require_text,
        _require_version,
        _revision_refusal,
        _row_to_item,
        _row_to_link,
        _source_version,
        _select_item_row,
        _touch_cursor,
        _touch_link_synced_at,
        _trim_adapter_events,
        _trim_fleet_run_links,
        _trim_import_keys,
        _update_item,
        _update_link,
        _utc_now,
    )


_CORE_BINDING_NAMES = (
    "ADAPTER_ID_MAX",
    "ADMIN_LINK_TYPES",
    "DISPLAY_REF_MAX",
    "EXTERNAL_KEY_MAX",
    "IDEMPOTENCY_KEY_MAX",
    "IMPORT_LINK_TYPES",
    "IMPORT_MAX_OBSERVATIONS",
    "KINDS",
    "REFUSED_ADAPTER_IDENTITY_CONFLICT",
    "REFUSED_DETAILS_MAX",
    "REFUSED_OPERATOR_MANAGED",
    "REFUSED_OWNER_NODE_MISMATCH",
    "SOURCE_POLICIES",
    "SOURCE_TYPES",
    "TRANSITIONS",
    "WorkloreConflict",
    "WorkloreForbidden",
    "WorkloreNotFound",
    "WorkloreValidationError",
    "_EVENT_PAGE_COLUMNS",
    "_ITEM_COLUMNS",
    "_LINK_COLUMNS",
    "_append_event",
    "_claim_item_link",
    "_cursor_owner",
    "_cursor_seq",
    "_decode_cursor",
    "_empty_link_summary",
    "_event_from_page_row",
    "_field_bound",
    "_find_link",
    "_has_source_link",
    "_https_url",
    "_hub_run_exists",
    "_import_headroom",
    "_in_transaction",
    "_insert_item",
    "_insert_link",
    "_item_from_create",
    "_items_for_observations",
    "_link_claim",
    "_link_from_observation",
    "_load_json_list",
    "_links_by_work_id",
    "_loaded_item",
    "_mint_id",
    "_observation_changed",
    "_observation_detail",
    "_observation_fingerprint",
    "_one_way_stale_transition",
    "_observed_source_version",
    "_optional_text",
    "_owning_adapter",
    "_page",
    "_parse_bool",
    "_parse_limit",
    "_parse_observation",
    "_pragma_foreign_keys",
    "_require_adapter_namespace",
    "_require_owned_fleet_run",
    "_require_text",
    "_require_version",
    "_revision_refusal",
    "_row_to_item",
    "_row_to_link",
    "_source_version",
    "_select_item_row",
    "_touch_cursor",
    "_touch_link_synced_at",
    "_trim_adapter_events",
    "_trim_fleet_run_links",
    "_trim_import_keys",
    "_update_item",
    "_update_link",
    "_utc_now",
)

_CORE: ModuleType | None = None


def bind_core(core: ModuleType) -> None:
    """Bind verified store helpers once, after the store facade has initialized."""
    global _CORE

    if _CORE is not None:
        if _CORE is not core:
            raise RuntimeError("worklore store operations core is already bound")
        return
    missing = [name for name in _CORE_BINDING_NAMES if not hasattr(core, name)]
    if missing:
        raise RuntimeError("worklore store operations core binding is incomplete")
    globals().update({name: getattr(core, name) for name in _CORE_BINDING_NAMES})
    _CORE = core


def _core() -> ModuleType:
    """Return the facade binding or fail before any operation can use unbound helpers."""
    if _CORE is None:
        raise RuntimeError("worklore store operations core is not bound")
    return _CORE


_ATTEMPT_EVENT_TYPES = ("attempt-started", "attempt-failed", "attempt-reset")


def record_attempt(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    action: str,
    expected_version: int,
    actor_id: str,
    actor_type: str = "operator",
    node_id: str | None = None,
    run_id: object | None = None,
) -> dict[str, Any]:
    """Record a bounded attempt, treating an authenticated run/action retry as a no-op."""
    actions = {
        "started": ("attempt-started", lambda count: count),
        "failed": ("attempt-failed", lambda count: count + 1),
        "reset": ("attempt-reset", lambda count: 0),
    }
    if action not in actions:
        raise WorkloreValidationError("action must be started, failed, or reset", code="field-bound")
    event_type, next_count = actions[action]
    core = _core()
    # Reset is intentionally not associated with an execution run. Discard the supplied
    # value before validation or persistence so it cannot become an event-log side channel.
    accepted_run_id = (
        None if action == "reset" else safe_optional_text(run_id, "run_id", max_len=core.ATTEMPT_RUN_ID_MAX)
    )

    def _write() -> dict[str, Any]:
        current = _loaded_item(conn, work_id)
        if node_id is not None and action in {"started", "failed"}:
            _require_owned_fleet_run(conn, work_id, node_id=node_id, run_id=accepted_run_id)
        if accepted_run_id is not None:
            duplicate = conn.execute(
                "SELECT 1 FROM work_events WHERE work_id = ? AND event_type = ? "
                "AND node_id IS ? AND run_id = ? LIMIT 1",
                (work_id, event_type, node_id, accepted_run_id),
            ).fetchone()
            if duplicate is not None:
                return current
        _require_version(current, expected_version)
        placeholders = ", ".join("?" for _ in _ATTEMPT_EVENT_TYPES)
        at_ceiling = conn.execute(
            "SELECT 1 FROM work_events INDEXED BY work_events_attempt_work_seq "
            f"WHERE work_id = ? AND event_type IN ({placeholders}) "
            "LIMIT 1 OFFSET ?",
            (work_id, *_ATTEMPT_EVENT_TYPES, core.ITEM_MAX_ATTEMPT_EVENTS - 1),
        ).fetchone()
        if at_ceiling is not None:
            raise WorkloreConflict(
                f"work item has reached its {core.ITEM_MAX_ATTEMPT_EVENTS} attempt event ceiling",
                code="attempt-conflict",
            )
        now = _utc_now()
        updated = {
            **current,
            "attempt_count": next_count(int(current["attempt_count"])),
            "version": int(current["version"]) + 1,
            "updated_at": now,
        }
        _update_item(conn, updated)
        _append_event(
            conn,
            work_id=work_id,
            event_type=event_type,
            from_status=str(current["status"]),
            to_status=str(current["status"]),
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
            node_id=node_id,
            run_id=accepted_run_id,
            detail={"action": action},
        )
        return _loaded_item(conn, work_id)

    return _in_transaction(conn, _write)


def _link_summary(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    """Read bounded burn/detail facts for one item through indexed LIMIT probes.

    A schema-v18 database may contain a legacy item with far more than the current write
    ceiling. These facts retain the full projection semantics without aggregating that
    partition: every probe identifies one policy/type state in a composite index and stops
    at its first match. The closed link-type contract makes source-policy exclusions apply
    only to imported source links, without a negative predicate that could scan an
    unbounded partition.
    """
    _core()
    source_types = tuple(sorted(IMPORT_LINK_TYPES))
    source_placeholders = ", ".join("?" for _ in source_types)
    source_params = (work_id, *source_types)
    has_source_link = conn.execute(
        f"SELECT 1 FROM work_links WHERE work_id = ? AND link_type IN ({source_placeholders}) LIMIT 1",
        source_params,
    ).fetchone()
    eligible_source_link = conn.execute(
        f"SELECT 1 FROM work_links WHERE work_id = ? AND source_policy = 'eligible' "
        f"AND link_type IN ({source_placeholders}) AND stale_at IS NULL LIMIT 1",
        source_params,
    ).fetchone()
    authoritative = min(
        (
            conn.execute(
                "SELECT source_acceptance_json, synced_at, link_id FROM work_links "
                "INDEXED BY work_links_work_policy_type_order WHERE work_id = ? "
                "AND source_policy = 'eligible' AND link_type = ? ORDER BY synced_at ASC, link_id ASC LIMIT 1",
                (work_id, link_type),
            ).fetchone()
            for link_type in source_types
        ),
        key=lambda row: (str(row[1]), str(row[2])) if row is not None else ("~", "~"),
        default=None,
    )
    blocking_policy = None
    for policy in sorted(SOURCE_POLICIES - {"eligible"}):
        source_blocker = conn.execute(
            f"SELECT 1 FROM work_links WHERE work_id = ? AND source_policy = ? "
            f"AND link_type IN ({source_placeholders}) AND stale_at IS NULL LIMIT 1",
            (work_id, policy, *source_types),
        ).fetchone()
        if source_blocker is not None:
            blocking_policy = policy
            break
    return {
        "blocking_policy": blocking_policy,
        "source_acceptance": _load_json_list(None if authoritative is None else authoritative[0]),
        "has_source_link": has_source_link is not None,
        "has_eligible_non_stale_source_link": eligible_source_link is not None,
    }


def _link_summaries(conn: sqlite3.Connection, work_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Summarize exactly ``work_ids`` without aggregating any item's link partition."""
    return {work_id: _link_summary(conn, work_id) for work_id in work_ids}


_LIST_SCAN_CHUNK = 200


def _item_candidates(conn: sqlite3.Connection, *, after: Mapping[str, str] | None, limit: int) -> list[Any]:
    predicate = ""
    params: tuple[object, ...] = ()
    if after is not None:
        predicate = " WHERE (created_at > ? OR (created_at = ? AND work_id > ?))"
        params = (after["created_at"], after["created_at"], after["work_id"])
    return list(
        conn.execute(
            f"SELECT {', '.join(_ITEM_COLUMNS)} FROM work_items{predicate} "
            "ORDER BY created_at ASC, work_id ASC LIMIT ?",
            (*params, limit),
        )
    )


def _source_matches(conn: sqlite3.Connection, work_id: str, source: str | None) -> bool:
    """Test one item's source through a bounded, order-compatible index probe."""
    if source is None:
        return True
    if source in _core().IMPORT_LINK_TYPES:
        row = conn.execute(
            "SELECT 1 FROM work_links INDEXED BY work_links_work_type WHERE work_id = ? AND link_type = ? LIMIT 1",
            (work_id, source),
        ).fetchone()
        return row is not None
    source_types = tuple(sorted(_core().IMPORT_LINK_TYPES))
    placeholders = ", ".join("?" for _ in source_types)
    row = conn.execute(
        f"SELECT 1 FROM work_links INDEXED BY work_links_work_type WHERE work_id = ? "
        f"AND link_type IN ({placeholders}) LIMIT 1",
        (work_id, *source_types),
    ).fetchone()
    return row is None


def _item_matches(
    conn: sqlite3.Connection,
    row: Any,
    *,
    status: str | None,
    kind: str | None,
    burn_eligible: bool | None,
    source: str | None,
) -> bool:
    return (
        (status is None or str(row[5]) == status)
        and (kind is None or str(row[3]) == kind)
        and (burn_eligible is None or bool(row[7]) is burn_eligible)
        and _source_matches(conn, str(row[0]), source)
    )


def _filtered_item_rows(
    conn: sqlite3.Connection,
    *,
    page_size: int,
    cursor: Mapping[str, str] | None,
    status: str | None,
    kind: str | None,
    burn_eligible: bool | None,
    source: str | None,
) -> tuple[list[Any], str | None]:
    """Walk at most ``LIST_SCAN_MAX`` globally ordered item candidates for one page."""
    core = _core()
    after = cursor
    rows: list[Any] = []
    scanned = 0
    while len(rows) < page_size and scanned < core.LIST_SCAN_MAX:
        candidates = _item_candidates(conn, after=after, limit=min(_LIST_SCAN_CHUNK, core.LIST_SCAN_MAX - scanned))
        if not candidates:
            break
        for candidate in candidates:
            scanned += 1
            after = {"created_at": str(candidate[18]), "work_id": str(candidate[0])}
            if _item_matches(conn, candidate, status=status, kind=kind, burn_eligible=burn_eligible, source=source):
                rows.append(candidate)
            if len(rows) >= page_size:
                break
    next_cursor = None
    if after is not None and _item_candidates(conn, after=after, limit=1):
        next_cursor = core._encode_cursor("items", after)
    return rows, next_cursor


def list_items(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    kind: str | None = None,
    burn_eligible: object | None = None,
    source: str | None = None,
    limit: object = 50,
    cursor: object | None = None,
) -> dict[str, Any]:
    """Return a deterministically paged item projection with its links."""
    core = _core()
    _pragma_foreign_keys(conn)
    page_size = _parse_limit(limit)
    if status is not None and status not in {*TRANSITIONS, "archived"}:
        raise _field_bound("status must be an allowed work status")
    if kind is not None and kind not in KINDS:
        raise _field_bound("kind must be an allowed work kind")
    if source is not None and source not in {"github", "brigade", "native"}:
        raise _field_bound("source must be github, brigade, or native")
    cursor_values = _decode_cursor(cursor, "items", ("created_at", "work_id")) if cursor is not None else None
    parsed_burn_eligible: bool | None = None
    if burn_eligible is not None:
        parsed_burn_eligible = _parse_bool(burn_eligible, "burn_eligible")
    if status is None and kind is None and parsed_burn_eligible is None and source is None:
        item_rows = _item_candidates(conn, after=cursor_values, limit=page_size + 1)
        item_rows, next_cursor = _page(
            item_rows,
            limit=page_size,
            kind="items",
            fields=("created_at", "work_id"),
            row_values=lambda row: {"created_at": str(row[18]), "work_id": str(row[0])},
        )
    else:
        item_rows, next_cursor = _filtered_item_rows(
            conn,
            page_size=page_size,
            cursor=cursor_values,
            status=status,
            kind=kind,
            burn_eligible=parsed_burn_eligible,
            source=source,
        )
    work_ids = [str(row[0]) for row in item_rows]
    summaries = _link_summaries(conn, work_ids)
    links_by_work, truncated = _links_by_work_id(
        conn,
        work_ids,
        _LINK_COLUMNS,
        per_item_limit=core.ITEM_LINK_PROJECTION_MAX,
    )
    items: list[dict[str, Any]] = []
    for work_id, row in zip(work_ids, item_rows, strict=True):
        item = _row_to_item(row, summaries.get(work_id, _empty_link_summary()))
        item["links"] = [_row_to_link(link) for link in links_by_work.get(work_id, [])]
        item["links_truncated"] = work_id in truncated
        items.append(item)
    return {"items": items, "next_cursor": next_cursor}


def _event_candidates(conn: sqlite3.Connection, *, after: Mapping[str, str] | None, limit: int) -> list[Any]:
    predicate = ""
    params: tuple[object, ...] = ()
    if after is not None:
        predicate = " WHERE (occurred_at < ? OR (occurred_at = ? AND seq < ?))"
        params = (after["occurred_at"], after["occurred_at"], _cursor_seq(after))
    return list(
        conn.execute(
            f"SELECT {', '.join(_EVENT_PAGE_COLUMNS)} FROM work_events{predicate} "
            "ORDER BY occurred_at DESC, seq DESC LIMIT ?",
            (*params, limit),
        )
    )


def _filtered_event_rows(
    conn: sqlite3.Connection,
    *,
    page_size: int,
    cursor: Mapping[str, str] | None,
    work_id: str | None,
    event_type: str | None,
) -> tuple[list[Any], str | None]:
    """Walk at most ``LIST_SCAN_MAX`` reverse-chronological event candidates for one page."""
    core = _core()
    after = cursor
    rows: list[Any] = []
    scanned = 0
    while len(rows) < page_size and scanned < core.LIST_SCAN_MAX:
        candidates = _event_candidates(conn, after=after, limit=min(_LIST_SCAN_CHUNK, core.LIST_SCAN_MAX - scanned))
        if not candidates:
            break
        for candidate in candidates:
            scanned += 1
            after = {"occurred_at": str(candidate[10]), "seq": str(candidate[12])}
            if (work_id is None or str(candidate[0]) == work_id) and (
                event_type is None or str(candidate[2]) == event_type
            ):
                rows.append(candidate)
            if len(rows) >= page_size:
                break
    next_cursor = None
    if after is not None and _event_candidates(conn, after=after, limit=1):
        next_cursor = core._encode_cursor("events", after)
    return rows, next_cursor


def list_all_events(
    conn: sqlite3.Connection,
    *,
    work_id: str | None = None,
    event_type: str | None = None,
    limit: object = 50,
    cursor: object | None = None,
) -> dict[str, Any]:
    """Return fleet-wide events in reverse chronological order."""
    _core()
    _pragma_foreign_keys(conn)
    page_size = _parse_limit(limit)
    if work_id is not None:
        _require_text(work_id, "work_id", max_len=256)
    if event_type is not None:
        _require_text(event_type, "event_type", max_len=128)
    cursor_values = _decode_cursor(cursor, "events", ("occurred_at", "seq")) if cursor is not None else None
    if work_id is None and event_type is None:
        rows = _event_candidates(conn, after=cursor_values, limit=page_size + 1)
        rows, next_cursor = _page(
            rows,
            limit=page_size,
            kind="events",
            fields=("occurred_at", "seq"),
            row_values=lambda row: {"occurred_at": str(row[10]), "seq": str(row[12])},
        )
    else:
        rows, next_cursor = _filtered_event_rows(
            conn,
            page_size=page_size,
            cursor=cursor_values,
            work_id=work_id,
            event_type=event_type,
        )
    return {"events": [_event_from_page_row(row) for row in rows], "next_cursor": next_cursor}


def list_events_page(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    limit: object = 50,
    cursor: object | None = None,
) -> dict[str, Any]:
    """Return a Worklore item's events in deterministic chronological order."""
    _core()
    _pragma_foreign_keys(conn)
    _select_item_row(conn, work_id)
    page_size = _parse_limit(limit)
    cursor_values = _decode_cursor(cursor, "item-events", ("seq",)) if cursor is not None else None
    params: list[object] = [work_id]
    predicate = ""
    if cursor_values is not None:
        predicate = " AND seq > ?"
        params.append(_cursor_seq(cursor_values))
    rows = conn.execute(
        f"SELECT {', '.join(_EVENT_PAGE_COLUMNS)} FROM work_events WHERE work_id = ?{predicate} "
        "ORDER BY seq ASC LIMIT ?",
        (*params, page_size + 1),
    ).fetchall()
    rows, next_cursor = _page(
        list(rows),
        limit=page_size,
        kind="item-events",
        fields=("seq",),
        row_values=lambda row: {"seq": str(row[12])},
    )
    return {"events": [_event_from_page_row(row) for row in rows], "next_cursor": next_cursor}


def _parse_admin_link(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _field_bound("link body must be an object")
    allowed = {"link_type", "external_key", "url", "display_ref"}
    unknown = [name for name in raw if name not in allowed]
    if unknown:
        raise WorkloreValidationError(f"unknown field: {unknown[0]}", code="unknown-field")
    link_type = raw.get("link_type")
    if link_type not in ADMIN_LINK_TYPES:
        raise WorkloreForbidden("link type is adapter or fleet managed", code="link-forbidden")
    external_key = _require_text(raw.get("external_key"), "external_key", max_len=EXTERNAL_KEY_MAX)
    url = _https_url(raw.get("url"))
    if link_type == "url":
        url = _https_url(url or external_key)
    return {
        "link_type": str(link_type),
        "external_key": external_key,
        "url": url,
        "display_ref": _optional_text(raw.get("display_ref"), "display_ref", max_len=DISPLAY_REF_MAX),
    }


def add_link(
    conn: sqlite3.Connection,
    work_id: str,
    raw: object,
    *,
    actor_id: str,
    actor_type: str = "operator",
    node_id: str | None = None,
) -> dict[str, Any]:
    """Add a documented operator-managed link without using SQLite errors as an API."""
    _core()

    def _write() -> dict[str, Any]:
        current = _loaded_item(conn, work_id)
        parsed = _parse_admin_link(raw)
        if _find_link(conn, parsed["link_type"], parsed["external_key"]) is not None:
            raise WorkloreConflict("external link already exists", code="link-conflict")
        _claim_item_link(conn, work_id, code="link-conflict")
        now = _utc_now()
        link = {
            "link_id": _mint_id("lnk-"),
            "work_id": work_id,
            **parsed,
            "source_policy": "eligible",
            "source_acceptance": [],
            "synced_at": now,
            "stale_at": None,
        }
        _insert_link(conn, link)
        _append_event(
            conn,
            work_id=work_id,
            event_type="linked",
            from_status=str(current["status"]),
            to_status=str(current["status"]),
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
            node_id=node_id,
            detail={"external_key": link["external_key"], "link_type": link["link_type"]},
        )
        return {**link}

    return _in_transaction(conn, _write)


def delete_link(
    conn: sqlite3.Connection,
    work_id: str,
    link_id: str,
    *,
    expected_version: int | None = None,
    actor_id: str,
    actor_type: str = "operator",
    node_id: str | None = None,
) -> dict[str, Any]:
    """Remove one operator-managed or retired source link and record the durable unlink event.

    An adapter-owned ``github`` or ``brigade`` link may be removed only once its
    ``source_policy`` has left ``eligible``; an eligible adapter link stays protected so an
    operator cannot detach live imported work. Removing the last source link converts the item
    to ``native``. Scheduling fields are never touched here.
    """
    _core()

    def _write() -> dict[str, Any]:
        current = _loaded_item(conn, work_id)
        if expected_version is not None and current["version"] != expected_version:
            raise WorkloreConflict("item version does not match", code="version-conflict")
        link = conn.execute(
            f"SELECT {', '.join(_LINK_COLUMNS)} FROM work_links WHERE link_id = ? AND work_id = ?",
            (link_id, work_id),
        ).fetchone()
        if link is None:
            raise WorkloreNotFound("work link not found")
        parsed = _row_to_link(link)
        link_type = str(parsed["link_type"])
        source_policy = str(parsed["source_policy"])
        if link_type not in ADMIN_LINK_TYPES:
            raise WorkloreForbidden("link is adapter or fleet managed", code="link-forbidden")
        owner = _owning_adapter(conn, work_id, link_type, str(parsed["external_key"]))
        adapter = None if owner is None else owner[0]
        if adapter is not None and source_policy == "eligible":
            raise WorkloreForbidden("adapter link is still eligible", code="link-forbidden")
        conn.execute("DELETE FROM work_links WHERE link_id = ?", (link_id,))
        detail: dict[str, Any] = {"external_key": parsed["external_key"], "link_type": link_type}
        if link_type in IMPORT_LINK_TYPES:
            detail["source_policy"] = source_policy
            if adapter is not None:
                detail["adapter_id"] = adapter
            if not _has_source_link(conn, work_id):
                detail["became_native"] = True
        now = _utc_now()
        current["version"] = int(current["version"]) + 1
        current["updated_at"] = now
        _update_item(conn, current)
        _append_event(
            conn,
            work_id=work_id,
            event_type="unlinked",
            from_status=str(current["status"]),
            to_status=str(current["status"]),
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
            node_id=node_id,
            detail=detail,
        )
        return current

    return _in_transaction(conn, _write)


def link_execution(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    node_id: str,
    run_id: str,
    actor_id: str,
    actor_type: str = "node",
    is_admin: bool = False,
) -> dict[str, Any]:
    """Join a Fleet Hub run to a Worklore item as a fleet-run link.

    ``is_admin`` is the caller's authority, passed explicitly by the HTTP boundary rather than
    inferred from a reserved ``actor_id`` string. Only an admin may link another node's run.
    """
    _core()

    def _write() -> dict[str, Any]:
        current = _loaded_item(conn, work_id)
        if not is_admin and actor_id != node_id:
            raise WorkloreForbidden("node may link only its own run", code="execution-mismatch")
        if not _hub_run_exists(conn, node_id, run_id):
            raise WorkloreForbidden("fleet run was not found for that node", code="execution-mismatch")
        external_key = f"{node_id}/{run_id}"
        existing = _find_link(conn, "fleet-run", external_key)
        if existing is not None:
            if existing["work_id"] == work_id:
                return existing
            raise WorkloreConflict("fleet run is already linked to another work item", code="link-conflict")
        # Retention runs before the claim, so an item whose ceiling is already full of its
        # own past runs makes room for this one instead of refusing every future run.
        _trim_fleet_run_links(conn, work_id)
        _claim_item_link(conn, work_id, code="link-conflict")
        now = _utc_now()
        link: dict[str, Any] = {
            "link_id": _mint_id("lnk-"),
            "work_id": work_id,
            "link_type": "fleet-run",
            "external_key": external_key,
            "display_ref": external_key,
            "url": None,
            "external_state": None,
            "external_updated_at": None,
            "source_policy": "eligible",
            "source_acceptance": [],
            "synced_at": now,
            "stale_at": None,
        }
        _insert_link(conn, link)
        _append_event(
            conn,
            work_id=work_id,
            event_type="execution-linked",
            from_status=str(current["status"]),
            to_status=str(current["status"]),
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
            node_id=node_id,
            run_id=run_id,
            detail={"external_key": link["external_key"]},
        )
        stored_link = _find_link(conn, "fleet-run", external_key)
        assert stored_link is not None
        return stored_link

    return _in_transaction(conn, _write)


def import_batch(
    conn: sqlite3.Connection,
    *,
    adapter_id: str,
    source_type: str,
    idempotency_key: str,
    observations: object,
    actor_id: str,
    owner_node: str,
) -> dict[str, Any]:
    """Apply one adapter observation batch without clobbering schedule fields.

    Returns counts, bounded ``refused_details``, and ``items`` containing each resolved
    observation's ``work_id`` and ``external_key``. A refusal is per identity: one
    observation the hub will not apply, while valid siblings still import.
    """
    _core()
    adapter = _require_text(adapter_id, "adapter_id", max_len=ADAPTER_ID_MAX)
    if source_type not in SOURCE_TYPES:
        raise _field_bound("source_type must be github or brigade")
    _require_adapter_namespace(adapter, source_type)
    key = _require_text(idempotency_key, "idempotency_key", max_len=IDEMPOTENCY_KEY_MAX)
    if not isinstance(observations, list):
        raise _field_bound("observations must be an array")
    if len(observations) > IMPORT_MAX_OBSERVATIONS:
        raise _field_bound(f"observations must have at most {IMPORT_MAX_OBSERVATIONS} items")
    parsed_observations = [_parse_observation(item, source_type=source_type) for item in observations]
    fingerprint = _observation_fingerprint(list(observations))

    def _write() -> dict[str, Any]:
        now = _utc_now()
        existing_owner = _cursor_owner(conn, adapter)
        if existing_owner is not None and existing_owner != owner_node:
            raise WorkloreForbidden("adapter is owned by another node", code="adapter-owner-mismatch")
        if not parsed_observations and existing_owner is None:
            # An empty first batch has nothing to reconcile, so it must not write the
            # cursor row that would hand this node the adapter namespace forever.
            return {"created": 0, "updated": 0, "unchanged": 0, "refused": 0, "refused_details": [], "items": []}
        stored = conn.execute(
            "SELECT fingerprint, created, updated, unchanged, refused FROM work_import_keys "
            "WHERE adapter_id = ? AND idempotency_key = ?",
            (adapter, key),
        ).fetchone()
        if stored is not None:
            if str(stored[0]) != fingerprint:
                raise WorkloreConflict("idempotency key reused with a different payload", code="import-conflict")
            _touch_cursor(
                conn,
                adapter_id=adapter,
                source_type=source_type,
                owner_node=owner_node,
                now=now,
            )
            return {
                "created": int(stored[1]),
                "updated": int(stored[2]),
                "unchanged": int(stored[3]),
                "refused": int(stored[4]),
                # Counts are stored and replayed exactly; the reasons are not, because a
                # replay re-reports a decision it did not take.
                "refused_details": [],
                "items": _items_for_observations(conn, parsed_observations),
            }
        created = 0
        updated = 0
        unchanged = 0
        refused = 0
        refused_details: list[dict[str, str]] = []

        def _refuse(observation: Mapping[str, Any], reason: str) -> None:
            nonlocal refused
            refused += 1
            if len(refused_details) < REFUSED_DETAILS_MAX:
                refused_details.append(
                    {
                        "external_key": str(observation["external_key"]),
                        "link_type": str(observation["link_type"]),
                        "reason": reason,
                    }
                )

        claim_headroom = _import_headroom(conn, adapter_id=adapter, owner_node=owner_node)
        for parsed in parsed_observations:
            existing = _find_link(conn, parsed["link_type"], parsed["external_key"])
            detail = _observation_detail(adapter, parsed)
            if existing is not None:
                work_id = str(existing["work_id"])
                claim = _link_claim(conn, work_id, parsed["link_type"], parsed["external_key"])
                if claim is None:
                    # An operator linked this identity by hand, so no adapter may adopt or
                    # mutate it. That is a fact about one identity, not about the batch:
                    # refusing the whole batch would let a single hand-linked issue stop an
                    # org's entire sync, so the sibling observations still import.
                    _refuse(parsed, REFUSED_OPERATOR_MANAGED)
                    continue
                owner_adapter, owner_event_node, high_water = claim
                if owner_adapter != adapter:
                    _refuse(parsed, REFUSED_ADAPTER_IDENTITY_CONFLICT)
                    continue
                if owner_event_node is not None and owner_event_node != owner_node:
                    _refuse(parsed, REFUSED_OWNER_NODE_MISMATCH)
                    continue
                current = _loaded_item(conn, work_id)
                changed = _observation_changed(current, existing, parsed)
                incoming_version = _source_version(parsed.get("external_updated_at"))
                stored_version = _source_version(high_water)
                title_changed = current["title"] != parsed["title"]
                title_same_revision = (
                    incoming_version is not None and stored_version is not None and incoming_version == stored_version
                )
                title_update = (
                    title_changed
                    and incoming_version is not None
                    and (stored_version is None or incoming_version > stored_version)
                )
                changed_for_revision = changed or (title_changed and not title_same_revision)
                stale_transition = _one_way_stale_transition(current, existing, parsed) and (
                    stored_version == incoming_version
                )
                reason = _revision_refusal(
                    high_water,
                    parsed.get("external_updated_at"),
                    changed=changed_for_revision,
                    stale_transition=stale_transition,
                )
                if reason is not None:
                    # A replay of an expired import key, an out-of-order delivery, or a
                    # source that will not say which revision it is speaking for. Nothing
                    # is written, not even synced_at: the projection already on record
                    # stands and the refusal is reported rather than folded into the
                    # unchanged count.
                    _refuse(parsed, reason)
                    continue
                if not changed_for_revision:
                    _touch_link_synced_at(conn, str(existing["link_id"]), now)
                    unchanged += 1
                    continue
                if stale_transition:
                    conn.execute(
                        "UPDATE work_links SET stale_at = ?, synced_at = ? WHERE link_id = ?",
                        (now, now, existing["link_id"]),
                    )
                else:
                    if title_update:
                        current["title"] = parsed["title"]
                        current["version"] = int(current["version"]) + 1
                        current["updated_at"] = now
                        _update_item(conn, current)
                    link = _link_from_observation(parsed, work_id=work_id, link_id=str(existing["link_id"]), now=now)
                    _update_link(conn, link, source_version=_observed_source_version(parsed))
                _append_event(
                    conn,
                    work_id=work_id,
                    event_type="sync-observed",
                    from_status=str(current["status"]),
                    to_status=str(current["status"]),
                    actor_type="adapter",
                    actor_id=actor_id,
                    now=now,
                    node_id=owner_node,
                    detail=detail,
                )
                if parsed["stale"]:
                    _append_event(
                        conn,
                        work_id=work_id,
                        event_type="sync-stale",
                        from_status=str(current["status"]),
                        to_status=str(current["status"]),
                        actor_type="adapter",
                        actor_id=actor_id,
                        now=now,
                        node_id=owner_node,
                        detail=detail,
                    )
                _trim_adapter_events(conn, work_id)
                updated += 1
                continue
            creation_reason = _revision_refusal(None, parsed.get("external_updated_at"), changed=True)
            if creation_reason is not None:
                # Creating an item is a mutation, so it is bought with a revision like any
                # other. Without one the hub could not later tell a fresh observation from
                # a replay of this one, which is how an unguarded identity gets rolled
                # back. Refusing here keeps every projection in the ledger guarded from
                # its first write, and the sibling observations in this batch still import.
                _refuse(parsed, creation_reason)
                continue
            claim_headroom()
            kind = "repo" if parsed["link_type"] == "github" else "other"
            create_body: dict[str, Any] = {"title": parsed["title"], "kind": kind}
            if parsed.get("description"):
                create_body["description"] = parsed["description"]
            item = _item_from_create(parse_create(create_body), work_id=_mint_id("wl-"), now=now)
            _insert_item(conn, item)
            link = _link_from_observation(parsed, work_id=item["work_id"], link_id=_mint_id("lnk-"), now=now)
            _insert_link(
                conn,
                link,
                adapter_id=adapter,
                owner_node=owner_node,
                source_version=_observed_source_version(parsed),
            )
            _append_event(
                conn,
                work_id=item["work_id"],
                event_type="created",
                from_status=None,
                to_status=item["status"],
                actor_type="adapter",
                actor_id=actor_id,
                now=now,
                node_id=owner_node,
                detail=detail,
            )
            _append_event(
                conn,
                work_id=item["work_id"],
                event_type="linked",
                from_status=item["status"],
                to_status=item["status"],
                actor_type="adapter",
                actor_id=actor_id,
                now=now,
                node_id=owner_node,
                detail=detail,
            )
            _append_event(
                conn,
                work_id=item["work_id"],
                event_type="sync-observed",
                from_status=item["status"],
                to_status=item["status"],
                actor_type="adapter",
                actor_id=actor_id,
                now=now,
                node_id=owner_node,
                detail=detail,
            )
            if parsed["stale"]:
                _append_event(
                    conn,
                    work_id=item["work_id"],
                    event_type="sync-stale",
                    from_status=item["status"],
                    to_status=item["status"],
                    actor_type="adapter",
                    actor_id=actor_id,
                    now=now,
                    node_id=owner_node,
                    detail=detail,
                )
            _trim_adapter_events(conn, item["work_id"])
            created += 1
        conn.execute(
            "INSERT INTO work_import_keys (adapter_id, idempotency_key, fingerprint, created, updated, unchanged, "
            "refused, received_at, owner_node) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (adapter, key, fingerprint, created, updated, unchanged, refused, now, owner_node),
        )
        _trim_import_keys(conn, adapter_id=adapter, owner_node=owner_node)
        _touch_cursor(
            conn,
            adapter_id=adapter,
            source_type=source_type,
            owner_node=owner_node,
            now=now,
        )
        return {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "refused": refused,
            "refused_details": refused_details,
            "items": _items_for_observations(conn, parsed_observations),
        }

    return _in_transaction(conn, _write)
