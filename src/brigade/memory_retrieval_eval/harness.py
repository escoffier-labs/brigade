"""Orchestrate adapters over the fixture corpus and emit a report payload."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapters import build_adapters
from .corpus import (
    corpus_stats,
    default_fixture_root,
    fixture_root_label,
    load_cards,
    load_queries,
    validate_gold,
)
from .metrics import aggregate_scores, ceiling_for_queries, score_query

DEFAULT_FIXTURE_ROOT = default_fixture_root()
DEFAULT_K = 5
KNOWN_ADAPTERS = frozenset({"current", "grep", "semantic"})


def run_eval(
    *,
    fixture_root: Path | None = None,
    k: int | None = None,
    adapters: list[str] | None = None,
) -> dict[str, Any]:
    """Run the retrieval eval and return a JSON-serializable report.

    Adapters ``current`` and ``grep`` always run offline. ``semantic`` is
    included when available; otherwise it is reported as skipped.
    """
    root = (fixture_root or DEFAULT_FIXTURE_ROOT).expanduser().resolve()
    queries_path = root / "queries.json"
    if not queries_path.is_file():
        raise FileNotFoundError(f"queries file not found: {queries_path}")

    cards = load_cards(root)
    default_k, queries = load_queries(queries_path)
    k_eff = int(k if k is not None else default_k)
    if k_eff <= 0:
        raise ValueError("k must be positive")

    problems = validate_gold(cards, queries)
    if problems:
        raise ValueError("fixture gold validation failed:\n- " + "\n- ".join(problems))

    wanted = adapters or ["current", "grep", "semantic"]
    unknown = [name for name in wanted if name not in KNOWN_ADAPTERS]
    if unknown:
        raise ValueError(f"unknown adapters: {', '.join(unknown)}")

    built = build_adapters(root, cards, wanted=wanted)

    per_adapter: dict[str, Any] = {}
    for name in wanted:
        meta = built[name]
        if not meta["available"] or meta["search"] is None:
            per_adapter[name] = {
                "available": False,
                "skipped": True,
                "reason": meta["reason"] or "unavailable",
            }
            continue

        search = meta["search"]
        corpus_limit = len(cards)
        per_query: list[dict[str, Any]] = []
        for query in queries:
            ranked_pairs = search(query.query, corpus_limit)
            ranked_ids = [card_id for card_id, _score in ranked_pairs]
            metrics = score_query(ranked_ids, query.gold, k_eff)
            per_query.append(
                {
                    "id": query.query_id,
                    "query": query.query,
                    "category": query.category,
                    "gold": list(query.gold),
                    **metrics,
                }
            )

        by_category: dict[str, Any] = {}
        for category in sorted({q.category for q in queries}):
            subset = [row for row in per_query if row["category"] == category]
            by_category[category] = aggregate_scores(subset)

        per_adapter[name] = {
            "available": True,
            "skipped": False,
            "reason": None,
            "overall": aggregate_scores(per_query),
            "by_category": by_category,
            "queries": per_query,
        }

    return {
        "kind": "memory-retrieval-eval",
        "issue": 722,
        "fixture_root": fixture_root_label(fixture_root, root),
        "k": k_eff,
        "corpus": corpus_stats(cards, queries),
        "ceiling": ceiling_for_queries(queries, k_eff),
        "adapters": per_adapter,
    }
