"""Conservative Markdown claim spans, with original character offsets.

This is a bounded structural classifier, not a CommonMark parser. Unsupported
blocks stay opaque. Container views are used only for classification; spans
always refer to unchanged source bytes (as decoded text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .memory_proposal_v2_ops import Block

ANCHOR_SUFFIX = re.compile(r" \^(c-[0-9a-f]{6})(?=[ \t]*\Z)")

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
    raw_comment = raw_blank = False
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
                raw_blank = raw_blank or not raw.strip()
                if raw_end.search(raw):
                    # In a list, a blank can end the HTML block before its
                    # closer. Do not expose only the following paragraph suffix.
                    if raw_comment and raw_blank and raw_base:
                        opaque = True
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
            raw_comment, raw_blank = line.lstrip().startswith("<!--"), False
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


# Rust `char::is_whitespace` is the Unicode White_Space property. Python's
# default str.strip() also removes U+001C..U+001F, which the hub reads as prose,
# so the port uses the exact set on both trims and whitespace-prefix checks.
_RUST_WHITESPACE = "".join(
    ["\t\n\v\f\r ", "\u0085\u00a0\u1680", *(chr(c) for c in range(0x2000, 0x200B)), "\u2028\u2029\u202f\u205f\u3000"]
)


def _hub_lines(text: str) -> list[str]:
    """Rust `split_inclusive('\\n')`: keep the newline on every non-final line."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def hub_spans(text: str) -> set[tuple[int, int]]:
    """Port of the hub's `proposal_blocks::spans()` as byte ranges.

    The hub classifier counts UTF-8 bytes, not Python str indices. Returned
    (start, end) pairs exclude the final newline. Convert a client str offset i
    with len(text[:i].encode("utf-8")) before comparing against these ranges.
    """
    if "<" in text:
        return set()
    result: set[tuple[int, int]] = set()
    offset = 0
    front = text.startswith("---\n") or text.startswith("---\r\n")
    fence: tuple[str, int] | None = None
    start: int | None = None
    end = 0
    opaque = False
    for line in _hub_lines(text):
        raw = line[:-1] if line.endswith("\n") else line
        if raw.endswith("\r"):
            raw = raw[:-1]
        size = len(line.encode("utf-8"))
        trimmed = raw.lstrip(_RUST_WHITESPACE)
        if front:
            if offset > 0 and raw == "---":
                front = False
            offset += size
            continue
        marker = trimmed[0] if trimmed[:1] in ("`", "~") else None
        width = len(trimmed) - len(trimmed.lstrip(marker)) if marker else 0
        if fence is not None:
            fence_char, fence_width = fence
            if marker == fence_char and width >= fence_width and not trimmed[width:].strip(_RUST_WHITESPACE):
                fence = None
            offset += size
            continue
        if not raw or raw.startswith(("- ", "+ ", "* ")) or width >= 3:
            if start is not None:
                if not opaque:
                    result.add((start, end))
                start = None
            opaque = False
        if width >= 3 and marker is not None:
            fence = (marker, width)
        elif raw:
            if start is None:
                start = offset
            opaque |= (
                raw[0] in _RUST_WHITESPACE
                or trimmed.startswith(("#", ">", "<", "["))
                or trimmed.startswith("previously")
                or any(char in raw for char in "|`<>")
                or all(char in "-=*_ " for char in raw)
            )
            end = offset + len(raw.encode("utf-8"))
        offset += size
    if start is not None and not opaque:
        result.add((start, end))
    return result


def hub_suffix_id(block: str) -> str | None:
    """Port of the hub's `suffix()`: a trailing ` ^c-xxxxxx` anchor, if present."""
    trimmed = block.rstrip(" \t")
    prefix, sep, ident = trimmed.rpartition(" ^")
    if not sep or len(ident) != 8 or not ident.startswith("c-"):
        return None
    return ident if all(char in "0123456789abcdef" for char in ident[2:]) else None


def hub_pins(card_text: str) -> dict[str, str]:
    """Map each anchor id the hub can pin in this card to its exact block text.

    Mirrors the hub's `pin()`: only addressable spans count, and a repeated id
    among them makes the hub refuse every pin in the card, so none are returned.
    Compute this once per card and pass it to `hub_addressable`.
    """
    data = card_text.encode("utf-8")
    pins: dict[str, str] = {}
    for start, end in hub_spans(card_text):
        block = data[start:end].decode("utf-8")
        ident = hub_suffix_id(block)
        if ident is None:
            continue
        if ident in pins:
            return {}
        pins[ident] = block
    return pins


def hub_addressable(card_text: str, block: Block, pins: dict[str, str] | None = None) -> bool:
    """True only when the hub can pin this client block by its anchor id.

    The client parser is more lenient than the hub's, so a block can look
    addressable here yet be opaque to the hub. The hub accepts a pin when the
    id resolves to exactly one addressable block whose bytes hash to the
    client's, so the block text must equal the hub's text for that id.
    """
    if not block.block_id:
        return False
    if pins is None:
        pins = hub_pins(card_text)
    return pins.get(block.block_id) == block.text
