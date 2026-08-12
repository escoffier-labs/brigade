"""Read-only Agent Activity dashboard view."""

from __future__ import annotations

import json
from pathlib import Path

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


def fetch(target: Path) -> dict:
    return data.run_json(target, ["center", "activity", "--limit", "100"])


def render(payload: dict, nonce: str) -> str:
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))
    records = [item for item in payload.get("agent_activity", []) if isinstance(item, dict)]
    if not records:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('No agent observations are available.')}</p>")
    headers = ["Provider", "Host", "Task", "State", "Started", "Elapsed", "Last update", "Details"]
    return (
        _stylesheet(nonce)
        + html.panel(html.esc("Provider summary"), _summary(payload.get("agent_activity_summary")))
        + html.panel(
            html.esc(TITLE),
            html.table([html.esc(header) for header in headers], [_row(item, records) for item in records]),
        )
    )


def _row(record: dict, records: list[dict]) -> list[str]:
    state = str(record.get("state") or "unknown")
    state_label = f"{_STATE_ICON.get(state, '?')} {state}"
    task = f"{'↳ ' * _depth(record, records)}{record.get('task_label') or 'Task unavailable'}"
    return [
        html.esc(f"{record.get('provider', 'unknown')} · {record.get('harness', 'observation')}"),
        html.esc(record.get("host") or "local"),
        html.esc(task),
        f'<span class="agent-state agent-state-{html.esc(state.replace(" ", "-"))}">{html.esc(state_label)}</span>',
        html.esc(record.get("started_at") or "-"),
        html.esc(_elapsed(record.get("elapsed_seconds"))),
        html.esc(record.get("last_updated_at") or "-"),
        _details(record),
    ]


def _summary(value: object) -> str:
    chips = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        counts = item.get("state_counts") if isinstance(item.get("state_counts"), dict) else {}
        words = ", ".join(f"{state}: {count}" for state, count in sorted(counts.items()))
        chips.append(f"<li><strong>{html.esc(item.get('provider') or 'unknown')}</strong>: {html.esc(words)}</li>")
    return (
        f'<ul class="agent-summary">{"".join(chips)}</ul>'
        if chips
        else f"<p>{html.esc('No provider counts available.')}</p>"
    )


def _details(record: dict) -> str:
    detail = {key: record.get(key) for key in ("activity_id", "parent_activity_id", "label", "source", "links")}
    return f"<details><summary>Details</summary><pre>{html.esc(json.dumps(detail, indent=2, sort_keys=True))}</pre></details>"


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


def _stylesheet(nonce: str) -> str:
    return f'''<style nonce="{html.esc(nonce)}">
.agent-summary {{ margin: 0; padding-left: 1.25rem; }}
.agent-state {{ border: 1px solid #666; border-radius: 999px; padding: 0.1rem 0.45rem; font-weight: 600; white-space: nowrap; }}
.agent-state-running {{ border-color: #1769aa; }}
.agent-state-stale, .agent-state-unknown {{ border-style: dashed; }}
.agent-state-failed, .agent-state-blocked {{ border-color: #8b0000; }}
</style>'''
