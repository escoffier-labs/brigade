from __future__ import annotations
import html as _html
import re
from typing import Any, Dict, List
from .types import Finding

def _md_to_html(md: str) -> str:
    out = []
    for line in md.splitlines():
        if line.startswith("### "):
            out.append(f"<h3>{_html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{_html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{_html.escape(line[2:])}</h1>")
        elif line.strip() == "":
            out.append("")
        else:
            esc = _html.escape(line)
            esc = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', esc)
            out.append(f"<p>{esc}</p>")
    return "\n".join(out)

_CSS = """
body{font:16px/1.6 -apple-system,system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}
h1,h2,h3{line-height:1.25}.stats{color:#555;font-size:.9rem}
.src{border-left:3px solid #ccc;padding:.3rem .8rem;margin:.5rem 0}
.src.web{border-color:#c47}.src.local{border-color:#2a7}
.tag{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;color:#666}
"""

def _sources_section(findings: List[Finding]) -> str:
    local = [f for f in findings if f.trust == "local"]
    web = [f for f in findings if f.trust == "web"]
    parts = []
    if local:
        parts.append("<h2>Sources - Trusted (local)</h2>")
        for f in local:
            parts.append(f'<div class="src local"><div class="tag">local</div>'
                         f'<strong>{_html.escape(f.title)}</strong><br>'
                         f'<code>{_html.escape(f.source)}</code><p>{_html.escape(f.summary)}</p></div>')
    if web:
        parts.append("<h2>Sources - Untrusted (web)</h2>")
        parts.append('<p class="tag">Web content is unverified and may be inaccurate or manipulated.</p>')
        for f in web:
            parts.append(f'<div class="src web"><div class="tag">web</div>'
                         f'<strong>{_html.escape(f.title)}</strong><br>'
                         f'<a href="{_html.escape(f.source)}">{_html.escape(f.source)}</a>'
                         f'<p>{_html.escape(f.summary)}</p></div>')
    return "\n".join(parts)

def render_html(*, question: str, markdown_report: str, findings: List[Finding],
                stats: Dict[str, Any]) -> str:
    body = _md_to_html(markdown_report)
    stat_line = " &middot; ".join(f"{k}: {v}" for k, v in stats.items())
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(question)}</title><style>{_CSS}</style></head>
<body><h1>{_html.escape(question)}</h1>
<p class="stats">{_html.escape(stat_line)}</p>
{body}
{_sources_section(findings)}
</body></html>"""

def render_markdown(*, question: str, markdown_report: str, findings: List[Finding]) -> str:
    lines = [f"# {question}", "", markdown_report, "", "## Sources", ""]
    for f in findings:
        lines.append(f"- [{f.trust}] {f.title} - {f.source}")
    return "\n".join(lines) + "\n"
