"""Deterministic near-duplicate candidates for scheduled hub reflection.

Cards whose claim bodies share most word 5-gram shingles become reviewed
`supersede_card` proposals. The hub classifies supersession as Tier 2, so
these proposals always wait for an explicit reviewer decision. Nothing here
reads beyond the heads the caller already bounded per scope, and no report
carries card text.
"""

from __future__ import annotations

import hashlib
import math
import re
import tomllib
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import memory_proposal_io as io
from . import memory_proposal_v2_ops as delta

CONFIG = ".brigade/memory-care.toml"
DEFAULT_THRESHOLD = 0.9
MIN_THRESHOLD = 0.5
MAX_THRESHOLD = 1.0
SHINGLE_WORDS = 5
# A shingle indexed by more cards than this is template text, not evidence of
# duplication. Dropping it keeps pair generation near-linear in the corpus.
POSTING_CAP = 64
# Distinct candidate pairs verified per run, across every scope.
MAX_PAIRS = 200_000
# Hub submission bounds: 1 to 64 ops over at most eight cards. Each op names
# one superseded card plus the keeper, so a chunk holds at most seven others.
MAX_OPS = 64
MAX_CARDS = 8
KNOWN_TRUST = {"agent-unreviewed", "reviewed", "imported", "policy-promoted"}
WORD = re.compile(r"\w+")


class ThresholdError(ValueError):
    """The [reflect] near-duplicate threshold is missing a valid value."""


def threshold(target: Path) -> float:
    """Read `[reflect] near_duplicate_threshold` from memory-care.toml."""
    raw = io.read_bytes(target / CONFIG, absent=True)
    if raw is None:
        return DEFAULT_THRESHOLD
    try:
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ThresholdError("invalid memory-care config") from None
    table = data.get("reflect", {})
    if not isinstance(table, dict):
        raise ThresholdError("[reflect] must be a table")
    value = table.get("near_duplicate_threshold", DEFAULT_THRESHOLD)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not MIN_THRESHOLD <= value <= MAX_THRESHOLD:
        raise ThresholdError(
            f"[reflect] near_duplicate_threshold must be a number from {MIN_THRESHOLD} to {MAX_THRESHOLD}"
        )
    return float(value)


@dataclass
class Budget:
    """Run-wide work counters, shared by every scope's pass."""

    pairs: int = field(default_factory=lambda: MAX_PAIRS)
    stats: dict[str, int] = field(
        default_factory=lambda: {"candidate_pairs": 0, "pair_checks": 0, "clusters": 0, "common_shingles": 0}
    )


def shingles(body: str) -> array:
    """Sorted, unique 64-bit hashes of normalized word 5-grams.

    A body shorter than five words is one shingle. Hashes are stable across
    processes, so candidates never depend on interpreter hash seeds.
    """
    words = WORD.findall(body.casefold())
    width = min(SHINGLE_WORDS, len(words))
    values = set()
    for start in range(len(words) - width + 1 if words else 0):
        digest = hashlib.blake2b(" ".join(words[start : start + width]).encode(), digest_size=8).digest()
        values.add(int.from_bytes(digest, "big"))
    return array("Q", sorted(values))


def _required(size: int, limit: float) -> int:
    # Tolerate binary rounding so 0.9 * 10 still requires 9 shared shingles.
    return max(1, math.ceil(limit * size - 1e-9))


def _field(text: str, key: str) -> str | None:
    """Mirror the hub's scalar metadata read. Raise where the hub would refuse."""
    if not text.startswith("---\n"):
        return None
    meta, separator, _ = text[4:].partition("\n---\n")
    if not separator:
        raise ValueError("unterminated card metadata")
    lines = meta.split("\n")
    found = None
    for index, line in enumerate(lines):
        name, colon, value = line.partition(":")
        if not colon or name != key:
            continue
        following = next((s for s in lines[index + 1 :] if s.strip()), "")
        if (
            found is not None
            or value.lstrip().startswith(("|", ">"))
            or following[:1].isspace()
            or following.startswith("- ")
        ):
            raise ValueError("ambiguous or multiline metadata field")
        found = value.strip()
    return found


def _superseded(text: str) -> bool:
    try:
        status = _field(text, "status")
        return _field(text, "superseded_by") is not None or (status or "").strip("\"'") == "superseded"
    except ValueError:
        return False  # The op stage reports this card's metadata as invalid.


