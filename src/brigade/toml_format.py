"""Minimal single-line TOML value formatting."""

from __future__ import annotations

import json


def format_toml_value(value: object) -> str:
    """Render a scalar as a single-line TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    rendered = str(value)
    if "'" not in rendered and not any(ord(char) < 32 or ord(char) == 127 for char in rendered):
        return f"'{rendered}'"
    return json.dumps(rendered, ensure_ascii=False).replace("\x7f", "\\u007f")
