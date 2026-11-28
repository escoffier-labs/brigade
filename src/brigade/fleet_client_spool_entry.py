"""Event spool paths and manual flushing.

Resolve client bindings at call time to preserve fleet_client overrides.
"""

from __future__ import annotations

import errno
import os
import stat
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
    from .fleet_client_flush import stuck_path

    config = client.load_fleet_config()
    hub = hub_url or config["hub_url"]
    tok = token or config["token"]
    path = client.spool_path(node_id)

    # A stale marker still needs locked cleanup, but absent state must not
    # create a spool directory or lock file. Re-read state after acquiring it.
    if not hub or (not _has_regular_spool(path) and not stuck_path(path).is_file()):
        return 0
    with client._spool_lock(path) as dir_fd:
        return client._flush_locked(path, hub, tok, max_batches=max_batches, timeout=timeout, dir_fd=dir_fd)


def _has_regular_spool(path: Path) -> bool:
    from . import fleet_client as client

    # Path.is_file() suppresses lookup errors on Python 3.14. Open safely
    # before taking the lock so absent spools leave no directory or lock file.
    try:
        fd = os.open(str(path), os.O_RDONLY | client._O_NOFOLLOW | client._O_CLOEXEC | client._O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return False
    except OSError as exc:
        # A link, socket, or Windows directory can fail before fstat.
        # Skip only when the lookup establishes that there is no regular file.
        if exc.errno in (errno.ELOOP, errno.ENXIO) or (os.name == "nt" and isinstance(exc, PermissionError)):
            try:
                if not stat.S_ISREG(path.stat().st_mode):
                    return False
            except (FileNotFoundError, NotADirectoryError):
                return False
        raise
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
