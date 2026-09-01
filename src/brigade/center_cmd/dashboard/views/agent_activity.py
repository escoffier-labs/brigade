"""Read-only Agent Activity dashboard view - machine cards first, table secondary.

Operator question this view answers: what are the agents doing right now
across the fleet?
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade.center_cmd import agent_activity as activity_records
from brigade.center_cmd.dashboard import render as html

NAME = "agents"
TITLE = "Agent Activity"
ORDER = 6.5

_STATE_ICON = {
    "queued": "○",
    "running": "●",
    "awaiting approval": "?",
    "blocked": "!",
    "succeeded": "✓",
    "failed": "×",
    "stale": "⌛",
    "unknown": "?",
}
_LIVE_RANK = {
    "running": 0,
    "awaiting approval": 1,
    "blocked": 2,
    "failed": 3,
    "queued": 4,
    "unknown": 5,
    "stale": 6,
    "succeeded": 9,
}
_NEEDS_ATTENTION = frozenset({"running", "awaiting approval", "blocked", "failed"})
_RECENCY_WINDOW_SECONDS = 12 * 3600
_ANTHROPIC_CORAL = "#d97757"


def fetch(target: Path) -> dict:
    # In-process collect: a fresh `python -m brigade center activity` subprocess
    # paid ~15s of import tax on this host and made the view look hung.
    records = activity_records.collect(target)
    return {
        "activity_envelope_version": activity_records.ACTIVITY_ENVELOPE_VERSION,
        "agent_activity": records,
        "agent_activity_count": len(records),
        "agent_activity_summary": activity_records.summary(records),
        "completed_window_seconds": activity_records.completed_window_seconds(target),
        "default_hosts": list(activity_records.default_hosts(target)),
        "host_kinds": activity_records.host_kinds(target),
        "local_host": activity_records.local_host_alias(target),
    }


def render(payload: dict, nonce: str) -> str:
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))
    records = [item for item in payload.get("agent_activity", []) if isinstance(item, dict)]
    window = payload.get("completed_window_seconds")
    if not isinstance(window, int) or window < _RECENCY_WINDOW_SECONDS:
        window = _RECENCY_WINDOW_SECONDS
    now = datetime.now(timezone.utc)
    hosts = [str(name) for name in payload.get("default_hosts", []) if isinstance(name, str)]
    kinds = payload.get("host_kinds")
    kinds = {str(k): str(v) for k, v in kinds.items()} if isinstance(kinds, dict) else {"cloud": "cloud"}
    local_host = payload.get("local_host")
    local_host = local_host if isinstance(local_host, str) and local_host else "local"
    summary = _summary_strip(records, local_host)
    legend = _state_legend()
    cards = _machine_cards_html(records, window, now, hosts, kinds, local_host)
    table_panel = html.panel(html.esc("Table"), _table(records, now))
    return _stylesheet(nonce) + summary + legend + cards + table_panel


def _bucket_state(record: dict) -> str:
    state = str(record.get("state") or "").strip().lower()
    return state if state in _STATE_ICON else "unknown"


def _summary_strip(records: list[dict], local_host: str = "local") -> str:
    counts: dict[str, int] = {}
    blocked_hosts: set[str] = set()
    for record in records:
        state = _bucket_state(record)
        counts[state] = counts.get(state, 0) + 1
        if state == "blocked":
            host = str(record.get("host") or "local").lower()
            if host == "local":
                host = local_host
            blocked_hosts.add(host)
    total = len(records)
    if not total:
        text = "No agent activity recorded right now."
    else:
        parts = [f"{total} agents tracked"]
        running = counts.get("running", 0)
        parts.append(f"{running} running")
        blocked = counts.get("blocked", 0)
        if blocked:
            hosts = ", ".join(sorted(blocked_hosts)) or "unknown host"
            parts.append(f"{blocked} blocked on {hosts}")
        failed = counts.get("failed", 0)
        if failed:
            parts.append(f"{failed} failed")
        awaiting = counts.get("awaiting approval", 0)
        if awaiting:
            parts.append(f"{awaiting} awaiting approval")
        unknown = counts.get("unknown", 0)
        if unknown:
            parts.append(f"{unknown} unknown")
        text = ", ".join(parts) + "."
    return f'<div class="page-summary" role="status">{html.esc(text)}</div>'


def _state_legend() -> str:
    entries = [
        f'<span class="legend-entry">{html.esc(icon)} {html.esc(state)}</span>' for state, icon in _STATE_ICON.items()
    ]
    return f'<div class="state-legend" aria-label="State legend">{"".join(entries)}</div>'


def _machine_cards_html(
    records: list[dict],
    window: int,
    now: datetime,
    default_hosts: list[str],
    kinds: dict[str, str],
    local_host: str,
) -> str:
    by_host: dict[str, list[dict]] = {}
    for record in records:
        host = str(record.get("host") or "local").lower()
        if host == "local":
            host = local_host
        by_host.setdefault(host, []).append(record)
    hosts: list[str] = []
    for name in default_hosts:
        if name not in hosts:
            hosts.append(name)
    for name in sorted(by_host):
        if name not in hosts and name != "cloud":
            hosts.append(name)
    hosts.append("cloud")
    cards = [_machine_card(host, by_host.get(host, []), window, now, kinds) for host in hosts]
    return f'<section class="machine-board" aria-label="{html.esc(TITLE)}">{"".join(cards)}</section>'


def _machine_card(host: str, records: list[dict], window: int, now: datetime, kinds: dict[str, str]) -> str:
    kind = kinds.get(host, "server")
    live, completed = _partition_records(records, window, now)
    live_sorted = sorted(live, key=_sort_key)
    trees = _build_trees(live_sorted)
    tiles = "".join(_tile(node, children, depth=0, now=now) for node, children in trees)
    if host == "cloud" and not records:
        tiles = f'<p class="cloud-placeholder">{html.esc("Cloud tracking not wired — see #890")}</p>'
    completed_block = ""
    if completed:
        completed_trees = _build_trees(sorted(completed, key=_sort_key))
        completed_tiles = "".join(_tile(node, children, depth=0, now=now) for node, children in completed_trees)
        completed_block = (
            f'<details class="completed-expander">'
            f"<summary>{html.esc(f'Show {len(completed)} older')}</summary>"
            f'<div class="agent-tile-list">{completed_tiles}</div></details>'
        )
    return (
        f'<article class="machine-card" data-host="{html.esc(host)}" data-kind="{html.esc(kind)}">'
        f'<header class="machine-card-header">'
        f"{_machine_glyph(kind)}"
        f'<div class="machine-card-meta">'
        f'<h2 class="machine-card-title" title="{html.esc(host)}">{html.esc(host)}</h2>'
        f"{_count_strip(records)}"
        f"</div></header>"
        f'<div class="agent-tile-list">{tiles or _empty_host()}</div>'
        f"{completed_block}"
        f"</article>"
    )


def _empty_host() -> str:
    return f'<p class="machine-empty">{html.esc("No recent agents on this host.")}</p>'


def _partition_records(records: list[dict], window: int, now: datetime) -> tuple[list[dict], list[dict]]:
    live: list[dict] = []
    completed: list[dict] = []
    for record in records:
        if _is_collapsed_completed(record, window, now):
            completed.append(record)
        else:
            live.append(record)
    return live, completed


def _is_collapsed_completed(record: dict, window: int, now: datetime) -> bool:
    state = str(record.get("state") or "")
    if state in _NEEDS_ATTENTION:
        return False
    parsed = _parse_stamp(record.get("last_updated_at") or record.get("started_at"))
    if parsed is None:
        return True
    return (now - parsed).total_seconds() > window


def _parse_stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hours_ago(record: dict, now: datetime) -> int | None:
    parsed = _parse_stamp(record.get("last_updated_at") or record.get("started_at"))
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds() // 3600))


def _state_label(record: dict, now: datetime) -> str:
    state = str(record.get("state") or "unknown")
    icon = _STATE_ICON.get(state, "?")
    if state != "stale":
        return f"{icon} {state}"
    hours = _hours_ago(record, now)
    if hours is None:
        return f"{icon} stale"
    return f"{icon} stale (last seen {hours}h ago)"


def _sort_key(record: dict) -> tuple[int, float]:
    state = str(record.get("state") or "unknown")
    stamp = record.get("last_updated_at") or record.get("started_at")
    return (_LIVE_RANK.get(state, 8), -_stamp_score(stamp))


def _stamp_score(value: object) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _build_trees(records: list[dict]) -> list[tuple[dict, list]]:
    by_id = {str(item.get("activity_id")): item for item in records if item.get("activity_id")}
    children_map: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for record in records:
        parent = record.get("parent_activity_id")
        if isinstance(parent, str) and parent in by_id:
            children_map.setdefault(parent, []).append(record)
        else:
            roots.append(record)

    def branch(node: dict) -> tuple[dict, list]:
        kids = [branch(child) for child in children_map.get(str(node.get("activity_id")), [])]
        return node, kids

    return [branch(root) for root in roots]


def _tile(record: dict, children: list, *, depth: int, now: datetime) -> str:
    state = str(record.get("state") or "unknown")
    state_label = _state_label(record, now)
    mark, glyph = _provider_mark(record)
    model = record.get("model")
    model_html = (
        f'<div class="agent-tile-model" title="{html.esc(model)}">{html.esc(model)}</div>'
        if isinstance(model, str) and model
        else ""
    )
    task = str(record.get("task_label") or "Unknown task")
    child_class = " agent-tile-child" if depth else ""
    nested = "".join(_tile(child, grand, depth=depth + 1, now=now) for child, grand in children)
    nested_html = f'<div class="agent-tile-children">{nested}</div>' if nested else ""
    details = _details(record)
    provider = str(record.get("provider") or "unknown")
    return (
        f'<div class="agent-tile{child_class}" data-provider="{html.esc(provider)}">'
        f'<div class="agent-tile-main">'
        f'<div class="provider-badge" data-mark="{html.esc(mark)}" title="{html.esc(provider)}" '
        f'aria-label="{html.esc(provider)}">'
        f"{glyph}{model_html}</div>"
        f'<div class="agent-tile-body">'
        f'<div class="agent-tile-task" title="{html.esc(task)}">{html.esc(task)}</div>'
        f'<div class="agent-tile-meta">'
        f'<span class="agent-state agent-state-{html.esc(state.replace(" ", "-"))}">'
        f"{html.esc(state_label)}</span>"
        f'<span class="agent-elapsed">{html.esc(_elapsed(record.get("elapsed_seconds")))}</span>'
        f"</div></div></div>"
        f"{nested_html}{details}</div>"
    )


def _provider_mark(record: dict) -> tuple[str, str]:
    provider = str(record.get("provider") or "").lower()
    harness = str(record.get("harness") or "").lower()
    if provider in {"cursor"} or "cursor" in harness:
        return "cursor", _cursor_glyph()
    if provider in {"codex", "openai"} or "codex" in harness:
        return "codex", _codex_glyph()
    if provider in {"claude", "anthropic"} or "claude" in harness:
        return "claude", _anthropic_glyph()
    if provider == "t3" or harness.startswith("t3"):
        return "t3", _t3_glyph()
    if provider == "brigade" or "brigade" in harness:
        return "brigade", "<span>Br</span>"
    fallback = html.esc((provider[:2] or "??").title())
    return provider or "unknown", f"<span>{fallback}</span>"


def _svg(body: str, *, view_box: str = "0 0 32 32") -> str:
    return (
        f'<svg class="provider-glyph" viewBox="{view_box}" width="26" height="26" '
        f'aria-hidden="true" focusable="false">{body}</svg>'
    )


def _cursor_glyph() -> str:
    """Isometric cube with a north-east arrow - Cursor's private-dashboard mark."""
    return _svg(
        '<polygon points="16,3 29,10.5 16,18 3,10.5" fill="#1a1a1a"/>'
        '<polygon points="3,10.5 16,18 16,29 3,21.5" fill="#555"/>'
        '<polygon points="16,18 29,10.5 29,21.5 16,29" fill="#111"/>'
        '<path d="M13.5 18.5 L22 8.5 L22 13 L27 13 L18.5 23 Z" fill="#fff"/>'
    )


