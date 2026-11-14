"""The owner's USER.md shape, built to exact byte sizes (slice 2 plan, "Acceptance fixture").

Every section is exactly its listed size before any pin. A pinned section gets the keep marker on the
line under its heading, which adds `len(KEEP) + 1` bytes. `Reference cards` holds only link lines. Every
other body is prose with no link, pointer, fence or marker. The builder asserts each length it writes.
"""

from __future__ import annotations

from brigade.instruction_sections import KEEP

OWNER_SECTIONS: tuple[tuple[str, int], ...] = (
    ("Employment", 1018),
    ("Education", 156),
    ("Business", 147),
    ("Career Story", 171),
    ("Voice & Communication", 651),
    ("What He Cares About", 195),
    ("What Annoys Him", 300),
    ("Preferences (core)", 906),
    ("Reference cards", 907),
)
OWNER_PREAMBLE = 1664
OWNER_PINS: tuple[str, ...] = ("Voice & Communication", "Preferences (core)")
LINK_SECTIONS = frozenset({"Reference cards"})

_WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa"
_LINE = 79  # prose characters per line, so each line is 80 bytes with its newline


def _prose(size: int) -> bytes:
    """Exactly `size` bytes of newline-terminated lowercase text with no markdown syntax."""
    text = (_WORDS + " ") * (size // len(_WORDS) + 2)
    out = bytearray()
    cursor = 0
    while size - len(out) > _LINE + 1:
        out += text[cursor : cursor + _LINE].encode() + b"\n"
        cursor += _LINE
    rest = size - len(out)
    if rest:
        out += text[cursor : cursor + rest - 1].encode() + b"\n"
    assert len(out) == size
    return bytes(out)


def _links(size: int) -> bytes:
    """Exactly `size` bytes of `- [card <n>](memory/cards/ref-<n>.md)` lines. The last line pads its number
    with zeros, because every line of this form is an even number of bytes long."""
    assert size % 2 == 0 and size >= 34
    out = b""
    number = 1
    while True:
        line = f"- [card {number}](memory/cards/ref-{number}.md)\n".encode()
        remaining = size - len(out)
        if remaining == len(line):
            return out + line
        if remaining - len(line) < 34:
            width = (remaining - 32) // 2
            padded = f"{number:0{width}d}"
            return out + f"- [card {padded}](memory/cards/ref-{padded}.md)\n".encode()
        out += line
        number += 1


def _section(name: str, size: int, *, pinned: bool) -> bytes:
    heading = f"## {name}\n".encode()
    marker = (KEEP + "\n").encode() if pinned else b""
    body_size = size - len(heading)
    body = _links(body_size) if name in LINK_SECTIONS else _prose(body_size)
    section = heading + marker + body
    assert len(section) == size + len(marker)
    return section


def owner_user_md(
    sections: tuple[tuple[str, int], ...] = OWNER_SECTIONS,
    *,
    preamble: int = OWNER_PREAMBLE,
    pins: tuple[str, ...] = OWNER_PINS,
) -> bytes:
    """`# User`, prose to `preamble` bytes, then one level-2 section per row."""
    head = b"# User\n\n"
    data = head + _prose(preamble - len(head))
    assert len(data) == preamble
    for name, size in sections:
        data += _section(name, size, pinned=name in pins)
    expected = preamble + sum(size for _, size in sections) + sum(len(KEEP) + 1 for name, _ in sections if name in pins)
    assert len(data) == expected
    return data
