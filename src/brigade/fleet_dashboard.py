"""Hub-served Fleet dashboard (issue #1124, phase 3).

Server-rendered, read-only HTML for ``GET /`` and ``GET /view/<board>`` on
the fleet hub. Two pivots over the same event stream:

- ``machines`` — one card per observed ``node_id`` with its live runs
  (run, repo, seat/harness, state, elapsed, last event) and the claims it
  holds.
- ``repos`` — one row per observed repo: where it is running, who claims it
  (owner node, conductor, TTL remaining), its last outcome, and neutral
  parallel activity details when two active nodes report the same repo label.

Everything here is a pure function of the hub's query results, so the tests
can render without a socket. The page mirrors the ``brigade center serve``
dashboard (``center_cmd.dashboard.render.page``) rather than importing it:
the hub must stay a light stdlib process and ``center_cmd`` pulls the whole
operator surface in on import. Sort and filter are query parameters and the
refresh is a ``<meta http-equiv="refresh">``, so every table works with
JavaScript disabled; the small inline script only ticks elapsed timers and
adds a client-side text filter on top. No token, cookie, or holder secret is
ever rendered.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode

from . import fleet_command_deck
from .fleet_command_deck import is_terminal_state

REFRESH_SECONDS = 10
MACHINE_CARD_LIMIT = 200
# A non-terminal run whose latest event is older than this has no heartbeat
# reaching the hub; it needs a human look.
STALE_AFTER_SECONDS = 30 * 60

VIEWS = ("machines", "repos")
DEFAULT_VIEW = "machines"
SORTS = ("attention", "age", "node", "repo", "state", "seat")
DEFAULT_SORT = "attention"
FILTER_KEYS = ("node", "repo", "seat", "state")
_MAX_FILTER_LEN = 128

_AWAITING_STATES = frozenset({"run.paused", "approval.requested", "approval.held"})

# Attention buckets in display rank: what needs a human sorts first.
BUCKET_RANK = {
    "failed": 0,
    "awaiting approval": 1,
    "unknown": 2,
    "stale": 2,
    "running": 3,
    "queued": 4,
    "interrupted": 5,
    "succeeded": 6,
}
NEEDS_ATTENTION = frozenset({"failed", "awaiting approval", "stale", "unknown"})
_BUCKET_ICON = {
    "failed": "×",
    "awaiting approval": "?",
    "unknown": "?",
    "stale": "⌛",
    "running": "●",
    "queued": "○",
    "interrupted": "‖",
    "succeeded": "✓",
}
_IDLE_RANK = len(BUCKET_RANK)


@dataclass(frozen=True)
class DashboardQuery:
    """Sanitised view of the dashboard's query string."""

    view: str = DEFAULT_VIEW
    sort: str = DEFAULT_SORT
    node: str = ""
    repo: str = ""
    seat: str = ""
    state: str = ""
    attention_only: bool = False
    include_all: bool = False

    def filters(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in FILTER_KEYS if getattr(self, key)}


@dataclass(frozen=True)
class RunRow:
    node_id: str
    run_id: str
    repo: str
    seat: str
    harness: str
    state: str
    bucket: str
    last_ts: str
    age_seconds: float | None
    started_epoch: float | None
    elapsed_seconds: float | None
    context: dict[str, Any] | None = None

    @property
    def live(self) -> bool:
        return not is_terminal_state(self.state)

    @property
    def active(self) -> bool:
        return self.live and self.bucket not in {"stale", "unknown"}

    @property
    def seat_label(self) -> str:
        return "/".join(part for part in (self.seat, self.harness) if part) or "-"


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def parse_query(query_string: str, *, view: str = DEFAULT_VIEW) -> DashboardQuery:
    """Parse ``?sort=&node=&repo=&seat=&state=&attention=&all=`` with safe defaults.

    Unknown sorts fall back to ``attention``; filter values are trimmed and
    capped so a hostile query cannot bloat the page.
    """
    raw = parse_qs(query_string or "", keep_blank_values=False)
    first = {key: values[0] for key, values in raw.items() if values}
    sort = first.get("sort", DEFAULT_SORT).strip().lower()
    if sort not in SORTS:
        sort = DEFAULT_SORT
    filters = {key: first.get(key, "").strip()[:_MAX_FILTER_LEN] for key in FILTER_KEYS}
    return DashboardQuery(
        view=view if view in VIEWS else DEFAULT_VIEW,
        sort=sort,
        attention_only=_truthy(first.get("attention", "")),
        include_all=_truthy(first.get("all", "")),
        **filters,
    )


