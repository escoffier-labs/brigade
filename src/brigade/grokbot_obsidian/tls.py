"""Pinned loopback HTTPS transport for the Obsidian upstream."""

from __future__ import annotations

import base64
import hashlib
import http.client
import ssl
from typing import Callable, NoReturn
from urllib.parse import urlparse

from .contracts import ERROR_MESSAGES, ObsidianError

SPKI_PIN = __import__("re").compile(r"^sha256:[A-Za-z0-9+/]{43}=$")
CONNECT_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 262_144


def _invalid() -> NoReturn:
    raise ObsidianError("invalid_request", "TLS configuration is invalid")


def _unavailable() -> NoReturn:
    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])


def _read_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        _invalid()
    first = data[offset]
    if first < 0x80:
        return first, offset + 1
    count = first & 0x7F
    if count == 0 or offset + count >= len(data):
        _invalid()
    length = 0
    for index in range(count):
        length = (length << 8) | data[offset + 1 + index]
    return length, offset + 1 + count


def _expect_sequence(data: bytes, offset: int) -> tuple[int, int, int]:
    if offset >= len(data) or data[offset] != 0x30:
        _invalid()
    length, next_offset = _read_length(data, offset + 1)
    end = next_offset + length
    if end > len(data):
        _invalid()
    return next_offset, end, end


def extract_spki_pin(der_cert: bytes) -> str:
    start, end, _ = _expect_sequence(der_cert, 0)
    tbs_start, tbs_end, _after_tbs = _expect_sequence(der_cert, start)
    cursor = tbs_start
    if cursor < tbs_end and der_cert[cursor] == 0xA0:
        length, next_offset = _read_length(der_cert, cursor + 1)
        cursor = next_offset + length
    for skip in range(6):
        if cursor >= tbs_end:
            _invalid()
        tag = der_cert[cursor]
        length, next_offset = _read_length(der_cert, cursor + 1)
        if skip == 5:
            if tag != 0x30:
                _invalid()
            spki = der_cert[cursor : next_offset + length]
            return "sha256:" + base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
        cursor = next_offset + length
    _invalid()
    raise AssertionError("unreachable")


def pin_of_pem(pem: str) -> str:
    return extract_spki_pin(ssl.PEM_cert_to_DER_cert(pem))


def _ca_pem(ca_bytes: bytes) -> str:
    if not ca_bytes:
        _invalid()
    if b"BEGIN" in ca_bytes:
        try:
            return ca_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ObsidianError("invalid_request", "TLS configuration is invalid") from exc
    try:
        return ssl.DER_cert_to_PEM_cert(ca_bytes)
    except ValueError as exc:
        raise ObsidianError("invalid_request", "TLS configuration is invalid") from exc


def create_pinned_context(ca_bytes: bytes, pins: tuple[str, ...]) -> ssl.SSLContext:
    if not 1 <= len(pins) <= 2 or any(not SPKI_PIN.fullmatch(pin) for pin in pins):
        _invalid()
    context = ssl.create_default_context(cadata=_ca_pem(ca_bytes))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _assert_peer_pin(sock: ssl.SSLSocket, pins: tuple[str, ...]) -> None:
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise ssl.SSLError("certificate identity is invalid")
    if extract_spki_pin(der) not in set(pins):
        raise ssl.SSLError("certificate identity is invalid")


def pinned_fetch(ca_bytes: bytes, pins: tuple[str, ...]) -> Callable[..., object]:
    context = create_pinned_context(ca_bytes, pins)

    def fetch(
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: int = CONNECT_TIMEOUT_SECONDS,
    ):
        if not url.startswith("https://"):
            _invalid()
        parsed = urlparse(url)
        host = parsed.hostname
        if host is None or parsed.username or parsed.password:
            _invalid()
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection = http.client.HTTPSConnection(host, port, context=context, timeout=timeout)
        try:
            connection.connect()
            sock = connection.sock
            if not isinstance(sock, ssl.SSLSocket):
                raise ssl.SSLError("certificate identity is invalid")
            _assert_peer_pin(sock, pins)
            connection.request(method, path, body=data, headers=headers or {})
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise http.client.HTTPException("redirects are disabled")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise OSError("response exceeded size limit")
            return {
                "status": response.status,
                "headers": {key: value for key, value in response.getheaders()},
                "body": body,
            }
        except ObsidianError:
            raise
        except (ssl.SSLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
            raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
        finally:
            connection.close()

    return fetch
