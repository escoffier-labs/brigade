"""Run timeline dashboard view.

Operator question answered here: what verification ran recently, did it pass, and what command was it?

Renders Brigade orchestration runs from the versioned ``brigade.runs-list.v1``
CLI contract (issue #631) alongside the existing verify-run receipts. Data
access stays CLI-owned; this module never reads run artifacts directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "runs"
TITLE = "Runs"
ORDER = 5

RUNS_LIST_SCHEMA = "brigade.runs-list.v1"

_RUNS_FETCH_LIMIT = 500
_BRIGADE_RUNS_LIMIT = 50
_CLIENT_PAGE_SIZE = 50


def fetch(target: Path) -> dict:
    return {
        "brigade": data.run_json_cwd(target, ["runs", "list", "--limit", str(_BRIGADE_RUNS_LIMIT)]),
        "verify": data.run_json(target, ["work", "verify", "runs", "--limit", str(_RUNS_FETCH_LIMIT)]),
    }


def render(payload: dict, nonce: str) -> str:
    brigade_payload = _brigade if isinstance(_brigade := payload.get("brigade"), dict) else {}
    verify_payload = _verify if isinstance(_verify := payload.get("verify"), dict) else {}
    strip = _summary_strip(brigade_payload, verify_payload)
    return strip + _render_brigade_panel(brigade_payload) + _render_verify_panel(verify_payload, nonce)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _relative_time(value: object) -> str:
    parsed = _parse_ts(value)
    if parsed is None:
        return "unknown age"
    delta = _now() - parsed
    if delta.total_seconds() < 0:
        # Clock skew: a future stamp is treated as effectively current.
        return "just now"
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _chip(status: object) -> str:
    failed = str(status or "").strip().lower() in {"failed", "error", "failing"}
    label = "fail" if failed else "pass"
    css = "mo-chip-fail" if failed else "mo-chip-pass"
    return f'<span class="mo-chip {css}">{html.esc(label)}</span>'


def _runs_of(payload: dict, key: str) -> list[dict]:
    runs = payload.get(key)
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict)]


def _week_start(now: datetime) -> datetime:
    # Monday 00:00 local server time of the dashboard host.
    local = now.astimezone()
    start = local - timedelta(days=local.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _summary_strip(brigade_payload: dict, verify_payload: dict) -> str:
    all_runs = _runs_of(brigade_payload, "runs") + _runs_of(verify_payload, "runs")
    week_start = _week_start(_now())
    this_week = 0
    failed = 0
    stamps: list[tuple[datetime, bool]] = []
    for run in all_runs:
        status = str(run.get("status") or "").strip().lower() in {"failed", "error", "failing"}
        if status:
            failed += 1
        parsed = _parse_ts(run.get("started_at"))
        if parsed is None:
            continue
        stamps.append((parsed, status))
        if parsed >= week_start:
            this_week += 1
    last_run = "unknown age"
    known = [parsed for (parsed, _) in stamps if parsed is not None]
    if known:
        last_run = _relative_time(max(known).isoformat())
    elif all_runs:
        last_run = "unknown age"
    else:
        last_run = "n/a"
    summary = f"{this_week} runs this week, {failed} failed, last run {last_run}."
    return f'<div class="mo-summary">{html.esc(summary)}</div>'


def _run_id_details(run_id: object) -> str:
    rendered = html.esc(str(run_id))
    return f"<details><summary>Run ID</summary><code>{rendered}</code></details>"


_MISSING_RUNS_DIR_MARKER = "runs directory not found"


def _is_missing_runs_directory(error: object) -> bool:
    """True only for the CLI's missing-runs-dir message, not other fetch failures."""
    return _MISSING_RUNS_DIR_MARKER in str(error)


def _render_brigade_panel(payload: dict) -> str:
    title = "Brigade runs"
    error = payload.get("error")
    if error:
        if _is_missing_runs_directory(error):
            # A missing runs directory is an empty state, not a failure.
            detail = (
                f"<details><summary>{html.esc('CLI diagnostic')}</summary><pre>{html.esc(str(error))}</pre></details>"
            )
            return html.panel(html.esc(title), f"<p>{html.esc('No Brigade runs yet.')}</p>{detail}")
        return html.error_panel(title, str(error))

    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        return html.panel(html.esc(title), f"<p>{html.esc('No Brigade runs yet.')}</p>")

    rows = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        rows.append(
            [
                f"{_chip(run.get('status'))} {html.esc(_value(run, 'task'))}",
                html.esc(_relative_time(run.get("started_at"))),
                html.esc(_value(run, "duration_seconds")),
                html.esc(_value(run, "failure_phase")),
                html.esc(_value(run, "mode")),
                html.esc("yes" if run.get("resume_available") else "no"),
                _run_id_details(_value(run, "run_id")),
            ]
        )
    if not rows:
        return html.panel(html.esc(title), f"<p>{html.esc('No Brigade runs yet.')}</p>")

    table = html.table(
        [
            html.esc("Task"),
            html.esc("Started"),
            html.esc("Duration"),
            html.esc("Failure phase"),
            html.esc("Mode"),
            html.esc("Resume"),
            html.esc("Run ID"),
        ],
        rows,
    )
    notes = []
    skipped = payload.get("skipped_invalid")
    if isinstance(skipped, int) and skipped > 0:
        notes.append(f"<p>{html.esc(f'Skipped {skipped} invalid run directories.')}</p>")
    if payload.get("schema") != RUNS_LIST_SCHEMA:
        notes.append(f"<p>{html.esc('Warning: unexpected runs list schema from CLI.')}</p>")
    body = '<div class="mo-scroll">' + table + "</div>" + "".join(notes)
    return html.panel(html.esc(title), body)


