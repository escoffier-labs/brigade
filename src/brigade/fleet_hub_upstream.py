"""Private transport contract for the managed upstream."""

import hmac
import ipaddress
import os
import sqlite3


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
