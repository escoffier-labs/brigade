"""Shared MCP Events plumbing: Standard Webhooks signing, callback policy, subscription identity.

The dot connector pack and the Fleet Hub both implement the draft MCP Events
webhook mode. This module holds the pieces that do not depend on either
runtime and uses only the standard library:

* ``secret_key`` / ``signed_headers`` / ``verify_signature``: Standard Webhooks
  HMAC-SHA256 over ``webhook-id.webhook-timestamp.body``, with a rotation
  window that signs under two keys.
* ``canonical_arguments`` / ``subscription_id``: the deterministic subscription
  identity (owner, callback URL, event name, canonical arguments).
* ``CallbackPolicy`` / ``parse_callback`` / ``post_webhook``: an exact-host
  allowlist, HTTPS only, public addresses only, one resolution pinned for the
  connection, no redirects, bounded response. A loopback HTTP receiver is
  allowed only when the policy says so explicitly (local test receivers).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit

MAX_BODY_BYTES = 262_144
MAX_RESPONSE_BYTES = 16_384
SIGNATURE_TOLERANCE_SECONDS = 300
# Closed vocabulary shared by the hub (which emits them) and the client (which must not trust free text).
CALLBACK_REASONS = (
    "timeout",
    "tls_error",
    "connection_refused",
    "challenge_failed",
    "http_4xx",
    "http_5xx",
    "http_status",
    "response_not_json",
    "challenge_mismatch",
)
EVENT_ERROR_CODES = frozenset(
    {
        "events_disabled",
        "events_invalid_arguments",
        "events_invalid_ttl",
        "events_invalid_delivery",
        "events_forbidden",
        "events_subscription_limit",
        "events_verification_limit",
        "events_busy",
        *(f"events_callback_{reason}" for reason in CALLBACK_REASONS),
    }
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_READ_CHUNK = 1024


def secret_key(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 100:
        raise ValueError("invalid_secret")
    key = base64.b64decode(secret[6:], validate=True)
    if not 24 <= len(key) <= 64:
        raise ValueError("invalid_secret")
    return key


def _signature(secret: str, signed: bytes) -> str:
    return "v1," + base64.b64encode(hmac.new(secret_key(secret), signed, hashlib.sha256).digest()).decode()


def signed_headers(
    secret: str, event_id: str, body: bytes, subscription_id: str, now: float | None = None, previous: str | None = None
) -> dict[str, str]:
    timestamp = str(int(time.time() if now is None else now))
    signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
    signatures = [_signature(value, signed) for value in (secret, previous) if value]
    return {
        "Content-Type": "application/json",
        "webhook-id": event_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": " ".join(signatures),
        "X-MCP-Subscription-Id": subscription_id,
    }


def verify_signature(
    secrets: Iterable[str],
    headers: Mapping[str, str],
    body: bytes,
    *,
    now: float | None = None,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Receiver-side check: any listed secret matches any presented signature, inside the clock window."""
    lowered = {key.lower(): value for key, value in headers.items()}
    event_id, timestamp, presented = (
        lowered.get(name) for name in ("webhook-id", "webhook-timestamp", "webhook-signature")
    )
    if not event_id or not timestamp or not presented or not timestamp.isascii() or not timestamp.isdigit():
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > tolerance:
        return False
    signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
    candidates = presented.split()
    for secret in secrets:
        try:
            expected = _signature(secret, signed)
        except ValueError:
            continue
        if any(hmac.compare_digest(expected, candidate) for candidate in candidates):
            return True
    return False


def canonical_arguments(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


def subscription_id(owner: str | None, url: str, name: str, canonical: str) -> str:
    return "sub_" + hashlib.sha256(json.dumps([owner, url, name, canonical]).encode()).hexdigest()


@dataclass(frozen=True)
class CallbackPolicy:
    """Where callbacks may go. ``allowed_hosts`` is an exact-name allowlist, never a suffix match."""

    allowed_hosts: frozenset[str]
    allow_loopback_http: bool = False


def parse_callback(url: str, policy: CallbackPolicy) -> SplitResult:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        raise ValueError("invalid_callback")
    parts = urlsplit(url)
    host = parts.hostname
    if host not in policy.allowed_hosts or parts.username or parts.password or parts.fragment:
        raise ValueError("invalid_callback")
    if parts.scheme == "https":
        if parts.port not in (None, 443):
            raise ValueError("invalid_callback")
    elif not (parts.scheme == "http" and policy.allow_loopback_http and host in _LOOPBACK_HOSTS):
        raise ValueError("invalid_callback")
    return parts


def _public(address: str) -> bool:
    if "%" in address:
        return False
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_global


def _loopback(address: str) -> bool:
    if "%" in address:
        return False
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


# A stalled resolver cannot be cancelled, only abandoned, so abandoned lookups are capped: a callback that cannot
# get a slot fails at once instead of piling up blocked threads.
_RESOLVER_SLOTS = threading.BoundedSemaphore(8)


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("callback_deadline_exceeded")
    return left


def _lookup(host: str, port: int, deadline: float | None) -> list[Any]:
    """``getaddrinfo`` inside the overall deadline: a worker runs it, the caller waits only for the time left."""
    if deadline is None:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not _RESOLVER_SLOTS.acquire(blocking=False):
        raise TimeoutError("callback_resolver_busy")
    outcome: list[Any] = []
    done = threading.Event()

    def work() -> None:
        try:
            outcome.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except BaseException as error:  # noqa: BLE001 - handed to the waiting caller
            outcome.append(error)
        finally:
            _RESOLVER_SLOTS.release()
            done.set()

    try:
        threading.Thread(target=work, name="fleet-callback-resolver", daemon=True).start()
    except BaseException:
        _RESOLVER_SLOTS.release()
        raise
    if not done.wait(_remaining(deadline)):
        raise TimeoutError("callback_deadline_exceeded")
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    return outcome[0]


def resolve_callback(parts: SplitResult, policy: CallbackPolicy, deadline: float | None = None) -> list[str]:
    """Resolve once and require every answer to be acceptable for the scheme.

    With a ``deadline`` (a ``time.monotonic()`` value) the lookup shares the delivery's overall budget.
    """
    assert parts.hostname is not None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    answers = _lookup(parts.hostname, port, deadline)
    ips = list(dict.fromkeys(str(item[4][0]) for item in answers))
    check = _public if parts.scheme == "https" else _loopback
    if not ips or not all(check(ip) for ip in ips):
        raise ValueError("invalid_callback_address")
    return ips


class _OwnedSocket:
    """Delivery-owned socket reference, kept apart from ``connection.sock``.

    ``getresponse()`` clears ``connection.sock`` when the response is ``Connection: close``, so a
    watchdog reading it would find nothing while the body is still being read.
    """

    owned_sock: socket.socket | None = None

    def abort_socket(self) -> None:
        sock = self.owned_sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


class _PinnedHTTPSConnection(_OwnedSocket, http.client.HTTPSConnection):
    """Connect to a validated IP while verifying the certificate against the original hostname."""

    def __init__(self, host: str, port: int, *, address: str, deadline: float) -> None:
        self._tls = ssl.create_default_context()
        super().__init__(host, port, timeout=_remaining(deadline), context=self._tls)
        self._address = address
        self._deadline = deadline

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), _remaining(self._deadline))
        self.owned_sock = sock
        sock.settimeout(_remaining(self._deadline))
        # Handshake manually so the wrapped socket is published before it can block.
        wrapped = self._tls.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
        self.owned_sock = wrapped
        self.sock = wrapped
        wrapped.settimeout(_remaining(self._deadline))
        wrapped.do_handshake()


