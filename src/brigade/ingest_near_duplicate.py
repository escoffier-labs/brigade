"""Reflect-compatible ingest matches and same-run review consolidation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import memory_reflect_near as near
from .card_fingerprint import (
    CardMatch,
    IndexedCard,
    _parse_frontmatter_lines,
    opposite_polarity,
    read_text_nofollow,
    write_text_nofollow_atomic,
)
from .memory_hub_sync import publication_identity
from .memory_proposal_v2_ops import claim_body


def similarity(left: str, right: str) -> float:
    """Use reflect's claim extraction and normalized word shingles."""
    a, b = set(near.shingles(claim_body(left))), set(near.shingles(claim_body(right)))
    return len(a & b) / len(a | b) if a and b else 0.0


def scope(text: str) -> str:
    """An absent scope belongs to the local workspace, never another workspace."""
    meta, _ = _parse_frontmatter_lines(text.lstrip())
    return str(meta.get("scope", ""))


def find_match(content: str, indexed: list[IndexedCard], target: Path) -> CardMatch | None:
    threshold, _ = near.settings(target)
    best: CardMatch | None = None
    for card in indexed:
        meta, _ = _parse_frontmatter_lines(card.text)
        if scope(card.text) != scope(content) or meta.get("superseded_by") or meta.get("status") == "superseded":
            continue
        score = similarity(content, card.text)
        if score + 1e-9 < threshold:
            continue
        if best is None or score > best.similarity:
            best = CardMatch(card.path, "near", score, opposite_polarity(content, card.text))
    return best


def card_id(path: Path, target: Path) -> str:
    return publication_identity(read_text_nofollow(path), path.relative_to(target).as_posix())[0]


@dataclass
class Pending:
    content: str
    path: Path
    matched_card: Path
    code: str


def merge_pending(
    content: str,
    pending: list[Pending],
    target: Path,
    handoff_name: str,
    snapshot: bytes,
    matched_card: Path,
    code: str,
) -> tuple[Path, bytes] | None:
    """Retain one review item and add a pointer to each archived source handoff.

    The list belongs to one ingest call. Existing inbox files from other runs
    are never searched or changed. The original sources remain in processed/.
    Return the path and previous UTF-8 bytes so a failed reinforcement can roll back.
    """
    import re

    from .memory_inbox_drain import classify

    incoming_review = classify(code, snapshot.decode("utf-8"))
    if incoming_review == "human":
        return None
    threshold, _ = near.settings(target)
    for item in pending:
        if (
            item.code != code
            or item.matched_card != matched_card
            or scope(content) != scope(item.content)
            or similarity(content, item.content) + 1e-9 < threshold
            # Two proposals can each look neutral beside the card yet contradict each other.
            or opposite_polarity(content, item.content)
        ):
            continue
        text = read_text_nofollow(item.path)
        # Never hide a stricter review disposition behind a source pointer.
        if incoming_review != classify(item.code, text):
            continue
        digest = hashlib.sha256(snapshot).hexdigest()
        name = re.sub(r"[^A-Za-z0-9._-]", "_", handoff_name)[:160]
        pointer = f"\n<!-- merged handoff: {name} sha256={digest} -->\n"
        if pointer not in text:
            write_review_text(target, item.path, text + pointer, previous=text.encode("utf-8"))
        return item.path, text.encode("utf-8")
    return None


def write_review_text(target: Path, path: Path, text: str, *, previous: bytes | None = None) -> None:
    """Retain existing authorization when consolidating or restoring known review bytes."""
    from . import handoff_origin

    if previous is None:
        previous = read_text_nofollow(path).encode("utf-8")
    review = handoff_origin.verify_review_draft(target, path, previous)
    write_text_nofollow_atomic(path, text)
    if review is not None:
        try:
            handoff_origin.stamp_review_draft(
                target,
                path,
                text.encode("utf-8"),
                handoff_origin.ReviewDraft(review.code, review.promote_cards, review.route_documents),
            )
        except OSError:
            # The draft remains for human review if signing fails.
            handoff_origin.remove_stamp(handoff_origin.stamp_path(target, path))
