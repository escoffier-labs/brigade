"""Decode local receipt JSON within the nesting budget of recursive reporters."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Receipt fields are shallow. Bound nested metadata independently of the
# interpreter's recursion limit so a successful decode is still safe to report.
MAX_NESTING = 128


def read(path: Path) -> Any:
    """Read JSON, raising normal I/O/decoder errors or ValueError for deep data."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    frames: list[Iterator[Any]] = [iter((payload,))]
    while frames:
        try:
            value = next(frames[-1])
        except StopIteration:
            frames.pop()
            continue
        if not isinstance(value, (dict, list)):
            continue
        if len(frames) > MAX_NESTING:
            raise ValueError("receipt JSON nesting exceeds 128 containers")
        frames.append(iter(value.values() if isinstance(value, dict) else value))
    return payload