class _PinnedHTTPConnection(_OwnedSocket, http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, address: str, deadline: float) -> None:
        super().__init__(host, port, timeout=_remaining(deadline))
        self._address = address
        self._deadline = deadline

    def connect(self) -> None:
        self.sock = self.owned_sock = socket.create_connection((self._address, self.port), _remaining(self._deadline))


def _read_response(connection: _PinnedHTTPConnection | _PinnedHTTPSConnection, deadline: float) -> tuple[int, bytes]:
    """Read status and a bounded body, re-arming the socket timeout to the time left before every read.

    ``read1`` returns whatever is available instead of waiting for a full chunk, so a receiver that
    drips one byte per idle interval cannot stretch the exchange past the overall deadline.
    """
    sock = connection.owned_sock
    assert sock is not None
    sock.settimeout(_remaining(deadline))
    response = connection.getresponse()
    if response.getheader("Content-Encoding", "identity").lower() != "identity":
        raise ValueError("callback_response_encoded")
    chunks: list[bytes] = []
    size = 0
    while True:
        if response.isclosed():
            # A finished body closes the socket under us; there is nothing left to bound.
            _remaining(deadline)
        else:
            sock.settimeout(_remaining(deadline))
        chunk = response.read1(_READ_CHUNK)
        if not chunk:
            return response.status, b"".join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("callback_response_too_large")
        chunks.append(chunk)


def post_webhook(
    url: str, body: bytes, headers: Mapping[str, str], policy: CallbackPolicy, *, timeout: float = 10.0
) -> tuple[int, bytes]:
    """POST once to a validated callback. Never follows redirects, uses no proxy, bounds the response.

    ``timeout`` is one overall deadline for DNS, connect, TLS, send, headers and body: per-read idle
    timeouts alone would let a slow-drip receiver pin the caller indefinitely.

    Raises ``ValueError`` for a rejected URL, address, or oversized body, and
    ``OSError`` (including timeouts) for transport failures.
    """
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("body_too_large")
    deadline = time.monotonic() + timeout
    parts = parse_callback(url, policy)
    assert parts.hostname is not None
    address = resolve_callback(parts, policy, deadline)[0]
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection: _PinnedHTTPConnection | _PinnedHTTPSConnection
    if parts.scheme == "https":
        connection = _PinnedHTTPSConnection(parts.hostname, parts.port or 443, address=address, deadline=deadline)
    else:
        connection = _PinnedHTTPConnection(parts.hostname, parts.port or 80, address=address, deadline=deadline)
    expired = threading.Event()

    def abort() -> None:
        # Unblocks a recv stuck mid-header, mid-chunk-framing or mid-handshake, which no per-call
        # timeout can bound. Uses the delivery-owned socket, not connection.sock (cleared by getresponse).
        expired.set()
        connection.abort_socket()

    watchdog = threading.Timer(max(deadline - time.monotonic(), 0.0), abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.request(
            "POST", target, body=body, headers={**headers, "Accept-Encoding": "identity", "Connection": "close"}
        )
        result = _read_response(connection, deadline)
        if expired.is_set():
            # A shutdown can surface as a clean EOF on a close-delimited body.
            raise TimeoutError("callback_deadline_exceeded")
        return result
    except (OSError, http.client.HTTPException) as error:
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("callback_deadline_exceeded") from error
        if isinstance(error, http.client.HTTPException):
            raise OSError("callback_protocol_error") from error
        raise
    finally:
        watchdog.cancel()
        watchdog.join()
        connection.close()
        if connection.owned_sock is not None:
            connection.owned_sock.close()
