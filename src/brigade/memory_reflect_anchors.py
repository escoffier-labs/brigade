"""Bounded anchoring proposals and observational replica coverage."""

from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_proposal_v2_ops as delta

MAX_PROPOSALS = 25


def clean_live(head: dict[str, Any]) -> bool:
    return head.get("scan_state") == "clean" and head.get("state") == "live"


def ops(heads: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    proposals = []
    for head in sorted(heads, key=lambda h: (h["scope"], h["card_id"])):
        if not clean_live(head):
            continue
        try:
            blocks = delta.blocks(head["text"])
            delta.reservations(head["text"])
        except ValueError:
            continue
        if any(block.block_id is None for block in blocks):
            proposals.append(
                [
                    {
                        "type": "anchor_blocks",
                        "scope": head["scope"],
                        "card_id": head["card_id"],
                        "base_revision": head["revision"],
                    }
                ]
            )
    return proposals


def coverage(heads: list[dict[str, Any]]) -> dict[str, Any]:
    clean = [head for head in heads if clean_live(head)]
    anchored = 0
    for head in clean:
        try:
            blocks = delta.blocks(head["text"])
            anchored += all(block.block_id is not None for block in blocks)
        except ValueError:
            pass
    return {
        "anchored_cards": anchored,
        "clean_live_cards": len(clean),
        "fraction": anchored / len(clean) if clean else None,
        "source": "replica",
    }


def status(target: Path) -> dict[str, Any] | None:
    """Cached progress is diagnostic only and never grants application rights."""
    try:
        settings = hubc.load_settings(target)
        if not settings.enabled:
            return None
        with store.locked(target):
            replica = store.replica(target)
            if not replica.meta:
                return None
            return coverage(list(replica.readable_heads(settings.read_scopes).values()))
    except (OSError, ValueError):
        return None