def _render_verify_panel(payload: dict, nonce: str) -> str:
    title = "Verify runs"
    if payload.get("error"):
        return html.error_panel(title, str(payload["error"]))

    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        return _empty_panel(title)

    rows = []
    for receipt in sorted(
        (item for item in runs if isinstance(item, dict)),
        key=lambda item: str(item.get("started_at") or item.get("run_id") or ""),
        reverse=True,
    ):
        commands = receipt.get("commands")
        command_records = commands if isinstance(commands, list) else []
        rows.append(
            [
                f"{_chip(receipt.get('status'))} {html.esc(_command_values(command_records, 'command'))}",
                html.esc(_command_values(command_records, "exit_code")),
                html.esc(_value(receipt, "duration_seconds")),
                html.esc(_relative_time(receipt.get("started_at"))),
                _receipt_details(receipt),
                _run_id_details(_value(receipt, "run_id")),
            ]
        )

    if not rows:
        return _empty_panel(title)

    table = html.table(
        [
            html.esc("Command"),
            html.esc("Exit code"),
            html.esc("Duration"),
            html.esc("Timestamp"),
            html.esc("Receipt"),
            html.esc("Run ID"),
        ],
        rows,
    )
    parts = table.split("<tbody>", 1)
    if len(parts) == 2:
        head, rest = parts
        body, tail = rest.split("</tbody>", 1)
        body = body.replace("<tr>", '<tr data-mo-row="1">')
        table = head + "<tbody>" + body + "</tbody>" + tail

    pager = (
        f'<div class="mo-pager" data-mo-page-size="{_CLIENT_PAGE_SIZE}" '
        f'data-mo-total="{html.esc(len(rows))}">'
        f'<button type="button" data-mo-page="prev" disabled>{html.esc("Prior")}</button>'
        f"<span data-mo-page-status>{html.esc('Page 1')}</span>"
        f'<button type="button" data-mo-page="next">{html.esc("Next")}</button>'
        "</div>"
    )
    wrapped = '<div class="mo-scroll">' + table + "</div>"
    return f"{_stylesheet(nonce)}{html.panel(html.esc(title), pager + wrapped)}{_script(nonce)}"


def _empty_panel(title: str) -> str:
    return html.panel(html.esc(title), f"<p>{html.esc('Nothing here.')}</p>")


def _value(receipt: dict, name: str) -> object:
    value = receipt.get(name)
    return "-" if value is None else value


def _command_values(commands: list[object], name: str) -> str:
    values = [str(command[name]) for command in commands if isinstance(command, dict) and name in command]
    return "; ".join(values) if values else "-"


def _receipt_details(receipt: dict) -> str:
    rendered = json.dumps(receipt, indent=2, sort_keys=True)
    if len(rendered) > 4_000:
        rendered = f"{rendered[:4_000]}\n[Receipt truncated at 4000 characters.]"
    return f"<details><summary>Receipt JSON</summary><pre>{html.esc(rendered)}</pre></details>"


def _stylesheet(nonce: str) -> str:
    return f"""<style nonce="{html.esc(nonce)}">
.mo-summary {{
  font: inherit;
  margin: 0 0 0.75rem;
}}
.mo-chip {{
  display: inline-block;
  padding: 0.05rem 0.5rem;
  border-radius: 999px;
  border: 1px solid #666;
  font-size: 0.85em;
}}
.mo-chip-pass {{
  color: #0a5c0a;
  border-color: #0a5c0a;
}}
.mo-chip-fail {{
  color: #8a1010;
  border-color: #8a1010;
}}
.mo-pager {{
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
  margin: 0 0 0.75rem;
}}
.mo-pager button {{
  font: inherit;
  padding: 0.3rem 0.7rem;
  border: 1px solid #666;
  border-radius: 0.2rem;
  background: #f8f8f8;
  cursor: pointer;
}}
.mo-pager button:disabled {{
  opacity: 0.5;
  cursor: not-allowed;
}}
.mo-scroll {{
  overflow-x: auto;
  max-width: 1440px;
}}
</style>"""


def _script(nonce: str) -> str:
    return f"""<script nonce="{html.esc(nonce)}">
(function () {{
  var pageIndex = 0;
  function renderPage() {{
    var pager = document.querySelector(".mo-pager");
    if (!pager) return;
    var size = parseInt(pager.getAttribute("data-mo-page-size") || "50", 10);
    var rows = document.querySelectorAll("tr[data-mo-row]");
    var pages = Math.max(1, Math.ceil(rows.length / size) || 1);
    if (pageIndex >= pages) pageIndex = pages - 1;
    if (pageIndex < 0) pageIndex = 0;
    var start = pageIndex * size;
    var end = start + size;
    for (var r = 0; r < rows.length; r++) {{
      rows[r].hidden = !(r >= start && r < end);
    }}
    var status = pager.querySelector("[data-mo-page-status]");
    if (status) {{
      status.textContent = "Page " + (pageIndex + 1) + " of " + pages +
        " (" + rows.length + " items)";
    }}
    var prev = pager.querySelector('[data-mo-page="prev"]');
    var next = pager.querySelector('[data-mo-page="next"]');
    if (prev) prev.disabled = pageIndex <= 0;
    if (next) next.disabled = pageIndex >= pages - 1;
  }}
  document.addEventListener("click", function (e) {{
    var t = e.target;
    if (!t || !t.getAttribute) return;
    var pageDir = t.getAttribute("data-mo-page");
    if (!pageDir) return;
    e.preventDefault();
    if (pageDir === "next") pageIndex += 1;
    if (pageDir === "prev") pageIndex -= 1;
    renderPage();
  }});
  renderPage();
}})();
</script>"""
