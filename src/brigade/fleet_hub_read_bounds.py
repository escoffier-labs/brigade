"""Bound SQL row hydration on read-only HTTP projections.

Native connections retain their identity, transactions and existing row factory.
A truncated roster or occupancy aggregate would misrepresent authority, so these
projections fail closed. Core lists use SQL pagination separately.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
import sqlite3
import weakref
from typing import Any, Callable, Iterator

MAX_PROJECTION_ROWS = 1000


class ResultLimitExceeded(ValueError):
    """A complete projection would exceed the bounded SQL result budget."""


class ReadLimit:
    """Stop before hydrating more than the row budget for any cursor.

    Cursor bookkeeping disappears when SQLite releases a cursor. Unlike a
    connection proxy, a row factory also covers direct cursor iteration and
    preserves consumers that explicitly require a sqlite3.Connection.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.exceeded = False
        self.group_nodes = False
        self.node_keys: weakref.WeakKeyDictionary[sqlite3.Cursor, set[Any]] = weakref.WeakKeyDictionary()
        self.counts: weakref.WeakKeyDictionary[sqlite3.Cursor, int] = weakref.WeakKeyDictionary()
        original = conn.row_factory

        def bounded_row(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> Any:
            columns = [column[0] for column in cursor.description or ()] if self.group_nodes else []
            if "node_id" in columns:
                keys = self.node_keys.setdefault(cursor, set())
                keys.add(row[columns.index("node_id")])
                count = len(keys)
            else:
                count = self.counts.get(cursor, 0) + 1
                self.counts[cursor] = count
            if count > MAX_PROJECTION_ROWS:
                self.exceeded = True
                raise ResultLimitExceeded()
            return original(cursor, row) if original is not None else row

        conn.row_factory = bounded_row

    @contextmanager
    def machine_projection(self) -> Iterator[None]:
        """Count distinct nodes while existing bounded board helpers stream runs.

        Both helpers retain their own named per-node and page limits. Keep
        the identity set bounded too, without imposing a smaller run-page cap.
        All queries outside this scope retain the ordinary row budget.
        """
        previous = self.group_nodes
        self.group_nodes = True
        try:
            yield
        finally:
            self.group_nodes = previous


def limit_payload() -> dict[str, Any]:
    """Explicit refusal shared by raised and internally caught read limits."""
    return {
        "error": f"complete projection exceeds {MAX_PROJECTION_ROWS} rows or node identities per query",
        "code": "result_limit_exceeded",
        "truncated": True,
    }


def guard_get(method: Callable[..., Any]) -> Callable[..., Any]:
    """Give every GET projection the same explicit, fail-closed limit response."""

    @functools.wraps(method)
    def guarded(handler: Any, *args: Any, **kwargs: Any) -> Any:
        handler._read_limits = []
        try:
            return method(handler, *args, **kwargs)
        except ResultLimitExceeded:
            handler._send_json(413, limit_payload())
            return None

    return guarded
