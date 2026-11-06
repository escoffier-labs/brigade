#!/usr/bin/env python3
"""Export the guard and Dot prompt-load rules for the native hub. Check in CI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from brigade.guard import Policy
from brigade.untrusted import _LINE_RULES

DEST = Path(__file__).resolve().parents[1] / "src/brigade/guard/memory-rules.json"
VERSION = "brigade-memory-scan.v7"
# NFKC runs only on code points Unicode 14.0.0 assigns, so Python 3.11 and later
# and the Rust crate normalize the same text the same way.
NFKC_UNICODE = "14.0.0"
UNASSIGNED = Path(__file__).resolve().parent / "memory_scan_unicode14_unassigned.ranges"

# Hub memory only (S18 amendment, D24). These guard rules describe the
# operator's own infrastructure and identity. Memory stores such text and loads
# it into the operator's local harness prompts, and refuses it on every path to
# a connector audience (Grok Bot, relays, public scopes, remote embedders).
# The publication guard keeps each rule's own severity in guard/rules.py.
PRIVATE_RULES = frozenset(
    {
        "loopback-ipv4",
        "private-ipv4",
        "port-reference",
        "localhost-bare",
        "localhost-port",
        "private-hostname",
        "ssh-private-target",
        "root-path",
        "home-path",
        "windows-home-path",
        "email",
        "example-email-reserved",
        "us-phone",
        "example-phone-555",
    }
)


def unassigned_ranges() -> str:
    """The source table as one `lo-hi,lo-hi` hex string, in order."""
    lines = [line.strip() for line in UNASSIGNED.read_text(encoding="utf-8").splitlines()]
    return ",".join(line for line in lines if line and not line.startswith("#"))


def bundle() -> str:
    policy = Policy()
    missing = PRIVATE_RULES - {rule.id for rule in policy.all_rules()}
    if missing:
        raise SystemExit(f"private memory rules missing from the guard: {sorted(missing)}")
    rules = [
        {
            "id": rule.id,
            "pattern": rule.pattern,
            "flags": int(rule.flags),
            "action": "private" if rule.id in PRIVATE_RULES else policy.action_for(rule),
        }
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
            "pattern": r"^(?=[^\n]{0,256}[A-Za-z0-9+/]{80,4096}={0,2})(?=[^\n]{0,4096}(?:\n[^\n]{0,4096}){0,3}\b(?i:base64|atob|decode|decrypt)\b)[^\n]{1,4096}",
            "flags": 8,
            "action": "warn",
        },
    ]
    nfkc = {"unicode": NFKC_UNICODE, "unassigned": unassigned_ranges()}
    return json.dumps({"version": VERSION, "nfkc": nfkc, "injections": injections, "rules": rules}, indent=2) + "\n"


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
