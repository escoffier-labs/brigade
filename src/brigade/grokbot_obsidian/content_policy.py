"""Read and write content filters. Vault text is untrusted."""

from __future__ import annotations

import re
from typing import NoReturn

from .contracts import ERROR_MESSAGES, ObsidianError
from .utf8 import is_well_formed

MAX_DEPTH = 8
MAX_ENTRIES = 256
FENCED_DATAVIEWJS = re.compile(r"(?:```|~~~)[^\n]*dataviewjs", re.IGNORECASE)
INLINE_DATAVIEW_JS = re.compile(r"\$=")
SCRIPT_TAG = re.compile(r"<\s*script[\s>/]", re.IGNORECASE)
JAVASCRIPT_URL = re.compile(r"javascript\s*:", re.IGNORECASE)
EVENT_HANDLER = re.compile(r"(?:^|[\s\"'</])on[a-z]+\s*=", re.IGNORECASE)
FENCED_BLOCK = re.compile(r"(^|\n)[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?\n[ \t]{0,3}\2[^\S\n]*(?:\n|$)")
INLINE_CODE = re.compile(r"`[^`\n]*`")


def _deny() -> NoReturn:
    raise ObsidianError("denied", ERROR_MESSAGES["denied"])


def _strip_inert_code(value: str) -> str:
    return INLINE_CODE.sub("", FENCED_BLOCK.sub(r"\1", value))


def contains_executable_markdown(value: str) -> bool:
    if FENCED_DATAVIEWJS.search(value):
        return True
    prose = _strip_inert_code(value)
    if "<%" in prose:
        return True
    if INLINE_DATAVIEW_JS.search(prose):
        return True
    if SCRIPT_TAG.search(prose):
        return True
    if JAVASCRIPT_URL.search(prose):
        return True
    return EVENT_HANDLER.search(prose) is not None


def _walk(value: object, depth: int, seen: set[int], budget: list[int]) -> None:
    if isinstance(value, str):
        if not is_well_formed(value) or contains_executable_markdown(value):
            _deny()
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value or value in {float("inf"), float("-inf")}:
            _deny()
        return
    if not isinstance(value, (list, dict)):
        _deny()
    identity = id(value)
    if identity in seen or depth >= MAX_DEPTH:
        _deny()
    seen.add(identity)
    if isinstance(value, list):
        budget[0] += len(value)
        if budget[0] > MAX_ENTRIES:
            _deny()
        for item in value:
            _walk(item, depth + 1, seen, budget)
        return
    if any(key in {"__proto__", "prototype", "constructor"} for key in value):
        _deny()
    budget[0] += len(value)
    if budget[0] > MAX_ENTRIES:
        _deny()
    for item in value.values():
        _walk(item, depth + 1, seen, budget)


def assert_safe_markdown(value: object) -> None:
    _walk(value, 0, set(), [0])
