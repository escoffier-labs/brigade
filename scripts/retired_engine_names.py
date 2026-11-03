#!/usr/bin/env python3
"""Count retired engine names in tracked paths and contents for the R0 ratchet.

Initialize the baseline in R0. Only R6 may regenerate the complete baseline;
intermediate compatibility slices may review increases in their own entries.
The baseline JSON is excluded because its path keys would count themselves.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

BASELINE = Path("tests/fixtures/retired-engine-names-baseline.json")
# Includes camel case, upper case env vars, and hyphen/underscore/space spellings.
RETIRED = re.compile(rb"graph[ _-]?trail|mise[ _-]?ledger", re.IGNORECASE)


def counts(root: Path) -> dict[str, int]:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    result = {}
    for raw in tracked.split(b"\0"):
        if not raw:
            continue
        name = raw.decode("utf-8", errors="surrogateescape")
        if name == BASELINE.as_posix():
            continue
        path = root / name
        # Deleted tracked files lower counts. Do not follow tracked symlinks.
        if path.is_symlink():
            content = str(path.readlink()).encode("utf-8", errors="surrogateescape")
        else:
            content = path.read_bytes() if path.is_file() else b""
        count = len(RETIRED.findall(raw)) + len(RETIRED.findall(content))
        if count:
            result[name] = count
    return result


def violations(root: Path, baseline: dict[str, int]) -> list[str]:
    return [
        f"{name}: {count} retired-name matches exceeds baseline {baseline.get(name, 0)}"
        for name, count in sorted(counts(root).items())
        if count > baseline.get(name, 0)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--write-baseline", action="store_true", help="R0 initialization or R6 only")
    args = parser.parse_args()
    baseline_path = args.root / BASELINE
    if args.write_baseline:
        baseline_path.write_text(json.dumps(counts(args.root), indent=2, sort_keys=True) + "\n")
        return 0
    failures = violations(args.root, json.loads(baseline_path.read_text()))
    for failure in failures:
        print(failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