def _codex_glyph() -> str:
    """OpenAI hexagonal knot, scaled to the badge box."""
    return _svg(
        '<path fill="#111" d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/>',
        view_box="0 0 24 24",
    )


def _anthropic_glyph() -> str:
    """Radial 8-arm asterisk in Anthropic coral."""
    return _svg(
        f'<g fill="{_ANTHROPIC_CORAL}" transform="translate(16 16)">'
        '<rect x="-1.2" y="-13" width="2.4" height="8.6" rx="1.1"/>'
        '<rect x="-1.2" y="4.4" width="2.4" height="8.6" rx="1.1"/>'
        '<rect x="-13" y="-1.2" width="8.6" height="2.4" rx="1.1"/>'
        '<rect x="4.4" y="-1.2" width="8.6" height="2.4" rx="1.1"/>'
        '<g transform="rotate(45)">'
        '<rect x="-1.2" y="-13" width="2.4" height="8.6" rx="1.1"/>'
        '<rect x="-1.2" y="4.4" width="2.4" height="8.6" rx="1.1"/>'
        '<rect x="-13" y="-1.2" width="8.6" height="2.4" rx="1.1"/>'
        '<rect x="4.4" y="-1.2" width="8.6" height="2.4" rx="1.1"/>'
        "</g></g>"
    )


