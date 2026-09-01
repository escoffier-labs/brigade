#!/usr/bin/env python3
"""Fail fast when the selected interpreter imports brigade from another checkout.

Borrowing a sibling checkout's virtualenv, or pointing PY at one, leaves the
gate half-honest: ruff, mypy, and in-process tests read this tree because
tests/conftest.py puts its src first on sys.path, while any test that spawns a
bare subprocess imports whatever the editable install resolves to. That reads as
an unrelated test failure 35 minutes into the run.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED = REPO_ROOT / "src" / "brigade"
SETUP = 'python -m venv .venv && .venv/bin/pip install -e ".[dev]"'


def main() -> int:
    try:
        import brigade
    except ModuleNotFoundError:
        print(
            f"verify: {sys.executable} cannot import brigade.\n  expected: {EXPECTED}\n  fix:      {SETUP}",
            file=sys.stderr,
        )
        return 1

    resolved = Path(brigade.__file__).resolve().parent
    if resolved != EXPECTED:
        print(
            "verify: this interpreter imports brigade from a different checkout.\n"
            f"  resolved: {resolved}\n"
            f"  expected: {EXPECTED}\n"
            f"  python:   {sys.executable}\n"
            f"  fix:      unset PY, or {SETUP}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
