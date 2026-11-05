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
_BIDI = frozenset(chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
_INVISIBLE = frozenset(chr(code) for code in (0x200B, *range(0x2060, 0x2065), 0xFEFF, *range(0xE0000, 0xE0080)))
_BUNDLE = json.loads(files("brigade.guard").joinpath("memory-rules.json").read_text(encoding="utf-8"))
_RULES = [
    (item["id"], item["action"], re.compile(item["pattern"], item["flags"]))
    for item in [*_BUNDLE["injections"], *_BUNDLE["rules"]]
]


def invisible_rule(text: str) -> str | None:
    found = set(text[1:] if text.startswith("\ufeff") else text)
    if found & _BIDI:
        return BIDI_RULE
    if found & _INVISIBLE:
        return INVISIBLE_RULE
    return None


def blocked_rule(texts: Iterable[str]) -> str | None:
    """Return an id only. Every finding withholds prompt text, including warnings."""
    # Scan each field independently too: a leading BOM exemption belongs to one
    # field, and concatenation must not hide a field's role directive at line start.
    values = [value for value in texts if isinstance(value, str)]
    for text in [*values, "\n".join(value.removeprefix("\ufeff") for value in values)]:
        rule = invisible_rule(text)
        if rule:
            return rule
        if "\u200c" in text or "\u200d" in text:
            return JOINER_RULE
        for name, _action, pattern in _RULES:
            if pattern.search(text):
                return name
        normalized = " ".join(text.split())
        if _RULES[0][2].search(normalized):
            return _RULES[0][0]
    return None
