"""Conservative Markdown claim spans, with original character offsets.

This is a bounded structural classifier, not a CommonMark parser. Unsupported
blocks stay opaque. Container views are used only for classification; spans
always refer to unchanged source bytes (as decoded text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])( +|$)")
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_THEMATIC = re.compile(r"^ {0,3}(?:(?:\* *){3,}|(?:_ *){3,}|(?:- *){3,})$")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?: |$)")
_REFERENCE = re.compile(r"^ {0,3}\[[^\]\n]+\]: *(.*)$")
_TABLE = re.compile(r"^ {0,3}\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$")
_RAW_HTML = (
    (re.compile(r"^ {0,3}<!--"), re.compile(r"-->")),
    (
        re.compile(r"^ {0,3}<(?:pre|script|style|textarea)(?:[ >]|$)", re.I),
        re.compile(r"</(?:pre|script|style|textarea)>", re.I),
    ),
    (re.compile(r"^ {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r"^ {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
    (re.compile(r"^ {0,3}<![A-Z]"), re.compile(r">")),
)


@dataclass(frozen=True)
class ClaimSpan:
    start: int
    end: int


def _closes_fence(marker: re.Match[str], fence: str) -> bool:
    return marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip()


def _container_view(line: str, containers: list[int], lazy: bool) -> tuple[str, bool, int]:
    """Strip list indentation without retaining containers across block boundaries."""
    indent = len(line) - len(line.lstrip(" "))
    interrupt = (
        _FENCE.match(line)
        or _HEADING.match(line)
        or _REFERENCE.match(line)
        or _THEMATIC.fullmatch(line)
        or _LIST.match(line[indent:])
        or line.lstrip().startswith((">", "<"))
    )
    while containers and indent < containers[-1]:
        if lazy and not interrupt:
            break
        containers.pop()
    base = min(indent, containers[-1]) if containers else 0
    view = line[base:]
    bullet = False
    while (match := _LIST.match(view)) and not _THEMATIC.fullmatch(view) and not (lazy and _SETEXT.fullmatch(view)):
        padding = len(match[1])
        # Empty markers still establish a container, but never a claim.
        width = match.end() - (padding - 1 if padding >= 5 else 0)
        base += width + (1 if not padding else 0)
        containers.append(base)
        view = view[width:]
        bullet = True
    return view, bullet, base


def _relative(line: str, base: int) -> str:
    return line[min(len(line) - len(line.lstrip(" ")), base) :]


def _reference_end(lines: list[str], index: int, destination: str, base: int) -> int:
    """Exclude destinations and optional multiline titles without editing them."""
    end = index + 1
    if not destination.strip() and end < len(lines) and _relative(lines[end], base).strip():
        destination = _relative(lines[end], base).strip()
        end += 1
    title = re.search(r"(?:^|[ \t])([\"'(])", destination)
    if title is None and end < len(lines) and _relative(lines[end], base).lstrip().startswith(('"', "'", "(")):
        destination = _relative(lines[end], base).lstrip()
        title = re.match(r"([\"'(])", destination)
        end += 1
    if title:
        closing = ")" if title[1] == "(" else title[1]
        tail = destination[title.end() :]
        while (
            not re.search(r"(?<!\\)" + re.escape(closing), tail)
            and end < len(lines)
            and _relative(lines[end], base).strip()
        ):
            tail = _relative(lines[end], base)
            end += 1
    while end < len(lines) and _relative(lines[end], base).strip() and _relative(lines[end], base).startswith("    "):
        end += 1
    return end


def claim_spans(text: str, offset: int = 0) -> list[ClaimSpan]:
    """Return addressable paragraphs and plain list items, excluding final EOLs."""
    lines = text[offset:].splitlines(keepends=True)
    expanded = [line.rstrip("\r\n").expandtabs(4) for line in lines]
    containers: list[int] = []
    positions = [offset]
    for line in lines:
        positions.append(positions[-1] + len(line))
    result: list[ClaimSpan] = []
    start: int | None = None
    end = offset
    fence: str | None = None
    fence_base = 0
    comment = quote = opaque = table = False
    empty_item = False
    raw_end: re.Pattern[str] | None = None
    raw_base = 0
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
        raw = expanded[index]
        indent = len(raw) - len(raw.lstrip(" "))
        if fence is not None:
            if raw.strip() and indent < fence_base:
                # The list ended. Reprocess this line at its outer level, so a
                # dedented fence opens a new block rather than closing this one.
                fence = None
            else:
                marker = _FENCE.match(raw[min(indent, fence_base) :])
                if marker and _closes_fence(marker, fence):
                    fence = None
                index += 1
                continue
        if raw_end is not None:
            if raw.strip() and indent < raw_base:
                # A raw block cannot outlive its list container. Reprocess the
                # dedented line so outer claims retain their original spans.
                raw_end = None
            else:
                if raw_end.search(raw):
                    raw_end = None
                index += 1
                continue
        if comment:
            comment = "-->" not in raw
            index += 1
            continue
        if not raw.strip():
            finish()
            if empty_item:
                containers.pop()
                empty_item = False
            quote = opaque = table = False
            index += 1
            continue
        if opaque:
            index += 1
            continue
        line, bullet, base = _container_view(raw, containers, start is not None or quote)
        empty_item = False
        marker = _FENCE.match(line)
        heading = _HEADING.match(line)
        thematic = _THEMATIC.fullmatch(line)
        reference = _REFERENCE.match(line)
        if bullet:
            finish()
            quote = table = False
        boundary = (
            marker or heading or thematic or (reference and not table) or bullet or line.lstrip().startswith((">", "<"))
        )
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
                fence, fence_base = marker[1], base
            index += 1
            continue
        if line.lstrip().startswith(">"):
            finish()
            quote = True
            index += 1
            continue
        if bullet and not line.strip():
            empty_item = True
            index += 1
            continue
        if reference:
            finish()
            index = _reference_end(expanded, index, reference[1], base)
            continue
        if "|" in line and index + 1 < len(lines) and _TABLE.fullmatch(_relative(expanded[index + 1], base)):
            finish()
            table = True
            index += 2
            continue
        if start is not None and _SETEXT.fullmatch(line):
            start = None
            index += 1
            continue
        if thematic or heading or (line.startswith("    ") and start is None) or line.lstrip().startswith("|"):
            finish()
            index += 1
            continue
        opened = next((end for opener, end in _RAW_HTML if opener.match(line)), None)
        if opened is not None:
            finish()
            raw_end = None if opened.search(line) else opened
            raw_base = base
            index += 1
            continue
        if "<!--" in line:
            # Keep the entire inline-comment paragraph opaque, including any
            # continuation after a same-line or multiline closing marker.
            start = None
            opaque = True
            comment = "-->" not in line.split("<!--", 1)[1]
            index += 1
            continue
        if (start is None and re.match(r"previously\b", line)) or line.lstrip().startswith("<"):
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
