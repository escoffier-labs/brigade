"""Cloudflare Access signed identity with bounded, lazy public-key transport."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from brigade.grokbot_mcp import OptionalDependencyError

from .contracts import MillerError
from .runtime_config import AuthConfig

METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
MAX_ASSERTION_BYTES = 16384
MAX_JWKS_BYTES = 65536
MAX_JWKS_WIRE_BYTES = 98304
CACHE_TTL = 300
REFRESH_INTERVAL = 30
FETCH_SECONDS = 5
CLOCK_LEEWAY = 30
MAX_HEADERS = 64
MAX_HEADER_BYTES = 32768


class AuthError(MillerError):
    """Fixed diagnostics only, never tokens, keys or upstream exception text."""

    def __init__(self, code: str = "authentication_required") -> None:
        super().__init__(code)


def ensure(value: object, code: str = "authentication_required") -> None:
    if not value:
        raise AuthError(code)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        ensure(key not in result)
        result[key] = value
    return result


def _jwt() -> ModuleType:
    try:
        import cryptography  # noqa: F401 - RSA verification requires this backend.
        import jwt
    except ImportError:
        raise OptionalDependencyError() from None
    if not jwt.algorithms.has_crypto or "RS256" not in jwt.algorithms.get_default_algorithms():
        raise OptionalDependencyError()
    return jwt


def bounded_headers(scope: Mapping[str, Any]) -> Sequence[tuple[bytes, bytes]]:
    headers = scope.get("headers", ())
    ensure(isinstance(headers, (list, tuple)) and len(headers) <= MAX_HEADERS)
    total = 0
    for item in headers:
        ensure(isinstance(item, (list, tuple)) and len(item) == 2)
        name, value = item
        ensure(isinstance(name, bytes) and isinstance(value, bytes))
        total += len(name) + len(value)
        ensure(total <= MAX_HEADER_BYTES)
    return headers


async def fetch_public_keys(team_domain: str) -> bytes:
    """Fetch the pinned public JWKS over verified TLS within one deadline.

    HTTP framing is parsed by h11. No redirects, proxies or decompression exist.
    Imports, TLS context creation and connection happen only when called.
    """
    try:
        import anyio

        with anyio.fail_after(FETCH_SECONDS):
            import ssl

            import h11

            host = team_domain
            connection = h11.Connection(h11.CLIENT, max_incomplete_event_size=MAX_HEADER_BYTES)
            request = connection.send(
                h11.Request(
                    method="GET",
                    target="/cdn-cgi/access/certs",
                    headers=[
                        ("Host", host),
                        ("Accept", "application/json"),
                        ("Accept-Encoding", "identity"),
                        ("Connection", "close"),
                    ],
                )
            )
            request = (request or b"") + (connection.send(h11.EndOfMessage()) or b"")
            async with await anyio.connect_tcp(
                host,
                443,
                tls=True,
                tls_hostname=host,
                ssl_context=ssl.create_default_context(),
                tls_standard_compatible=False,
            ) as stream:
                await stream.send(request)
                body, header = bytearray(), bytearray()
                wire_bytes, received_response, eof = 0, False, False
                while True:
                    event = connection.next_event()
                    if event is h11.NEED_DATA:
                        ensure(not eof)
                        try:
                            chunk = await stream.receive(4096)
                        except anyio.EndOfStream:
                            chunk = b""
                        ensure(len(chunk) <= 4096)
                        wire_bytes += len(chunk)
                        ensure(wire_bytes <= MAX_JWKS_WIRE_BYTES)
                        if not received_response:
                            header.extend(chunk)
                            end = header.find(b"\r\n\r\n")
                            ensure((len(header) if end < 0 else end + 4) <= MAX_HEADER_BYTES)
                        eof = not chunk
                        connection.receive_data(chunk)
                    elif isinstance(event, h11.Response):
                        ensure(not received_response and event.status_code == 200 and header.find(b"\r\n\r\n") >= 0)
                        ensure(header[: header.find(b"\r\n\r\n")].count(b"\r\n") <= MAX_HEADERS)
                        headers = bounded_headers({"headers": list(event.headers)})
                        length, chunked = None, False
                        for name, value in headers:
                            if name == b"content-encoding":
                                ensure(value.lower() == b"identity")
                            elif name == b"content-length":
                                ensure(value.isdigit() and len(value) <= 8 and int(value) <= MAX_JWKS_BYTES)
                                length = int(value)
                            elif name == b"transfer-encoding":
                                ensure(value.lower() == b"chunked")
                                chunked = True
                        ensure(length is None or not chunked)
                        received_response = True
                        header.clear()
                    elif isinstance(event, h11.Data):
                        ensure(received_response and len(body) + len(event.data) <= MAX_JWKS_BYTES)
                        body.extend(event.data)
                    elif isinstance(event, h11.EndOfMessage):
                        # Trailers and surplus bytes are outside this single-response protocol.
                        ensure(received_response and not event.headers and not connection.trailing_data[0])
                        return bytes(body)
                    else:
                        # Includes informational responses, protocol switches and premature close.
                        raise AuthError("authentication_required")
    except ImportError:
        raise OptionalDependencyError() from None
    except OptionalDependencyError:
        raise
    except Exception:
        raise AuthError("authentication_required") from None


def parse_keys(data: bytes) -> dict[str, Any]:
    jwt = _jwt()
    ensure(isinstance(data, bytes) and 0 < len(data) <= MAX_JWKS_BYTES)
    value = json.loads(data, object_pairs_hook=unique_object)
    ensure(isinstance(value, dict))
    items = value.get("keys")
    ensure(isinstance(items, list) and 1 <= len(items) <= 8)
    keys = {}
    for item in items:
        ensure(isinstance(item, dict) and set(item) <= {"kid", "kty", "alg", "use", "n", "e"})
        kid = item.get("kid")
        ensure(
            isinstance(kid, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", kid)
            and kid not in keys
            and item.get("kty") == "RSA"
            and item.get("alg", "RS256") == "RS256"
            and item.get("use", "sig") == "sig"
        )
        for field, bound in (("n", 1400), ("e", 8)):
            number = item.get(field)
            ensure(isinstance(number, str) and 1 <= len(number) <= bound and re.fullmatch(r"[A-Za-z0-9_-]+", number))
        key = jwt.PyJWK.from_dict(item, algorithm="RS256").key
        ensure(2048 <= key.key_size <= 8192)
        keys[kid] = key
    return keys


class AccessVerifier:
    """A process-local bounded JWKS cache, serialized refresh, no stale fallback.

    `kid` selects only among verified public keys. It never selects a URL.
    `validated` means the config schema was validated, not deployment acceptance.
    Ownership is the approved exact email, stable across human subject changes.
    """

    def __init__(
        self,
        auth: AuthConfig,
        *,
        fetch: Callable[[], Awaitable[bytes]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = auth
        self.resource = auth.resource
        parsed = urlsplit(auth.resource)
        self.authorization_server = f"{parsed.scheme}://{parsed.netloc}"
        self.metadata_url = self.authorization_server + METADATA_PATH

        async def configured_fetch() -> bytes:
            return await fetch_public_keys(auth.team_domain)

        self.fetch = configured_fetch if fetch is None else fetch
        self.monotonic = monotonic
        self.keys: dict[str, Any] = {}
        self.expires = 0.0
        self.last_attempt = float("-inf")
        self.lock = asyncio.Lock()
        self.validated = True

    async def signing_key(self, kid: str) -> Any:
        import anyio

        async with self.lock:
            now = self.monotonic()
            if now < self.expires and kid in self.keys:
                return self.keys[kid]
            ensure(now - self.last_attempt >= REFRESH_INTERVAL)
            self.last_attempt = now  # Failed refreshes are throttled too.
            with anyio.fail_after(FETCH_SECONDS):
                data = await self.fetch()
            keys = parse_keys(data)  # Commit only an entirely valid key set.
            self.keys, self.expires = keys, self.monotonic() + CACHE_TTL
            ensure(kid in keys)
            return keys[kid]

    async def verify(self, scope: Mapping[str, Any]) -> str:
        jwt = _jwt()
        try:
            assertions = [value for name, value in bounded_headers(scope) if name.lower() == b"cf-access-jwt-assertion"]
            ensure(len(assertions) == 1)
            raw = assertions[0]
            ensure(0 < len(raw) <= MAX_ASSERTION_BYTES and raw.isascii())
            token = raw.decode("ascii")
            ensure(re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token))
            # Parse only bounded JSON. These unverified values never establish identity.
            header_part, payload_part, _ = token.split(".")
            header = json.loads(jwt.utils.base64url_decode(header_part), object_pairs_hook=unique_object)
            payload = json.loads(jwt.utils.base64url_decode(payload_part), object_pairs_hook=unique_object)
            ensure(
                isinstance(header, dict)
                and set(header) <= {"alg", "kid", "typ"}
                and header.get("alg") == "RS256"
                and header.get("typ", "JWT") == "JWT"
            )
            kid = header.get("kid")
            ensure(isinstance(kid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", kid))
            ensure(isinstance(payload, dict))
            key = await self.signing_key(kid)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=self.config.issuer,
                audience=self.config.audience,
                leeway=CLOCK_LEEWAY,
                options={
                    "require": ["iss", "aud", "sub", "email", "exp", "nbf", "iat"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    "verify_sub": True,
                    "enforce_minimum_key_length": True,
                },
            )
            ensure(
                claims["iss"] == self.config.issuer
                and (claims["aud"] == self.config.audience or claims["aud"] == [self.config.audience])
            )
            ensure(
                claims["email"] == self.config.approved_email
                and claims.get("type") == "app"
                and "common_name" not in claims
            )
            subject = claims["sub"]
            ensure(
                isinstance(subject, str) and 0 < len(subject) <= 256 and all(32 < ord(char) < 127 for char in subject)
            )
            ensure(
                all(type(claims[name]) is int and 0 <= claims[name] <= 253402300799 for name in ("exp", "nbf", "iat"))
            )
            ensure(claims["iat"] <= claims["exp"] and claims["nbf"] <= claims["exp"])
            return self.config.approved_email
        except OptionalDependencyError:
            raise
        except Exception:
            raise AuthError("authentication_required") from None