def _pairs(sets: list[array], limit: float, budget: Budget, skipped: dict[str, int]) -> list[tuple[int, int]]:
    """Prefix-filtered candidate pairs that reach the Jaccard threshold.

    Every card indexes only its first n - ceil(t*n) + 1 shingles in hash order.
    Two sets with Jaccard >= t always share a token in those prefixes, so the
    filter loses no pair except through the posting-list cap.
    """
    index: dict[int, list[int]] = {}
    for position, values in enumerate(sets):
        for token in values[: len(values) - _required(len(values), limit) + 1]:
            index.setdefault(token, []).append(position)
    seen: set[tuple[int, int]] = set()
    edges: list[tuple[int, int]] = []
    for token in sorted(index):
        posting = index[token]
        if len(posting) > POSTING_CAP:
            budget.stats["common_shingles"] += 1
            continue
        for offset, left in enumerate(posting):
            for right in posting[offset + 1 :]:
                budget.stats["pair_checks"] += 1
                pair = (left, right)
                if pair in seen:
                    continue
                small, large = sorted((len(sets[left]), len(sets[right])))
                if small < _required(large, limit):
                    continue
                if budget.pairs <= 0:
                    skipped["near_pair_limit"] = skipped.get("near_pair_limit", 0) + 1
                    return edges
                budget.pairs -= 1
                budget.stats["candidate_pairs"] += 1
                seen.add(pair)
                shared = len(set(sets[left]).intersection(sets[right]))
                if shared >= _required(len(sets[left]) + len(sets[right]) - shared, limit):
                    edges.append(pair)
    return edges


def _pin(head: dict[str, Any]) -> delta.Block | None:
    try:
        return next((block for block in delta.blocks(head["text"]) if block.block_id), None)
    except ValueError:
        return None


def ops(
    heads: list[dict[str, Any]], *, limit: float, budget: Budget
) -> tuple[list[list[dict[str, Any]]], dict[str, int]]:
    """Return supersede submissions for one scope's heads, and counted skips.

    Exact duplicate groups belong to the exact pass and are left out here.
    Clusters are stars: the keeper is the most recently revised card (highest
    hub revision, which is the log sequence), ties to the lowest card id, and
    each member is a direct near-duplicate of that keeper.
    """
    bodies: dict[str, list[dict[str, Any]]] = {}
    for head in sorted(heads, key=lambda h: h["card_id"]):
        body = delta.claim_body(head["text"])
        if body.strip() and not _superseded(head["text"]):
            bodies.setdefault(body, []).append(head)
    cards, sets = [], []
    for body, group in bodies.items():
        values = shingles(body)
        if len(group) == 1 and values:
            cards.append(group[0])
            sets.append(values)
    skipped: dict[str, int] = {}
    neighbours: dict[int, set[int]] = {}
    for left, right in _pairs(sets, limit, budget, skipped):
        neighbours.setdefault(left, set()).add(right)
        neighbours.setdefault(right, set()).add(left)
    order = sorted(neighbours, key=lambda i: (-cards[i]["revision"], cards[i]["card_id"]))
    assigned: set[int] = set()
    submissions: list[list[dict[str, Any]]] = []
    for position in order:
        if position in assigned:
            continue
        members = sorted(neighbours[position] - assigned, key=lambda i: cards[i]["card_id"])
        if not members:
            continue
        assigned.update([position, *members])
        budget.stats["clusters"] += 1
        keeper, others = cards[position], [cards[i] for i in members]
        group = [keeper, *others]
        trusts = {card.get("trust") for card in group}
        reason = None
        if any(card.get("scan_state") != "clean" for card in group):
            reason = "near_non_clean"
        elif len(trusts) != 1:
            reason = "near_mixed_trust"
        elif not trusts <= KNOWN_TRUST:
            reason = "near_unknown_trust"
        elif _pin(keeper) is None:
            reason = "near_missing_pin"
        if reason:
            skipped[reason] = skipped.get(reason, 0) + len(others)
            continue
        cluster_ops = []
        for other in others:
            try:
                block = _pin(other)
                if block is None:
                    skipped["near_missing_pin"] = skipped.get("near_missing_pin", 0) + 1
                    continue
                for key in ("status", "superseded_by"):
                    _field(other["text"], key)
            except ValueError:
                skipped["near_invalid_metadata"] = skipped.get("near_invalid_metadata", 0) + 1
                continue
            cluster_ops.append(
                {
                    "type": "supersede_card",
                    "scope": other["scope"],
                    "card_id": other["card_id"],
                    "base_revision": other["revision"],
                    "block_id": block.block_id,
                    "expected_block_hash": block.sha256,
                    "other_card": {
                        "scope": keeper["scope"],
                        "card_id": keeper["card_id"],
                        "base_revision": keeper["revision"],
                    },
                }
            )
        size = min(MAX_OPS, MAX_CARDS - 1)
        submissions.extend(cluster_ops[start : start + size] for start in range(0, len(cluster_ops), size))
    return submissions, skipped
