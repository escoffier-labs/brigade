"""Private transport contract for the managed upstream."""

import hmac
import ipaddress
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path


def key() -> str:
    return os.environ.get("BRIGADE_HUB_UPSTREAM_KEY", "")


def configure_connection(conn: sqlite3.Connection) -> None:
    if key():
        conn.execute("PRAGMA wal_autocheckpoint=0")
        if conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] != 0:
            raise RuntimeError("upstream wal_autocheckpoint is not zero")


def authorize(handler: object) -> bool:
    """Check the key before method dispatch, including unsupported methods."""
    expected = key()
    if not expected:
        return True
    headers = handler.headers  # type: ignore[attr-defined]
    supplied = headers.get_all("X-Brigade-Upstream-Key", [])
    if len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode(), expected.encode()):
        handler.send_error(403, "upstream key required")  # type: ignore[attr-defined]
        return False
    addresses = headers.get_all("X-Brigade-Client-Address", [])
    try:
        address = str(ipaddress.ip_address(addresses[0])) if len(addresses) == 1 else None
    except ValueError:
        address = None
    if address is None:
        handler.send_error(400, "upstream client address required")  # type: ignore[attr-defined]
        return False
    handler.upstream_client_address = address  # type: ignore[attr-defined]
    handler.client_address = (address, handler.client_address[1])  # type: ignore[attr-defined]
    server = getattr(handler, "server", None)
    if server is not None:
        server.rekey_upstream_request(handler.request, address)  # type: ignore[attr-defined]
    return True


PASSIVE_WAL_BYTES = 64 * 1024 * 1024
TRUNCATE_WAL_BYTES = 256 * 1024 * 1024
_LOG = logging.getLogger(__name__)


class CheckpointMaintenance:
    """Checkpoint owner for keyed upstreams without managed Rust maintenance."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path.expanduser().resolve()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="hub-checkpoints", daemon=True)
        # Count of finished checkpoint attempts. Tests wait on it instead of
        # sleeping, so they do not depend on how fast the host schedules this thread.
        self._attempts = 0
        self._attempted = threading.Condition()

    def close(self) -> None:
        self.stop.set()
        self.thread.join()

    @property
    def attempts(self) -> int:
        with self._attempted:
            return self._attempts

    def wait_for_attempts(self, count: int, timeout: float) -> bool:
        """Block until ``count`` checkpoint attempts have finished; False on timeout."""
        with self._attempted:
            return self._attempted.wait_for(lambda: self._attempts >= count, timeout)

    def _finish_attempt(self) -> None:
        with self._attempted:
            self._attempts += 1
            self._attempted.notify_all()

    def _run(self) -> None:
        # Open and close on the maintenance thread, never on a writer connection.
        conn = sqlite3.connect(f"{self.path.as_uri()}?mode=rw", uri=True, timeout=0.25)
        try:
            configure_connection(conn)
            conn.execute("PRAGMA busy_timeout=250")
            version = conn.execute("PRAGMA data_version").fetchone()[0]
            last_write = time.monotonic()
            pending = True
            while not self.stop.wait(0.1):
                try:
                    current = conn.execute("PRAGMA data_version").fetchone()[0]
                    if current != version:
                        version = current
                        last_write = time.monotonic()
                        pending = True
                    wal = Path(f"{self.path}-wal")
                    size = wal.stat().st_size if wal.exists() else 0
                    if size > TRUNCATE_WAL_BYTES:
                        mode = "TRUNCATE"
                    elif size > PASSIVE_WAL_BYTES or pending and time.monotonic() - last_write >= 1:
                        mode = "PASSIVE"
                    else:
                        continue
                    busy, pages, done = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
                    self._finish_attempt()
                    pending = bool(busy or done < pages)
                    if pending:
                        _LOG.warning("upstream %s checkpoint busy (%s/%s pages); retrying later", mode, done, pages)
                except (sqlite3.Error, OSError):
                    _LOG.exception("upstream checkpoint failed; retrying later")
        finally:
            conn.close()
