"""Orchestrate adapters over the fixture corpus and emit a report payload."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .adapters import build_adapters, current_adapter, grep_adapter
from .corpus import (
    CardDoc,
    corpus_stats,
    default_fixture_root,
    fixture_root_label,
    load_cards,
    load_queries,
    validate_gold,
)
from .metrics import aggregate_scores, ceiling_for_queries, score_query
from .projection import build_projection_section, default_projection_root

DEFAULT_FIXTURE_ROOT = default_fixture_root()
DEFAULT_K = 5
KNOWN_ADAPTERS = frozenset({"current", "grep", "semantic"})


def _load_projection_cards(projection_root: Path) -> list[CardDoc]:
    """Load eval-only projection cards for trusted-path adapter checks."""
    cards_dir = projection_root / "cards"
    if not cards_dir.is_dir():
        return []
    temp_root = projection_root / ".eval-projection-cards"
    target_cards = temp_root / "memory" / "cards"
    if target_cards.exists():
        shutil.rmtree(temp_root)
    target_cards.mkdir(parents=True)
    for path in sorted(cards_dir.glob("*.md")):
        shutil.copy2(path, target_cards / path.name)
    try:
        return load_cards(temp_root)
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)


def _combined_cards(fixture_root: Path, projection_root: Path) -> list[CardDoc]:
    cards = load_cards(fixture_root)
    projection_cards = _load_projection_cards(projection_root)
    known = {card.card_id for card in cards}
    for card in projection_cards:
        if card.card_id not in known:
            cards.append(card)
    cards.sort(key=lambda c: c.card_id)
    return cards


def _combined_fixture_target(fixture_root: Path, projection_root: Path) -> Path:
    """Materialize fixture + projection cards for filesystem-backed adapters."""
    temp_root = projection_root / ".eval-combined-fixture"
    cards_dir = temp_root / "memory" / "cards"
    if temp_root.exists():
        shutil.rmtree(temp_root)
    cards_dir.mkdir(parents=True)
    for src_root in (fixture_root / "memory" / "cards", projection_root / "cards"):
        if not src_root.is_dir():
            continue
        for path in sorted(src_root.glob("*.md")):
            shutil.copy2(path, cards_dir / path.name)
    shutil.copy2(fixture_root / "queries.json", temp_root / "queries.json")
    return temp_root


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

    projection_root = root / "projection"
    if not projection_root.is_dir():
        projection_root = default_projection_root()
    combined_cards = _combined_cards(root, projection_root)
    combined_target = _combined_fixture_target(root, projection_root)
    query_texts = [query.query for query in queries]
    projection_adapters: dict[str, Any] = {}
    for name in wanted:
        meta = built[name]
        if not meta["available"] or meta["search"] is None:
            projection_adapters[name] = {
                "available": False,
                "skipped": True,
                "reason": meta["reason"] or "unavailable",
            }
            continue
        if name == "current":
            projection_search = current_adapter(combined_target)
        elif name == "grep":
            projection_search = grep_adapter(combined_cards)
        else:
            projection_search = meta["search"]
        projection_adapters[name] = build_projection_section(
            projection_root=projection_root,
            cards=combined_cards,
            query_texts=query_texts,
            search=projection_search,
            adapter=name,
        )
    if combined_target.exists():
        shutil.rmtree(combined_target)

    return {
        "kind": "memory-retrieval-eval",
        "issue": 722,
        "fixture_root": fixture_root_label(fixture_root, root),
        "k": k_eff,
        "corpus": corpus_stats(cards, queries),
        "ceiling": ceiling_for_queries(queries, k_eff),
        "adapters": per_adapter,
        "projection": {
            "schema": "memory-projection-eval.v1",
            "issue": 845,
            "adapters": projection_adapters,
        },
    }
