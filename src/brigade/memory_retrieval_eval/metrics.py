"""Precision/recall and rank metrics for the retrieval eval."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .corpus import QuerySpec


def precision_at_k(ranked_ids: Sequence[str], gold: Sequence[str], k: int) -> float:
    if k <= 0:
        return 0.0
    top = list(ranked_ids[:k])
    if not top:
        return 0.0
    gold_set = set(gold)
    hits = sum(1 for item in top if item in gold_set)
    return hits / float(k)


def recall_at_k(ranked_ids: Sequence[str], gold: Sequence[str], k: int) -> float:
    gold_set = set(gold)
    if not gold_set:
        return 0.0
    top = set(ranked_ids[:k])
    return len(top & gold_set) / float(len(gold_set))


def hit_at_k(ranked_ids: Sequence[str], gold: Sequence[str], k: int) -> float:
    gold_set = set(gold)
    return 1.0 if gold_set & set(ranked_ids[:k]) else 0.0


def first_gold_rank(ranked_ids: Sequence[str], gold: Sequence[str]) -> int | None:
    gold_set = set(gold)
    for index, card_id in enumerate(ranked_ids, start=1):
        if card_id in gold_set:
            return index
    return None


def score_query(ranked_ids: Sequence[str], gold: Sequence[str], k: int) -> dict[str, Any]:
    return {
        "precision_at_k": precision_at_k(ranked_ids, gold, k),
        "recall_at_k": recall_at_k(ranked_ids, gold, k),
        "hit_at_k": hit_at_k(ranked_ids, gold, k),
        "first_gold_rank": first_gold_rank(ranked_ids, gold),
        "ranked": list(ranked_ids[:k]),
    }


def mean(values: Iterable[float]) -> float:
    items = list(values)
    if not items:
        return 0.0
    return sum(items) / float(len(items))


def aggregate_scores(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-query metric dicts into means + first-gold stats."""
    if not rows:
        return {
            "query_count": 0,
            "precision_at_k": 0.0,
            "recall_at_k": 0.0,
            "hit_rate": 0.0,
            "mean_first_gold_rank": None,
            "median_first_gold_rank": None,
            "miss_count": 0,
        }
    ranks = [row["first_gold_rank"] for row in rows if row["first_gold_rank"] is not None]
    miss_count = sum(1 for row in rows if row["first_gold_rank"] is None)
    median_rank: float | None
    if ranks:
        ordered = sorted(ranks)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            median_rank = float(ordered[mid])
        else:
            median_rank = (ordered[mid - 1] + ordered[mid]) / 2.0
    else:
        median_rank = None
    return {
        "query_count": len(rows),
        "precision_at_k": mean(row["precision_at_k"] for row in rows),
        "recall_at_k": mean(row["recall_at_k"] for row in rows),
        "hit_rate": mean(row["hit_at_k"] for row in rows),
        "mean_first_gold_rank": mean(float(r) for r in ranks) if ranks else None,
        "median_first_gold_rank": median_rank,
        "miss_count": miss_count,
    }


def ceiling_for_queries(queries: Sequence[QuerySpec], k: int) -> dict[str, Any]:
    """Oracle ceiling: always ranks every gold id first (when gold subset of corpus)."""
    rows: list[dict[str, Any]] = []
    for query in queries:
        ranked = list(query.gold)[:k]
        # Pad is unnecessary for metrics; gold-first is enough for the ceiling.
        rows.append(score_query(ranked, query.gold, k))
    overall = aggregate_scores(rows)
    by_category: dict[str, Any] = {}
    categories = sorted({q.category for q in queries})
    for category in categories:
        subset = [row for row, q in zip(rows, queries, strict=True) if q.category == category]
        by_category[category] = aggregate_scores(subset)
    return {"overall": overall, "by_category": by_category}
