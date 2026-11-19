"""Plain BM25 ranking for memory card search (#2196).

The index is built in memory on every call and then dropped. There is no cache
and no persistent index: a full build over about 1,900 cards takes a fraction
of a second, which is cheaper than keeping a derived file in sync.

Each card is one field: title, tags, summary, and the card text, joined. The
card text leaves out the ``evidence`` and ``claim_anchor_ids`` front-matter
lines and anchor suffixes, which are stored and shown but never searched
(``ranking_text``).
Query terms are deduplicated. Scores are rounded to ``SCORE_DIGITS`` decimals
so near-identical cards still tie and callers can break ties by path.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from .memory_claim_blocks import hub_spans, hub_suffix_id

K1 = 1.2
B = 0.75
SCORE_DIGITS = 4

_TOKEN_RE = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens; punctuation, hyphens, and slashes split words."""
    return _TOKEN_RE.findall(text.lower())


def _writer_lines(meta: str) -> list[str]:
    """``str::lines`` in Rust: split at ``\\n``, drop a ``\\r`` before it and a final empty line."""
    parts = meta.split("\n")
    lines = [part[:-1] if part.endswith("\r") else part for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _front_matter_body(text: str) -> str | None:
    """What follows the closing fence ``memory_cmd._parse_frontmatter`` reads."""
    if not text.startswith("---\n"):
        return None
    lines = text.splitlines(keepends=True)
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[index + 1 :])
    return None


_ANCHOR_ID = re.compile(r"c-[0-9a-f]{6}\Z")
# The most ids the anchoring transform reserves for one card.
ANCHOR_ID_LIMIT = 4096
_ANCHOR_FIELD = "claim_anchor_ids: "


def _anchor_id_line(line: str) -> bool:
    """A reservation line holding only ids the writer mints, nothing after them."""
    if not line.startswith(_ANCHOR_FIELD):
        return False
    try:
        ids = json.loads(line[len(_ANCHOR_FIELD) :])
    except ValueError:
        return False
    return (
        isinstance(ids, list)
        and len(ids) <= ANCHOR_ID_LIMIT
        and all(isinstance(ident, str) and _ANCHOR_ID.fullmatch(ident) for ident in ids)
    )


def _hidden_line(line: str, keys: Collection[str]) -> bool:
    """Whether search leaves `line` out: a blank line or a field with nothing to read."""
    key = line.partition(":")[0]
    if ":" not in line or key not in keys:
        return False
    return key != "claim_anchor_ids" or _anchor_id_line(line)


def _without_keys(text: str, keys: Collection[str]) -> str:
    """Drop the front-matter lines the hub writer treats as one of ``keys``, and blank lines.

    The lines come from the writer's own parse, so a blank line or a final newline in
    the metadata reads the same before and after the writer rewrites it: the writer
    inserts a field at the closing fence, after any trailing blank line. A
    ``claim_anchor_ids`` line is dropped only while it holds nothing but anchor ids, so
    a comment someone adds to it is still searched and digest-relevant. Nothing is
    dropped when the writer's closing fence and the search parser's differ.
    """
    if not text.startswith("---\n"):
        return text
    meta, sep, body = text[4:].partition("\n---\n")
    if not sep:
        return text
    if _front_matter_body(text) != body:
        return text
    lines = _writer_lines(meta)
    kept = [line for line in lines if line.strip() and not _hidden_line(line, keys)]
    # A front matter left with no lines goes too, so a card that gains its first reserved field reads the same.
    return "---\n" + "\n".join(kept) + "\n---\n" + body if kept else body


def ranking_text(text: str) -> str:
    """The card as search sees it, the mirror of the hub's ``text::ranking_text``.

    Evidence citations and claim-anchor reservations are stored and shown but
    never searched, so anchoring a card cannot move any ranking. The evidence
    line goes first, because a reference holding ``<`` would otherwise make the
    whole card opaque to the anchor span parser.
    """
    text = _without_keys(text, ("evidence", "claim_anchor_ids"))
    data = text.encode("utf-8")
    for start, end in sorted(hub_spans(text), reverse=True):
        block = data[start:end].decode("utf-8")
        if hub_suffix_id(block) is not None:
            trimmed_end = start + len(block.rstrip(" \t").encode("utf-8"))
            # The suffix is exactly " ^c-xxxxxx", ten ASCII bytes.
            data = data[: trimmed_end - 10] + data[trimmed_end:]
    return data.decode("utf-8")


def card_text(fields: dict[str, Any]) -> str:
    """The single BM25 field for one ``_card_search_fields`` record."""
    tags = " ".join(str(tag) for tag in fields.get("tags") or [])
    body = ranking_text(str(fields.get("body") or ""))
    return f"{fields.get('title') or ''}\n{tags}\n{fields.get('summary') or ''}\n{body}"


@dataclass(frozen=True)
class Bm25Index:
    """Postings as parallel document-index and term-frequency lists."""

    doc_count: int
    doc_lengths: tuple[int, ...]
    avg_length: float
    postings: dict[str, tuple[list[int], list[int]]]

    @classmethod
    def build(cls, texts: Sequence[str], vocabulary: Collection[str] | None = None) -> Bm25Index:
        """Index ``texts``; with ``vocabulary``, keep postings for those terms only.

        Document lengths always cover every token, so scores for the kept terms
        are identical to a full build.
        """
        postings: dict[str, tuple[list[int], list[int]]] = {}
        lengths: list[int] = []
        for doc, text in enumerate(texts):
            tokens = tokenize(text)
            lengths.append(len(tokens))
            counts = Counter(tokens)
            items = counts.items() if vocabulary is None else ((t, counts[t]) for t in vocabulary if t in counts)
            for term, freq in items:
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
    # One query per build, so only its terms need postings (about 40% faster).
    raw = Bm25Index.build(texts, vocabulary=set(tokenize(query))).scores(query)
    ranked = [(doc, round(score, SCORE_DIGITS)) for doc, score in raw.items()]
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked
