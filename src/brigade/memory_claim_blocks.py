"""Conservative Markdown claim spans, with original character offsets.

This is a bounded structural classifier, not a CommonMark parser. Unsupported
blocks stay opaque. Container views are used only for classification; spans
always refer to unchanged source bytes (as decoded text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])( +)(?=\S)")
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_THEMATIC = re.compile(r"^ {0,3}(?:(?:\* *){3,}|(?:_ *){3,}|(?:- *){3,})$")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?: |$)")
_REFERENCE = re.compile(r"^ {0,3}\[[^\]\n]+\]: *(.*)$")
_TABLE = re.compile(r"^ {0,3}\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$")


@dataclass(frozen=True)
class ClaimSpan:
    start: int
    end: int


def _closes_fence(marker: re.Match[str], fence: str) -> bool:
    return marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip()


def _views(lines: list[str]) -> list[tuple[str, bool]]:
    """Remove active list containers using four-column tab stops.

    List padding of five or more columns uses one column of padding and leaves
    the rest as content indentation. The same relative view feeds every block
    rule, including quotes, code and Setext underlines, not just fences.
    """
    containers: list[int] = []
    blank = False
    fence: str | None = None
    fence_base = 0
    comment = opaque = False
    result = []
    for raw in lines:
        line = raw.rstrip("\r\n").expandtabs(4)
        indent = len(line) - len(line.lstrip(" "))
        if comment:
            comment = "-->" not in line
            result.append((line, False))
            continue
        if opaque and line.strip():
            result.append((line, False))
            continue
        if fence is not None:
            view = line[min(indent, fence_base) :]
            marker = _FENCE.match(view)
            if marker and _closes_fence(marker, fence):
                fence = None
            result.append((view, False))
            continue
        if not line.strip():
            result.append(("", False))
            blank = True
            opaque = False
            continue
        while containers and indent < containers[-1]:
            # Nonblank lazy continuation belongs to the innermost list unless
            # it starts a sibling item. Blank-separated dedents leave it.
            if not blank and not _LIST.match(line[indent:]) and not _THEMATIC.fullmatch(line):
                break
            containers.pop()
        base = min(indent, containers[-1]) if containers else 0
        view = line[base:]
        bullet = False
        while (match := _LIST.match(view)) and not _THEMATIC.fullmatch(view):
            padding = len(match[1])
            width = match.end() - (padding - 1 if padding >= 5 else 0)
            base += width
            containers.append(base)
            view = view[width:]
            bullet = True
        marker = _FENCE.match(view)
        if marker:
            fence, fence_base = marker[1], base
        elif "<!--" in view:
            comment = "-->" not in view.split("<!--", 1)[1]
        elif re.match(r"previously\b", view) or view.lstrip().startswith("<"):
            opaque = True
        result.append((view, bullet))
        blank = False
    return result


def _reference_end(lines: list[str], index: int, destination: str) -> int:
    """Exclude destinations and optional multiline titles without editing them."""
    end = index + 1
    if not destination.strip() and end < len(lines) and lines[end].strip():
        destination = lines[end].strip()
        end += 1
    title = re.search(r"(?:^|[ \t])([\"'(])", destination)
    if title is None and end < len(lines) and lines[end].lstrip().startswith(('"', "'", "(")):
        destination = lines[end].lstrip()
        title = re.match(r"([\"'(])", destination)
        end += 1
    if title:
        closing = ")" if title[1] == "(" else title[1]
        tail = destination[title.end() :]
        while not re.search(r"(?<!\\)" + re.escape(closing), tail) and end < len(lines) and lines[end].strip():
            tail = lines[end]
            end += 1
    while end < len(lines) and lines[end].strip() and lines[end].startswith("    "):
        end += 1
    return end


def claim_spans(text: str, offset: int = 0) -> list[ClaimSpan]:
    """Return addressable paragraphs and plain list items, excluding final EOLs."""
    lines = text[offset:].splitlines(keepends=True)
    views = _views(lines)
    relative = [line for line, _ in views]
    positions = [offset]
    for line in lines:
        positions.append(positions[-1] + len(line))
    result: list[ClaimSpan] = []
    start: int | None = None
    end = offset
    fence: str | None = None
    comment = quote = opaque = table = False
    index = 0

    def finish() -> None:
        nonlocal start
        if start is not None:
            stop = end
            while stop > start and text[stop - 1] in "\r\n":
                stop -= 1
            result.append(ClaimSpan(start, stop))
            start = None

    while index < len(lines):
        line, bullet = views[index]
        marker = _FENCE.match(line)
        if fence is not None:
            if marker and _closes_fence(marker, fence):
                fence = None
            index += 1
            continue
        if comment:
            comment = "-->" not in line
            index += 1
            continue
        if not line.strip():
            finish()
            quote = opaque = table = False
            index += 1
            continue
        heading = _HEADING.match(line)
        thematic = _THEMATIC.fullmatch(line)
        reference = _REFERENCE.match(line)
        if opaque:
            index += 1
            continue
        if bullet:
            finish()
            quote = table = False
        boundary = marker or heading or thematic or reference or bullet or line.lstrip().startswith((">", "<"))
        if quote or table:
            if not boundary:
                index += 1
                continue
            quote = table = False
        if marker:
            finish()
            if marker[1][0] == "`" and "`" in marker[2]:
                opaque = True
            else:
                fence = marker[1]
            index += 1
            continue
        if line.lstrip().startswith(">"):
            finish()
            quote = True
            index += 1
            continue
        if reference:
            finish()
            index = _reference_end(relative, index, reference[1])
            continue
        if "|" in line and index + 1 < len(lines) and _TABLE.fullmatch(relative[index + 1]):
            finish()
            table = True
            index += 2
            continue
        if start is not None and _SETEXT.fullmatch(line):
            start = None
            index += 1
            continue
        if thematic or heading or line.startswith("    ") or line.lstrip().startswith("|"):
            finish()
            index += 1
            continue
        if "<!--" in line:
            # An inline multiline comment makes the containing paragraph opaque.
            start = None
            comment = "-->" not in line.split("<!--", 1)[1]
            index += 1
            continue
        if re.match(r"previously\b", line) or line.lstrip().startswith("<"):
            finish()
            opaque = True
            index += 1
            continue
        if start is None:
            start = positions[index]
        end = positions[index + 1]
        index += 1
    finish()
    return result
