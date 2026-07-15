"""Shared normalization for Memory Handoff content sections."""

from __future__ import annotations


def _without_leading_html_comments_before_fence(lines: list[str]) -> list[str]:
    """Drop HTML comment blocks that wrap, rather than belong to, a fence."""
    start = 0
    while start < len(lines):
        while start < len(lines) and not lines[start].strip():
            start += 1
        if start >= len(lines) or not lines[start].strip().startswith("<!--"):
            break
        end = start
        while end < len(lines) and not lines[end].strip().endswith("-->"):
            end += 1
        if end >= len(lines):
            return lines
        start = end + 1

    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and lines[start].strip().startswith("```"):
        return lines[start:]
    return lines


def _without_trailing_html_comments(lines: list[str]) -> list[str]:
    """Drop HTML comment blocks that follow an outer content fence."""
    end = len(lines)
    while end:
        while end and not lines[end - 1].strip():
            end -= 1
        if not end or not lines[end - 1].strip().endswith("-->"):
            break
        start = end - 1
        while start >= 0 and not lines[start].strip().startswith("<!--"):
            start -= 1
        if start < 0:
            break
        end = start
    return lines[:end]


def normalize_suggested_card_content(value: str) -> tuple[str, str | None]:
    """Remove a supported outer Markdown fence and report malformed fences."""
    lines = _without_leading_html_comments_before_fence(value.splitlines())
    if not lines:
        return value, None

    opening = lines[0].strip()
    if not opening.startswith("```"):
        return value, None

    language = opening[3:].strip()
    if language and language.lower() != "markdown":
        return value, f"Suggested card content uses an unsupported Markdown fence language: {language}"

    fenced_lines = _without_trailing_html_comments(lines[1:])
    if not fenced_lines or fenced_lines[-1].strip() != "```":
        return value, "Suggested card content Markdown fence is missing a closing fence"

    return "\n".join(fenced_lines[:-1]).strip(), None
