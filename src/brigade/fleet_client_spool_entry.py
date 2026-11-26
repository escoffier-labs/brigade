"""Event spool paths and manual flushing.

Resolve client bindings at call time to preserve fleet_client overrides.
"""

from __future__ import annotations

from pathlib import Path

FLUSH_TIMEOUT_SECONDS = 5.0


def spool_path(node_id: str) -> Path:
    from . import fleet_client as client
    from .fleet_home_guard import fleet_home

    safe_node_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in node_id) or "unknown"
    return fleet_home() / client.SPOOL_DIRNAME / f"{safe_node_id}.jsonl"


def flush_spool(
    node_id: str,
    *,
    hub_url: str | None = None,
    token: str | None = None,
    max_batches: int | None = None,
    timeout: float = FLUSH_TIMEOUT_SECONDS,
) -> int:
    """Re-POST spooled events in order, ``FLUSH_BATCH_SIZE`` per request.

    Delivered events are removed from the spool via an atomic rewrite. A
    rejected batch is bisected so only the rejected events move to the
    bounded dead letter (``fleet_client_flush``); undelivered ones keep their
    order so the next flush resumes where this one stopped. A flush that makes no progress leaves
    the file untouched. Returns the number of delivered events. Never raises
    for network failures.
    """
    from . import fleet_client as client

    config = client.load_fleet_config()
    hub = hub_url or config["hub_url"]
    tok = token or config["token"]
    path = client.spool_path(node_id)
    if not hub or not path.is_file():
        return 0
    with client._spool_lock(path) as dir_fd:
        return client._flush_locked(path, hub, tok, max_batches=max_batches, timeout=timeout, dir_fd=dir_fd)
