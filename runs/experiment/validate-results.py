#!/usr/bin/env python3
"""Check experiment score totals against the recorded token accounting."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ROUTES = ("A", "B", "C")


def main() -> None:
    for json_path in ROOT.rglob("*.json"):
        json.loads(json_path.read_text())

    scores = json.loads((ROOT / "scored-results.json").read_text())
    accounting = json.loads((ROOT / "token-accounting.json").read_text())

    assert scores["base_commit"] == "0941c3e66274b76c36a8277c106cf85ffd8a90c9"
    assert set(scores["cases"]) == set(accounting["cases"]) == {f"{number:02d}" for number in range(1, 9)}

    totals = {route: {"matches": 0, "contradictions": 0, "minority": 0} for route in ROUTES}
    for case_id, case_scores in scores["cases"].items():
        for route in ROUTES:
            score = case_scores[route]
            usage = accounting["cases"][case_id][route]
            assert usage["output_tokens"] <= scores["output_token_target_per_case_route"]
            assert usage["within_4000_token_ceiling"] is True
            if score["status"] == "planning_failure":
                assert usage["status"] == "failed"
                assert usage["model_calls"] == 0
                assert usage["output_tokens"] == 0
            else:
                assert usage["status"] == "ok"
                assert usage["model_calls"] == 3

            totals[route]["matches"] += int(score["matches_known_good"])
            if score["contradiction_caught"] is not None:
                totals[route]["contradictions"] += int(score["contradiction_caught"])
            totals[route]["minority"] += int(bool(score["useful_minority_finding"]))

    expected = {
        "A": {"matches": 8, "contradictions": 2, "minority": 7},
        "B": {"matches": 5, "contradictions": 1, "minority": 7},
        "C": {"matches": 0, "contradictions": 0, "minority": 0},
    }
    assert totals == expected
    print(json.dumps(totals, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
