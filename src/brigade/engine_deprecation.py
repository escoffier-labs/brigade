"""One stderr notice per process and legacy spelling during the rename window."""

from __future__ import annotations

import sys
from collections.abc import Mapping

_seen: set[str] = set()


def notice(legacy: str, canonical: str) -> None:
    if legacy not in _seen:
        _seen.add(legacy)
        print(f"{legacy} is deprecated; use {canonical}; removed in v0.33.0", file=sys.stderr)


def environment(legacy: str, canonical: str, env: Mapping[str, str]) -> None:
    if env.get(legacy):
        notice(legacy, canonical)
