"""Standard Webhooks signing and bounded HTTPS delivery ported from the bridge."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import socket
import ssl
import time
from urllib.parse import SplitResult, urlsplit
from typing import cast


def secret_key(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 100:
        raise ValueError("invalid_secret")
    key = base64.b64decode(secret[6:], validate=True)
    if not 24 <= len(key) <= 64:
        raise ValueError("invalid_secret")
    return key


def signed_headers(
    secret: str, event_id: str, body: bytes, subscription_id: str, now: float | None = None, previous: str | None = None
) -> dict[str, str]:
    timestamp = str(int(time.time() if now is None else now))
    signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
    signatures = [
        "v1," + base64.b64encode(hmac.new(secret_key(value), signed, hashlib.sha256).digest()).decode()
        for value in (secret, previous)
        if value
    ]
    return {
        "Content-Type": "application/json",
        "webhook-id": event_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": " ".join(signatures),
        "X-MCP-Subscription-Id": subscription_id,
    }


def callback_url(url: str, allowed_hosts: frozenset[str]) -> SplitResult:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        raise ValueError("invalid_callback")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in allowed_hosts
        or parts.username
        or parts.password
        or parts.fragment
        or parts.port not in (None, 443)
    ):
        raise ValueError("invalid_callback")
    return parts


async def webhook_post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    allowed_hosts: frozenset[str] = frozenset({"connectors.api.openai.com"}),
) -> tuple[int, bytes]:
    """Resolve once, reject every non-public IP, connect to that IP with host TLS.

    h11 bounds framing. No redirects, proxies, DNS re-resolution or decompression.
    """
    import anyio
    import h11

    parts = callback_url(url, allowed_hosts)
    assert parts.hostname is not None
    if len(body) > 262144:
        raise ValueError("body_too_large")
    with anyio.fail_after(10):
        addresses = await asyncio.get_running_loop().getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
        ips = list(dict.fromkeys(cast(str, item[4][0]) for item in addresses))
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError("invalid_callback_address")
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection = h11.Connection(h11.CLIENT, max_incomplete_event_size=16384)
        request = connection.send(
            h11.Request(
                method="POST",
                target=target,
                # h11 takes (name, value) pairs; a dict iterates keys only.
                headers=list(
                    {
                        **headers,
                        "Host": parts.hostname,
                        "Content-Length": str(len(body)),
                        "Connection": "close",
                        "Accept-Encoding": "identity",
                    }.items()
                ),
            )
        )
        request += connection.send(h11.Data(data=body)) + connection.send(h11.EndOfMessage())
        async with await anyio.connect_tcp(
            ips[0],
            443,
            tls=True,
            tls_hostname=parts.hostname,
            ssl_context=ssl.create_default_context(),
            tls_standard_compatible=False,
        ) as stream:
            await stream.send(request)
            result, status, wire = bytearray(), None, 0
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    try:
                        chunk = await stream.receive(4096)
                    except (anyio.EndOfStream, anyio.BrokenResourceError):
                        chunk = b""
                    wire += len(chunk)
                    if wire > 65536:
                        raise ValueError("callback_response_invalid")
                    # b"" tells h11 the peer closed; it ends a read-until-close
                    # body or raises if the message is incomplete.
                    connection.receive_data(chunk)
                elif isinstance(event, h11.Response):
                    status = event.status_code
                    if any(k == b"content-encoding" and v != b"identity" for k, v in event.headers):
                        raise ValueError("callback_response_encoded")
                elif isinstance(event, h11.Data):
                    result.extend(event.data)
                    if len(result) > 16384:
                        raise ValueError("callback_response_too_large")
                elif isinstance(event, h11.EndOfMessage):
                    assert status is not None
                    return status, bytes(result)
                else:
                    raise ValueError("callback_response_unexpected_" + type(event).__name__)
