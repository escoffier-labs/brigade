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
import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
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


def resolve_callback(parts: SplitResult, policy: CallbackPolicy) -> list[str]:
    """Resolve once and require every answer to be acceptable for the scheme."""
    assert parts.hostname is not None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    answers = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(str(item[4][0]) for item in answers))
    check = _public if parts.scheme == "https" else _loopback
    if not ips or not all(check(ip) for ip in ips):
        raise ValueError("invalid_callback_address")
    return ips


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated IP while verifying the certificate against the original hostname."""

    def __init__(self, host: str, port: int, *, address: str, timeout: float) -> None:
        self._tls = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._tls)
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


def post_webhook(
    url: str, body: bytes, headers: Mapping[str, str], policy: CallbackPolicy, *, timeout: float = 10.0
) -> tuple[int, bytes]:
    """POST once to a validated callback. Never follows redirects, uses no proxy, bounds the response.

    Raises ``ValueError`` for a rejected URL, address, or oversized body, and
    ``OSError`` (including timeouts) for transport failures.
    """
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("body_too_large")
    parts = parse_callback(url, policy)
    assert parts.hostname is not None
    address = resolve_callback(parts, policy)[0]
    deadline = time.monotonic() + timeout
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection: http.client.HTTPConnection
    if parts.scheme == "https":
        connection = _PinnedHTTPSConnection(parts.hostname, parts.port or 443, address=address, timeout=timeout)
    else:
        connection = _PinnedHTTPConnection(parts.hostname, parts.port or 80, address=address, timeout=timeout)
    try:
        connection.request(
            "POST", target, body=body, headers={**headers, "Accept-Encoding": "identity", "Connection": "close"}
        )
        response = connection.getresponse()
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("callback_response_encoded")
        chunks: list[bytes] = []
        size = 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("callback_response_slow")
            chunk = response.read(_READ_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("callback_response_too_large")
            chunks.append(chunk)
        return response.status, b"".join(chunks)
    except http.client.HTTPException as error:
        raise OSError("callback_protocol_error") from error
    finally:
        connection.close()
