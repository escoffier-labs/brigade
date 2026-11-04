"""Connection admission is enforced before an unauthenticated handler starts."""

import http.client
import json
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from socketserver import BaseRequestHandler

import pytest

from brigade import fleet_hub


def test_requests_leave_wal_cleanup_to_server_close(tmp_path):
    """An idle hub must keep request closes off SQLite's final-checkpoint path."""
    db = tmp_path / "hub.db"
    wal = Path(f"{db}-wal")
    server = fleet_hub.make_server("127.0.0.1", 0, db, "fixture-admin-token")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        for number in range(3):
            client = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                started = monotonic()
                client.request(
                    "POST",
                    "/nodes",
                    body=json.dumps({"action": "add", "node_id": f"fixture-node-{number}"}),
                    headers={"Authorization": "Bearer fixture-admin-token", "Content-Type": "application/json"},
                )
                response = client.getresponse()
                payload = json.loads(response.read())
                print(f"SQLite {sqlite3.sqlite_version}: enrollment {number} took {monotonic() - started:.3f}s")
                assert response.status == 200, payload
            finally:
                client.close()
            assert wal.exists(), "request close checkpointed and deleted the WAL before sending its response"

            # An idle keepalive must hold neither a read snapshot nor a write lock.
            conn = fleet_hub.open_db(db)
            try:
                assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
                assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
                conn.execute("BEGIN IMMEDIATE")
                assert conn.execute("SELECT count(*) FROM nodes").fetchone()[0] == number + 1
                conn.rollback()
                assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            finally:
                conn.close()
            assert wal.exists(), "a helper connection must not become the final connection either"
    finally:
        server.shutdown()
        # Callers may construct and close the server on different threads.
        with ThreadPoolExecutor(max_workers=1) as closer:
            closer.submit(server.server_close).result(timeout=5)
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not wal.exists(), "server_close must release the final connection and clean up the WAL"
    server.server_close()  # Closing twice must remain harmless.
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM nodes").fetchone()[0] == 3
    finally:
        conn.close()


def _read_ready(peer):
    received = b""
    while len(received) < 5:
        chunk = peer.recv(5 - len(received))
        if not chunk:
            break
        received += chunk
    return received


@pytest.fixture
def connections(tmp_path, monkeypatch):
    threads = {}
    peers = []
    errors = []
    accepted = {}

    class WaitingHandler(BaseRequestHandler):
        def handle(self):
            threads[self.request] = threading.current_thread()
            accepted[self.client_address] = self.request
            self.request.sendall(b"ready")
            if self.request.recv(1) == b"!":
                raise RuntimeError("fixture handler failed")

    monkeypatch.setattr(fleet_hub, "make_handler", lambda *args, **kwargs: WaitingHandler)
    server = fleet_hub.make_server("127.0.0.1", 0, tmp_path / "hub.db", "fixture-admin-token")
    monkeypatch.setattr(server, "handle_error", lambda request, address: errors.append(address))

    class Connections:
        def open(self, source="source-a", port=1):
            request, peer = socket.socketpair()
            peer.settimeout(5)
            peers.append(peer)
            try:
                server.process_request(request, (source, port))
            except BaseException:
                request.close()
                raise
            return request, peer, _read_ready(peer)

        def open_tcp(self):
            peer = socket.create_connection(server.server_address, timeout=5)
            peers.append(peer)
            server.handle_request()
            admitted = _read_ready(peer)
            request = accepted[peer.getsockname()] if admitted else None
            return request, peer, admitted

        def finish(self, connection, message=b"."):
            request, peer, admitted = connection
            assert admitted == b"ready"
            peer.sendall(message)
            assert peer.recv(1) == b""
            threads[request].join(timeout=5)
            assert not threads[request].is_alive()

        def complete_current(self, request):
            peer = peers[-1]
            self.finish((request, peer, _read_ready(peer)))

    harness = Connections()
    harness.server = server
    harness.errors = errors
    yield harness
    for peer in peers:
        peer.close()
    for thread in list(threads.values()):
        thread.join(timeout=5)
        assert not thread.is_alive()
    server.server_close()


