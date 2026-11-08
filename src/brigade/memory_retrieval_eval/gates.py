"""R0 parity and the public half of R1. No private qualification is inferred."""

from __future__ import annotations

import re
from typing import Any


def evaluate_gate(report: dict[str, Any], gate: str) -> dict[str, Any]:
    baseline = report["adapters"].get("bm25", {})
    candidate = report["adapters"].get("hub-lexical" if gate == "R0" else "hub-hybrid", {})
    failures = []
    if not baseline.get("available") or not candidate.get("available"):
        failures.append("required adapter unavailable")
    elif gate == "R0":
        if candidate["binding"]["ranking"] != "raw":
            failures.append("R0 requires raw lexical ranking")
        if "queries" in baseline and "queries" in candidate:
            parity = [r["ranked_candidates"] for r in baseline["queries"]] == [
                r["ranked_candidates"] for r in candidate["queries"]
            ]
        else:
            digest = baseline.get("ranking_sha256")
            parity = (
                isinstance(digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
                and digest == candidate.get("ranking_sha256")
            )
        if not parity:
            failures.append("lexical order differs")
    else:
        if report["k"] != 5:
            failures.append("R1 requires k=5")
        if candidate["binding"]["ranking"] != "default":
            failures.append("R1 requires default ranking")
        slices = [(baseline["overall"], candidate["overall"])]
        slices += [(baseline["by_category"][key], value) for key, value in candidate["by_category"].items()]
        for before, after in slices:
            if any(after[key] < before[key] for key in ("hit_rate", "mrr", "precision_at_k", "recall_at_k")):
                failures.append("hybrid regresses overall or a category")
                break
        if not any(
            candidate["by_category"].get(category, {}).get(metric, 0)
            > baseline["by_category"].get(category, {}).get(metric, 0)
            for category in ("paraphrase", "abbreviation")
            for metric in ("hit_rate", "mrr", "precision_at_k", "recall_at_k")
        ):
            failures.append("no strict improvement in paraphrase or abbreviation")
    return {
        "name": gate if gate == "R0" else "R1-public",
        "passed": not failures,
        "failures": failures,
        "private_qualification": "not_evaluated",
    }
