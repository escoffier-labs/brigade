"""Shared HTML rendering helpers for the operator dashboard."""

from __future__ import annotations

import html
from collections.abc import Sequence


def esc(value: object) -> str:
    """Escape a value for safe inclusion in HTML."""
    return html.escape(str(value), quote=True)


def panel(title: str, inner: str) -> str:
    """Render a titled panel. *title* and *inner* must already be escaped."""
    return f'<section class="panel"><h2 class="panel-title">{title}</h2><div class="panel-body">{inner}</div></section>'


def error_panel(title: str, message: str) -> str:
    """Render a degradation panel for a failed data fetch."""
    return panel(esc(title), f'<p class="error">{esc(message)}</p>')


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a table. *headers* and every cell in *rows* must already be escaped."""
    head_cells = "".join(f"<th>{cell}</th>" for cell in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{cell}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")
    body = "".join(body_rows)
    return f'<table class="data-table"><thead><tr>{head_cells}</tr></thead><tbody>{body}</tbody></table>'


def page(title: str, nonce: str, nav: str, body: str) -> str:
    """Render a full HTML document. *title*, *nav*, and *body* must already be escaped."""
    nonce_attr = esc(nonce)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style nonce="{nonce_attr}">
body {{
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.5;
  margin: 0;
  color: #111;
  background: #fff;
}}
a {{
  color: #0066cc;
  text-decoration: none;
}}
a:hover {{
  text-decoration: underline;
}}
nav.dashboard-nav {{
  border-bottom: 1px solid #ddd;
  padding: 0.75rem 1.5rem;
  background: #f8f8f8;
}}
nav.dashboard-nav ul {{
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
}}
nav.dashboard-nav a {{
  font-weight: 500;
}}
nav.dashboard-nav a[aria-current="page"] {{
  font-weight: 700;
  text-decoration: underline;
}}
main.dashboard-main {{
  padding: 1.5rem;
}}
h1.page-title {{
  font-size: 1.5rem;
  margin: 0 0 1rem;
  color: #0066cc;
}}
.panel {{
  border: 1px solid #ddd;
  border-radius: 0.25rem;
  margin-bottom: 1rem;
}}
.panel-title {{
  font-size: 1rem;
  margin: 0;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid #ddd;
  background: #f8f8f8;
}}
.panel-body {{
  padding: 1rem;
}}
.panel-body p {{
  margin: 0;
}}
p.error {{
  color: #8b0000;
}}
table.data-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
}}
table.data-table th,
table.data-table td {{
  border: 1px solid #ddd;
  padding: 0.4rem 0.6rem;
  text-align: left;
}}
table.data-table th {{
  background: #f0f0f0;
}}
</style>
</head>
<body>
{nav}
<main class="dashboard-main">
{body}
</main>
<script nonce="{nonce_attr}">
setTimeout(function () {{
  location.reload();
}}, 30000);
document.addEventListener("input", function (e) {{
  var input = e.target;
  if (!input || !input.getAttribute) return;
  var targetId = input.getAttribute("data-filter-target");
  if (!targetId) return;
  var table = document.getElementById(targetId);
  if (!table) return;
  var query = (input.value || "").toLowerCase();
  var rows = table.querySelectorAll("tbody tr");
  for (var i = 0; i < rows.length; i++) {{
    var row = rows[i];
    var text = (row.textContent || "").toLowerCase();
    row.hidden = query !== "" && text.indexOf(query) === -1;
  }}
}});
</script>
</body>
</html>
"""