def _parse_stamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def bucket_for(state: str, *, age_seconds: float | None, exit_status: int | None = None) -> str:
    """Map a run_event type plus its age to an attention bucket."""
    if state == "external.unknown":
        return "unknown"
    if state == "run.stale":
        return "stale"
    if state == "run.orphaned":
        return "interrupted"
    if state == "verify.completed":
        if exit_status == 130:
            return "interrupted"
        if exit_status not in (None, 0):
            return "failed"
        return "succeeded"
    if state.endswith((".failed", ".cancelled", ".canceled", ".timed_out", ".timeout")):
        if state.endswith(".failed"):
            return "failed"
        return "interrupted"
    if state.endswith(".completed"):
        return "succeeded"
    if state.endswith(".interrupted"):
        return "interrupted"
    if state in _AWAITING_STATES:
        return "awaiting approval"
    if age_seconds is not None and age_seconds > STALE_AFTER_SECONDS:
        return "stale"
    if state == "run.created":
        return "queued"
    return "running"


def build_rows(
    runs: list[dict[str, Any]],
    started_at: dict[tuple[str, str], str],
    *,
    now: datetime,
) -> list[RunRow]:
    """Enrich hub ``/status`` rows with bucket, age, and elapsed."""
    rows: list[RunRow] = []
    for run in runs:
        node_id = str(run.get("node_id") or "")
        run_id = str(run.get("run_id") or "")
        state = str(run.get("state") or "")
        last_ts = str(run.get("ts") or "")
        last = _parse_stamp(run.get("received_at")) or _parse_stamp(last_ts)
        age = max(0.0, (now - last).total_seconds()) if last is not None else None
        started = _parse_stamp(started_at.get((node_id, run_id)) or run.get("started_at"))
        live = not is_terminal_state(state)
        elapsed: float | None = None
        if started is not None:
            end = now if live or last is None else last
            elapsed = max(0.0, (end - started).total_seconds())
        raw_exit = run.get("exit_status")
        exit_status = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        rows.append(
            RunRow(
                node_id=node_id,
                run_id=run_id,
                repo=str(run.get("repo") or ""),
                seat=str(run.get("seat") or ""),
                harness=str(run.get("harness") or ""),
                state=state,
                bucket=bucket_for(state, age_seconds=age if live else None, exit_status=exit_status),
                last_ts=last_ts,
                age_seconds=age,
                started_epoch=started.timestamp() if started is not None and live else None,
                elapsed_seconds=elapsed,
                context=run.get("context"),
            )
        )
    return rows


