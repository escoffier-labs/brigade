"""Retrieval adapters for the memory eval harness."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade import memory_bm25
from brigade.memory_cmd import search_cards_payload

from .corpus import CardDoc
from .hub import HubAdapter, HubOptions
from brigade.memory_hub_client import MemoryHubClient

_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)

AdapterFn = Callable[[str, int], list[tuple[str, float]]]


@dataclass(frozen=True)
class AdapterResult:
    name: str
    available: bool
    reason: str | None
    ranked: list[tuple[str, float]] | None = None


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text) if t]


def current_adapter(target: Path) -> AdapterFn:
    """Production search: ``memory_cmd.search_cards_payload`` (BM25 since #2196)."""

    def search(query: str, limit: int) -> list[tuple[str, float]]:
        payload = search_cards_payload(target, query, limit=limit)
        ranked: list[tuple[str, float]] = []
        for match in payload["matches"]:
            ranked.append((str(match.get("card_id") or match["path"]), float(match["score"])))
        return ranked

    return search


def bm25_adapter(cards: Sequence[CardDoc]) -> AdapterFn:
    """``memory_bm25`` over the loaded cards, with the index built once per eval run."""
    texts = [card.searchable_text for card in cards]
    index = memory_bm25.Bm25Index.build(texts)

    def search(query: str, limit: int) -> list[tuple[str, float]]:
        # A query with no word characters takes production's literal fallback.
        raw = index.scores(query) if memory_bm25.tokenize(query) else dict(memory_bm25.rank(texts, query))
        # Ties break by card path, as in ``search_cards_payload``; ids come after sorting.
        scored = [(round(score, memory_bm25.SCORE_DIGITS), cards[doc]) for doc, score in raw.items()]
        scored.sort(key=lambda item: (-item[0], item[1].path))
        return [(card.card_id, score) for score, card in scored[:limit]]

    return search


def substring_adapter(cards: Sequence[CardDoc]) -> AdapterFn:
    """The pre-#2196 production scorer, kept only as a named eval baseline.

    Each whitespace query term scores 3 when it is a substring of the title,
    tags, or summary, else 1 when it is a substring of the card text.
    """

    def search(query: str, limit: int) -> list[tuple[str, float]]:
        terms = [term for term in query.lower().split() if term]
        scored: list[tuple[float, CardDoc]] = []
        for card in cards:
            heading = f"{card.title} {' '.join(card.tags)} {card.summary}".lower()
            body = card.body.lower()
            score = sum(3 if term in heading else 1 if term in body else 0 for term in terms)
            if score > 0:
                scored.append((float(score), card))
        # Ties break by card path, as the pre-#2196 production scorer did.
        scored.sort(key=lambda item: (-item[0], item[1].path))
        return [(card.card_id, score) for score, card in scored[:limit]]

    return search


def grep_adapter(cards: Sequence[CardDoc]) -> AdapterFn:
    """Naive tokenized substring matching: count of query tokens found anywhere.

    Deliberately dumb floor: no title boost, no IDF, no stemming. Any upgrade
    that cannot beat this on paraphrase-heavy queries is not worth a dependency.
    """

    def search(query: str, limit: int) -> list[tuple[str, float]]:
        terms = tokenize(query)
        scored: list[tuple[str, float]] = []
        for card in cards:
            hay = card.searchable_text.lower()
            if not terms:
                continue
            score = float(sum(1 for term in terms if term in hay))
            if score > 0:
                scored.append((card.card_id, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]

    return search


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def semantic_adapter(cards: Sequence[CardDoc]) -> tuple[AdapterFn | None, str | None]:
    """Optional on-device embeddings + cosine.

    Wired only when ``sentence_transformers`` is importable and a local model
    can be loaded with ``local_files_only=True`` (no network download). Model
    id/path comes from ``BRIGADE_MEMORY_EVAL_EMBED_MODEL`` (default
    ``sentence-transformers/all-MiniLM-L6-v2``).
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        return None, f"sentence_transformers unavailable ({type(exc).__name__}: {exc})"

    model_name = os.environ.get("BRIGADE_MEMORY_EVAL_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    try:
        model = SentenceTransformer(model_name, local_files_only=True)
    except Exception as exc:  # noqa: BLE001 - any load failure means skip
        return None, f"local embedding model unavailable ({type(exc).__name__}: {exc})"

    texts = [card.searchable_text for card in cards]
    try:
        matrix = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    except Exception as exc:  # noqa: BLE001
        return None, f"embedding encode failed ({type(exc).__name__}: {exc})"

    card_ids = [card.card_id for card in cards]
    vectors: list[list[float]] = [[float(x) for x in row] for row in matrix]

    def search(query: str, limit: int) -> list[tuple[str, float]]:
        try:
            q_vec = [float(x) for x in model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]]
        except Exception:
            return []
        scored = [(card_ids[i], _cosine(q_vec, vectors[i])) for i in range(len(card_ids))]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]

    return search, None


def build_adapters(
    target: Path,
    cards: Sequence[CardDoc],
    *,
    wanted: Sequence[str],
    hub: MemoryHubClient | None = None,
    hub_options: HubOptions | None = None,
) -> dict[str, dict[str, Any]]:
    """Return adapter metadata and callables keyed by adapter name."""
    adapters: dict[str, dict[str, Any]] = {}
    if "current" in wanted:
        adapters["current"] = {
            "available": True,
            "reason": None,
            "search": current_adapter(target),
        }
    if "bm25" in wanted:
        adapters["bm25"] = {"available": True, "reason": None, "search": bm25_adapter(cards)}
    if "substring" in wanted:
        adapters["substring"] = {"available": True, "reason": None, "search": substring_adapter(cards)}
    if "grep" in wanted:
        adapters["grep"] = {
            "available": True,
            "reason": None,
            "search": grep_adapter(cards),
        }
    if "semantic" in wanted:
        semantic_fn, reason = semantic_adapter(cards)
        adapters["semantic"] = {
            "available": semantic_fn is not None,
            "reason": reason,
            "search": semantic_fn,
        }
    for name, mode in (("hub-lexical", "lexical"), ("hub-hybrid", "hybrid")):
        if name in wanted:
            available = hub is not None
            if hub and mode == "hybrid":
                status = hub.call("status").get("retrieval", {})
                available = status.get("effective") == "hybrid" and status.get("state") == "ok"
            search = HubAdapter(hub, mode, hub_options or HubOptions(), cards) if hub and available else None
            adapters[name] = {
                "available": search is not None,
                "reason": None if search else "hub binary or requested embedder unavailable",
                "search": search,
            }
    return adapters
