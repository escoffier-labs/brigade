"""Split an instruction file into the sections the instructions stage may move.

Pure functions over bytes. Nothing here reads a file, starts a subprocess or raises on any
input. A unit is a top section with its subsections. The text before the first unit, pins,
managed blocks and pointer lines never move (see the slice 2 plan, "Planner decisions").
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

KEEP = "<!-- brigade:keep -->"
KEEP_UNTIL = re.compile(rb"<!-- brigade:keep until=(\d{4}-\d{2}-\d{2}) -->")
POINTER_LINE = re.compile(
    rb"- (?:Moved to|Overflow in) memory \d{4}-\d{2}-\d{2}: memory/cards/instructions/[a-z0-9-]+\.md"
    rb" \(undo: [0-9]{8}T[0-9]{6}Z-[a-z]+-[0-9a-f]{8}\)"
)

_KEEP = KEEP.encode("ascii")
_HEADING = re.compile(rb"(#{1,6})(?: |$)")
_FENCE_OPEN = re.compile(rb"[ \t]*(`{3,}|~{3,})(.*)")
_COMMENT = re.compile(rb"<!--((?:(?!-->).)*)-->")
_COLON_MARKER = re.compile(rb"(\S+):(start|end)", re.IGNORECASE)
_BRIGADE_BEGIN = re.compile(rb"BEGIN BRIGADE\s+([A-Za-z][A-Za-z0-9_-]*)(?:\s.*)?")
_BRIGADE_END = re.compile(rb"END BRIGADE\s+([A-Za-z][A-Za-z0-9_-]*)\s*")
_BRIGADE_LOOSE = re.compile(rb"(?:BEGIN|END) BRIGADE\b")
_LINK_LINE = re.compile(rb"\]\(|`[^`\n]*/[^`\n]*`")


@dataclass(frozen=True)
class Unit:
    index: int
    start: int  # byte offset of the heading line
    end: int  # exclusive, before any trailing pointer run
    level: int
    heading: bytes  # the heading line, no newline
    headings: tuple[bytes, ...]  # this heading and every subheading, in order
    pin: str | None  # "marker", "until:YYYY-MM-DD" or None, from this heading or any subheading
    blocked: str | None  # "managed-block" | "pointer" | None
    links: bool  # more than half of the non-blank body lines hold a link or a backticked path
    sha256: str  # of data[start:end]


@dataclass(frozen=True)
class SectionDocument:
    data: bytes
    units: tuple[Unit, ...]  # in file order
    fixed_end: int  # end of the text before the first unit
    problem: str | None  # "not-utf8" | "unbalanced-markers" | None
    eof_in_fence: bool  # the file ends inside an unclosed fence (row W30)


@dataclass(frozen=True)
class PointerSpan:
    start: int
    end: int  # the POINTER_LINE line with its newline
    in_block: bool  # inside a fence or a managed block


@dataclass(frozen=True)
class _Line:
    start: int
    end: int  # includes the newline
    text: bytes  # without the newline
    kind: str  # "heading" | "marker" | "pointer" | "other"
    in_block: bool  # inside a fence or a managed block (marker lines themselves are not)
    in_managed: bool  # inside a managed block

    @property
    def clean(self) -> bytes:
        return self.text.rstrip(b"\r")


@dataclass(frozen=True)
class _Scan:
    lines: tuple[_Line, ...]
    unbalanced: bool
    eof_in_fence: bool


def _marker(text: bytes) -> tuple[str, tuple[str, bytes]] | None:
    """`("start" | "end", (family, name))` for a managed-block marker line, `("bad", ...)` for a malformed
    BRIGADE marker, None for any other line. A marker is a line that holds only one HTML comment."""
    comment = _COMMENT.fullmatch(text.strip())
    if comment is None:
        return None
    inner = comment.group(1).strip()
    begin = _BRIGADE_BEGIN.fullmatch(inner)
    if begin is not None:
        return ("start", ("brigade", begin.group(1)))
    end = _BRIGADE_END.fullmatch(inner)
    if end is not None:
        return ("end", ("brigade", end.group(1)))
    if _BRIGADE_LOOSE.match(inner):
        return ("bad", ("brigade", b""))
    colon = _COLON_MARKER.fullmatch(inner)
    if colon is not None:
        return (colon.group(2).lower().decode("ascii"), ("colon", colon.group(1)))
    return None


def _scan(data: bytes) -> _Scan:
    lines: list[_Line] = []
    stack: list[tuple[str, bytes]] = []
    unbalanced = False
    fence: tuple[int, int] | None = None  # (character, length)
    offset = 0
    for raw in data.splitlines(keepends=True):
        text = raw[:-1] if raw.endswith(b"\n") else raw
        clean = text.rstrip(b"\r")
        in_block = fence is not None or bool(stack)
        in_managed = bool(stack)
        kind = "other"
        if fence is not None:
            closing = rb"[ \t]*" + re.escape(bytes([fence[0]])) + (rb"{%d,}[ \t]*" % fence[1])
            if re.fullmatch(closing, clean):
                fence = None
        else:
            opening = _FENCE_OPEN.fullmatch(clean)
            if opening is not None and not (opening.group(1)[:1] == b"`" and b"`" in opening.group(2)):
                fence = (opening.group(1)[0], len(opening.group(1)))
                in_block = True
            else:
                marker = _marker(clean)
                if marker is not None:
                    kind = "marker"
                    role, name = marker
                    if role == "bad":
                        unbalanced = True
                    elif role == "start":
                        unbalanced = unbalanced or name in stack
                        stack.append(name)
                    elif stack and stack[-1] == name:
                        stack.pop()
                    else:
                        unbalanced = True
                        if name in stack:
                            stack.remove(name)
                    in_block = False
                elif not stack and _HEADING.match(clean):
                    kind = "heading"
        if kind == "other" and POINTER_LINE.fullmatch(text):
            kind = "pointer"
        lines.append(_Line(offset, offset + len(raw), text, kind, in_block, in_managed))
        offset += len(raw)
    return _Scan(tuple(lines), unbalanced or bool(stack), fence is not None)


def _level(line: _Line) -> int:
    return len(_HEADING.match(line.clean).group(1))  # type: ignore[union-attr]


def _pin_of(line: _Line) -> str | None:
    if line.clean == _KEEP:
        return "marker"
    until = KEEP_UNTIL.fullmatch(line.clean)
    if until is None:
        return None
    try:
        date.fromisoformat(until.group(1).decode("ascii"))
    except ValueError:
        return "marker"  # an unreadable date never lets a section move
    return "until:" + until.group(1).decode("ascii")


def _unit_pin(lines: tuple[_Line, ...], first: int, last: int) -> str | None:
    found: list[str] = []
    for index in range(first, last):
        if lines[index].kind == "heading" and index + 1 < last:
            pin = _pin_of(lines[index + 1])
            if pin is not None:
                found.append(pin)
    if "marker" in found:
        return "marker"
    return max(found) if found else None  # ISO dates sort as text


def _cut_pointer_run(lines: tuple[_Line, ...], first: int, last: int) -> int:
    """Index one past the last line the unit keeps: the trailing run of pointer and blank lines is cut off."""
    cut = last
    index = last - 1
    while index > first:
        line = lines[index]
        if line.kind == "pointer" and not line.in_block:
            cut = index
        elif line.text.strip():
            break
        index -= 1
    return cut


def parse(data: bytes) -> SectionDocument:
    """The units of `data`. Never raises. A file with a problem has no units."""
    scan = _scan(data)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return SectionDocument(data, (), len(data), "not-utf8", scan.eof_in_fence)
    if scan.unbalanced:
        return SectionDocument(data, (), len(data), "unbalanced-markers", scan.eof_in_fence)
    lines = scan.lines
    heads = [index for index, line in enumerate(lines) if line.kind == "heading"]
    if heads and _level(lines[heads[0]]) == 1:
        heads = heads[1:]  # the fixed text runs to the next heading
    if not heads:
        return SectionDocument(data, (), len(data), None, scan.eof_in_fence)
    starts: list[int] = []
    current: int | None = None
    for index in heads:
        level = _level(lines[index])
        if current is None or level <= current:
            starts.append(index)
            current = level
    units: list[Unit] = []
    for position, first in enumerate(starts):
        last = starts[position + 1] if position + 1 < len(starts) else len(lines)
        cut = _cut_pointer_run(lines, first, last)
        start = lines[first].start
        end = lines[cut].start if cut < last else lines[last - 1].end
        span = lines[first:cut]
        blocked = None
        if any(line.kind == "marker" or line.in_managed for line in span):
            blocked = "managed-block"
        elif any(line.kind == "pointer" for line in span):
            blocked = "pointer"
        body = [line for line in span[1:] if line.text.strip() and _pin_of(line) is None]
        links = bool(body) and 2 * sum(1 for line in body if _LINK_LINE.search(line.text)) > len(body)
        units.append(
            Unit(
                index=position,
                start=start,
                end=end,
                level=_level(lines[first]),
                heading=lines[first].clean,
                headings=tuple(line.clean for line in lines[first:last] if line.kind == "heading"),
                pin=_unit_pin(lines, first, last),
                blocked=blocked,
                links=links,
                sha256=hashlib.sha256(data[start:end]).hexdigest(),
            )
        )
    return SectionDocument(data, tuple(units), lines[starts[0]].start, None, scan.eof_in_fence)


def pointer_spans(data: bytes) -> tuple[PointerSpan, ...]:
    """Every `POINTER_LINE` line of `data`, in file order, with its newline."""
    return tuple(
        PointerSpan(line.start, line.end, line.in_block) for line in _scan(data).lines if line.kind == "pointer"
    )


def whole_section_count(doc: SectionDocument, body: bytes) -> int:
    """How many times `body` equals the bytes from one unit's start to a later or the same unit's end, with
    no gap between the spans. A substring, text inside a fence and text that runs past a unit's end never count."""
    if not body:
        return 0
    count = 0
    units = doc.units
    for first, unit in enumerate(units):
        if not doc.data.startswith(body, unit.start):
            continue
        stop = unit.start + len(body)
        last = first
        while units[last].end < stop and last + 1 < len(units) and units[last].end == units[last + 1].start:
            last += 1
        if units[last].end == stop:
            count += 1
    return count