def _contains(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


def row_matches(row: RunRow, query: DashboardQuery) -> bool:
    if query.node and not _contains(row.node_id, query.node):
        return False
    if query.repo and not _contains(row.repo, query.repo):
        return False
    if query.seat and not (_contains(row.seat, query.seat) or _contains(row.harness, query.seat)):
        return False
    if query.state and not (_contains(row.bucket, query.state) or _contains(row.state, query.state)):
        return False
    if query.attention_only and row.bucket not in NEEDS_ATTENTION:
        return False
    return True


def filter_rows(rows: list[RunRow], query: DashboardQuery) -> list[RunRow]:
    if query.include_all:
        kept = rows
    elif query.attention_only:
        kept = [row for row in rows if row.active or row.bucket in {"stale", "unknown"}]
    else:
        kept = [row for row in rows if row.active]
    return [row for row in kept if row_matches(row, query)]


def _rank(row: RunRow) -> int:
    return BUCKET_RANK.get(row.bucket, _IDLE_RANK)


def _elapsed_key(row: RunRow) -> float:
    return -(row.elapsed_seconds if row.elapsed_seconds is not None else -1.0)


def sort_rows(rows: list[RunRow], sort: str) -> list[RunRow]:
    """Order runs; ``attention`` puts failed / awaiting / stale first, oldest first within a bucket."""
    attention = lambda row: (_rank(row), _elapsed_key(row), row.node_id, row.run_id)  # noqa: E731
    keys = {
        "attention": attention,
        "age": lambda row: (_elapsed_key(row), _rank(row), row.node_id, row.run_id),
        "node": lambda row: (row.node_id, *attention(row)),
        "repo": lambda row: (row.repo, *attention(row)),
        "state": lambda row: (_rank(row), row.state, _elapsed_key(row), row.node_id, row.run_id),
        "seat": lambda row: (row.seat, row.harness, *attention(row)),
    }
    return sorted(rows, key=keys.get(sort, attention))


# --- claims ----------------------------------------------------------------


@dataclass(frozen=True)
class ClaimRow:
    target: str
    owner_node: str
    owner_conductor: str
    harness: str
    role: str
    job: str
    session: str
    ttl_remaining: float | None
    expired: bool


def build_claims(claims: list[dict[str, Any]], *, now: datetime) -> list[ClaimRow]:
    rows: list[ClaimRow] = []
    for claim in claims:
        expires = _parse_stamp(claim.get("expires_at"))
        remaining = (expires - now).total_seconds() if expires is not None else None
        expired = bool(claim.get("expired")) or (remaining is not None and remaining <= 0)
        rows.append(
            ClaimRow(
                target=str(claim.get("target") or ""),
                owner_node=str(claim.get("owner_node") or ""),
                owner_conductor=str(claim.get("owner_conductor") or ""),
                harness=str(claim.get("harness") or ""),
                role=str(claim.get("role") or ""),
                job=str(claim.get("job") or ""),
                session=str(claim.get("session") or ""),
                ttl_remaining=remaining,
                expired=expired,
            )
        )
    return rows


# --- formatting ------------------------------------------------------------


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def short_node(node_id: str) -> str:
    """First 12 characters of a node id (uuid4 ids are unique well before that)."""
    return node_id[:12] if len(node_id) > 12 else node_id


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    if total < 86400:
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"
    return f"{total // 86400}d {(total % 86400) // 3600}h"


def format_age(seconds: float | None) -> str:
    return "-" if seconds is None else f"{format_duration(seconds)} ago"


def _badge(bucket: str, *, extra: str = "") -> str:
    icon = _BUCKET_ICON.get(bucket, "?")
    slug = bucket.replace(" ", "-")
    text = f"{icon} {bucket}{extra}"
    return f'<span class="fleet-state fleet-state-{esc(slug)}">{esc(text)}</span>'


def _run_badge(row: RunRow) -> str:
    extra = f" ({row.state})" if row.bucket in NEEDS_ATTENTION or not row.live else ""
    return _badge(row.bucket, extra=extra)


def _elapsed_cell(row: RunRow) -> str:
    text = esc(format_duration(row.elapsed_seconds))
    if row.started_epoch is None:
        return text
    return f'<span class="fleet-elapsed" data-since="{int(row.started_epoch)}">{text}</span>'


def _node_label(node_id: str) -> str:
    return f'<span class="fleet-node" title="{esc(node_id)}">{esc(short_node(node_id))}</span>'


def _claim_text(claim: ClaimRow) -> str:
    who = short_node(claim.owner_node)
    if claim.owner_conductor:
        who += f" · {claim.owner_conductor}"
    if claim.harness:
        who += f" · {claim.harness}"
    if claim.session:
        who += f" · {claim.session}"
    if claim.expired:
        return f"{who} (expired)"
    if claim.ttl_remaining is None:
        return who
    return f"{who} · {format_duration(claim.ttl_remaining)} left"


def _href(query: DashboardQuery, **overrides: object) -> str:
    params: dict[str, object] = {
        "sort": query.sort,
        **query.filters(),
        "attention": "1" if query.attention_only else "",
        "all": "1" if query.include_all else "",
    }
    params.update(overrides)
    view = str(params.pop("view", query.view))
    if params.get("sort") == DEFAULT_SORT:
        params.pop("sort")
    encoded = urlencode({key: str(value) for key, value in params.items() if value not in ("", None, False)})
    base = "/" if view == DEFAULT_VIEW else f"/view/{view}"
    return f"{base}?{encoded}" if encoded else base


# --- boards ----------------------------------------------------------------


def _summary(rows: list[RunRow], node_ids: list[str], claims: list[ClaimRow]) -> str:
    live = [row for row in rows if row.active]
    attention = sum(1 for row in live if row.bucket in NEEDS_ATTENTION)
    active_claims = sum(1 for claim in claims if not claim.expired)
    if not rows and not node_ids:
        text = "No fleet events recorded yet."
    else:
        parts = [
            f"{len(live)} live run{'s' if len(live) != 1 else ''}",
            f"{len(node_ids)} machine{'s' if len(node_ids) != 1 else ''}",
            f"{attention} need{'s' if attention == 1 else ''} attention",
            f"{active_claims} claim{'s' if active_claims != 1 else ''} held",
        ]
        text = ", ".join(parts) + "."
    return f'<div class="page-summary" role="status">{esc(text)}</div>'


def _legend() -> str:
    entries = "".join(
        f'<span class="legend-entry">{esc(_BUCKET_ICON[bucket])} {esc(bucket)}</span>' for bucket in BUCKET_RANK
    )
    return f'<div class="state-legend" aria-label="State legend">{entries}</div>'


def _board_href(query: DashboardQuery, view: str) -> str:
    """Explicit /view/<board> path with the query's filters; never the bare '/'."""
    href = _href(query, view=view)
    if href == "/" or href.startswith("/?"):
        return f"/view/{view}{href[1:]}"
    return href


def _nav(query: DashboardQuery) -> str:
    links = [
        (_board_href(query, "machines"), "machines board"),
        (_board_href(query, "repos"), "repos board"),
    ]
    items = []
    for href, label in links:
        current = ' aria-current="page"' if label.startswith(query.view) else ""
        items.append(f'<a href="{esc(href)}"{current}>{esc(label)}</a>')
    return f'<nav class="board-nav" aria-label="Board views">{" ".join(items)}</nav>'


def _controls(query: DashboardQuery) -> str:
    options = "".join(
        f'<option value="{esc(sort)}"{" selected" if sort == query.sort else ""}>{esc(sort)}</option>' for sort in SORTS
    )
    inputs = "".join(
        f'<label>{esc(key)} <input type="search" name="{esc(key)}" value="{esc(getattr(query, key))}" '
        f'placeholder="{esc("contains")}" maxlength="{_MAX_FILTER_LEN}"></label>'
        for key in FILTER_KEYS
    )
    attention_checked = " checked" if query.attention_only else ""
    all_checked = " checked" if query.include_all else ""
    action = "/" if query.view == DEFAULT_VIEW else f"/view/{query.view}"
    quick = (
        f'<a href="{esc(_href(query, attention="1"))}">{esc("needs attention only")}</a>'
        f' · <a href="{esc(_href(DashboardQuery(view=query.view)))}">{esc("clear")}</a>'
    )
    return (
        f'<form class="fleet-controls" method="get" action="{esc(action)}">'
        f'<label>sort <select name="sort">{options}</select></label>'
        f"{inputs}"
        f'<label><input type="checkbox" name="attention" value="1"{attention_checked}> {esc("needs attention")}</label>'
        f'<label><input type="checkbox" name="all" value="1"{all_checked}> {esc("include finished")}</label>'
        f'<button type="submit">{esc("apply")}</button>'
        f'<span class="fleet-quick">{quick}</span>'
        f"</form>"
    )


def _execution_context(context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    initiator = context.get("initiator", {})
    parts = [f"initiator (reported): {initiator.get('label') or 'unknown'}"]
    for field, label in (("target_host", "target"), ("request_id", "request")):
        if context.get(field):
            parts.append(f"{label}: {context[field]}")
    return '<small class="fleet-execution-context">' + esc(" · ".join(parts)) + "</small>"


def _run_table(rows: list[RunRow], table_id: str, *, with_node: bool) -> str:
    headers = ["Run", "Repo", "Seat/harness", "State", "Elapsed", "Last event"]
    if with_node:
        headers.insert(0, "Node")
    head = "".join(f"<th>{esc(header)}</th>" for header in headers)
    body = []
    for row in rows:
        cells = [
            f'<td class="fleet-run-id" title="{esc(row.run_id)}">{esc(row.run_id)}{_execution_context(row.context)}</td>',
            f"<td>{esc(row.repo or '-')}</td>",
            f"<td>{esc(row.seat_label)}</td>",
            f"<td>{_run_badge(row)}</td>",
            f"<td>{_elapsed_cell(row)}</td>",
            f'<td title="{esc(row.last_ts)}">{esc(format_age(row.age_seconds))}</td>',
        ]
        if with_node:
            cells.insert(0, f"<td>{_node_label(row.node_id)}</td>")
        body.append(f'<tr data-bucket="{esc(row.bucket)}">{"".join(cells)}</tr>')
    if not body:
        return f'<p class="machine-empty">{esc("No runs match.")}</p>'
    return (
        f'<div class="table-wrap"><table class="data-table" id="{esc(table_id)}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _count_strip(rows: list[RunRow]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.bucket] = counts.get(row.bucket, 0) + 1
    if not counts:
        return f'<div class="state-strip"><span class="state-chip">{esc("idle")}</span></div>'
    chips = []
    for bucket, count in sorted(counts.items(), key=lambda item: BUCKET_RANK.get(item[0], _IDLE_RANK)):
        label = f"{_BUCKET_ICON.get(bucket, '?')} {bucket} {count}"
        slug = bucket.replace(" ", "-")
        chips.append(f'<span class="state-chip fleet-state-{esc(slug)}">{esc(label)}</span>')
    return f'<div class="state-strip">{"".join(chips)}</div>'


def _machine_glyph() -> str:
    return (
        '<svg class="machine-glyph" viewBox="0 0 32 32" width="36" height="36" aria-hidden="true" focusable="false">'
        '<rect x="6" y="8" width="20" height="16" rx="2"/><path d="M10 12h12M10 16h12M10 20h8"/></svg>'
    )


def _machine_card(
    node_id: str,
    rows: list[RunRow],
    node_info: dict[str, Any] | None,
    held: list[ClaimRow],
    *,
    now: datetime,
    sort: str,
    unread_count: int = 0,
    truncated_history: bool = False,
) -> str:
    last_seen = _parse_stamp((node_info or {}).get("last_received_at"))
    last_age = (now - last_seen).total_seconds() if last_seen is not None else None
    live_count = sum(1 for row in rows if row.active)
    meta = [f"{live_count} live", f"last event {format_age(last_age)}"]
    events = (node_info or {}).get("events")
    if isinstance(events, int):
        meta.append(f"{events} events")
    if unread_count:
        meta.append(f"{unread_count} unread message{'s' if unread_count != 1 else ''}")
    holds = ""
    if held:
        items = "".join(f"<li>{esc(claim.target)}: {esc(_claim_text(claim))}</li>" for claim in held)
        holds = f'<div class="fleet-holds">{esc("holds")}<ul>{items}</ul></div>'
    history_note = (
        f'<p class="fleet-history-truncated">{esc("older runs omitted; showing a bounded recent history")}</p>'
        if truncated_history
        else ""
    )
    table = _run_table(sort_rows(rows, sort), f"node-{short_node(node_id)}", with_node=False)
    return (
        f'<article class="machine-card" data-node="{esc(node_id)}">'
        f'<header class="machine-card-header">{_machine_glyph()}'
        f'<div class="machine-card-meta">'
        f'<h2 class="machine-card-title" title="{esc(node_id)}">{esc(short_node(node_id))}</h2>'
        f'<div class="machine-card-line">{esc(" · ".join(meta))}</div>'
        f"{_count_strip(rows)}</div></header>"
        f"{holds}{history_note}{table}</article>"
    )


def _machine_base_node_ids(
    rows: list[RunRow], nodes: list[dict[str, Any]], claims: list[ClaimRow], query: DashboardQuery
) -> set[str]:
    node_ids = {row.node_id for row in rows}
    info_ids = {str(node.get("node_id") or "") for node in nodes}
    claim_ids = {claim.owner_node for claim in claims if claim.owner_node and not claim.expired}
    if not query.node and not query.attention_only and not query.seat and not query.state and not query.repo:
        node_ids |= {node_id for node_id in info_ids if node_id}
        node_ids |= claim_ids
    elif query.node:
        node_ids |= {node_id for node_id in info_ids if node_id and _contains(node_id, query.node)}
        node_ids |= {node_id for node_id in claim_ids if _contains(node_id, query.node)}
    return node_ids


def _machine_unread_candidates(
    query: DashboardQuery, unread_counts: dict[str, int], *, excluded_node_ids: set[str] | None = None
) -> list[str]:
    if query.attention_only or query.seat or query.state or query.repo:
        return []
    excluded_node_ids = excluded_node_ids or set()
    return sorted(
        node_id
        for node_id, count in unread_counts.items()
        if count > 0 and node_id not in excluded_node_ids and (not query.node or _contains(node_id, query.node))
    )


def _machine_unread_exclusions(
    query: DashboardQuery, current_base_node_ids: set[str], complete_base_node_ids: set[str]
) -> set[str]:
    excluded = set(current_base_node_ids)
    if not query.node and not query.attention_only and not query.seat and not query.state and not query.repo:
        excluded |= complete_base_node_ids
    elif query.node:
        excluded |= {node_id for node_id in complete_base_node_ids if _contains(node_id, query.node)}
    return excluded


def machine_sort_key(node_id: str, rows: list[RunRow], query: DashboardQuery) -> tuple[Any, ...]:
    """Use the most urgent matching run to order a machine before pagination."""
    keys: list[tuple[Any, ...]] = []
    for row in rows:
        if not (row.live or query.include_all):
            continue
        if query.sort == "age":
            keys.append((_elapsed_key(row), _rank(row)))
        elif query.sort == "state":
            keys.append((_rank(row), row.state, _elapsed_key(row)))
        elif query.sort == "attention":
            keys.append((_rank(row), _elapsed_key(row)))
    if not keys:
        if query.sort == "age":
            return (1.0, _IDLE_RANK, node_id)
        if query.sort == "state":
            return (_IDLE_RANK, "", 1.0, node_id)
        if query.sort == "attention":
            return (_IDLE_RANK, 1.0, node_id)
        return (0, node_id)
    return (*min(keys), node_id)


def _machine_board(
    rows: list[RunRow],
    nodes: list[dict[str, Any]],
    claims: list[ClaimRow],
    query: DashboardQuery,
    *,
    now: datetime,
    unread_counts: dict[str, int] | None = None,
    unread_offset: int = 0,
    unread_only_page: bool = False,
    complete_base_node_ids: set[str] | None = None,
    truncated_node_ids: set[str] | None = None,
) -> str:
    info = {str(node.get("node_id") or ""): node for node in nodes}
    by_node: dict[str, list[RunRow]] = {}
    for row in rows:
        by_node.setdefault(row.node_id, []).append(row)
    unread_counts = unread_counts or {}
    base_node_ids = _machine_base_node_ids(rows, nodes, claims, query)

    node_ids = set() if unread_only_page else base_node_ids
    room = max(0, MACHINE_CARD_LIMIT - len(node_ids))
    excluded_ids = _machine_unread_exclusions(query, base_node_ids, complete_base_node_ids or set())
    unread_ids = _machine_unread_candidates(query, unread_counts, excluded_node_ids=excluded_ids)
    node_ids.update(unread_ids[unread_offset : unread_offset + room])

    def card_key(node_id: str) -> tuple[Any, ...]:
        return machine_sort_key(node_id, by_node.get(node_id, []), query)

    cards = []
    for node_id in sorted(node_ids, key=card_key)[:MACHINE_CARD_LIMIT]:
        held = [claim for claim in claims if claim.owner_node == node_id and not claim.expired]
        cards.append(
            _machine_card(
                node_id,
                by_node.get(node_id, []),
                info.get(node_id),
                held,
                now=now,
                sort=query.sort,
                unread_count=unread_counts.get(node_id, 0),
                truncated_history=node_id in (truncated_node_ids or set()),
            )
        )
    if not cards:
        return f'<p class="machine-empty">{esc("No machines match.")}</p>'
    return f'<section class="machine-board" aria-label="Machines">{"".join(cards)}</section>'


@dataclass
class _RepoEntry:
    repo: str
    live: list[RunRow]  # rows shown after filtering
    all_live: list[RunRow]  # every active run, so parallel context survives a filter
    claim: ClaimRow | None
    last_outcome: RunRow | None

    @property
    def collision(self) -> bool:
        nodes = {row.node_id for row in self.all_live if row.active}
        return len(nodes) > 1

    @property
    def rank(self) -> int:
        return min((_rank(row) for row in self.live), default=_IDLE_RANK)


def _repo_entries(all_rows: list[RunRow], claims: list[ClaimRow], query: DashboardQuery) -> list[_RepoEntry]:
    live_by_repo: dict[str, list[RunRow]] = {}
    abandoned_by_repo: dict[str, list[RunRow]] = {}
    outcome_by_repo: dict[str, RunRow] = {}
    for row in all_rows:
        if not row.repo:
            continue
        if row.active:
            live_by_repo.setdefault(row.repo, []).append(row)
        elif row.live:
            abandoned_by_repo.setdefault(row.repo, []).append(row)
        else:
            current = outcome_by_repo.get(row.repo)
            if current is None or (row.age_seconds or 0) < (current.age_seconds or 0):
                outcome_by_repo[row.repo] = row
    claim_by_repo = {claim.target: claim for claim in claims if not claim.expired}
    repos = set(live_by_repo) | set(outcome_by_repo) | set(claim_by_repo)
    if query.attention_only or query.include_all:
        repos |= set(abandoned_by_repo)
    entries = []
    for repo in sorted(repos):
        if query.repo and not _contains(repo, query.repo):
            continue
        all_live = live_by_repo.get(repo, [])
        display_runs = list(all_live)
        if query.attention_only or query.include_all:
            display_runs.extend(abandoned_by_repo.get(repo, []))
        live = [row for row in display_runs if row_matches(row, query)]
        claim = claim_by_repo.get(repo)
        entry = _RepoEntry(repo=repo, live=live, all_live=all_live, claim=claim, last_outcome=outcome_by_repo.get(repo))
        narrowed = bool(query.seat or query.state or query.attention_only)
        if query.node and not live and not (claim and _contains(claim.owner_node, query.node)):
            continue
        if narrowed and not live:
            continue
        entries.append(entry)
    return entries


def _repo_sort_key(entry: _RepoEntry, sort: str) -> tuple[Any, ...]:
    oldest = max((row.elapsed_seconds or 0.0 for row in entry.live), default=-1.0)
    first_node = min((row.node_id for row in entry.live), default=entry.claim.owner_node if entry.claim else "~")
    first_seat = min((row.seat_label for row in entry.live), default="~")
    if sort == "repo":
        return (entry.repo,)
    if sort == "age":
        return (-oldest, entry.rank, entry.repo)
    if sort == "node":
        return (first_node, entry.rank, entry.repo)
    if sort == "seat":
        return (first_seat, entry.rank, entry.repo)
    return (entry.rank, -oldest, entry.repo)


def _repo_board(entries: list[_RepoEntry], sort: str) -> str:
    headers = ["Repo", "Running where", "Claim", "Last outcome", "Flags"]
    head = "".join(f"<th>{esc(header)}</th>" for header in headers)
    body = []
    for entry in sorted(entries, key=lambda item: _repo_sort_key(item, sort)):
        if entry.live:
            where = "".join(
                f"<li>{_node_label(row.node_id)} · {esc(row.seat_label)} · {_run_badge(row)} · {_elapsed_cell(row)}"
                f' <span class="fleet-run-id" title="{esc(row.run_id)}">{esc(row.run_id)}</span>{_execution_context(row.context)}</li>'
                for row in sort_rows(entry.live, sort)
            )
            where = f'<ul class="fleet-where">{where}</ul>'
        else:
            where = esc("idle")
        claim = esc(_claim_text(entry.claim)) if entry.claim else esc("-")
        if entry.last_outcome is not None:
            outcome = f"{_badge(entry.last_outcome.bucket)} {esc(format_age(entry.last_outcome.age_seconds))}"
        else:
            outcome = esc("-")
        flags = (
            "<details><summary>parallel activity</summary>"
            "<p>Same repo label on multiple machines. File overlap unknown.</p></details>"
            if entry.collision
            else esc("-")
        )
        body.append(
            f'<tr data-collision="{"1" if entry.collision else "0"}">'
            f"<td>{esc(entry.repo)}</td><td>{where}</td><td>{claim}</td><td>{outcome}</td><td>{flags}</td></tr>"
        )
    if not body:
        return f'<p class="machine-empty">{esc("No repos match.")}</p>'
    return (
        f'<div class="table-wrap"><table class="data-table" id="repo-board"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _agent_board(unread_counts: dict[str, int], more: bool = False) -> str:
    rows = "".join(
        f'<tr data-principal="{esc(principal)}"><td class="fleet-node">{esc(principal)}</td>'
        f"<td>{count} unread message{'s' if count != 1 else ''}</td></tr>"
        for principal, count in sorted(unread_counts.items())
        if count > 0
    )
    if not rows:
        return ""
    note = '<p class="fleet-more">More agents have unread messages than are shown.</p>' if more else ""
    return (
        '<section aria-label="Agents"><h2>Agents</h2>'
        '<div class="table-wrap"><table class="data-table"><thead><tr>'
        '<th scope="col">Principal</th><th scope="col">Inbox</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>{note}</section>"
    )


# --- page ------------------------------------------------------------------


def render_page(
    *,
    view: str,
    query_string: str,
    runs: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    started_at: dict[tuple[str, str], str],
    nonce: str,
    now: datetime | None = None,
    more_href: str | None = None,
    unread_counts: dict[str, int] | None = None,
    agent_unread_counts: dict[str, int] | None = None,
    agent_unread_more: bool = False,
    unread_offset: int = 0,
    unread_only_page: bool = False,
    complete_base_node_ids: set[str] | None = None,
    truncated_node_ids: set[str] | None = None,
) -> str:
    """Render the full dashboard document for one board.

    ``more_href`` is supplied by the hub when the bounded latest-state page
    has another LIMIT page. The renderer does not query the journal.
    """
    current = now or datetime.now(timezone.utc)
    query = parse_query(query_string, view=view)
    all_rows = build_rows(runs, started_at, now=current)
    claim_rows = build_claims(claims, now=current)
    visible = filter_rows(all_rows, query)
    node_ids = sorted(
        {str(node.get("node_id") or "") for node in nodes}
        | {row.node_id for row in all_rows}
        | {claim.owner_node for claim in claim_rows if claim.owner_node and not claim.expired}
        | {node_id for node_id, count in (unread_counts or {}).items() if count > 0}
    )
    node_ids = [node_id for node_id in node_ids if node_id]
    if query.view == "repos":
        board = _repo_board(_repo_entries(all_rows, claim_rows, query), query.sort)
        title = "Fleet: Repos"
    else:
        board = _machine_board(
            visible,
            nodes,
            claim_rows,
            query,
            now=current,
            unread_counts=unread_counts,
            unread_offset=unread_offset,
            unread_only_page=unread_only_page,
            complete_base_node_ids=complete_base_node_ids,
            truncated_node_ids=truncated_node_ids,
        )
        board += _agent_board(agent_unread_counts or {}, agent_unread_more)
        title = "Fleet: Machines"
    stamp = current.astimezone(timezone.utc).strftime("%H:%M:%S UTC")
    more = f'<p class="fleet-more"><a href="{esc(more_href)}">{esc("more")}</a></p>' if more_href else ""
    heading = "Machines" if query.view != "repos" else "Repos"
    nav = _nav(query)
    body = (
        '<main class="deck-shell fleet-board">'
        f'<header class="page-head"><div><h1>{esc(heading)} board</h1>'
        f'<p class="page-sub">Refreshes every {REFRESH_SECONDS}s</p></div>'
        f'<p class="header-meta">Updated {esc(stamp)} '
        f'<a class="refresh-link" href="{esc(_href(query))}">Refresh</a></p></header>'
        f"{nav}"
        f"{_summary(all_rows, node_ids, claim_rows)}{_legend()}{_controls(query)}"
        f'<label class="fleet-quick-filter">{esc("filter rows")} '
        f'<input type="search" data-filter-all="1" placeholder="{esc("type to narrow (needs JS)")}"></label>'
        f"{board}{more}"
        "</main>"
    )
    return _document(f"{title} - Brigade Fleet", nonce, body, now=current)


_BOARD_CSS = """
.fleet-board .page-summary { margin: var(--space-4) 0 var(--space-2); font-weight: 600; color: var(--ink); }
.fleet-board .state-legend { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-3); margin-bottom: var(--space-3); font-size: 12px; color: var(--muted); }
.fleet-board .legend-entry { white-space: nowrap; }
.fleet-board .fleet-controls { display: flex; flex-wrap: wrap; gap: var(--space-3) var(--space-4); align-items: end; margin-bottom: var(--space-3); font-size: 12px; color: var(--muted); }
.fleet-board .fleet-controls label { display: grid; gap: 6px; min-width: 0; }
.fleet-board .fleet-controls input, .fleet-board .fleet-controls select { max-width: 100%; min-width: 0; border-radius: var(--radius-control); text-transform: none; }
.fleet-board .fleet-controls input[type="search"], .fleet-board .fleet-controls select { min-height: 36px; padding: var(--space-2) 10px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface-raised); color: var(--ink); font: inherit; width: 10rem; }
.fleet-board .fleet-controls button { min-height: var(--space-8); padding: 0 14px; font: inherit; font-weight: 500; cursor: pointer; }
.fleet-board .fleet-quick { color: var(--muted); }
.fleet-board .fleet-quick-filter { display: grid; gap: 6px; margin-bottom: var(--space-4); font-size: 12px; color: var(--muted); }
.fleet-board .fleet-quick-filter input { min-height: 36px; padding: var(--space-2) 10px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface-raised); color: var(--ink); font: inherit; width: 100%; max-width: 24rem; min-width: 0; }
.fleet-board .machine-board { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(840px, 100%), 1fr)); gap: var(--space-4); margin-bottom: var(--space-4); }
.fleet-board .machine-card { min-width: 0; padding: var(--space-4); border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--surface); }
.fleet-board .machine-card-header { display: flex; gap: var(--space-3); align-items: center; margin-bottom: var(--space-3); }
.fleet-board .machine-glyph { flex: 0 0 auto; stroke: var(--muted); fill: none; stroke-width: 1.6; }
.fleet-board .machine-card-meta { min-width: 0; flex: 1; }
.fleet-board .machine-card-title { margin: 0; font-size: 14px; font-weight: 700; color: var(--ink); font-family: var(--font-mono); }
.fleet-board .machine-card-line { color: var(--muted); font-size: 12px; }
.fleet-board .state-strip { display: flex; flex-wrap: wrap; gap: 6px; margin-top: var(--space-2); }
.fleet-board .state-chip { border: 1px solid var(--line); border-radius: 999px; padding: 2px var(--space-2); font-size: 11px; color: var(--muted); }
.fleet-board .fleet-holds { font-size: 12px; color: var(--muted); margin-bottom: var(--space-3); }
.fleet-board .fleet-holds ul { margin: 0; padding-left: 1.2rem; }
.fleet-board table.data-table { table-layout: auto; }
.fleet-board .machine-card table.data-table, .fleet-board #repo-board { min-width: 800px; }
.fleet-board table.data-table th:nth-child(n) { width: auto; overflow-wrap: normal; white-space: nowrap; }
.fleet-board .fleet-run-id, .fleet-board .fleet-node { font-family: var(--font-mono); font-size: 12px; word-break: break-all; }
.fleet-board .fleet-where { margin: 0; padding-left: 1.1rem; }
.fleet-board .fleet-state { display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 2px var(--space-2); font-size: 11px; font-weight: 700; white-space: nowrap; color: var(--ink); }
.fleet-board .fleet-execution-context { display: block; color: var(--muted); }
.fleet-board .fleet-state-running { border-color: var(--signal); color: var(--signal); }
.fleet-board .fleet-state-succeeded { border-color: var(--ok); color: var(--ok); }
.fleet-board .fleet-state-awaiting-approval { border-color: var(--signal); background: var(--signal-quiet); }
.fleet-board .fleet-state-failed { border-color: var(--bad); color: var(--bad); }
.fleet-board .fleet-state-stale { border-style: dashed; border-color: var(--bad); }
.fleet-board .fleet-state-queued, .fleet-board .fleet-state-interrupted { border-style: dashed; }
.fleet-board .machine-empty { margin: 0; color: var(--muted); font-size: 12px; }
.fleet-board .fleet-more { margin: var(--space-4) 0 0; font-size: 12px; }
"""

_SCRIPT = """(function () {
  function fmt(s) {
    s = Math.max(0, Math.floor(s));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
    if (s < 86400) return Math.floor(s / 3600) + "h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m";
    return Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h";
  }
  function tick() {
    var now = Date.now() / 1000;
    var els = document.querySelectorAll("[data-since]");
    for (var i = 0; i < els.length; i++) {
      els[i].textContent = fmt(now - Number(els[i].getAttribute("data-since")));
    }
  }
  setInterval(tick, 1000);
  document.addEventListener("input", function (e) {
    var input = e.target;
    if (!input || !input.getAttribute || !input.hasAttribute("data-filter-all")) return;
    var query = (input.value || "").toLowerCase();
    var rows = document.querySelectorAll("table.data-table tbody tr");
    for (var i = 0; i < rows.length; i++) {
      var text = (rows[i].textContent || "").toLowerCase();
      rows[i].hidden = query !== "" && text.indexOf(query) === -1;
    }
  });
})();"""


def _document(title: str, nonce: str, body: str, *, now: datetime) -> str:
    return fleet_command_deck._document(
        body,
        title=title,
        nonce=nonce,
        now=now,
        active="boards",
        refresh_seconds=REFRESH_SECONDS,
        extra_css=_BOARD_CSS,
        script=_SCRIPT,
    )
