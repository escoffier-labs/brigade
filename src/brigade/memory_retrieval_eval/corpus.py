"""Load fixture cards and gold-labeled queries for the retrieval eval."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade.memory_cmd import MemoryCareConfig, _card_search_fields, _iter_cards

CATEGORIES = ("exact", "paraphrase", "abbreviation", "cross_tag")


@dataclass(frozen=True)
class CardDoc:
    card_id: str
    path: str
    title: str
    tags: tuple[str, ...]
    summary: str
    body: str

    @property
    def searchable_text(self) -> str:
        return f"{self.title}\n{' '.join(self.tags)}\n{self.summary}\n{self.body}"


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    query: str
    gold: tuple[str, ...]
    category: str


def default_fixture_root() -> Path:
    """Repo-relative fixture root: ``evals/memory-retrieval``."""
    # src/brigade/memory_retrieval_eval/corpus.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3] / "evals" / "memory-retrieval"


def load_cards(target: Path) -> list[CardDoc]:
    """Load cards from a workspace-shaped target (``memory/cards/*.md``)."""
    target = target.expanduser().resolve()
    config = MemoryCareConfig()
    cards: list[CardDoc] = []
    for path in _iter_cards(target, config):
        fields = _card_search_fields(path, target)
        if not fields:
            continue
        card_id = path.stem
        cards.append(
            CardDoc(
                card_id=card_id,
                path=fields["rel"],
                title=str(fields["title"]),
                tags=tuple(str(t) for t in fields["tags"]),
                summary=str(fields["summary"]),
                body=str(fields["body"]),
            )
        )
    cards.sort(key=lambda c: c.card_id)
    return cards


def load_queries(queries_path: Path) -> tuple[int, list[QuerySpec]]:
    """Load gold queries; return (default_k, queries)."""
    payload = json.loads(queries_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("queries file must be a JSON object")
    k = int(payload.get("k", 5))
    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("queries file must contain a non-empty queries list")
    queries: list[QuerySpec] = []
    for item in raw_queries:
        if not isinstance(item, dict):
            raise ValueError("each query must be an object")
        query_id = str(item["id"])
        query = str(item["query"]).strip()
        gold_raw = item.get("gold")
        if not isinstance(gold_raw, list) or not gold_raw or not all(isinstance(g, str) for g in gold_raw):
            raise ValueError(f"query {query_id!r} gold must be a non-empty list of strings")
        category = str(item.get("category") or "exact")
        if category not in CATEGORIES:
            raise ValueError(f"query {query_id!r} has unknown category {category!r}")
        if not query:
            raise ValueError(f"query {query_id!r} has empty query text")
        queries.append(QuerySpec(query_id=query_id, query=query, gold=tuple(gold_raw), category=category))
    return k, queries


def validate_gold(cards: list[CardDoc], queries: list[QuerySpec]) -> list[str]:
    """Return human-readable problems when gold ids are missing from the corpus."""
    known = {c.card_id for c in cards}
    problems: list[str] = []
    for q in queries:
        missing = [g for g in q.gold if g not in known]
        if missing:
            problems.append(f"{q.query_id}: missing gold card ids: {', '.join(missing)}")
    return problems


def corpus_stats(cards: list[CardDoc], queries: list[QuerySpec]) -> dict[str, Any]:
    by_category: dict[str, int] = {c: 0 for c in CATEGORIES}
    for q in queries:
        by_category[q.category] = by_category.get(q.category, 0) + 1
    return {
        "card_count": len(cards),
        "query_count": len(queries),
        "queries_by_category": by_category,
    }
