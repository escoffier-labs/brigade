"""Shared prompt-load rules for local connectors and the native hub.

The versioned bundle exports the guard defaults and the Dot injection families.
Hub scanning deliberately uses raw matches without repository context exceptions.
"""

from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Iterable

BIDI_RULE = "bidi-control"
INVISIBLE_RULE = "invisible-character"
JOINER_RULE = "zero-width-joiner"
# Explicit sets shared with the native scanner, independent of runtime Unicode.
_WHITESPACE = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    + "".join(map(chr, range(0x2000, 0x200B)))
    + "\u2028\u2029\u202f\u205f\u3000"
)
_BOUNDARIES = "\n\v\f\r\x1c\x1d\x1e\x85\u2028\u2029"
_BIDI = frozenset(chr(code) for code in (0x61C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)))
# Unicode 16 Default_Ignorable_Code_Point ranges. Never edit stored bytes.
_IGNORABLE_RANGES = (
    (0xAD, 0xAD),
    (0x34F, 0x34F),
    (0x61C, 0x61C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_IGNORABLE = frozenset(chr(code) for lo, hi in _IGNORABLE_RANGES for code in range(lo, hi + 1))
_INVISIBLE = _IGNORABLE | frozenset("\ufff9\ufffa\ufffb")
_STRIP_IGNORABLE = dict.fromkeys(map(ord, _IGNORABLE))
_FOLD_FULLWIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}
_LINE_TRANSLATION = dict.fromkeys(map(ord, _BOUNDARIES), "\n")
_SPACE_TRANSLATION = dict.fromkeys(map(ord, _WHITESPACE), " ")


def _emoji_base(char: str) -> bool:
    # Conservative explicit allowlist: sun, heart, rocket and emoticons.
    return char in "☀❤🚀" or 0x1F600 <= ord(char) <= 0x1F64F


_BUNDLE = json.loads(files("brigade.guard").joinpath("memory-rules.json").read_text(encoding="utf-8"))


def _compile(items: list[dict], ascii_mode: bool = False) -> list[tuple[str, str, re.Pattern[str]]]:
    return [
        (
            item["id"],
            item["action"],
            re.compile(item["pattern"], (item["flags"] & ~re.UNICODE) | re.ASCII if ascii_mode else item["flags"]),
        )
        for item in items
    ]


_INJECTIONS = _compile(_BUNDLE["injections"]) + _compile(_BUNDLE["injections"], True)
_RULES = _INJECTIONS + _compile(_BUNDLE["rules"]) + _compile(_BUNDLE["rules"], True)


def invisible_rule(text: str) -> str | None:
    if set(text) & _BIDI:
        return BIDI_RULE
    for index, char in enumerate(text):
        if char == "\ufeff" and index == 0:
            continue
        if char in "\u200c\u200d":
            continue
        if "\ufe00" <= char <= "\ufe0f" and index and _emoji_base(text[index - 1]):
            continue
        if char in _INVISIBLE:
            return INVISIBLE_RULE
    return None


def _scan_copies(text: str) -> tuple[str, str, str]:
    # Preserve all splitlines boundaries before applying MULTILINE line rules.
    lines = text.replace("\r\n", "\n").translate(_LINE_TRANSLATION)
    lines = lines.translate(_STRIP_IGNORABLE).translate(_FOLD_FULLWIDTH)
    normalized = " ".join(part for part in lines.translate(_SPACE_TRANSLATION).split(" ") if part)
    injection_lines = "\n".join(
        " ".join(part for part in line.translate(_SPACE_TRANSLATION).split(" ") if part) for line in lines.split("\n")
    )
    return lines, injection_lines, normalized


def blocked_rule(texts: Iterable[str]) -> str | None:
    """Return an id only. Every finding withholds prompt text, including warnings."""
    # Scan each field independently too: a leading BOM exemption belongs to one
    # field, and concatenation must not hide a field's role directive at line start.
    values = [value for value in texts if isinstance(value, str)]
    # Equal fields and an equal joined value cannot add a finding. Keep the
    # BOM-stripped joined copy when it differs, and preserve field order.
    for text in dict.fromkeys([*values, "\n".join(value.removeprefix("\ufeff") for value in values)]):
        rule = invisible_rule(text)
        if rule:
            return rule
        joiner = "\u200c" in text or "\u200d" in text
        lines, injection_lines, normalized = _scan_copies(text)
        # Bundle patterns are ASCII. On an ASCII copy the earlier Unicode
        # pass is identical, so only non-ASCII copies need the ASCII pass.
        copies = tuple(dict.fromkeys((text, lines)))
        extra_copies = tuple(copy for copy in dict.fromkeys((injection_lines, normalized)) if copy not in copies)
        # These rules require a literal @. Without it, dotted near-matches
        # otherwise restart an unanchored local-part search at every label.
        for name, _action, pattern in _RULES:
            if name in ("email", "example-email-reserved", "ssh-private-target") and not any(
                "@" in copy for copy in copies
            ):
                continue
            if any(pattern.search(copy) for copy in copies if not (pattern.flags & re.ASCII and copy.isascii())):
                return name
        for name, _action, pattern in _INJECTIONS:
            if any(pattern.search(copy) for copy in extra_copies if not (pattern.flags & re.ASCII and copy.isascii())):
                return name
        if joiner:
            return JOINER_RULE
    return None