@pytest.mark.parametrize("source", ["source-a", "192.0.2.10", "2001:db8::10"])
def test_one_source_cannot_fill_all_handler_slots(connections, source):
    for port in range(8):
        assert connections.open(source, port)[2] == b"ready"
    assert connections.open(source, 99)[2] == b""
    assert connections.open("source-b")[2] == b"ready"


@pytest.mark.parametrize("front", ["127.0.0.1", "127.0.0.2", "::1"])
def test_loopback_front_is_admitted_up_to_the_global_cap(connections, front):
    # Behind Tailscale Serve every client arrives from loopback (#2229).
    held = [connections.open(front, port) for port in range(32)]
    assert all(connection[2] == b"ready" for connection in held)
    assert connections.open(front, 99)[2] == b"", "the global cap still applies to the front"
    assert connections.open("192.0.2.10")[2] == b""
    connections.finish(held[0])
    assert connections.open(front, 100)[2] == b"ready"


def test_non_loopback_source_is_capped_while_the_front_holds_slots(connections):
    front = [connections.open("127.0.0.1", port) for port in range(16)]
    assert all(connection[2] == b"ready" for connection in front)
    remote = [connections.open("192.0.2.10", port) for port in range(8)]
    assert all(connection[2] == b"ready" for connection in remote)
    assert connections.open("192.0.2.10", 99)[2] == b""
    assert connections.open("127.0.0.1", 99)[2] == b"ready"


def test_loopback_peer_is_an_ordinary_source_without_a_loopback_bind(connections):
    connections.server._loopback_bind = False
    for port in range(8):
        assert connections.open("127.0.0.1", port)[2] == b"ready"
    assert connections.open("127.0.0.1", 99)[2] == b""


@pytest.mark.parametrize("host,loopback", [("127.0.0.1", True), ("0.0.0.0", False)])
def test_bound_address_decides_the_front_exemption(tmp_path, host, loopback):
    from brigade.fleet_hub_server import BoundedThreadingHTTPServer

    db = tmp_path / "hub.db"
    sqlite3.connect(db).close()
    # Bind without listen(): the wildcard socket never accepts a connection.
    server = BoundedThreadingHTTPServer((host, 0), BaseRequestHandler, bind_and_activate=False, db_path=db)
    try:
        assert server._loopback_bind is False, "nothing is exempt before the bind"
        server.server_bind()
        assert server._loopback_bind is loopback
    finally:
        server.server_close()


def test_tcp_accept_path_admits_the_loopback_front_and_recovers(connections):
    held = [connections.open_tcp() for _ in range(32)]
    assert all(connection[2] == b"ready" for connection in held)
    assert connections.open_tcp()[2] == b""
    connections.finish(held[0])
    assert connections.open_tcp()[2] == b"ready"


def test_global_handler_cap_and_recovery(connections):
    held = [connections.open(f"source-{number}") for number in range(32)]
    assert all(connection[2] == b"ready" for connection in held)
    assert connections.open("source-overflow")[2] == b""
    connections.finish(held[0])
    assert connections.open("source-overflow")[2] == b"ready"
    assert connections.open("source-another")[2] == b""


@pytest.mark.parametrize("message", [b".", b"!"])
def test_source_slot_recovers_after_completion_or_handler_error(connections, message):
    held = [connections.open() for _ in range(8)]
    connections.finish(held[0], message)
    assert connections.errors == ([] if message == b"." else [("source-a", 1)])
    assert connections.open()[2] == b"ready"
    assert connections.open()[2] == b""


@pytest.mark.parametrize("use_tcp", [False, True])
def test_thread_start_failure_returns_reserved_capacity(connections, monkeypatch, use_tcp):
    open_connection = connections.open_tcp if use_tcp else connections.open
    # Real TCP arrives from the loopback front, so only the global cap applies.
    capacity = 32 if use_tcp else 8
    for _ in range(capacity - 1):
        assert open_connection()[2] == b"ready"

    def fail_start(self):
        raise RuntimeError("fixture thread start failed")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_start)
        if use_tcp:
            assert open_connection()[2] == b""
            assert len(connections.errors) == 1
        else:
            with pytest.raises(RuntimeError, match="fixture thread start failed"):
                open_connection()
    assert open_connection()[2] == b"ready"
    assert open_connection()[2] == b""


