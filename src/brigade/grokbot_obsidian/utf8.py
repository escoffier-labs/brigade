"""Bounded UTF-8 helpers for the Obsidian Operator pack."""

from __future__ import annotations


def utf8_byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def is_well_formed(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def utf8_in_range(value: object, minimum: int, maximum: int) -> bool:
    if not isinstance(value, str) or not is_well_formed(value):
        return False
    size = utf8_byte_length(value)
    return minimum <= size <= maximum


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    if not is_well_formed(text):
        return "", True
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "", True
