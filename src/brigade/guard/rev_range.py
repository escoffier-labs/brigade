from __future__ import annotations

import os
import subprocess
import sys
from typing import NoReturn

_INVALID_OPERAND = "invalid revision range operand"
_MAX_OPERAND_BYTES = 65_536


def validate_rev_range_operand(operand: str) -> None:
    """Reject option-shaped or unbounded operands before git rev-list."""
    if _operand_invalid(operand):
        fail_invalid_rev_range_operand()


def _operand_invalid(operand: str) -> bool:
    if not operand or operand.isspace():
        return True
    if operand[0] == "-":
        return True
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in operand):
        return True
    if len(os.fsencode(operand)) > _MAX_OPERAND_BYTES:
        return True
    return False


def fail_invalid_rev_range_operand() -> NoReturn:
    print(_INVALID_OPERAND, file=sys.stderr)
    raise SystemExit(2)


def git_has_head() -> bool:
    """Return false only for an unborn branch; fail on other Git errors."""
    try:
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], capture_output=True, text=True, check=False)
        if head.returncode == 0:
            return True

        symbolic = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True, check=False)
        branch_ref = symbolic.stdout.strip()
        if symbolic.returncode == 0 and branch_ref:
            branch = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", branch_ref],
                capture_output=True,
                text=True,
                check=False,
            )
            if branch.returncode == 1:
                return False
    except OSError:
        print("git command failed", file=sys.stderr)
        raise SystemExit(2) from None

    print((head.stderr or symbolic.stderr or "git rev-parse failed").strip(), file=sys.stderr)
    raise SystemExit(2)
