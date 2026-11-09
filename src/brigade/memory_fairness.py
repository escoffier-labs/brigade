"""Shared per-scope fairness order for scheduled memory passes.

The near-duplicate and link-repair lanes of hub reflection and the
verification pass all wait in the same kind of line: least recently proposed
first, then oldest first seen, then a 128-bit key digest. The key names a
scope and card ids, never card text, so rewriting a card keeps its place.
"""

from __future__ import annotations

from collections.abc import Iterable

from . import memory_proposal_io as io

# A fairness entry is [first_seen_ms, proposed_ms], with 0 for never proposed.
Entry = list[int]


def key_digest(scope: str, card_ids: Iterable[str]) -> str:
    """The 128-bit key of a scope and a set of card ids, as 32 lowercase hex digits."""
    return io.sha256(io.json_bytes([scope, sorted(set(card_ids))]))[:32]


def priority(key: str, entry: Entry) -> tuple[int, int, str]:
    """Sort key: least recently proposed, then oldest first seen, then key digest."""
    return entry[1], entry[0], key
