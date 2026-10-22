"""Standard Webhooks signing and bounded HTTPS delivery ported from the bridge."""

import asyncio
import ipaddress
import socket
import ssl
from urllib.parse import SplitResult
from typing import cast

from brigade.mcp_events import (
    CallbackPolicy,
    parse_callback,
    secret_key as secret_key,
    signed_headers as signed_headers,
)


def callback_url(url: str, allowed_hosts: frozenset[str]) -> SplitResult:
    return parse_callback(url, CallbackPolicy(allowed_hosts))


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
