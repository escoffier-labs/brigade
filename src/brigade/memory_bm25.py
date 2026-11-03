"""Plain BM25 ranking for memory card search (#2196).

The index is built in memory on every call and then dropped. There is no cache
and no persistent index: a full build over about 1,900 cards takes a fraction
of a second, which is cheaper than keeping a derived file in sync.

Each card is one field: title, tags, summary, and the full card text, joined.
Query terms are deduplicated. Scores are rounded to ``SCORE_DIGITS`` decimals
so near-identical cards still tie and callers can break ties by path.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

K1 = 1.2
B = 0.75
SCORE_DIGITS = 4

_TOKEN_RE = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens; punctuation, hyphens, and slashes split words."""
    return _TOKEN_RE.findall(text.lower())


def card_text(fields: dict[str, Any]) -> str:
    """The single BM25 field for one ``_card_search_fields`` record."""
    tags = " ".join(str(tag) for tag in fields.get("tags") or [])
    return f"{fields.get('title') or ''}\n{tags}\n{fields.get('summary') or ''}\n{fields.get('body') or ''}"


@dataclass(frozen=True)
class Bm25Index:
    """Postings as parallel document-index and term-frequency lists."""

    doc_count: int
    doc_lengths: tuple[int, ...]
    avg_length: float
    postings: dict[str, tuple[list[int], list[int]]]

    @classmethod
    def build(cls, texts: Sequence[str]) -> Bm25Index:
        postings: dict[str, tuple[list[int], list[int]]] = {}
        lengths: list[int] = []
        for doc, text in enumerate(texts):
            tokens = tokenize(text)
            lengths.append(len(tokens))
            for term, freq in Counter(tokens).items():
                entry = postings.get(term)
                if entry is None:
                    entry = postings[term] = ([], [])
                entry[0].append(doc)
                entry[1].append(freq)
        total = sum(lengths)
        avg = total / len(lengths) if lengths else 0.0
        return cls(doc_count=len(lengths), doc_lengths=tuple(lengths), avg_length=avg, postings=postings)

    def scores(self, query: str) -> dict[int, float]:
        """Raw BM25 score per matching document index (unrounded, all > 0)."""
        n = self.doc_count
        avg = self.avg_length or 1.0
        out: dict[int, float] = {}
        for term in dict.fromkeys(tokenize(query)):
            entry = self.postings.get(term)
            if entry is None:
                continue
            docs, freqs = entry
            idf = math.log(1.0 + (n - len(docs) + 0.5) / (len(docs) + 0.5))
            for doc, freq in zip(docs, freqs, strict=True):
                norm = K1 * (1.0 - B + B * self.doc_lengths[doc] / avg)
                out[doc] = out.get(doc, 0.0) + idf * freq * (K1 + 1.0) / (freq + norm)
        return out


def rank(texts: Sequence[str], query: str) -> list[tuple[int, float]]:
    """Return ``(doc_index, rounded_score)`` for every matching document.

    Order is by score, descending; callers add their own deterministic
    tie-break. A query with no word characters (for example ``:``) keeps the
    old literal substring match so list-all callers still work; those matches
    score ``0.0``.
    """
    if not tokenize(query):
        needles = [term for term in query.lower().split() if term]
        if not needles:
            return []
        return [(doc, 0.0) for doc, text in enumerate(texts) if any(needle in text.lower() for needle in needles)]
    raw = Bm25Index.build(texts).scores(query)
    ranked = [(doc, round(score, SCORE_DIGITS)) for doc, score in raw.items()]
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked
