"""Bounded anchoring proposals and observational replica coverage."""

from pathlib import Path
from typing import Any

from . import memory_claim_blocks as claim_blocks
from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_proposal_v2_ops as delta

MAX_PROPOSALS = 25


def suffix(block: str) -> str | None:
    """The hub's anchor suffix, if this block carries one."""
    return claim_blocks.hub_suffix_id(block)


def _char_offsets(text: str, spans: list[tuple[int, int]]) -> dict[int, int]:
    """Map the UTF-8 byte offsets that bound `spans` to their character index."""
    boundaries = sorted({offset for start, end in spans for offset in (start, end)})
    offsets: dict[int, int] = {}
    wanted = iter(boundaries)
    target = next(wanted, None)
    position = 0
    for index, char in enumerate(text):
        if position == target:
            offsets[position] = index
            target = next(wanted, None)
        position += len(char.encode("utf-8"))
    if position == target:
        offsets[position] = len(text)
    return offsets


def blocks(text: str) -> list[delta.Block]:
    """The hub's block classification, with character offsets.

    `memory_claim_blocks.hub_spans` is the single Python port of the hub's
    `proposal_blocks::spans`; the shared golden pins both sides to the same
    bytes. Eligibility and coverage read these spans, so a card the hub cannot
    address is never proposed and never counted as covered.
    """
    spans = sorted(claim_blocks.hub_spans(text))
    result = []
    if spans:
        data = text.encode("utf-8")
        offsets = _char_offsets(text, spans)
        for start, end in spans:
            value = data[start:end].decode("utf-8")
            result.append(delta.Block(offsets[start], offsets[end], value, suffix(value)))
    ids = [block.block_id for block in result if block.block_id]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate claim block ids")
    return result


def clean_live(head: dict[str, Any]) -> bool:
    return head.get("scan_state") == "clean" and head.get("state") == "live"


def ops(heads: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    proposals = []
    for head in sorted(heads, key=lambda h: (h["scope"], h["card_id"])):
        if not clean_live(head) or head.get("auto_authored") is not True:
            continue
        try:
            card_blocks = blocks(head["text"])
            delta.reservations(head["text"])
        except ValueError:
            continue
        if any(block.block_id is None for block in card_blocks):
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
            card_blocks = blocks(head["text"])
            anchored += all(block.block_id is not None for block in card_blocks)
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
        replica = store.replica(target)
        if not replica.meta:
            return None
        return coverage(list(replica.readable_heads(settings.read_scopes).values()))
    except (OSError, ValueError):
        return None
