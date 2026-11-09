"""The verify client's copy of the hub's unbound manifest lifetime must follow the hub's own constant."""

from __future__ import annotations

import re
from pathlib import Path

from brigade import memory_verify_packets as pk

HUB_SOURCE = Path(__file__).resolve().parents[1] / "engines/hub/src/memory/manifest_binding.rs"
# The whole declaration line, a product of integer literals, and nothing else.
DECLARATION = re.compile(r"^pub const UNBOUND_MS: i64 = (\d+(?: \* \d+)*);$", re.MULTILINE)
HOUR_MS = 3600 * 1000


def hub_unbound_ms() -> int:
    found = DECLARATION.findall(HUB_SOURCE.read_text(encoding="utf-8"))
    assert len(found) == 1, "could not parse exactly one `pub const UNBOUND_MS` literal from the hub source"
    value = 1
    for factor in found[0].split(" * "):
        value *= int(factor)
    return value


def test_the_manifest_window_is_the_hub_lifetime_less_one_hour():
    hub = hub_unbound_ms()
    assert pk.HUB_UNBOUND_MS == hub
    assert pk.MANIFEST_WINDOW_MS == hub - HOUR_MS
