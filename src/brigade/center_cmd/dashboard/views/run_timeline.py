"""Run timeline dashboard view."""

from __future__ import annotations

import json
from pathlib import Path

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "runs"
TITLE = "Runs"
ORDER = 5

_RUNS_FETCH_LIMIT = 500
_CLIENT_PAGE_SIZE = 50


def fetch(target: Path) -> dict:
    return data.run_json(target, ["work", "verify", "runs", "--limit", str(_RUNS_FETCH_LIMIT)])


def render(payload: dict, nonce: str) -> str:
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        return _empty_panel()

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
                html.esc(_value(receipt, "run_id")),
                html.esc(_value(receipt, "status")),
                html.esc(_command_values(command_records, "command")),
                html.esc(_command_values(command_records, "exit_code")),
                html.esc(_value(receipt, "duration_seconds")),
                html.esc(_value(receipt, "started_at")),
                _receipt_details(receipt),
            ]
        )

    if not rows:
        return _empty_panel()

    table = html.table(
        [
            html.esc("Run ID"),
            html.esc("Status"),
            html.esc("Command"),
            html.esc("Exit code"),
            html.esc("Duration"),
            html.esc("Timestamp"),
            html.esc("Receipt"),
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
    return f"{_stylesheet(nonce)}{html.panel(html.esc(TITLE), pager + wrapped)}{_script(nonce)}"


def _empty_panel() -> str:
    return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")


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
