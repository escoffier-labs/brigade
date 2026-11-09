"""Bounded ``ast.literal_eval`` for bracketed front matter values.

Card front matter holds short flat lists (tags, aliases, evidence). Search,
hub sync, proposals, memory care and the doctor lint all parse every card, so
one card with a huge or deeply nested bracketed value must cost a scan, never
a full parse. ``bounded_literal_list`` keeps the long-standing fallback: when a
value cannot be evaluated safely it comes back as the original string.
"""

from __future__ import annotations

import ast
from typing import Any

# No real card list is near either bound; a 4 KiB list already holds hundreds of tags.
LITERAL_MAX_CHARS = 16384
LITERAL_MAX_DEPTH = 32

_OPENERS = "[({"
_CLOSERS = "])}"


def _within_bounds(value: str) -> bool:
    """Length and bracket depth of ``value``, without recursion.

    Brackets inside a quoted string do not count, so a tag such as
    ``'a[[b'`` is judged by what the evaluator would nest, not by its text.
    """
    if len(value) > LITERAL_MAX_CHARS:
        return False
    # Depth never exceeds the opener count, so most values skip the character scan.
    if sum(value.count(opener) for opener in _OPENERS) <= LITERAL_MAX_DEPTH:
        return True
    depth = 0
    quote = ""
    escaped = False
    for char in value:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in _OPENERS:
            depth += 1
            if depth > LITERAL_MAX_DEPTH:
                return False
        elif char in _CLOSERS:
            depth -= 1
    return True


def bounded_literal_list(value: str) -> Any:
    """``ast.literal_eval(value)``, or ``value`` itself when it is unsafe or invalid.

    Oversized, too deeply nested, malformed and evaluator-hostile input
    (including ``MemoryError`` and ``RecursionError``) all return the original
    string, the same fallback a syntax error always had.
    """
    if not _within_bounds(value):
        return value
    try:
        return ast.literal_eval(value)
    except Exception:  # noqa: BLE001 - literal_eval's failure set includes MemoryError and RecursionError
        return value