def _t3_glyph() -> str:
    """Geometric T3 wordmark - no font dependency."""
    return _svg(
        '<path fill="none" stroke="#111" stroke-width="2.6" stroke-linecap="round" '
        'stroke-linejoin="round" d="M3 8.8h12.2M9.1 8.8v15"/>'
        '<path fill="none" stroke="#111" stroke-width="2.6" stroke-linecap="round" '
        'stroke-linejoin="round" d="M17.4 9.2h9.2a3.6 3.6 0 0 1 0 7.1H20.4m0 0h6.6a3.9 3.9 0 0 1 0 8.1H17.2"/>'
    )


def _count_strip(records: list[dict]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        state = _bucket_state(record)
        counts[state] = counts.get(state, 0) + 1
    if not counts:
        return f'<div class="state-strip"><span class="state-chip state-chip-empty">{html.esc("0")}</span></div>'
    chips = []
    for state, count in sorted(counts.items(), key=lambda item: _LIVE_RANK.get(item[0], 8)):
        icon = _STATE_ICON.get(state, "?")
        label = state
        chips.append(
            f'<span class="state-chip agent-state-{html.esc(label.replace(" ", "-"))}" '
            f'title="{html.esc(label)}">{html.esc(f"{icon} {label} {count}")}</span>'
        )
    return f'<div class="state-strip">{"".join(chips)}</div>'


def _table(records: list[dict], now: datetime) -> str:
    headers = ["Provider", "Host", "Task", "Model", "State", "Started", "Elapsed", "Last update", "Details"]
    return html.table(
        [html.esc(header) for header in headers],
        [_row(item, records, now) for item in records],
    )


def _row(record: dict, records: list[dict], now: datetime) -> list[str]:
    state = str(record.get("state") or "unknown")
    state_label = _state_label(record, now)
    task = f"{'↳ ' * _depth(record, records)}{record.get('task_label') or 'Unknown task'}"
    return [
        html.esc(f"{record.get('provider', 'unknown')} · {record.get('harness', 'observation')}"),
        html.esc(record.get("host") or "local"),
        html.esc(task),
        html.esc(record.get("model") or "-"),
        f'<span class="agent-state agent-state-{html.esc(state.replace(" ", "-"))}">{html.esc(state_label)}</span>',
        html.esc(record.get("started_at") or "-"),
        html.esc(_elapsed(record.get("elapsed_seconds"))),
        html.esc(record.get("last_updated_at") or "-"),
        _details(record),
    ]


def _details(record: dict) -> str:
    detail = {
        key: record.get(key) for key in ("activity_id", "parent_activity_id", "label", "model", "source", "links")
    }
    return (
        f"<details><summary>Details</summary>"
        f"<pre>{html.esc(json.dumps(detail, indent=2, sort_keys=True))}</pre></details>"
    )


def _depth(record: dict, records: list[dict]) -> int:
    parents = {str(item.get("activity_id")): item for item in records}
    parent, depth = record.get("parent_activity_id"), 0
    while isinstance(parent, str) and parent in parents and depth < 4:
        depth += 1
        parent = parents[parent].get("parent_activity_id")
    return depth


def _elapsed(value: object) -> str:
    if not isinstance(value, int):
        return "-"
    minutes, seconds = divmod(value, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"


def _machine_glyph(kind: str) -> str:
    """Neutral inline SVG glyphs - no third-party logos."""
    if kind == "workstation":
        body = '<rect x="4" y="6" width="24" height="16" rx="2"/><path d="M10 26h12"/><path d="M16 22v4"/>'
    elif kind == "gpu":
        body = '<rect x="8" y="4" width="16" height="24" rx="2"/><path d="M12 8h8M12 12h8M12 16h8M12 20h8"/>'
    elif kind == "desktop":
        body = (
            '<rect x="6" y="6" width="20" height="14" rx="1"/>'
            '<path d="M12 24h8"/><path d="M16 20v4"/>'
            '<rect x="10" y="10" width="12" height="6"/>'
        )
    elif kind == "cloud":
        body = '<path d="M10 20h14a6 6 0 0 0 0-12 8 8 0 0 0-15 3 5 5 0 0 0 1 9z"/>'
    else:
        body = '<rect x="6" y="8" width="20" height="16" rx="2"/><path d="M10 12h12M10 16h12M10 20h8"/>'
    return (
        f'<svg class="machine-glyph" viewBox="0 0 32 32" width="36" height="36" '
        f'aria-hidden="true" focusable="false">{body}</svg>'
    )


def _stylesheet(nonce: str) -> str:
    return f'''<style nonce="{html.esc(nonce)}">
.page-summary {{
  margin-bottom: 0.5rem;
  font-weight: 600;
  color: #111;
}}
.state-legend {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  margin-bottom: 0.75rem;
  font-size: 0.8rem;
  color: #333;
}}
.legend-entry {{ white-space: nowrap; }}
.machine-board {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1rem;
  margin-bottom: 1rem;
}}
.machine-card {{
  border: 1px solid #ddd;
  border-radius: 0.35rem;
  background: #fafafa;
  padding: 0.75rem;
  color: #111;
}}
.machine-card-header {{
  display: flex;
  gap: 0.75rem;
  align-items: center;
  margin-bottom: 0.75rem;
}}
.machine-glyph {{
  flex: 0 0 auto;
  stroke: #333;
  fill: none;
  stroke-width: 1.6;
}}
.machine-card-title {{
  margin: 0;
  font-size: 1rem;
  font-weight: 650;
  color: #111;
  max-width: 12rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.machine-card-meta {{
  min-width: 0;
  flex: 1;
}}
.state-strip {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  margin-top: 0.35rem;
}}
.state-chip {{
  border: 1px solid #666;
  border-radius: 999px;
  padding: 0.05rem 0.4rem;
  font-size: 0.8rem;
  color: #111;
  background: #fff;
}}
.agent-tile-list {{
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}}
.agent-tile {{
  border: 1px solid #ddd;
  border-radius: 0.3rem;
  background: #fff;
  padding: 0.55rem 0.65rem;
  color: #111;
}}
.agent-tile-child {{
  margin-left: 1rem;
  border-left: 2px solid #ccc;
}}
.agent-tile-main {{
  display: flex;
  gap: 0.65rem;
  align-items: flex-start;
  min-width: 0;
}}
.provider-badge {{
  flex: 0 0 auto;
  width: 2.6rem;
  text-align: center;
  border: 1px solid #666;
  border-radius: 0.35rem;
  padding: 0.25rem 0.15rem;
  background: #f5f5f5;
  color: #111;
  font-weight: 700;
  font-size: 0.85rem;
  display: flex;
  flex-direction: column;
  align-items: center;
}}
.provider-glyph {{
  display: block;
  width: 1.65rem;
  height: 1.65rem;
}}
.agent-tile-model {{
  margin-top: 0.15rem;
  font-size: 0.65rem;
  font-weight: 500;
  color: #333;
  max-width: 2.4rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.agent-tile-body {{
  min-width: 0;
  flex: 1;
}}
.agent-tile-task {{
  font-weight: 550;
  color: #111;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}}
.agent-tile-meta {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-top: 0.3rem;
  align-items: center;
}}
.agent-elapsed {{
  color: #333;
  font-size: 0.85rem;
}}
.agent-tile-children {{
  margin-top: 0.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}}
.agent-state {{
  border: 1px solid #666;
  border-radius: 999px;
  padding: 0.1rem 0.45rem;
  font-weight: 600;
  white-space: nowrap;
  color: #111;
  background: #fff;
}}
.agent-state-running {{ border-color: #1769aa; }}
.agent-state-stale, .agent-state-unknown {{ border-style: dashed; }}
.agent-state-failed, .agent-state-blocked {{ border-color: #8b0000; }}
.completed-expander {{
  margin-top: 0.75rem;
  color: #333;
}}
.completed-expander > summary {{
  cursor: pointer;
  font-size: 0.9rem;
}}
.machine-empty, .cloud-placeholder {{
  margin: 0;
  color: #333;
  font-size: 0.9rem;
}}
</style>'''
