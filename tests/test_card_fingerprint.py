"""Unit tests for card content fingerprints and near-match helpers (#724)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brigade.card_fingerprint import (
    NEAR_MATCH_THRESHOLD,
    content_fingerprint,
    find_card_match,
    index_cards,
    jaccard_similarity,
    normalize_card_content,
    opposite_polarity,
    reinforce_existing_card,
)

SHARED_FACT = (
    "Always flush the shared widget cache after a schema migration completes "
    "otherwise application reads return stale rows for several minutes"
)


def test_normalize_strips_frontmatter_punctuation_and_case():
    text = "---\ntopic: X\n---\n\n# Hello, World!\n\nFoo   BAR.\n"
    assert normalize_card_content(text) == "hello world foo bar"


def test_content_fingerprint_stable_across_punctuation():
    a = "---\ntopic: t\n---\n\nFlush the cache after migrations.\n"
    b = "---\ntopic: other\n---\n\nflush the cache after migrations!\n"
    assert content_fingerprint(a) == content_fingerprint(b)


def test_jaccard_near_match_threshold_and_polarity():
    near = (
        "Always please flush the shared widget cache after a schema migration completes "
        "otherwise application reads return stale rows for several minutes"
    )
    assert jaccard_similarity(SHARED_FACT, near) >= NEAR_MATCH_THRESHOLD
    assert opposite_polarity(SHARED_FACT, near) is False

    works = (
        "The foo compatibility flag works when bar is set during service startup "
        "and the worker pool is warm enough for traffic"
    )
    fails = (
        "The foo compatibility flag fails when bar is set during service startup "
        "and the worker pool is warm enough for traffic"
    )
    assert jaccard_similarity(works, fails) >= NEAR_MATCH_THRESHOLD
    assert opposite_polarity(works, fails) is True

    unrelated = "Beta workers need object store credentials before startup."
    assert jaccard_similarity(SHARED_FACT, unrelated) < NEAR_MATCH_THRESHOLD


def test_find_card_match_exact_near_and_polarity(tmp_path: Path):
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "one.md").write_text(f"---\ntopic: one\n---\n\n{SHARED_FACT}\n")
    (cards / "flag.md").write_text(
        "---\ntopic: flag\n---\n\n"
        "The foo compatibility flag works when bar is set during service startup "
        "and the worker pool is warm enough for traffic\n"
    )
    indexed = index_cards(cards)

    exact = find_card_match(f"---\ntopic: copy\n---\n\n{SHARED_FACT}\n", indexed)
    assert exact is not None
    assert exact.kind == "exact"
    assert exact.path.name == "one.md"

    near = find_card_match(
        "---\ntopic: copy\n---\n\n"
        "Always please flush the shared widget cache after a schema migration completes "
        "otherwise application reads return stale rows for several minutes\n",
        indexed,
    )
    assert near is not None
    assert near.kind == "near"
    assert near.path.name == "one.md"
    assert near.similarity >= NEAR_MATCH_THRESHOLD
    assert near.opposite_polarity is False

    polarity = find_card_match(
        "---\ntopic: copy\n---\n\n"
        "The foo compatibility flag fails when bar is set during service startup "
        "and the worker pool is warm enough for traffic\n",
        indexed,
    )
    assert polarity is not None
    assert polarity.kind == "near"
    assert polarity.path.name == "flag.md"
    assert polarity.opposite_polarity is True


def test_reinforce_existing_card_updates_frontmatter(tmp_path: Path):
    path = tmp_path / "card.md"
    path.write_text('---\ntopic: t\nlast_reviewed: 2026-01-01\nevidence: ["README.md"]\n---\n\nBody.\n')
    updates = reinforce_existing_card(
        path,
        today=date(2026, 5, 13),
        evidence_pointer="handoffs/example.md",
    )
    text = path.read_text()
    assert updates["reinforcements"] == 1
    assert updates["last_reviewed"] == "2026-05-13"
    assert "last_reviewed: 2026-05-13" in text
    assert "reinforcements: 1" in text
    assert "fingerprint:" in text
    assert "handoffs/example.md" in text
