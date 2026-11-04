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


def test_upstream_writes_reuse_checkpointed_wal(tmp_path, monkeypatch):
    import time

    from brigade import fleet_hub_upstream

    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-key")
    # Exercise the same policy with a smaller WAL to keep the regression fast.
    monkeypatch.setattr(fleet_hub_upstream, "PASSIVE_WAL_BYTES", 256 * 1024, raising=False)
    path = tmp_path / "hub.db"
    server = fleet_hub.make_server(str(ipaddress.IPv4Address(2130706433)), 0, path, "fake-admin")
    conn = fleet_hub.open_db(path)
    try:
        conn.execute("CREATE TABLE checkpoint_probe (value BLOB)")
        conn.execute("INSERT INTO checkpoint_probe VALUES (zeroblob(16384))")
        conn.commit()
        for _batch in range(30):
            for _ in range(30):
                conn.execute("UPDATE checkpoint_probe SET value = randomblob(16384)")
                conn.commit()
            time.sleep(0.12)
        assert path.with_suffix(".db-wal").stat().st_size < 2 * 1024 * 1024
    finally:
        conn.close()
        server.server_close()


def test_upstream_checkpoint_thread_stops_on_close(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-key")
    server = fleet_hub.make_server(str(ipaddress.IPv4Address(2130706433)), 0, tmp_path / "hub.db", "fake-admin")
    try:
        maintenance = server._checkpoint_maintenance
        assert maintenance.thread.is_alive()
    finally:
        server.server_close()
    assert not maintenance.thread.is_alive()
    server.server_close()


def test_upstream_checkpoint_idle_and_busy_retry(tmp_path, monkeypatch, caplog):
    import time

    from brigade import fleet_hub_upstream

    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-key")
    monkeypatch.setattr(fleet_hub_upstream, "TRUNCATE_WAL_BYTES", 1)
    path = tmp_path / "hub.db"
    server = fleet_hub.make_server(str(ipaddress.IPv4Address(2130706433)), 0, path, "fake-admin")
    reader = fleet_hub.open_db(path)
    writer = fleet_hub.open_db(path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM nodes").fetchall()
        writer.execute("CREATE TABLE checkpoint_busy (value)")
        writer.commit()
        deadline = time.monotonic() + 5
        while "checkpoint busy" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "checkpoint busy" in caplog.text
        reader.rollback()
        wal = path.with_suffix(".db-wal")
        while wal.stat().st_size and time.monotonic() < deadline:
            time.sleep(0.05)
        assert wal.stat().st_size == 0
        # Below the size thresholds, the idle timer must still checkpoint.
        monkeypatch.setattr(fleet_hub_upstream, "TRUNCATE_WAL_BYTES", 256 * 1024 * 1024)
        writer.execute("INSERT INTO checkpoint_busy VALUES (1)")
        writer.commit()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            # A separate observer can inspect checkpoint progress in the WAL index
            # without issuing a checkpoint itself.
            import struct

            shm = path.with_suffix(".db-shm").read_bytes()
            frames = struct.unpack_from("=I", shm, 16)[0]
            backfilled = struct.unpack_from("=I", shm, 96)[0]
            if frames and backfilled == frames:
                break
            time.sleep(0.05)
        assert frames and backfilled == frames
    finally:
        reader.close()
        writer.close()
        server.server_close()


@pytest.mark.parametrize(
    "upstream,owner,expected", [(True, "rust", False), (True, "python", True), (False, "rust", False)]
)
def test_checkpoint_owner(tmp_path, monkeypatch, upstream, owner, expected):
    if upstream:
        monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-key")
    else:
        monkeypatch.delenv("BRIGADE_HUB_UPSTREAM_KEY", raising=False)
    monkeypatch.delenv("BRIGADE_HUB_MANAGED_LOOPBACK", raising=False)
    monkeypatch.setenv("BRIGADE_HUB_CHECKPOINT_OWNER", owner)
    server = fleet_hub.make_server(str(ipaddress.IPv4Address(2130706433)), 0, tmp_path / "hub.db", "fake-admin")
    try:
        maintenance = server._checkpoint_maintenance
        assert (maintenance is not None) == expected
        if maintenance:
            assert maintenance.thread.is_alive()
    finally:
        server.server_close()
