from __future__ import annotations

import sys

_INVALID_OPERAND = "invalid revision range operand"

_ALLOWED_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~/^:@{}-")
_FORBIDDEN_METACHAR = frozenset(";|&$`()<>\\\"' \t\n\r*?[]!%=")


def validate_rev_range_operand(operand: str) -> None:
    """Reject user-controlled revision/range operands before git rev-list."""
    if _operand_invalid(operand):
        print(_INVALID_OPERAND, file=sys.stderr)
        raise SystemExit(2)


def _operand_invalid(operand: str) -> bool:
    if not operand or operand != operand.strip():
        return True
    if operand[0] == "-":
        return True
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in operand):
        return True
    if any(ch in _FORBIDDEN_METACHAR for ch in operand):
        return True
    components = _split_rev_range(operand)
    if components is None:
        return True
    for component in components:
        if not component or component[0] == "-":
            return True
        if not all(ch in _ALLOWED_CHARS for ch in component):
            return True
    return False


def _split_rev_range(operand: str) -> list[str] | None:
    if "..." in operand:
        if operand.count("...") != 1:
            return None
        left, _, right = operand.partition("...")
        if not left or not right:
            return None
        return [left, right]
    if ".." in operand:
        if operand.count("..") != 1:
            return None
        left, _, right = operand.partition("..")
        if not left or not right:
            return None
        return [left, right]
    return [operand]
