"""Bounded anchoring proposals and observational replica coverage."""

from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_proposal_v2_ops as delta

MAX_PROPOSALS = 25
# Rust char::is_whitespace follows Unicode White_Space, unlike Python isspace.
SPACE = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def suffix(block: str) -> tuple[int, str] | None:
    """Hub suffix syntax, before trailing spaces or tabs."""
    match = delta.ANCHOR.search(block.rstrip(" \t"))
    return (match.start(), match[1]) if match else None


def blocks(text: str) -> list[delta.Block]:
    """Port of hub proposal_blocks::spans, used only for reflection anchoring.

    Offsets here index Python characters. Rust indexes UTF-8 bytes. The shared
    fixture asserts the exact extracted block bytes in both implementations.
    """
    if "<" in text:
        return []
    result = []
    offset = 0
    front = text.startswith(("---\n", "---\r\n"))
    fence: tuple[str, int] | None = None
    start: int | None = None
    end = 0
    opaque = False
    for line in text.split("\n"):
        # Split only on LF, matching Rust even for Unicode line separators.
        raw = line.removesuffix("\r")
        trimmed = raw.lstrip(SPACE)
        if front:
            if offset > 0 and raw == "---":
                front = False
            offset += len(line) + 1
            continue
        marker = trimmed[:1] if trimmed[:1] in ("`", "~") else ""
        width = len(trimmed) - len(trimmed.lstrip(marker)) if marker else 0
        if fence:
            if marker == fence[0] and width >= fence[1] and not trimmed[width:].strip(SPACE):
                fence = None
            offset += len(line) + 1
            continue
        if not raw or raw.startswith(("- ", "+ ", "* ")) or width >= 3:
            if start is not None and not opaque:
                value = text[start:end]
                found = suffix(value)
                result.append(delta.Block(start, end, value, found[1] if found else None))
            start = None
            opaque = False
        if width >= 3:
            fence = (marker, width)
        elif raw:
            if start is None:
                start = offset
            opaque |= (
                raw[0] in SPACE
                or trimmed.startswith(("#", ">", "<", "[", "previously"))
                or any(c in raw for c in "|`<>")
                or all(c in "-=*_ " for c in raw)
            )
            end = offset + len(raw)
        offset += len(line) + 1
    if start is not None and not opaque:
        value = text[start:end]
        found = suffix(value)
        result.append(delta.Block(start, end, value, found[1] if found else None))
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
