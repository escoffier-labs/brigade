"""Connection admission is enforced before an unauthenticated handler starts."""

import socket
import threading
from socketserver import BaseRequestHandler

import pytest

from brigade import fleet_hub


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


def test_one_source_cannot_fill_all_handler_slots(connections):
    for port in range(8):
        assert connections.open(port=port)[2] == b"ready"
    assert connections.open(port=99)[2] == b""
    assert connections.open("source-b")[2] == b"ready"


def test_tcp_accept_path_closes_overflow_and_recovers(connections):
    held = [connections.open_tcp() for _ in range(8)]
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
    for _ in range(7):
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
