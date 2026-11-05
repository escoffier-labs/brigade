"""One stderr notice per process and legacy spelling during the rename window."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from typing import Any

_seen: set[str] = set()


def notice(legacy: str, canonical: str) -> None:
    if legacy not in _seen:
        _seen.add(legacy)
        print(f"{legacy} is deprecated; use {canonical}; removed in v0.33.0", file=sys.stderr)


def environment(legacy: str, canonical: str, env: Mapping[str, str]) -> None:
    if env.get(legacy):
        notice(legacy, canonical)


class LegacyOption(argparse.Action):
    def __init__(self, *args: Any, replacement: str, **kwargs: Any) -> None:
        self.replacement = replacement
        super().__init__(*args, **kwargs)

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: str | None = None) -> None:
        notice(option_string or self.dest, self.replacement)
        setattr(namespace, self.dest, values)
