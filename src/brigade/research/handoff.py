from __future__ import annotations
from typing import Any, Dict, List
from .types import Finding

def render_handoff(*, question: str, markdown_report: str, findings: List[Finding],
                   stats: Dict[str, Any]) -> str:
    local = [f for f in findings if f.trust == "local"]
    web = [f for f in findings if f.trust == "web"]
    lines = [
        "---",
        f"title: Research - {question}",
        "destination: card",
        "tags: [research, deep-research]",
        "---",
        "",
        f"# Research: {question}",
        "",
        markdown_report,
        "",
        "## Provenance",
        "",
        "### Trusted (local)",
    ]
    lines += [f"- {f.title} ({f.source})" for f in local] or ["- (none)"]
    lines += ["", "### Untrusted (web)"]
    lines += [f"- {f.title} ({f.source})" for f in web] or ["- (none)"]
    lines += ["", f"_Stats: {stats}_", ""]
    return "\n".join(lines)
