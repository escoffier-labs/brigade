"""Unit tests for card content fingerprints and near-match helpers (#724)."""

from __future__ import annotations

import os
import stat
from datetime import date
from pathlib import Path

import pytest

from brigade.card_fingerprint import (
    NEAR_MATCH_THRESHOLD,
    content_fingerprint,
    find_card_match,
    index_cards,
    jaccard_similarity,
    normalize_card_content,
    opposite_polarity,
    read_text_nofollow,
    reinforce_existing_card,
    write_text_nofollow_atomic,
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


def test_write_text_nofollow_atomic_does_not_chmod_temp_pathname(tmp_path: Path, monkeypatch):
    path = tmp_path / "card.md"
    path.write_text("---\ntopic: t\n---\n\nOriginal body.\n", encoding="utf-8")
    os.chmod(path, 0o640)

    chmod_paths: list[Path] = []
    real_chmod = os.chmod

    def tracking_chmod(candidate: os.PathLike[str] | str, mode: int) -> None:
        chmod_paths.append(Path(candidate))
        real_chmod(candidate, mode)

    monkeypatch.setattr(os, "chmod", tracking_chmod)

    write_text_nofollow_atomic(path, "---\ntopic: t\n---\n\nUpdated body.\n")

    assert not chmod_paths


def test_write_text_nofollow_atomic_preserves_existing_file_mode(tmp_path: Path):
    path = tmp_path / "card.md"
    original = "---\ntopic: t\n---\n\nOriginal body.\n"
    path.write_text(original, encoding="utf-8")
    desired_mode = 0o644
    os.chmod(path, desired_mode)

    updated = "---\ntopic: t\n---\n\nUpdated body.\n"
    write_text_nofollow_atomic(path, updated)

    assert read_text_nofollow(path) == updated
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == desired_mode


def test_read_text_nofollow_reads_to_eof_with_partial_reads(tmp_path: Path):
    path = tmp_path / "card.md"
    expected = "---\ntopic: t\n---\n\nCafé résumé — full UTF-8 body.\n"
    path.write_text(expected, encoding="utf-8")
    payload = expected.encode("utf-8")
    remaining = bytearray(payload)
    calls: list[int] = []

    def partial_read(fd: int, size: int) -> bytes:
        calls.append(size)
        if not remaining:
            return b""
        chunk = bytes(remaining[:1])
        del remaining[:1]
        return chunk

    assert read_text_nofollow(path, read_fn=partial_read) == expected
    assert len(calls) > 1


def test_index_cards_ignores_stale_stored_fingerprint(tmp_path: Path):
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    original_body = SHARED_FACT
    edited_body = (
        "Always flush the shared widget cache after a schema migration completes "
        "otherwise application reads return stale rows for several minutes and "
        "operators should verify the drain"
    )
    stale_fp = content_fingerprint(f"---\ntopic: one\n---\n\n{original_body}\n")
    (cards / "edited.md").write_text(f"---\ntopic: one\nfingerprint: {stale_fp}\n---\n\n{edited_body}\n")

    indexed = index_cards(cards)

    stale_match = find_card_match(f"---\ntopic: copy\n---\n\n{original_body}\n", indexed)
    assert stale_match is None

    current_match = find_card_match(f"---\ntopic: copy\n---\n\n{edited_body}\n", indexed)
    assert current_match is not None
    assert current_match.kind == "exact"
    assert current_match.path.name == "edited.md"


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


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_index_cards_skips_symlinked_card_files(tmp_path: Path):
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    victim = tmp_path / "outside" / "victim.md"
    victim.parent.mkdir(parents=True)
    victim.write_text(f"---\ntopic: victim\n---\n\n{SHARED_FACT}\n", encoding="utf-8")
    (cards / "real.md").write_text("---\ntopic: real\n---\n\nOther body.\n", encoding="utf-8")
    (cards / "link.md").symlink_to(victim)

    indexed = index_cards(cards)
    paths = {card.path.name for card in indexed}
    assert "real.md" in paths
    assert "link.md" not in paths
    assert find_card_match(f"---\ntopic: copy\n---\n\n{SHARED_FACT}\n", indexed) is None


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_reinforce_refuses_symlink_destination(tmp_path: Path):
    victim = tmp_path / "outside" / "victim.md"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"VICTIM_SECRET_BYTES\n")
    link = tmp_path / "card.md"
    link.symlink_to(victim)

    with pytest.raises(OSError, match="refusing symlinked path"):
        reinforce_existing_card(
            link,
            today=date(2026, 5, 13),
            evidence_pointer="handoffs/example.md",
        )
    assert victim.read_bytes() == b"VICTIM_SECRET_BYTES\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_read_text_nofollow_swap_time_refusal_without_o_nofollow(tmp_path: Path, monkeypatch):
    """Post-open inode identity check must catch a swap when O_NOFOLLOW is absent."""
    path = tmp_path / "card.md"
    path.write_text("---\ntopic: t\n---\n\nBody.\n", encoding="utf-8")
    victim = tmp_path / "outside" / "victim.md"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"VICTIM_SECRET_BYTES\n")

    real_lstat = os.lstat
    calls = {"n": 0}

    def tracking_lstat(candidate: os.PathLike[str] | str):
        calls["n"] += 1
        if calls["n"] == 2 and Path(candidate) == path:
            path.unlink()
            path.symlink_to(victim)
        return real_lstat(candidate)

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(os, "lstat", tracking_lstat)

    with pytest.raises(OSError, match="swapped|symlink"):
        read_text_nofollow(path)
    assert victim.read_bytes() == b"VICTIM_SECRET_BYTES\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_reinforce_swap_time_refusal(tmp_path: Path):
    path = tmp_path / "card.md"
    path.write_text("---\ntopic: t\n---\n\nBody.\n", encoding="utf-8")
    victim = tmp_path / "outside" / "victim.md"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"VICTIM_SECRET_BYTES\n")
    calls = {"n": 0}

    def probe(candidate: Path) -> int | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return os.lstat(candidate).st_mode
        if candidate == path and path.exists() and not path.is_symlink():
            path.unlink()
            path.symlink_to(victim)
        return os.lstat(candidate).st_mode

    with pytest.raises(OSError, match="symlink"):
        reinforce_existing_card(
            path,
            today=date(2026, 5, 13),
            evidence_pointer="handoffs/example.md",
            lstat_probe=probe,
        )
    assert victim.read_bytes() == b"VICTIM_SECRET_BYTES\n"
