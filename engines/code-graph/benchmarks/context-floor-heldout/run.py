#!/usr/bin/env python3
"""Score `graphtrail context` entry points against held-out issue-title labels.

Usage: run.py --graphtrail BIN --db GRAPH_DB [--labels labels.json] [--limit 8]

The labels come from real Brigade issue titles, judged against a synced index
of the Brigade repository. See README.md for how they were made. Never tune the
relevance floor on this set: calibrate on ../context-ranking/floor-corpus.json
and only report numbers here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def score(binary: str, db: str, labels: dict, limit: int) -> dict:
    returned = hits = relevant = 0
    false_empty = correct_empty = labeled_none = unjudged = 0
    cases = []
    for case in labels["cases"]:
        wanted = {(item["qualified_name"], item["file_path"]) for item in case["relevant"]}
        judged = {(item["qualified_name"], item["file_path"]) for item in case.get("judged", [])} | wanted
        out = subprocess.run(
            [binary, "--db", db, "context", case["title"], "--json", "--limit", str(limit)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        got = [(entry["qualified_name"], entry["file_path"]) for entry in json.loads(out)["entry_points"]]
        case_hits = sum(1 for item in got if item in wanted)
        returned += len(got)
        hits += case_hits
        relevant += len(wanted)
        unjudged += sum(1 for item in got if item not in judged)
        if not wanted:
            labeled_none += 1
            correct_empty += int(not got)
        elif not got:
            false_empty += 1
        cases.append({"issue": case["issue"], "returned": len(got), "hits": case_hits, "relevant": len(wanted)})
    precision = hits / returned if returned else 1.0
    recall = hits / relevant if relevant else 1.0
    f05 = 1.25 * precision * recall / (0.25 * precision + recall) if precision + recall else 0.0
    return {
        "returned": returned,
        "hits": hits,
        "relevant": relevant,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f05": round(f05, 3),
        "titles_with_false_empty": false_empty,
        "none_titles_answered_empty": f"{correct_empty}/{labeled_none}",
        "returned_unjudged": unjudged,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphtrail", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--labels", default=str(Path(__file__).with_name("labels.json")))
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--cases", action="store_true", help="include per-title counts")
    args = parser.parse_args()
    result = score(args.graphtrail, args.db, json.loads(Path(args.labels).read_text()), args.limit)
    if not args.cases:
        result.pop("cases")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