def test_late_start_interrupt_does_not_release_a_completed_handler_twice(connections, monkeypatch):
    server_type = fleet_hub.ThreadingHTTPServer
    real_dispatch = server_type.process_request

    def interrupt_after_completion(server, request, address):
        real_dispatch(server, request, address)
        connections.complete_current(request)
        raise KeyboardInterrupt("fixture late start interrupt")

    with monkeypatch.context() as patch:
        patch.setattr(server_type, "process_request", interrupt_after_completion)
        with pytest.raises(KeyboardInterrupt, match="fixture late start interrupt"):
            connections.open()
    for _ in range(8):
        assert connections.open()[2] == b"ready"
    assert connections.open()[2] == b""


def test_inactive_sources_do_not_accumulate_accounting(connections):
    for number in range(64):
        connections.finish(connections.open(f"source-{number}"))
    assert connections.server._source_counts == {}


def test_server_close_keeps_database_open_until_admitted_handlers_finish(connections, tmp_path):
    held = connections.open()
    db = tmp_path / "hub.db"
    wal = Path(f"{db}-wal")
    conn = fleet_hub.open_db(db)
    try:
        fleet_hub.add_node(conn, "fixture-node")
    finally:
        conn.close()
    assert wal.exists()
    connections.server.server_close()
    assert wal.exists(), "a daemon handler still owns a request slot"
    assert connections.open()[2] == b"", "shutdown must refuse new admissions"
    connections.finish(held)
    assert not wal.exists(), "the last admitted handler must release the shutdown connection"


def test_schema_read_failure_releases_database(tmp_path, monkeypatch):
    db = tmp_path / "hub.db"

    class FailingSchemaConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("fixture schema read failed")

    conn = sqlite3.connect(db, factory=FailingSchemaConnection)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: conn)
    server_type = fleet_hub.BoundedThreadingHTTPServer
    server = server_type.__new__(server_type)
    try:
        with pytest.raises(sqlite3.OperationalError, match="fixture schema read failed"):
            server.__init__(("127.0.0.1", 0), BaseRequestHandler, db_path=db)
        assert server._db_keepalive is None, "failed startup must release the idle connection"
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            sqlite3.Connection.execute(conn, "SELECT 1")
    finally:
        conn.close()


@pytest.mark.parametrize("enabled,backlog", [(False, 5), (True, 32)])
def test_managed_loopback_opt_in(tmp_path, monkeypatch, enabled, backlog, capsys):
    from ipaddress import IPv4Address

    monkeypatch.delenv("BRIGADE_HUB_MANAGED_LOOPBACK", raising=False)
    if enabled:
        monkeypatch.setenv("BRIGADE_HUB_MANAGED_LOOPBACK", "fake-start-key")
        monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-start-key")
    server = fleet_hub.make_server(str(IPv4Address(2130706433)), 0, tmp_path / "hub.db", "fake-token")
    try:
        assert server._loopback_bind is True
        assert server.request_queue_size == backlog
        assert ("BRIGADE_UPSTREAM_PORT=" in capsys.readouterr().out) == enabled
    finally:
        server.server_close()


def test_managed_upstream_refuses_non_loopback(tmp_path, monkeypatch):
    from ipaddress import IPv4Address

    monkeypatch.setenv("BRIGADE_HUB_MANAGED_LOOPBACK", "fake-start-key")
    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-start-key")
    with pytest.raises(ValueError, match="numeric loopback"):
        fleet_hub.make_server(str(IPv4Address(0)), 0, tmp_path / "hub.db", "fake-token")


