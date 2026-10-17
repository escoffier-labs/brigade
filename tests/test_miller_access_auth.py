"""Synthetic RSA signatures and in-memory JWKS transport, with no network IO."""

import asyncio
import builtins
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from brigade.miller.access_auth import (
    AccessVerifier,
    AuthError,
    MAX_ASSERTION_BYTES,
    MAX_JWKS_BYTES,
    MAX_JWKS_WIRE_BYTES,
    CACHE_TTL,
    REFRESH_INTERVAL,
    METADATA_PATH,
    fetch_public_keys,
)
from brigade.miller.runtime_config import AuthConfig
from brigade.grokbot_mcp import OptionalDependencyError
from brigade.miller import access_auth

AUTH = AuthConfig(
    issuer="https://team.example.test",
    team_domain="team.example.test",
    audience="a" * 64,
    approved_email="miller@example.test",
    resource="https://miller.example.test/mcp",
)


class PublicKeyTransportChecks(unittest.TestCase):
    def test_import_and_verifier_construction_are_inert(self):
        import runpy

        with (
            patch("anyio.connect_tcp", side_effect=AssertionError("connection forbidden")) as connector,
            patch("ssl.create_default_context", side_effect=AssertionError("TLS context forbidden")) as context,
        ):
            namespace = runpy.run_path(access_auth.__file__, run_name="brigade.miller._inert_fixture")
            namespace["AccessVerifier"](AUTH)
            connector.assert_not_called()
            context.assert_not_called()

    def test_optional_imports_are_lazy_and_missing_dependencies_are_reported(self):
        import runpy

        original_import = builtins.__import__

        def import_without_optional(name, *args, **kwargs):
            if name in {"jwt", "anyio", "h11", "ssl"}:
                raise ImportError("synthetic missing dependency")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_optional):
            namespace = runpy.run_path(access_auth.__file__, run_name="brigade.miller._inert_fixture")
            verifier = namespace["AccessVerifier"](AUTH)
            with self.assertRaises(OptionalDependencyError):
                asyncio.run(verifier.verify({"headers": []}))

        for missing in ("anyio", "h11", "ssl"):

            def import_without_one(name, *args, missing=missing, **kwargs):
                if name == missing:
                    raise ImportError("synthetic missing dependency")
                return original_import(name, *args, **kwargs)

            with self.subTest(missing=missing), patch("builtins.__import__", side_effect=import_without_one):
                with self.assertRaises(OptionalDependencyError):
                    asyncio.run(fetch_public_keys(AUTH.team_domain))

    class Stream:
        def __init__(self, response, fragment, receive_error=None):
            self.remaining = bytearray(response)
            self.fragment = fragment
            self.receive_error = receive_error
            self.sent, self.read_sizes = [], []
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def send(self, data):
            self.sent.append(data)

        async def receive(self, max_bytes):
            import anyio

            self.read_sizes.append(max_bytes)
            if self.receive_error is not None:
                raise self.receive_error
            if not self.remaining:
                raise anyio.EndOfStream
            count = min(max_bytes, self.fragment, len(self.remaining))
            data = bytes(self.remaining[:count])
            del self.remaining[:count]
            return data

    async def exchange(self, response, *, fragment=4096, reject=False, receive_error=None):
        import ssl

        stream = self.Stream(response, fragment, receive_error)

        async def connect(host, port, **options):
            self.assertEqual((host, port), (AUTH.team_domain, 443))
            self.assertEqual(set(options), {"tls", "tls_hostname", "ssl_context", "tls_standard_compatible"})
            self.assertIs(options["tls"], True)
            self.assertEqual(options["tls_hostname"], host)
            self.assertIs(options["tls_standard_compatible"], False)
            self.assertIsInstance(options["ssl_context"], ssl.SSLContext)
            self.assertTrue(options["ssl_context"].check_hostname)
            self.assertEqual(options["ssl_context"].verify_mode, ssl.CERT_REQUIRED)
            return stream

        with patch("anyio.connect_tcp", side_effect=connect) as connector:
            if reject:
                with self.assertRaises(AuthError) as error:
                    await fetch_public_keys(AUTH.team_domain)
                self.assertEqual(str(error.exception), "authentication_required")
                self.assertTrue(error.exception.__suppress_context__)
                result = None
            else:
                result = await fetch_public_keys(AUTH.team_domain)
            connector.assert_awaited_once()
        self.assertTrue(stream.closed)
        self.assertEqual(
            stream.sent,
            [
                b"GET /cdn-cgi/access/certs HTTP/1.1\r\n"
                + b"Host: "
                + AUTH.team_domain.encode("ascii")
                + b"\r\n"
                + b"Accept: application/json\r\nAccept-Encoding: identity\r\n"
                b"Connection: close\r\n\r\n"
            ],
        )
        self.assertTrue(stream.read_sizes)
        self.assertEqual(set(stream.read_sizes), {4096})
        return result, stream

    def test_pinned_tls_request_and_supported_framing(self):
        body = b'{"keys":[]}'
        responses = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n" + body,
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nb\r\n" + body + b"\r\n0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Encoding: IDENTITY\r\n\r\n" + body,
        ]

        async def scenario():
            for response in responses:
                for fragment in (1, 4096):
                    with self.subTest(response=response[:40], fragment=fragment):
                        result, _ = await self.exchange(response, fragment=fragment)
                        self.assertEqual(result, body)
            body_at_limit = b"x" * MAX_JWKS_BYTES
            result, _ = await self.exchange(b"HTTP/1.1 200 OK\r\nContent-Length: 65536\r\n\r\n" + body_at_limit)
            self.assertEqual(result, body_at_limit)

        asyncio.run(scenario())

    def test_status_encoding_and_declared_length_fail_closed(self):
        responses = [
            b"HTTP/1.1 302 Found\r\nLocation: https://foreign.invalid/\r\n\r\n",
            b"HTTP/1.1 500 Error\r\n\r\n",
            b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\n\r\n",
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n",
        ]
        headers = [
            b"Content-Encoding: gzip",
            b"Content-Encoding: br",
            b"Content-Encoding: identity\r\nContent-Encoding: gzip",
            b"Content-Length: 65537",
            b"Content-Length: 999999999",
            b"Content-Length: -1",
            b"Content-Length: bad",
            b"Content-Length: 1\r\nContent-Length: 2",
            b"Content-Length: 1\r\nTransfer-Encoding: chunked",
            b"Transfer-Encoding: gzip",
        ]
        responses += [b"HTTP/1.1 200 OK\r\n" + header + b"\r\n\r\n" for header in headers]

        async def scenario():
            for index, response in enumerate(responses):
                with self.subTest(case=index):
                    _, stream = await self.exchange(response, reject=True)
                    self.assertEqual(len(stream.read_sizes), 1)

        asyncio.run(scenario())

    def test_malformed_truncated_and_surplus_framing_fail_closed(self):
        responses = [
            b"not HTTP\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nX: incomplete",
            b"HTTP/1.1 200 OK\r\nBroken header\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nx",
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nxy",
        ]
        chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        responses += [
            chunked + tail
            for tail in (
                b"z\r\nx\r\n0\r\n\r\n",
                b"2\r\nx",
                b"1\r\nx!!",
                b"1\r\nx\r\n",
                b"0\r\n",
                b"0\r\nX-Trailer: rejected\r\n\r\n",
            )
        ]

        async def scenario():
            for index, response in enumerate(responses):
                with self.subTest(case=index):
                    await self.exchange(response, reject=True)

        asyncio.run(scenario())

    def test_complete_incomplete_header_and_body_bounds(self):
        prefix = b"HTTP/1.1 200 OK\r\nX: "
        at_limit = prefix + b"x" * (32768 - len(prefix) - 4) + b"\r\n\r\n"

        async def scenario():
            result, _ = await self.exchange(at_limit)
            self.assertEqual(result, b"")
            result, _ = await self.exchange(b"HTTP/1.1 200 OK\r\n" + b"X: y\r\n" * 64 + b"\r\n")
            self.assertEqual(result, b"")
            responses = [
                at_limit[:-4] + b"x\r\n\r\n",
                prefix + b"x" * 32768,
                b"HTTP/1.1 200 OK\r\n" + b"X: y\r\n" * 65 + b"\r\n",
                b"HTTP/1.1 200 OK\r\n" + b"Content-Length: 0\r\n" * 65 + b"\r\n",
                b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * (MAX_JWKS_BYTES + 1),
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"10001\r\n" + b"x" * (MAX_JWKS_BYTES + 1) + b"\r\n0\r\n\r\n",
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;" + b"x" * 32768,
            ]
            for index, response in enumerate(responses):
                with self.subTest(case=index):
                    await self.exchange(response, reject=True)

        asyncio.run(scenario())

    def test_wire_limit_counts_chunk_extensions(self):
        prefix = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"

        def response(total):
            # Three small body chunks with bounded but large chunk extensions.
            overhead = len(prefix) + 3 * len(b"1;x=\r\nx\r\n") + len(b"0\r\n\r\n")
            padding = total - overhead
            sizes = [padding // 3, padding // 3, padding - 2 * (padding // 3)]
            return prefix + b"".join(b"1;x=" + b"a" * size + b"\r\nx\r\n" for size in sizes) + b"0\r\n\r\n"

        async def scenario():
            at_limit = response(MAX_JWKS_WIRE_BYTES)
            self.assertEqual(len(at_limit), MAX_JWKS_WIRE_BYTES)
            result, _ = await self.exchange(at_limit)
            self.assertEqual(result, b"xxx")
            await self.exchange(response(MAX_JWKS_WIRE_BYTES + 1), reject=True)

        asyncio.run(scenario())

    def test_io_failures_and_whole_operation_deadline(self):
        async def scenario():
            await self.exchange(b"", reject=True, receive_error=OSError("synthetic upstream private text"))
            with patch("anyio.connect_tcp", side_effect=OSError("synthetic TLS failure")) as connector:
                with self.assertRaisesRegex(AuthError, "^authentication_required$"):
                    await fetch_public_keys(AUTH.team_domain)
                connector.assert_awaited_once()
            for stage in ("connect", "send", "receive", "close"):
                stream = self.Stream(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", 4096)

                async def stall(*args):
                    await asyncio.Future()  # Cancellation only, no sleep or external IO.

                if stage == "send":
                    stream.send = stall
                elif stage == "receive":
                    stream.receive = stall
                # Special methods are looked up on the type, so use a subclass for close.
                if stage == "close":

                    class SlowClose(self.Stream):
                        async def __aexit__(self, *args):
                            self.closed = True
                            await stall()

                    stream = SlowClose(stream.remaining, 4096)

                async def connect(*args, stage=stage, stream=stream, **kwargs):
                    if stage == "connect":
                        await stall()
                    return stream

                with (
                    self.subTest(stage=stage),
                    patch("anyio.connect_tcp", side_effect=connect) as connector,
                    patch("brigade.miller.access_auth.FETCH_SECONDS", 0),
                ):
                    with self.assertRaisesRegex(AuthError, "^authentication_required$"):
                        await fetch_public_keys(AUTH.team_domain)
                    connector.assert_awaited_once()
                if stage != "connect":
                    self.assertTrue(stream.closed)

        asyncio.run(scenario())


class SignatureChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Isolated ephemeral test signing keys. Never persisted or printed.
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.public = dict(kid="fixture", kty=public["kty"], alg="RS256", use="sig", n=public["n"], e=public["e"])

    def token(self, *, changes=None, remove=(), key=None, algorithm="RS256", kid="fixture"):
        now = int(datetime.now(timezone.utc).timestamp())
        claims = dict(
            iss=AUTH.issuer,
            aud=[AUTH.audience],
            sub="synthetic-human-subject",
            email=AUTH.approved_email,
            type="app",
            iat=now - 10,
            nbf=now - 10,
            exp=now + 300,
        )
        claims.update(changes or {})
        for name in remove:
            claims.pop(name)
        return jwt.encode(claims, self.key if key is None else key, algorithm=algorithm, headers={"kid": kid})

    def verifier(self, payload=None, *, clock=None):
        calls = []
        data = json.dumps({"keys": [self.public]}).encode() if payload is None else payload

        async def fetch():
            calls.append(True)
            return data

        return AccessVerifier(AUTH, fetch=fetch, monotonic=clock or (lambda: 1000)), calls

    async def verify(self, verifier, token, extra=()):
        return await verifier.verify({"headers": [(b"cf-access-jwt-assertion", token.encode("ascii")), *extra]})

    def test_real_signature_returns_stable_email_owner(self):
        async def scenario():
            verifier, calls = self.verifier()
            self.assertEqual(await self.verify(verifier, self.token()), AUTH.approved_email)
            self.assertEqual(
                await self.verify(verifier, self.token(changes={"sub": "another-human"})), AUTH.approved_email
            )
            now = int(datetime.now(timezone.utc).timestamp())
            self.assertEqual(
                await self.verify(verifier, self.token(changes={"iat": now - 100, "nbf": now - 100, "exp": now - 10})),
                AUTH.approved_email,
            )  # Bounded 30-second tolerance.
            self.assertEqual(len(calls), 1)

        asyncio.run(scenario())

    def test_signed_claims_identity_time_and_wrong_signature_fail_closed(self):
        now = int(datetime.now(timezone.utc).timestamp())
        changes = [
            dict(iss=AUTH.issuer + "/"),
            dict(aud=["b" * 64]),
            dict(aud=[AUTH.audience, "b" * 64]),
            dict(email="foreign@example.invalid"),
            dict(sub=""),
            dict(sub=42),
            dict(type="service"),
            dict(type="org"),
            dict(common_name="service"),
            dict(exp=now - 120),
            dict(nbf=now + 120),
            dict(iat=now + 120),
            dict(iat=True),
            dict(exp=str(now + 300)),
            dict(nbf=None),
            dict(exp=now - 5, iat=now + 5, nbf=now - 10),
        ]
        tokens = [self.token(changes=value) for value in changes]
        # A token without an explicit `type: app` is not an application token (Cloudflare uses `org` for sessions).
        tokens += [self.token(remove=(name,)) for name in ("iss", "aud", "sub", "email", "exp", "nbf", "iat", "type")]
        tokens.append(self.token(key=self.other_key))

        async def scenario():
            verifier, _ = self.verifier()
            for token in tokens:
                with self.subTest(token_index=tokens.index(token)), self.assertRaises(AuthError):
                    await self.verify(verifier, token)

        asyncio.run(scenario())

    def test_algorithms_headers_and_assertion_bounds_reject_before_fetch(self):
        async def scenario():
            verifier, calls = self.verifier()
            tokens = [
                self.token(key="synthetic-hmac-secret-long-enough-for-fixture-only", algorithm="HS256"),
                self.token(key="", algorithm="none"),
                self.token(kid="../remote"),
                "x" * (MAX_ASSERTION_BYTES + 1),
                "bad.token",
            ]
            good = self.token()
            for token in tokens:
                with self.assertRaises(AuthError):
                    await self.verify(verifier, token)
            with self.assertRaises(AuthError):
                await self.verify(verifier, good, [(b"Cf-Access-Jwt-Assertion", good.encode())])
            for headers in (
                [],
                [(b"authorization", good.encode())],
                [(b"cf-access-authenticated-user-email", AUTH.approved_email.encode())],
                [(b"cf-access-jwt-assertion", b"\xff")],
                [(b"cf-access-jwt-assertion", good.encode()), (b"x", b"z" * 32768)],
                [(b"cf-access-jwt-assertion", good.encode())] + [(b"x", b"z")] * 64,
            ):
                with self.assertRaises(AuthError):
                    await verifier.verify({"headers": headers})
            remote = jwt.encode(
                dict(iss=AUTH.issuer),
                self.key,
                algorithm="RS256",
                headers={"kid": "fixture", "jku": "https://example.invalid/keys"},
            )
            with self.assertRaises(AuthError):
                await self.verify(verifier, remote)
            self.assertEqual(calls, [])

        asyncio.run(scenario())

    def test_malformed_duplicate_private_and_oversized_key_sets_deny(self):
        payloads = [
            b"{}",
            b"[]",
            b'{"keys":[],"keys":[]}',
            b'{"keys":[]}',
            b"x" * (MAX_JWKS_BYTES + 1),
            json.dumps({"keys": [self.public] * 9}).encode(),
            json.dumps({"keys": [self.public, self.public]}).encode(),
            json.dumps({"keys": [dict(self.public, kty="oct")]}).encode(),
            json.dumps({"keys": [dict(self.public, alg="HS256")]}).encode(),
            json.dumps({"keys": [dict(self.public, use="enc")]}).encode(),
            json.dumps({"keys": [dict(self.public, d="private-field")]}).encode(),
            json.dumps({"keys": [dict(self.public, n="!")]}).encode(),
        ]

        async def scenario():
            for index, payload in enumerate(payloads):
                verifier, calls = self.verifier(payload)
                with self.subTest(keyset_index=index), self.assertRaises(AuthError):
                    await self.verify(verifier, self.token())
                self.assertEqual(len(calls), 1)

        asyncio.run(scenario())

    def test_unknown_kid_refresh_rate_cache_expiry_and_fetch_failure(self):
        async def scenario():
            now = [1000]
            verifier, calls = self.verifier(clock=lambda: now[0])
            good, unknown = self.token(), self.token(kid="unknown")
            await self.verify(verifier, good)
            for _ in range(3):
                with self.assertRaises(AuthError):
                    await self.verify(verifier, unknown)
            self.assertEqual(len(calls), 1)
            now[0] += REFRESH_INTERVAL
            with self.assertRaises(AuthError):
                await self.verify(verifier, unknown)
            self.assertEqual(len(calls), 2)
            await self.verify(verifier, good)
            now[0] += CACHE_TTL

            async def failed_fetch():
                raise OSError("synthetic private diagnostic")

            verifier.fetch = failed_fetch
            with self.assertRaises(AuthError) as error:
                await self.verify(verifier, good)
            self.assertEqual(str(error.exception), "authentication_required")
            with self.assertRaises(AuthError):
                await self.verify(verifier, good)  # Never use expired cached keys.

        asyncio.run(scenario())

    def test_concurrent_checks_share_fetch_and_rotation_recovers_new_kid(self):
        async def scenario():
            now, payload, calls = [1000], [{"keys": [self.public]}], []

            async def fetch():
                calls.append(True)
                return json.dumps(payload[0]).encode()

            verifier = AccessVerifier(AUTH, fetch=fetch, monotonic=lambda: now[0])
            token = self.token()
            owners = await asyncio.gather(*(self.verify(verifier, token) for _ in range(4)))
            self.assertEqual(owners, [AUTH.approved_email] * 4)
            self.assertEqual(len(calls), 1)
            now[0] += REFRESH_INTERVAL
            public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.other_key.public_key()))
            payload[0] = {
                "keys": [dict(kid="rotated", kty=public["kty"], alg="RS256", use="sig", n=public["n"], e=public["e"])]
            }
            self.assertEqual(
                await self.verify(verifier, self.token(kid="rotated", key=self.other_key)), AUTH.approved_email
            )
            self.assertEqual(len(calls), 2)

        asyncio.run(scenario())


class CompositionChecks(unittest.TestCase):
    def test_metadata_uses_configured_resource_origin(self):
        verifier = AccessVerifier(AUTH)
        self.assertEqual(verifier.resource, AUTH.resource)
        self.assertEqual(verifier.authorization_server, "https://miller.example.test")
        self.assertEqual(verifier.metadata_url, verifier.authorization_server + METADATA_PATH)

    def test_unauthenticated_request_denied_without_fetch(self):
        async def forbidden():
            raise AssertionError("unauthenticated request fetched keys")

        async def scenario():
            verifier = AccessVerifier(AUTH, fetch=forbidden)
            with self.assertRaisesRegex(AuthError, "^authentication_required$"):
                await verifier.verify({"headers": []})

        asyncio.run(scenario())
