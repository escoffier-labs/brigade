"""Read-only Agent Activity dashboard view - machine cards first, table secondary."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade.center_cmd import agent_activity as activity_records
from brigade.center_cmd.dashboard import data
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
_PROVIDER_MONOGRAM = {
    "cursor": "Cu",
    "codex": "Cx",
    "claude": "Cl",
    "anthropic": "Cl",
    "t3": "T3",
    "brigade": "Br",
}
_MACHINE_KIND = {
    "rocinante": "workstation",
    "shadowfax": "gpu",
    "gandalf": "desktop",
    "cloud": "cloud",
}


def fetch(target: Path) -> dict:
    payload = data.run_json(target, ["center", "activity", "--limit", "100"])
    if "error" not in payload:
        payload.setdefault("completed_window_seconds", activity_records.completed_window_seconds(target))
    return payload


def render(payload: dict, nonce: str) -> str:
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))
    records = [item for item in payload.get("agent_activity", []) if isinstance(item, dict)]
    window = payload.get("completed_window_seconds")
    if not isinstance(window, int) or window < 60:
        window = 3600
    now = datetime.now(timezone.utc)
    cards = _machine_cards_html(records, window, now)
    table_panel = html.panel(html.esc("Table"), _table(records))
    return _stylesheet(nonce) + cards + table_panel


def _machine_cards_html(records: list[dict], window: int, now: datetime) -> str:
    by_host: dict[str, list[dict]] = {}
    for record in records:
        host = str(record.get("host") or "local").lower()
        if host == "local":
            host = "rocinante"
        by_host.setdefault(host, []).append(record)
    hosts: list[str] = []
    for name in activity_records.default_hosts():
        if name not in hosts:
            hosts.append(name)
    for name in sorted(by_host):
        if name not in hosts and name != "cloud":
            hosts.append(name)
    hosts.append("cloud")
    cards = [_machine_card(host, by_host.get(host, []), window, now) for host in hosts]
    return f'<section class="machine-board" aria-label="{html.esc(TITLE)}">{"".join(cards)}</section>'


def _machine_card(host: str, records: list[dict], window: int, now: datetime) -> str:
    kind = _MACHINE_KIND.get(host, "server")
    live, completed = _partition_records(records, window, now)
    live_sorted = sorted(live, key=_sort_key)
    trees = _build_trees(live_sorted)
    tiles = "".join(_tile(node, children, depth=0) for node, children in trees)
    if host == "cloud" and not records:
        tiles = f'<p class="cloud-placeholder">{html.esc("Cloud tracking not wired — see #890")}</p>'
    completed_block = ""
    if completed:
        completed_trees = _build_trees(sorted(completed, key=_sort_key))
        completed_tiles = "".join(_tile(node, children, depth=0) for node, children in completed_trees)
        completed_block = (
            f'<details class="completed-expander">'
            f"<summary>{html.esc(f'Show {len(completed)} completed')}</summary>"
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
    if str(record.get("state") or "") != "succeeded":
        return False
    stamp = record.get("last_updated_at") or record.get("started_at")
    if not isinstance(stamp, str):
        return True
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() > window


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


def _tile(record: dict, children: list, *, depth: int) -> str:
    state = str(record.get("state") or "unknown")
    state_label = f"{_STATE_ICON.get(state, '?')} {state}"
    monogram = _monogram(record)
    model = record.get("model")
    model_html = (
        f'<div class="agent-tile-model" title="{html.esc(model)}">{html.esc(model)}</div>'
        if isinstance(model, str) and model
        else ""
    )
    task = str(record.get("task_label") or "Unknown task")
    child_class = " agent-tile-child" if depth else ""
    nested = "".join(_tile(child, grand, depth=depth + 1) for child, grand in children)
    nested_html = f'<div class="agent-tile-children">{nested}</div>' if nested else ""
    details = _details(record)
    return (
        f'<div class="agent-tile{child_class}" data-provider="{html.esc(record.get("provider") or "unknown")}">'
        f'<div class="agent-tile-main">'
        f'<div class="provider-badge" title="{html.esc(record.get("provider") or "unknown")}" '
        f'aria-label="{html.esc(record.get("provider") or "unknown")}">'
        f"<span>{html.esc(monogram)}</span>{model_html}</div>"
        f'<div class="agent-tile-body">'
        f'<div class="agent-tile-task" title="{html.esc(task)}">{html.esc(task)}</div>'
        f'<div class="agent-tile-meta">'
        f'<span class="agent-state agent-state-{html.esc(state.replace(" ", "-"))}">'
        f"{html.esc(state_label)}</span>"
        f'<span class="agent-elapsed">{html.esc(_elapsed(record.get("elapsed_seconds")))}</span>'
        f"</div></div></div>"
        f"{nested_html}{details}</div>"
    )


def _monogram(record: dict) -> str:
    provider = str(record.get("provider") or "").lower()
    harness = str(record.get("harness") or "").lower()
    if provider in _PROVIDER_MONOGRAM:
        return _PROVIDER_MONOGRAM[provider]
    if "brigade" in harness:
        return "Br"
    if "cursor" in harness:
        return "Cu"
    if "codex" in harness:
        return "Cx"
    if "claude" in harness:
        return "Cl"
    if harness.startswith("t3"):
        return "T3"
    return (provider[:2] or "??").title()


def _count_strip(records: list[dict]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        state = str(record.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    if not counts:
        return f'<div class="state-strip"><span class="state-chip state-chip-empty">{html.esc("0")}</span></div>'
    chips = []
    for state, count in sorted(counts.items(), key=lambda item: _LIVE_RANK.get(item[0], 8)):
        icon = _STATE_ICON.get(state, "?")
        chips.append(
            f'<span class="state-chip agent-state-{html.esc(state.replace(" ", "-"))}" '
            f'title="{html.esc(state)}">{html.esc(f"{icon} {count}")}</span>'
        )
    return f'<div class="state-strip">{"".join(chips)}</div>'


def _table(records: list[dict]) -> str:
    headers = ["Provider", "Host", "Task", "Model", "State", "Started", "Elapsed", "Last update", "Details"]
    return html.table(
        [html.esc(header) for header in headers],
        [_row(item, records) for item in records],
    )


def _row(record: dict, records: list[dict]) -> list[str]:
    state = str(record.get("state") or "unknown")
    state_label = f"{_STATE_ICON.get(state, '?')} {state}"
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
