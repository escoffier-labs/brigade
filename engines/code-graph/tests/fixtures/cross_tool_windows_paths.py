#!/usr/bin/env python3
"""Adapter fixture that deliberately emits Windows-style relative paths."""

import json
from pathlib import Path
import sys


json.load(sys.stdin)
paths = sorted(
    path.relative_to(Path.cwd())
    for path in Path.cwd().rglob("*")
    if path.is_file() and ".git" not in path.parts
)
print(
    json.dumps(
        {
            "response": "\n".join(path.read_text(encoding="utf-8") for path in paths),
            "files": [str(path).replace("/", "\\") for path in paths],
            "setup_steps": 0,
            "tool_calls": 1,
        }
    )
)
