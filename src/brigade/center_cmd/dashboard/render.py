"""Shared HTML rendering helpers for the operator dashboard."""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import datetime, timezone

from brigade.center_cmd.dashboard.snapshot import Snapshot


def esc(value: object) -> str:
    """Escape a value for safe inclusion in HTML."""
    return html.escape(str(value), quote=True)


def panel(title: str, inner: str) -> str:
    """Render a titled panel. *title* and *inner* must already be escaped."""
    return f'<section class="panel"><h2 class="panel-title">{title}</h2><div class="panel-body">{inner}</div></section>'


def error_panel(title: str, message: str) -> str:
    """Render a degradation panel for a failed data fetch."""
    return panel(esc(title), f'<p class="error">{esc(message)}</p>')


def loading_panel(title: str) -> str:
    """Render a shell panel while a snapshot is still being gathered."""
    return panel(esc(title), f"<p>{esc('This view is still gathering data.')}</p>")


def freshness_banner(snapshot: Snapshot, href: str) -> str:
    """Render the explicit staleness stamp with a same-view refresh link."""
    status = snapshot.status
    if status == "loading":
        return (
            f'<p class="center-freshness" data-center-freshness="loading" data-center-loading="1">'
            f'{esc("Loading live data.")} <a href="{esc(href)}">{esc("refresh")}</a></p>'
        )
    fetched_at = snapshot.fetched_at
    clock = "--:--:--" if fetched_at is None else fetched_at.astimezone().strftime("%H:%M:%S")
    text = f"data as of {clock} (stale)" if status == "stale" else f"data as of {clock}"
    return (
        f'<p class="center-freshness" data-center-freshness="{esc(status)}">'
        f'{esc(text)}, <a href="{esc(href)}">{esc("refresh")}</a></p>'
    )


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a table. *headers* and every cell in *rows* must already be escaped."""
    head_cells = "".join(f"<th>{cell}</th>" for cell in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{cell}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")
    body = "".join(body_rows)
    return f'<table class="data-table"><thead><tr>{head_cells}</tr></thead><tbody>{body}</tbody></table>'


_CENTER_CSS = """
table details { margin: 0; padding: 0; border: 0; background: transparent; }
table summary { font-weight: 600; }
.panel-title { margin: 0 0 12px; font-size: 15px; }
.panel-body p { margin: 0; }
p.error { color: var(--bad); }
table.data-table { table-layout: auto; }
table.data-table th:nth-child(n) { width: auto; }
.center-freshness { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
"""

_CENTER_SCRIPT = """(function () {
  var pollMs = %d;
  if (document.querySelector("[data-center-loading]")) {
    setTimeout(function () { location.reload(); }, pollMs);
    return;
  }
  setInterval(function () {
    location.reload();
  }, pollMs);
})();
document.addEventListener("input", function (e) {
  var input = e.target;
  if (!input || !input.getAttribute) return;
  var targetId = input.getAttribute("data-filter-target");
  if (!targetId) return;
  var table = document.getElementById(targetId);
  if (!table) return;
  var query = (input.value || "").toLowerCase();
  var rows = table.querySelectorAll("tbody tr");
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var text = (row.textContent || "").toLowerCase();
    row.hidden = query !== "" && text.indexOf(query) === -1;
  }
});"""


def page(title: str, nonce: str, nav: str, body: str, *, reload_ms: int = 15000) -> str:
    """Render a full HTML document. *title*, *nav*, and *body* must already be escaped."""
    from brigade import ui_theme

    reload_delay = max(500, int(reload_ms))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    heading = title.split(" - ", 1)[0]
    shell = (
        '<main class="deck-shell">'
        f'<header class="masthead"><div><p class="eyebrow">Center</p><h1>{heading}</h1></div>'
        f'<p class="header-meta">{esc(stamp)}</p></header>'
        f"{nav}{body}</main>"
    )
    return ui_theme.document(
        html.unescape(title),
        nonce,
        shell,
        as_of=stamp,
        extra_css=_CENTER_CSS,
        script=_CENTER_SCRIPT % reload_delay,
    )
