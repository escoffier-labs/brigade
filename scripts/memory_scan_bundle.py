#!/usr/bin/env python3
"""Export the guard and Dot prompt-load rules for the native hub. Check in CI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from brigade.guard import Policy
from brigade.untrusted import _LINE_RULES

DEST = Path(__file__).resolve().parents[1] / "src/brigade/guard/memory-rules.json"


def bundle() -> str:
    policy = Policy()
    rules = [
        {"id": rule.id, "pattern": rule.pattern, "flags": int(rule.flags), "action": policy.action_for(rule)}
        for rule in policy.all_rules()
    ]
    injections = [
        {"id": name, "pattern": pattern.pattern, "flags": int(pattern.flags | 8), "action": "warn"}
        for name, pattern in _LINE_RULES
    ]
    injections += [
        {"id": "role-override", "pattern": r"(?i)\bnew system prompt\b", "flags": 0, "action": "warn"},
        {
            "id": "base64-decode-chain",
            "pattern": r"^(?=[^\n]*[A-Za-z0-9+/]{80,}={0,2})(?=[^\n]*(?:\n[^\n]*){0,3}\b(?i:base64|atob|decode|decrypt)\b)[^\n]+",
            "flags": 8,
            "action": "warn",
        },
    ]
    return json.dumps({"version": "brigade-memory-scan.v2", "injections": injections, "rules": rules}, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = bundle()
    if args.check:
        if DEST.read_text(encoding="utf-8") != expected:
            print("memory scan bundle is stale: run scripts/memory_scan_bundle.py")
            return 1
    else:
        DEST.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
