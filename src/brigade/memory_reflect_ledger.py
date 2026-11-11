"""Revision currency and compatibility retention for reflection submissions."""

from typing import Any

from . import memory_hub_client as hubc

RETENTION_MS = 30 * 24 * 60 * 60 * 1000


def pins(ops: list[dict[str, Any]]) -> list[list[Any]]:
    result = set()
    for op in ops:
        for card in (op, op.get("other_card")):
            if card is not None:
                result.add((card["scope"], card["card_id"], card["base_revision"]))
    return [list(pin) for pin in sorted(result)]


def valid_pins(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(pin, list)
            and len(pin) == 3
            and isinstance(pin[0], str)
            and isinstance(pin[1], str)
            and type(pin[2]) is int
            and pin[2] >= 0
            for pin in value
        )
    )


def current(entry: dict[str, Any], heads: dict[tuple[str, str], int], now: int) -> bool:
    if "pins" in entry:
        return all(heads.get((scope, card_id)) == revision for scope, card_id, revision in entry["pins"])
    timestamp = hubc.event_id_ms(entry["client_event_id"])
    return timestamp is not None and now - timestamp < RETENTION_MS
