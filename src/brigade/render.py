"""Shared CLI output rendering.

Every command renders one payload two ways: JSON for machines, lines for
humans. Build the payload and the text lines, then emit once. This replaces
the per-module `if json_output: print(json.dumps(...))` copies.
"""

from __future__ import annotations

import json
from typing import Any


def emit(payload: dict[str, Any], json_output: bool, text_lines: list[str], rc: int) -> int:
    """Print `payload` as JSON or `text_lines` as text, then return `rc`."""
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in text_lines:
            print(line)
    return rc
