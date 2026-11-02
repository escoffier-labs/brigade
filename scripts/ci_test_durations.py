#!/usr/bin/env python3
"""Build per-file test durations for CI shard balancing from pytest junit XML.

CI uploads one `junit-3.12-<shard>` artifact per Python 3.12 shard. Download
them from a recent green run and refresh the committed table:

    gh run download <run-id> --pattern 'junit-3.12-*' --dir /tmp/junit
    python scripts/ci_test_durations.py /tmp/junit/*/junit.xml

Stale or missing entries only cost balance: untimed files fall back to hash
sharding, and every collected test still runs in exactly one shard.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
from collections.abc import Sequence
import xml.etree.ElementTree as ElementTree

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path(__file__).with_name("ci_test_durations.json")


def _case_file(case: ElementTree.Element, root: Path) -> str | None:
    # xunit1 reports carry the file; xunit2 only has a dotted classname such as
    # tests.test_module or tests.test_module.TestClass.
    recorded = case.get("file")
    if recorded:
        return Path(recorded).as_posix()
    parts = (case.get("classname") or "").split(".")
    for end in range(len(parts), 0, -1):
        candidate = "/".join(parts[:end]) + ".py"
        if (root / candidate).is_file():
            return candidate
    return None


def file_durations(reports: Sequence[Path], root: Path = REPO_ROOT) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for report in reports:
        for case in ElementTree.parse(report).iter("testcase"):
            name = _case_file(case, root)
            try:
                seconds = float(case.get("time") or 0)
            except ValueError:
                continue
            if name is not None and seconds >= 0:
                totals[name] += seconds
    return {name: round(totals[name], 2) for name in sorted(totals)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reports", nargs="+", type=Path, help="pytest junit XML files")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parsed = parser.parse_args(argv)
    files = file_durations(parsed.reports)
    if not files:
        parser.error("no test cases with a resolvable file were found")
    parsed.output.write_text(json.dumps({"version": 1, "files": files}, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(files)} files, {sum(files.values()):.0f} s total, to {parsed.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
