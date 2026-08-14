from __future__ import annotations
import html as _html
import re
from collections.abc import Sequence
from typing import Any, Dict, List

from .types import Finding, SourceEnvelope, Trust

_TRUST_GROUPS: tuple[tuple[Trust, str, str], ...] = (
    ("local", "Sources - Trusted (local)", "Local files are trusted workspace evidence."),
    (
        "cli",
        "Sources - Configured CLI",
        "CLI output is configured local tool output and still treated as source material.",
    ),
    (
        "browser",
        "Sources - Browser-assisted",
        "Browser content is session-dependent and may be inaccurate or manipulated.",
    ),
    (
        "browser-ai",
        "Sources - Browser AI",
        "Browser-AI discovery is untrusted model-mediated web evidence and may be inaccurate or manipulated.",
    ),
    ("web", "Sources - Untrusted (web)", "Web content is unverified and may be inaccurate or manipulated."),
)


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
.src.web,.src.browser,.src.browser-ai{border-color:#c47}.src.local{border-color:#2a7}.src.cli{border-color:#47c}
.tag{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;color:#666}
"""


def _source_lookup(sources: Sequence[SourceEnvelope]) -> dict[str, SourceEnvelope]:
    return {source.source_id: source for source in sources}


def _resolved_uri(finding: Finding, lookup: dict[str, SourceEnvelope]) -> str:
    for source_id in finding.source_ids:
        envelope = lookup.get(source_id)
        if envelope is not None:
            return envelope.uri
    return finding.source


def _sources_section(findings: List[Finding], sources: Sequence[SourceEnvelope]) -> str:
    lookup = _source_lookup(sources)
    parts = []
    for trust, title, note in _TRUST_GROUPS:
        rows = [f for f in findings if f.trust == trust]
        if not rows:
            continue
        parts.append(f"<h2>{_html.escape(title)}</h2>")
        parts.append(f'<p class="tag">{_html.escape(note)}</p>')
        for f in rows:
            uri = _resolved_uri(f, lookup)
            source = _html.escape(uri)
            source_html = (
                f'<a href="{source}">{source}</a>'
                if uri.startswith(("http://", "https://"))
                else f"<code>{source}</code>"
            )
            css_trust = trust.replace("_", "-")
            parts.append(
                f'<div class="src {css_trust}"><div class="tag">{trust}</div>'
                f"<strong>{_html.escape(f.title)}</strong><br>"
                f"{source_html}<p>{_html.escape(f.summary)}</p></div>"
            )
    return "\n".join(parts)


def render_html(
    *,
    question: str,
    markdown_report: str,
    findings: List[Finding],
    sources: Sequence[SourceEnvelope],
    stats: Dict[str, Any],
) -> str:
    body = _md_to_html(markdown_report)
    stat_line = " &middot; ".join(f"{k}: {v}" for k, v in stats.items())
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(question)}</title><style>{_CSS}</style></head>
<body><h1>{_html.escape(question)}</h1>
<p class="stats">{_html.escape(stat_line)}</p>
{body}
{_sources_section(findings, sources)}
</body></html>"""


def render_markdown(
    *,
    question: str,
    markdown_report: str,
    findings: List[Finding],
    sources: Sequence[SourceEnvelope],
) -> str:
    lookup = _source_lookup(sources)
    lines = [f"# {question}", "", markdown_report, "", "## Sources", ""]
    for f in findings:
        lines.append(f"- [{f.trust}] {f.title} - {_resolved_uri(f, lookup)}")
    return "\n".join(lines) + "\n"