def test_managed_deferred_bind_skips_port_announcement(tmp_path, monkeypatch, capsys):
    from ipaddress import IPv4Address

    from brigade.fleet_hub_server import BoundedThreadingHTTPServer

    monkeypatch.setenv("BRIGADE_HUB_MANAGED_LOOPBACK", "fake-start-key")
    monkeypatch.setenv("BRIGADE_HUB_UPSTREAM_KEY", "fake-start-key")
    db = tmp_path / "hub.db"
    sqlite3.connect(db).close()
    server = BoundedThreadingHTTPServer(
        (str(IPv4Address(2130706433)), 0), BaseRequestHandler, bind_and_activate=False, db_path=db
    )
    try:
        assert "BRIGADE_UPSTREAM_PORT=" not in capsys.readouterr().out
        server.server_bind()
        server.server_activate()
        assert server.server_port > 0
    finally:
        server.server_close()


def _post_json(base, token, path, body):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_waiters_at_their_limit_leave_handlers_for_send_and_claim(tmp_path):
    from brigade import fleet_hub_messages
    from brigade.fleet_hub_server import MAX_HANDLER_THREADS, MAX_HANDLERS_PER_SOURCE

    waiters = fleet_hub_messages.MAX_WAITERS
    per_caller = fleet_hub_messages.MAX_WAITERS_PER_CALLER
    # Long polls can hold at most a quarter of the handlers, so non-waiting
    # requests always keep the rest. Through one loopback front, a full set of
    # waiters is already the old per-source cap of 8.
    assert waiters <= MAX_HANDLER_THREADS // 4
    assert waiters >= MAX_HANDLERS_PER_SOURCE
    db = tmp_path / "hub.db"
    conn = fleet_hub.init_db(db)
    try:
        nodes = [str(uuid.uuid4()) for _ in range(waiters // per_caller + 1)]
        tokens = [fleet_hub.add_node(conn, node)[1] for node in nodes]
    finally:
        conn.close()
    server = fleet_hub.make_server("127.0.0.1", 0, db, "fixture-admin-token")
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    serving.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    results = []
    polls = [
        threading.Thread(
            target=lambda token=token: results.append(
                _post_json(base, token, "/messages", {"action": "list", "after": 0, "unacked": True, "wait_ms": 10000})
            ),
            daemon=True,
        )
        for token in tokens[:-1]
        for _ in range(per_caller)
    ]
    sender, sender_token = nodes[-1], tokens[-1]
    released = set()
    try:
        for poll in polls:
            poll.start()
        deadline = monotonic() + 5
        while monotonic() < deadline:
            with fleet_hub_messages._WAKE_LOCK:
                if sum(fleet_hub_messages._ACTIVE_WAITERS.values()) == waiters:
                    break
            threading.Event().wait(0.01)
        with fleet_hub_messages._WAKE_LOCK:
            assert sum(fleet_hub_messages._ACTIVE_WAITERS.values()) == waiters
        with server._admission_lock:
            assert len(server._active_requests) >= waiters

        status, refused = _post_json(base, sender_token, "/messages", {"action": "list", "wait_ms": 10000})
        assert (status, refused["code"]) == (429, "message_wait_capped")
        claim = {"action": "acquire", "node_id": sender, "target": "example/project", "holder": "fixture-holder"}
        status, claimed = _post_json(base, sender_token, "/claims", claim)
        assert status == 200, claimed
        assert claimed["granted"] is True
        # The first send arrives while every waiter still holds its handler.
        for node in nodes[:-1]:
            wake = {"action": "send", "to": node, "subject": "Wake", "body": "Release"}
            assert _post_json(base, sender_token, "/messages", wake)[0] == 201
            released.add(node)
    finally:
        for node in nodes[:-1]:
            if node not in released:
                wake = {"action": "send", "to": node, "subject": "Wake", "body": "Release"}
                _post_json(base, sender_token, "/messages", wake)
        for poll in polls:
            poll.join(timeout=5)
            assert not poll.is_alive()
        server.shutdown()
        server.server_close()
    assert [status for status, _page in results] == [200] * waiters
    assert all(len(page["messages"]) == 1 for _status, page in results)
