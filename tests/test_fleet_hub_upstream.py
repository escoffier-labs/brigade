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
    assert handler.client_address == (address, peer[1])


def test_address_consumers_use_keyed_address(tmp_path, monkeypatch):
    from brigade import fleet_hub_messages

    host = str(ipaddress.IPv4Address(2130706433))
    address = str(ipaddress.IPv4Address(3221225985))
    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-key")
    server = fleet_hub.make_server(
        host, 0, tmp_path / "hub.db", "fake-admin", allow_admin_writes=True, trust_tailscale_identity=True
    )
    observed = {}
    original = server.RequestHandlerClass

    class Handler(original):
        def _route(self, method, *, refuse_token_query):
            observed["logging"] = self.address_string()
            observed["loopback"] = self._tailscale_identity_authorized()
            observed["secure_transport"] = self._secure_transport()
            observed["source_key"] = self.server._active_requests[self.request]
            return super()._route(method, refuse_token_query=refuse_token_query)

    server.RequestHandlerClass = Handler

    def messages(*args, **kwargs):
        observed["source_ip"] = kwargs["source_ip"]
        observed["waiter_source_cap"] = fleet_hub_messages._source_cap_applies(kwargs["source_ip"])
        return 200, {}

    monkeypatch.setattr(fleet_hub_messages, "handle", messages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(host, server.server_port, timeout=2)
        conn.request(
            "POST",
            "/messages",
            b"{}",
            {
                "Authorization": "Bearer fake-admin",
                "Content-Type": "application/json",
                "X-Brigade-Upstream-Key": "fake-key",
                "X-Brigade-Client-Address": address,
                "Tailscale-User-Login": "viewer@example.invalid",
                "X-Forwarded-Proto": "https",
            },
        )
        assert conn.getresponse().status == 200
        conn.close()
        assert observed == {
            "logging": address,
            "loopback": False,
            "secure_transport": False,
            "source_key": address,
            "source_ip": address,
            "waiter_source_cap": True,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_standalone_ignores_internal_address_header(monkeypatch):
    from email.message import Message
    from types import SimpleNamespace
    from brigade.fleet_hub_upstream import authorize

    monkeypatch.delenv("BRIGADE_HUB_UPSTREAM_KEY", raising=False)
    peer = (str(ipaddress.IPv4Address(2130706433)), 1234)
    headers = Message()
    headers["X-Brigade-Client-Address"] = str(ipaddress.IPv4Address(3221225985))
    handler = SimpleNamespace(headers=headers, client_address=peer)
    assert authorize(handler)
    assert handler.client_address == peer
