#!/usr/bin/env python3
"""Deliberately invalid adapter that only echoes its benchmark request."""

import json
import sys


request = json.load(sys.stdin)
print(
    json.dumps(
        {
            "response": json.dumps(request, sort_keys=True),
            "files": [],
            "setup_steps": 0,
            "tool_calls": 0,
        }
    )
)
