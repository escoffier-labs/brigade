"""CLI helpers for untrusted-context wrapping and scanning."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .untrusted import SOURCE_KINDS, scan_untrusted, wrap_untrusted


def _read_input(*, text: list[str], from_file: Path | None) -> tuple[str | None, str | None]:
    if from_file is not None:
        path = from_file.expanduser()
        try:
            return path.read_text(errors="replace"), None
        except OSError as exc:
            return None, f"cannot read input file: {exc}"
    if text:
        if len(text) == 1 and "\n" not in text[0]:
            candidate = Path(text[0]).expanduser()
            if candidate.is_file():
                return None, (
                    f"{text[0]} is a file path; pass --from-file {text[0]} to scan its contents "
                    "(positional text is scanned literally)"
                )
        return " ".join(text), None
    return None, "provide text or --from-file"


def scan(*, text: list[str], from_file: Path | None = None, json_output: bool = False) -> int:
    content, error = _read_input(text=text, from_file=from_file)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    result = scan_untrusted(content or "")
    payload: dict[str, Any] = {
        "flagged": result.flagged,
        "count": result.count,
        "markers": result.markers,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not result.flagged else 1
    print(f"flagged: {str(result.flagged).lower()}")
    print(f"markers: {result.count}")
    for marker in result.markers:
        print(f"- {marker}")
    return 0 if not result.flagged else 1


def wrap(
    *,
    text: list[str],
    source_kind: str,
    from_file: Path | None = None,
    goal: str | None = None,
    max_chars: int | None = None,
    json_output: bool = False,
) -> int:
    content, error = _read_input(text=text, from_file=from_file)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        wrapped = wrap_untrusted(content or "", source_kind=source_kind, goal=goal, max_chars=max_chars)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(
            json.dumps(
                {
                    "source_kind": source_kind,
                    "goal": goal,
                    "max_chars": max_chars,
                    "wrapped": wrapped,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(wrapped)
    return 0
