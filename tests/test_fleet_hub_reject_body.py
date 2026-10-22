"""Live TCP regressions for responses sent before a request body is read."""

import secrets
import socket
import struct
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest

from brigade import fleet_hub, fleet_hub_enrollment, fleet_hub_http, fleet_hub_roster_page, fleet_policy_page


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    address = socket.inet_ntoa(struct.pack("!I", socket.INADDR_LOOPBACK))
    credential = secrets.token_urlsafe()
    server = fleet_hub.make_server(address, 0, tmp_path / "hub.db", credential)
    server.admin = credential
    with fleet_hub.open_db(tmp_path / "hub.db") as conn:
        _, server.node = fleet_hub.add_node(conn, str(uuid4()))
    conn.close()
    server.completed = threading.Event()
    process_request = server.process_request_thread

    def complete(*args):
        try:
            process_request(*args)
        finally:
            server.completed.set()

    monkeypatch.setattr(server, "process_request_thread", complete)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _upload(
    hub, path, length, *, method="POST", media_type="application/x-www-form-urlencoded", site="same-origin", bearer=None
):
    with socket.create_connection(hub.server_address, timeout=5) as peer:
        peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        authorization = f"Authorization: Bearer {getattr(hub, bearer)}\r\n" if bearer else ""
        headers = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {hub.server_address[0]}:{hub.server_address[1]}\r\n"
            f"Content-Type: {media_type}\r\n"
            f"Sec-Fetch-Site: {site}\r\n"
            f"{authorization}"
            f"Content-Length: {length}\r\n\r\n"
        ).encode("ascii")
        peer.sendall(headers + b"x")
        # Wait for the response and its FIN before resuming the upload. This
        # schedules the late writes after rejection without relying on sleeps.
        response = bytearray()
        while chunk := peer.recv(8192):
            response.extend(chunk)
        head, body = bytes(response).split(b"\r\n\r\n", 1)
        yield peer, head, body


@pytest.mark.parametrize(
    ("path", "length", "expected_body"),
    [
        ("/deck/policy", fleet_policy_page.MAX_FORM_BYTES + 1, b"form body too large\n"),
        ("/deck/roster", fleet_hub_roster_page.MAX_FORM_BYTES + 1, b"form body too large\n"),
        ("/enroll", fleet_hub_enrollment.BODY_LIMIT + 1, b"Dashboard request refused.\n"),
    ],
)
def test_oversized_body_can_finish_sending_after_rejection(hub, path, length, expected_body):
    with _upload(hub, path, length) as (peer, head, body):
        for offset in range(1, length, 8192):
            peer.sendall(b"x" * min(8192, length - offset))
        peer.shutdown(socket.SHUT_WR)
        assert head.split(b"\r\n", 1)[0] == b"HTTP/1.0 413 Request Entity Too Large"
        assert body == expected_body
        assert b"Connection: close\r\n" in head + b"\r\n"


@pytest.mark.parametrize(
    ("path", "options", "status"),
    [
        ("/deck/policy", {"media_type": "application/json"}, 415),
        ("/deck/roster", {"media_type": "application/json"}, 415),
        ("/enroll", {"media_type": "application/json"}, 415),
        ("/deck/policy", {"length": "invalid"}, 400),
        ("/deck/roster", {"length": "invalid"}, 400),
        ("/enroll", {"length": "invalid"}, 400),
        ("/enroll", {"length": "32769\r\nContent-Length: 32769"}, 400),
        ("/enroll", {"site": "cross-site"}, 403),
        ("/deck/policy", {}, 401),
        ("/deck/roster", {}, 401),
        ("/deck/policy", {"bearer": "admin", "site": "cross-site"}, 403),
        ("/deck/roster", {"bearer": "admin", "site": "cross-site"}, 403),
        ("/dashboard/enrollment", {}, 401),
        ("/events", {}, 401),
        ("/messages", {}, 401),
        ("/messages", {"bearer": "admin"}, 403),
        ("/message-principals", {}, 401),
        ("/message-principals", {"bearer": "node"}, 403),
        ("/messages", {"bearer": "node", "length": fleet_hub.MAX_BODY_BYTES + 1}, 413),
        ("/message-principals", {"bearer": "admin", "length": fleet_hub.MAX_BODY_BYTES + 1}, 413),
        ("/messages", {"bearer": "node", "length": "invalid"}, 400),
        ("/message-principals", {"bearer": "admin", "length": "invalid"}, 400),
        ("/preference", {"method": "PUT"}, 401),
        ("/actions", {}, 401),
        ("/stats?invalid=1", {}, 400),
        ("/missing", {"method": "PATCH"}, 404),
        ("/missing", {"method": "DELETE"}, 404),
    ],
)
def test_other_early_rejections_allow_late_body_chunks(hub, path, options, status):
    with _upload(hub, path, **{"length": 8193, **options}) as (peer, head, body):
        peer.sendall(b"x" * 4096)
        peer.sendall(b"x" * 4096)
        peer.shutdown(socket.SHUT_WR)
        assert int(head.split(b" ", 2)[1]) == status
        assert body
        assert b"Connection: close\r\n" in head + b"\r\n"


def test_rejected_trickling_upload_has_an_absolute_deadline(hub):
    with _upload(hub, "/deck/policy", 10**12) as (peer, head, _body):
        assert b" 413 " in head
        start = time.monotonic()
        while not hub.completed.wait(0.01):
            if time.monotonic() - start > 2:
                pytest.fail("trickling upload retained the handler beyond its drain deadline")
            try:
                peer.sendall(b"x")
            except OSError:
                # The bounded drain may have expired between wait and send.
                break
        assert hub.completed.wait(1)


def test_rejected_upload_has_a_byte_cap(hub, monkeypatch):
    # Separate the byte bound from the time bound. Keep the client open after
    # sending more than the byte budget, and require handler completion early.
    monkeypatch.setattr(fleet_hub_http, "_REJECT_DRAIN_SECONDS", 5)
    with _upload(hub, "/deck/policy", 10**12) as (peer, head, _body):
        assert b" 413 " in head
        try:
            peer.sendall(b"x" * (2 * 1024 * 1024))
        except OSError:
            # Closing once the cap is reached is explicitly allowed.
            pass
        assert hub.completed.wait(2)
