"""Retrieval adapters for the memory eval harness."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade.memory_cmd import search_cards_payload

from .corpus import CardDoc

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


def _path_to_card_id(rel_path: str) -> str:
    return Path(rel_path).stem


def current_adapter(target: Path) -> AdapterFn:
    """Existing keyword scorer from ``memory_cmd.search_cards_payload``."""

    def search(query: str, limit: int) -> list[tuple[str, float]]:
        payload = search_cards_payload(target, query, limit=limit)
        ranked: list[tuple[str, float]] = []
        for match in payload["matches"]:
            ranked.append((_path_to_card_id(str(match["path"])), float(match["score"])))
        return ranked

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
    except ImportError:
        return None, "sentence_transformers not installed"

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


def build_adapters(target: Path, cards: Sequence[CardDoc]) -> dict[str, dict[str, Any]]:
    """Return adapter metadata and callables keyed by adapter name."""
    adapters: dict[str, dict[str, Any]] = {
        "current": {
            "available": True,
            "reason": None,
            "search": current_adapter(target),
        },
        "grep": {
            "available": True,
            "reason": None,
            "search": grep_adapter(cards),
        },
    }
    semantic_fn, reason = semantic_adapter(cards)
    adapters["semantic"] = {
        "available": semantic_fn is not None,
        "reason": reason,
        "search": semantic_fn,
    }
    return adapters
