"""One read of a native config file in the apply path, and an apply-time check of the plan.

``sync`` and the harness profile layer used to read a config file several times while
applying: once to decode the live entries and project, again to render the new text, and
a third time for the ``expected_before`` digest. An edit between those reads (for example
a ``headersHelper`` added by hand) was rendered over by the first projection while the
last read made the drift check pass, so the helper vanished without a signal. Now the raw
bytes are read ONCE, every decode and render comes from that exact snapshot, and the same
bytes are the ``expected_before``: a later edit is drift and nothing is written for the file.

The plan is also built from an earlier read, so the live entry can move between plan and
apply. ``projection_moved`` detects that by comparing the apply-time projection with the
planned fingerprint, and ``refresh_fidelity`` restates the item so the report describes
what is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import mcp_fidelity, mcp_native_auth
from .mcp_adapters import CanonicalServer

LIVE_CHANGED = "live_entry_changed"


def decode(data: bytes) -> str:
    """Text as ``Path.read_text`` would return it (universal newlines), from the given bytes."""
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def read_snapshot(path: Path) -> tuple[bytes | None, str | None]:
    """Raw bytes and the text decoded from those same bytes; ``(None, None)`` for an absent file."""
    data = path.read_bytes() if path.is_file() else None
    return data, None if data is None else decode(data)


def projection_moved(harness: str, projected: dict[str, Any], item: dict[str, Any]) -> bool:
    """True when the apply-time projection differs from the one the plan fingerprinted."""
    return mcp_native_auth.entry_fingerprint(harness, projected) != item.get("_proj_fp")


def refresh_fidelity(
    item: dict[str, Any],
    harness: str,
    server: CanonicalServer,
    projected: dict[str, Any],
    live: dict[str, Any],
    native_record: dict[str, Any],
    *,
    scope: str,
    location: str | None,
) -> None:
    """Recompute the item's fidelity rows and fingerprint from the apply-time state."""
    item["_proj_fp"] = mcp_native_auth.entry_fingerprint(harness, projected)
    keys = native_record.get("native_auth_keys", native_record.get("opencode_native_keys", {}))
    item["_fidelity"] = mcp_fidelity.settle_precedence(
        mcp_fidelity.evaluate(
            server, harness, scope=scope, location=location, projected=projected, live=live, native_keys=keys
        ),
        item,
    )


def block_moved(item: dict[str, Any], harness: str, server: CanonicalServer, scope: str) -> None:
    """A current item whose projection moved since the plan: nothing is written for it."""
    item.update(
        status="conflicted",
        action="conflict",
        reason=LIVE_CHANGED,
        detail=f"{server.name}: the live entry changed while the plan was built; nothing was written for this "
        "server; run the sync again",
    )
    item["_fidelity"] = mcp_fidelity.evaluate(server, harness, scope=scope, error_code=LIVE_CHANGED)
