from __future__ import annotations
import re
from collections.abc import Sequence
from typing import Any, Dict, List

from .provenance import envelope_label, finding_envelope, split_finding_text
from .types import Finding, SourceEnvelope, Trust


def _demote_level_two_headings(markdown: str) -> str:
    return re.sub(r"(?m)^## ", "### ", markdown.strip())


def _source_lookup(sources: Sequence[SourceEnvelope]) -> dict[str, SourceEnvelope]:
    return {source.source_id: source for source in sources}


def _resolved_uri(finding: Finding, lookup: dict[str, SourceEnvelope]) -> str:
    for source_id in finding.source_ids:
        envelope = lookup.get(source_id)
        if envelope is not None:
            return envelope.uri
    return finding.source


def _section_lines(
    title: str,
    findings: List[Finding],
    lookup: dict[str, SourceEnvelope],
) -> list[str]:
    lines = ["", title]
    if findings:
        lines.extend(
            f"- {split_finding_text(f)[0]} ({_resolved_uri(f, lookup)}) [{envelope_label(finding_envelope(f))}]"
            for f in findings
        )
    else:
        lines.append("- (none)")
    return lines


def render_handoff(
    *,
    question: str,
    markdown_report: str,
    findings: List[Finding],
    sources: Sequence[SourceEnvelope],
    stats: Dict[str, Any],
) -> str:
    lookup = _source_lookup(sources)
    groups: tuple[tuple[Trust, str], ...] = (
        ("local", "### Trusted (local)"),
        ("cli", "### Configured CLI"),
        ("browser", "### Browser-assisted"),
        ("browser-ai", "### Browser AI"),
        ("web", "### Untrusted (web)"),
    )
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
    ]
    for trust, heading in groups:
        rows = [f for f in findings if f.trust == trust]
        lines.extend(_section_lines(heading, rows, lookup))
    lines += ["", "### Report", "", report or "No information gathered.", "", f"_Stats: {stats}_", ""]
    return "\n".join(lines)
