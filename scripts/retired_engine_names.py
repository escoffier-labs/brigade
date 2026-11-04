#!/usr/bin/env python3
"""Reject retired engine names outside the reviewed R6 allowlist."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

ALLOWLIST = Path("tests/fixtures/retired-engine-names-allowlist.json")
REMOVABLE_KINDS = frozenset({"shim"})
KINDS = frozenset({"frozen", "shim", "history", "fixture"})
# Includes camel case, upper case env vars, and hyphen/underscore/space spellings.
RETIRED = re.compile(rb"graph[ _-]?trail|mise[ _-]?ledger", re.IGNORECASE)


def matches(root: Path) -> dict[str, Counter[str]]:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    result = {}
    for raw in tracked.split(b"\0"):
        if not raw:
            continue
        name = raw.decode("utf-8", errors="surrogateescape")
        if name == ALLOWLIST.as_posix():
            continue
        path = root / name
        if path.is_symlink():
            content = str(path.readlink()).encode("utf-8", errors="surrogateescape")
        else:
            content = path.read_bytes() if path.is_file() else b""
        found = Counter(match.decode("utf-8") for match in RETIRED.findall(raw + b"\n" + content))
        if found:
            result[name] = found
    return result


def violations(root: Path, entries: list[dict], version: str) -> list[str]:
    failures = []
    allowed = {}
    current_version = tuple(int(part) for part in version.split(".")[:3])
    for entry in entries:
        name = entry["path"]
        kind = entry["kind"]
        if name in allowed or kind not in KINDS or not entry.get("reason") or not entry.get("literals"):
            failures.append(f"{name}: invalid or duplicate allowlist entry")
        allowed[name] = entry["literals"]
        if kind in REMOVABLE_KINDS:
            if entry.get("remove_in") != "0.33.0":
                failures.append(f"{name}: removable entry requires remove_in 0.33.0")
            if current_version >= (0, 33, 0):
                failures.append(f"{name}: {kind} allowance expired in 0.33.0")
    for name, found in sorted(matches(root).items()):
        limits = allowed.get(name, {})
        for literal, count in sorted(found.items()):
            if count > limits.get(literal, 0):
                failures.append(f"{name}: {literal!r} count {count} exceeds allowance {limits.get(literal, 0)}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    entries = json.loads((args.root / ALLOWLIST).read_text(encoding="utf-8"))
    # Read the package version without importing the installed checkout.
    source = (args.root / "src/brigade/__init__.py").read_text(encoding="utf-8")
    version = re.search(r'__version__ = "([^"]+)"', source)
    if version is None:
        parser.error("package version not found")
    failures = violations(args.root, entries, version.group(1))
    for failure in failures:
        print(failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
