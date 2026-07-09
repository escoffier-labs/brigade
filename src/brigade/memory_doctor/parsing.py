"""Frontmatter, wiki-link, and handoff-section parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
KV_LINE_RE = re.compile(r"^([a-zA-Z0-9_.-]+)\s*:\s*(.*)$")
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")


class HandoffParseError(Exception):
    pass


@dataclass(frozen=True)
class ParsedHandoff:
    path: Path
    action: str  # 'create-card' | 'update-card' | 'no-card'
    target: str  # filename, e.g. 'foo.md' (cards/ prefix stripped)
    content: str  # body of the "Suggested card content" section


def extract_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    fm: dict[str, str] = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line:
            continue
        km = KV_LINE_RE.match(line)
        if not km:
            continue
        fm[km.group(1)] = km.group(2).strip()
    rest = text[m.end() :]
    return fm, rest


def extract_wiki_links(text: str) -> list[str]:
    out: list[str] = []
    for m in WIKI_LINK_RE.finditer(text):
        raw = m.group(1).strip()
        if raw.startswith("cards/"):
            raw = raw[len("cards/") :]
        out.append(raw)
    return out


def _section_lines(text: str, heading: str) -> list[str] | None:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE | re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    start = m.end()
    next_heading = re.search(r"^##\s+", text[start:], re.MULTILINE)
    end = start + (next_heading.start() if next_heading else len(text) - start)
    return text[start:end].splitlines()


def _section_lines_to_eof(text: str, heading: str) -> list[str] | None:
    """Like _section_lines but reads to end-of-file instead of next ## heading.

    Used for the "Suggested card content" section, which by template convention
    is the LAST section of a handoff file. Card bodies legitimately contain
    `## ` headings of their own (sub-sections of the card), so the standard
    "stop at next ##" rule would truncate them.
    """
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE | re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    return text[m.end() :].splitlines()


def _first_nonblank_line(lines: list[str]) -> str:
    for line in lines:
        s = line.strip().strip("`").strip("'\"")
        if s:
            return s
    return ""


def parse_handoff(path: Path) -> ParsedHandoff:
    text = path.read_text()

    action_lines = _section_lines(text, "Recommended memory action")
    if action_lines is None:
        raise HandoffParseError(f"{path}: missing 'Recommended memory action' section")
    action = _first_nonblank_line(action_lines)
    if action not in {"create-card", "update-card", "no-card"}:
        raise HandoffParseError(f"{path}: unknown action {action!r}")

    target_lines = _section_lines(text, "Target card") or []
    raw_target = _first_nonblank_line(target_lines)
    if raw_target.startswith("cards/"):
        raw_target = raw_target[len("cards/") :]
    if raw_target and not raw_target.endswith(".md"):
        raw_target = raw_target + ".md"
    target = raw_target

    # Suggested card content is the FINAL section of the template; parse to EOF
    # so embedded `## ` sub-headings inside the card body are preserved.
    content_lines = _section_lines_to_eof(text, "Suggested card content") or []
    content = "\n".join(content_lines).strip()

    if action in {"create-card", "update-card"}:
        if not target:
            raise HandoffParseError(f"{path}: action {action} requires 'Target card'")
        if not content:
            raise HandoffParseError(f"{path}: action {action} requires 'Suggested card content'")

    return ParsedHandoff(path=path, action=action, target=target, content=content)
