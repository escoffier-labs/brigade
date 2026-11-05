"""Shared prompt-load rules for local connectors and the native hub.

The versioned bundle exports the guard defaults and the Dot injection families.
Hub scanning deliberately uses raw matches without repository context exceptions.
"""

from __future__ import annotations

import json
import bisect
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


# Necessary conditions only. These linear searches have no failing suffix on
# their token runs. They avoid retrying a bounded local-part or digit search
# at every dotted word in a 512 KiB source card. IGNORECASE includes Python's
# extra Unicode case folds, so the filter is also a superset of ASCII mode.
_EMAIL_DOMAIN = re.compile(r"@[A-Z0-9.-]{1,256}\.[A-Z]{2,63}", re.IGNORECASE)
_RESERVED_DOMAIN = re.compile(r"(?:example\.(?:com|org|net)|test|invalid|localhost|local)", re.IGNORECASE)
_SSH_HOST = re.compile(r"@(?:10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.|127\.)")
_ASSIGNMENT_START = re.compile(r"\b(?:api[_-]?key|token|secret)\s{0,256}[:=]\s{0,256}", re.IGNORECASE)
_ASSIGNMENT_START_ASCII = re.compile(_ASSIGNMENT_START.pattern, re.IGNORECASE | re.ASCII)
_DIGIT = re.compile(r"[0-9]")
_TOKEN_RUN = re.compile(r"[A-Z0-9._~+/=-]+", re.IGNORECASE)


def _assignment_search(pattern: re.Pattern[str], text: str) -> bool:
    # Short unquoted matches need an ASCII digit within 4096 characters.
    # Saturated runs and quoted values bypass that requirement. Index both
    # once instead of walking 4096 characters at every assignment start.
    digits = [match.start() for match in _DIGIT.finditer(text)]
    long_runs = [(run.start(), run.end()) for run in _TOKEN_RUN.finditer(text) if run.end() - run.start() >= 4097]
    run_index = 0
    prefixes = _ASSIGNMENT_START_ASCII if pattern.flags & re.ASCII else _ASSIGNMENT_START
    for prefix in prefixes.finditer(text):
        value = prefix.end()
        if value == len(text):
            continue
        while run_index < len(long_runs) and long_runs[run_index][1] < value:
            run_index += 1
        saturated = run_index < len(long_runs) and long_runs[run_index][0] <= value <= long_runs[run_index][1] - 4097
        if text[value] not in "\"'" and not saturated:
            digit = bisect.bisect_left(digits, value)
            if digit == len(digits) or digits[digit] - value > 4095:
                continue
        if pattern.match(text, prefix.start()):
            return True
    return False


def _rule_possible(name: str, text: str) -> bool:
    if name == "email":
        return _EMAIL_DOMAIN.search(text) is not None
    if name == "example-email-reserved":
        return "@" in text and _RESERVED_DOMAIN.search(text) is not None
    if name == "ssh-private-target":
        return _SSH_HOST.search(text) is not None
    return True


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
        # pass finds a superset for these patterns, so only non-ASCII copies
        # need the ASCII pass. Bundle tests require review of inverse classes.
        copies = tuple(dict.fromkeys((text, lines)))
        extra_copies = tuple(copy for copy in dict.fromkeys((injection_lines, normalized)) if copy not in copies)
        possible: dict[tuple[str, str], bool] = {}
        for name, _action, pattern in _RULES:
            for copy in copies:
                if pattern.flags & re.ASCII and copy.isascii():
                    continue
                key = (name, copy)
                if key not in possible:
                    possible[key] = _rule_possible(name, copy)
                if possible[key] and (
                    _assignment_search(pattern, copy) if name == "api-key-assignment" else pattern.search(copy)
                ):
                    return name
        for name, _action, pattern in _INJECTIONS:
            if any(pattern.search(copy) for copy in extra_copies if not (pattern.flags & re.ASCII and copy.isascii())):
                return name
        if joiner:
            return JOINER_RULE
    return None
