from __future__ import annotations
import re
from typing import Any, Dict, List
from .types import Finding


def _demote_level_two_headings(markdown: str) -> str:
    return re.sub(r"(?m)^## ", "### ", markdown.strip())


def render_handoff(*, question: str, markdown_report: str, findings: List[Finding], stats: Dict[str, Any]) -> str:
    local = [f for f in findings if f.trust == "local"]
    cli = [f for f in findings if f.trust == "cli"]
    browser = [f for f in findings if f.trust == "browser"]
    web = [f for f in findings if f.trust == "web"]
    report = _demote_level_two_headings(markdown_report)
    lines = [
        "# Memory Handoff",
        "",
        "## Type",
        "",
        "research",
        "",
        "## Title",
        "",
        f"Research - {question}",
        "",
        "## Summary",
        "",
        f"Research report generated for: {question}",
        "",
        "## Evidence",
        "",
        f"- findings: {len(findings)}",
        f"- stats: {stats}",
        "",
        "## Recommended memory action",
        "",
        "no-card",
        "",
        "## Target document",
        "",
        ".learnings/LEARNINGS.md",
        "",
        "## Suggested document content",
        "",
        f"### Research: {question}",
        "",
        "### Trusted (local)",
    ]
    lines += [f"- {f.title} ({f.source})" for f in local] or ["- (none)"]
    lines += ["", "### Configured CLI"]
    lines += [f"- {f.title} ({f.source})" for f in cli] or ["- (none)"]
    lines += ["", "### Browser-assisted"]
    lines += [f"- {f.title} ({f.source})" for f in browser] or ["- (none)"]
    lines += ["", "### Untrusted (web)"]
    lines += [f"- {f.title} ({f.source})" for f in web] or ["- (none)"]
    lines += ["", "### Report", "", report or "No information gathered.", "", f"_Stats: {stats}_", ""]
    return "\n".join(lines)
