"""Upstream mode refuses unkeyed requests before routing or reading a body."""

import http.client
import ipaddress
import threading

import pytest

from brigade import fleet_hub


@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "OPTIONS", "DELETE"])
def test_key_required_before_routing(tmp_path, monkeypatch, method):
    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-start-key")
    host = str(ipaddress.IPv4Address(2130706433))
    server = fleet_hub.make_server(host, 0, tmp_path / "hub.db", "fake-admin")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(host, server.server_port, timeout=2)
        conn.request(method, "/health", headers={"Content-Length": "1000000"})
        assert conn.getresponse().status == 403
        conn.close()
        conn = http.client.HTTPConnection(host, server.server_port, timeout=2)
        conn.request(
            "GET",
            "/health",
            headers={
                "X-Brigade-Upstream-Key": "fake-start-key",
                "X-Brigade-Client-Address": str(ipaddress.IPv4Address(3221225985)),
            },
        )
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_all_connection_factories_disable_autocheckpoint(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-start-key")
    for factory in (fleet_hub.init_db, fleet_hub.open_db):
        conn = factory(path)
        try:
            assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
            assert not conn.in_transaction
        finally:
            conn.close()
    monkeypatch.delenv("BRIGADE_HUB_UPSTREAM_KEY")
    conn = fleet_hub.open_db(path)
    try:
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
    finally:
        conn.close()


def test_managed_switch_requires_matching_key(tmp_path, monkeypatch):
    host = str(ipaddress.IPv4Address(2130706433))
    monkeypatch.setenv("BRIGADE_HUB_MANAGED_LOOPBACK", "fake-switch")
    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-key")
    with pytest.raises(ValueError, match="match the per-start key"):
        fleet_hub.make_server(host, 0, tmp_path / "hub.db", "fake-admin")


def test_internal_address_is_available_for_audit(monkeypatch):
    from types import SimpleNamespace
    from email.message import Message

    from brigade.fleet_hub_upstream import authorize

    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-key")
    headers = Message()
    headers["X-Brigade-Upstream-Key"] = "fake-key"
    address = str(ipaddress.IPv4Address(3221225985))
    headers["X-Brigade-Client-Address"] = address
    peer = (str(ipaddress.IPv4Address(2130706433)), 1234)
    handler = SimpleNamespace(headers=headers, client_address=peer)
    assert authorize(handler)
    assert handler.upstream_client_address == address
    assert handler.client_address == peer
