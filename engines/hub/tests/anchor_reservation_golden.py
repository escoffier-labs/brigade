"""Python-generated goldens for hub claim-anchor reservation bytes.

The hub rewrites `claim_anchor_ids` in Rust. The local proposal path is the
oracle for those bytes, so this script records what
`memory_proposals._update_metadata` writes for the same card and id set.

One case is `fail_closed`: an indented `claim_anchor_ids` line is an upsert
target for the writer but not the field for the hub's exact-key reader, so the
hub refuses the transform instead of rewriting a line it does not read. The
golden still records what Python would have written for comparison.

Run from the repository root with `PYTHONPATH=src`:

    python engines/hub/tests/anchor_reservation_golden.py --write
    python engines/hub/tests/anchor_reservation_golden.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from brigade import memory_proposals

HUB = Path(__file__).resolve().parents[1]
GOLDEN = HUB / "tests" / "anchor-reservation-golden.json"

# Every case names a front-matter shape the hub writer must reproduce, plus the
# ids the transform is reserving. The ids are sorted before writing, as the
# local path sorts the union it records.
CASES = [
    {"name": "no-front-matter", "text": "Claim.\n", "ids": ["c-111111"]},
    {
        "name": "no-front-matter-crlf-body",
        "text": "Claim.\r\n",
        "ids": ["c-111111"],
    },
    {
        "name": "empty-list",
        "text": '---\nclaim_anchor_ids: []\n---\nClaim.\n',
        "ids": ["c-111111"],
    },
    {
        "name": "existing-list",
        "text": '---\ntitle: T\nclaim_anchor_ids: ["c-111111"]\n---\nClaim.\n',
        "ids": ["c-111111", "c-222222"],
    },
    {
        "name": "existing-list-compact",
        "text": '---\nclaim_anchor_ids: ["c-111111","c-222222"]\ntitle: T\n---\nClaim.\n',
        "ids": ["c-111111", "c-222222", "c-333333"],
    },
    {
        "name": "existing-list-unsorted",
        "text": '---\nclaim_anchor_ids: ["c-333333", "c-111111"]\n---\nClaim.\n',
        "ids": ["c-111111", "c-222222", "c-333333"],
    },
    {
        "name": "trailing-blank-line",
        "text": "---\ntitle: T\n\n---\nClaim.\n",
        "ids": ["c-111111"],
    },
    {
        "name": "trailing-blank-lines",
        "text": "---\ntitle: T\n\n\n---\nClaim.\n",
        "ids": ["c-111111"],
    },
    {
        "name": "crlf",
        "text": "---\r\ntitle: T\r\n---\r\nClaim.\r\n",
        "ids": ["c-111111"],
    },
    {
        "name": "crlf-existing-list",
        "text": '---\r\nclaim_anchor_ids: ["c-111111"]\r\n---\r\nClaim.\r\n',
        "ids": ["c-111111", "c-222222"],
    },
    {
        "name": "leading-space-key",
        "text": '---\n claim_anchor_ids: ["c-111111"]\n---\nClaim.\n',
        "ids": ["c-111111", "c-222222"],
        "fail_closed": True,
    },
    {
        "name": "other-field-kept",
        "text": "---\ntitle: T\nevidence: []\n---\nClaim.\n",
        "ids": ["c-111111"],
    },
]


def golden() -> list[dict[str, object]]:
    cases = []
    for case in CASES:
        written = memory_proposals._update_metadata(
            case["text"], {"claim_anchor_ids": sorted(case["ids"])}
        )
        if case.get("fail_closed"):
            cases.append(
                {
                    "name": case["name"],
                    "text": case["text"],
                    "ids": case["ids"],
                    "expected": None,
                    "python": written,
                }
            )
        else:
            cases.append(
                {
                    "name": case["name"],
                    "text": case["text"],
                    "ids": case["ids"],
                    "expected": written,
                }
            )
    return cases


def render() -> dict[Path, str]:
    return {
        GOLDEN: json.dumps(golden(), indent=1, sort_keys=True, ensure_ascii=True) + "\n"
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    stale = []
    for path, content in render().items():
        if args.write:
            path.write_text(content, encoding="utf-8", newline="\n")
        elif not path.exists() or path.read_text(encoding="utf-8") != content:
            stale.append(str(path.relative_to(HUB)))
    if stale:
        print(
            "stale anchor reservation goldens: "
            + ", ".join(stale)
            + " (run with --write)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
