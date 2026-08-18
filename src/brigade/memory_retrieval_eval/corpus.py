"""Load fixture cards and gold-labeled queries for the retrieval eval."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from brigade.card_identity import IdentityIndex
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
    aliases: tuple[str, ...] = ()

    @property
    def searchable_text(self) -> str:
        return f"{self.title}\n{' '.join(self.tags)}\n{self.summary}\n{self.body}"


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    query: str
    gold: tuple[str, ...]
    category: str


def repo_root() -> Path:
    """Repository root (``evals/`` and ``src/`` live here)."""
    return Path(__file__).resolve().parents[3]


def default_fixture_root() -> Path:
    """Repo-relative fixture root: ``evals/memory-retrieval``."""
    return repo_root() / "evals" / "memory-retrieval"


def fixture_root_label(original: Path | None, resolved: Path) -> str:
    """Stable, non-absolute fixture identifier for eval reports."""
    default_resolved = default_fixture_root().resolve()
    if resolved == default_resolved:
        return "evals/memory-retrieval"
    try:
        return resolved.relative_to(repo_root()).as_posix()
    except ValueError:
        pass
    if original is not None and not original.expanduser().is_absolute():
        return original.expanduser().as_posix()
    return f"external/{resolved.name}"


def load_cards(target: Path) -> list[CardDoc]:
    """Load cards from a workspace-shaped target (``memory/cards/*.md``)."""
    target = target.expanduser().resolve()
    config = MemoryCareConfig()
    cards: list[CardDoc] = []
    for path in _iter_cards(target, config):
        fields = _card_search_fields(path, target)
        if not fields:
            continue
        cards.append(
            CardDoc(
                card_id=str(fields["card_id"]),
                path=fields["rel"],
                title=str(fields["title"]),
                tags=tuple(str(t) for t in fields["tags"]),
                summary=str(fields["summary"]),
                body=str(fields["body"]),
                aliases=tuple(str(alias) for alias in fields["card_aliases"]),
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
        if "id" not in item:
            raise ValueError("each query must include an id")
        if "query" not in item:
            raise ValueError("each query must include a query string")
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


def _gold_index(cards: list[CardDoc]) -> IdentityIndex[CardDoc]:
    index: IdentityIndex[CardDoc] = IdentityIndex()
    for card in cards:
        index.claim(card.card_id, card)
        for alias in card.aliases:
            index.claim(alias, card)
    return index


def validate_gold(cards: list[CardDoc], queries: list[QuerySpec]) -> list[str]:
    """Return human-readable problems when gold ids are missing or colliding."""
    index = _gold_index(cards)
    problems: list[str] = []
    for key in index.colliding_keys():
        problems.append(f"alias collision: {key}")
    for q in queries:
        missing: list[str] = []
        colliding: list[str] = []
        for gold in q.gold:
            if index.is_collision(gold):
                colliding.append(gold)
            elif index.resolve(gold) is None:
                missing.append(gold)
        if colliding:
            problems.append(f"{q.query_id}: colliding gold card ids: {', '.join(colliding)}")
        if missing:
            problems.append(f"{q.query_id}: missing gold card ids: {', '.join(missing)}")
    return problems


def resolve_gold_aliases(cards: list[CardDoc], queries: list[QuerySpec]) -> list[QuerySpec]:
    """Translate legacy fixture keys to their explicit card IDs for scoring."""
    index = _gold_index(cards)
    resolved: list[QuerySpec] = []
    for query in queries:
        gold_ids: list[str] = []
        for gold in query.gold:
            card = index.resolve(gold)
            if card is None:
                gold_ids.append(gold)
            else:
                gold_ids.append(card.card_id)
        resolved.append(replace(query, gold=tuple(gold_ids)))
    return resolved


def corpus_stats(cards: list[CardDoc], queries: list[QuerySpec]) -> dict[str, Any]:
    by_category: dict[str, int] = {c: 0 for c in CATEGORIES}
    for q in queries:
        by_category[q.category] = by_category.get(q.category, 0) + 1
    return {
        "card_count": len(cards),
        "query_count": len(queries),
        "queries_by_category": by_category,
    }
