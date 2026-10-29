"""Bounded transport uses real signed identity and durable custody fixtures."""

import importlib
import math

import pytest


def test_transport_import_and_finite_budget():
    transport = importlib.import_module("brigade.fleet_mesh_transport")
    for timeout in (True, 0, -1, math.inf, math.nan):
        with pytest.raises(transport.MeshTransportError) as caught:
            transport._Budget(timeout)
        assert caught.value.code == "mesh_transport_timeout"
        assert caught.value.remote_effect == "none"


@pytest.fixture
def transport():
    return importlib.import_module("brigade.fleet_mesh_transport")


@pytest.fixture
def server():
    """Disposable real HTTP server; callback controls only the synthetic Hub."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import json
    import threading

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond(None)

        def do_POST(self):
            self.respond(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def respond(self, body):
            srv.calls.append((self.command, self.path, self.headers.get("Authorization"), body))
            status, value = srv.callback(body)
            if status == 0:
                self.close_connection = True
                return
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", srv.origin + "/redirect-target")
            self.end_headers()
            try:
                self.wfile.write(value if isinstance(value, bytes) else json.dumps(value).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("localhost", 0), Handler)
    srv.origin = "http://localhost:" + str(srv.server_port)
    srv.calls = []
    srv.callback = lambda body: (200, {})
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join()


def authority_for(monkeypatch, server):
    from brigade import fleet_client
    from brigade.fleet_client_message_credentials import MessageAuthority
    from brigade import fleet_client_message_credentials as credentials

    monkeypatch.setattr(
        credentials, "message_settings", lambda *args, **kwargs: (server.origin, "t" * 40, "local-spool")
    )
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {})
    return MessageAuthority(server.origin, "t" * 40, "local-spool", token_file="explicit-fixture")


def signed_identity(origin):
    import hashlib
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from brigade import fleet_mesh_crypto as crypto
    from brigade import fleet_mesh_statements as statements

    seed = bytes(range(32))
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    hub = "h_" + crypto.encode_binary(hashlib.sha256(crypto.LABELS["hub-id"] + public).digest()[:16])
    value = {
        "v": 1,
        "hub_id": hub,
        "hub_sig_pub": crypto.encode_binary(public),
        "endpoint": origin,
        "require_sealed": False,
        "max_envelope_bytes": 98304,
        "envelope_versions": [1],
        "suites": ["bm1"],
        "backend": "full",
        "capabilities": ["messages"],
        "created_at": "2026-01-02T00:00:00Z",
        "seq": 1,
    }
    return statements.sign_statement(value, seed, expected_kind="hub-identity")


@pytest.fixture
def prepared(tmp_path, server, monkeypatch, transport):
    from brigade import dirfd
    from brigade import fleet_mesh_crypto as crypto
    from brigade import fleet_mesh_hub_identity as identity
    from brigade import fleet_mesh_keys as keys
    from brigade import fleet_mesh_trust as trust

    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    authority = authority_for(monkeypatch, server)
    if not dirfd.posix_available():
        with pytest.raises(crypto.MeshError, match="platform_unsupported"):
            transport.observe_hub_identity(brigade_home=home, authority=authority)
        return None
    identity.initialize_identity_store(brigade_home=home)
    signed = signed_identity(server.origin)
    server.callback = lambda body: (200, signed)
    transport.observe_hub_identity(brigade_home=home, authority=authority)
    identity.confirm_identity(
        brigade_home=home, origin=server.origin, expected_hub_id=signed["hub_id"], source="operator"
    )
    trust.initialize_trust(brigade_home=home)
    state = keys.load_local_keys(brigade_home=home, principal="node-a", hub_id=signed["hub_id"], create=True)
    server.calls.clear()
    return home, authority, state, signed


def entry(record):
    return {
        "key": {
            "principal_id": record["principal"],
            "kid": record["kid"],
            "record": record,
            "state": "active",
            "created_at": record["created_at"],
            "retired_at": None,
            "revoked_at": None,
            "compromised_from": None,
        }
    }


def publish(transport, prepared, **kwargs):
    home, authority, _, _ = prepared
    return transport.publish_active_key(brigade_home=home, authority=authority, expected_principal="node-a", **kwargs)


@pytest.mark.parametrize(
    "raw",
    [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b"\xff", b"[]", b'{"a":' + b"[" * 20 + b"0" + b"]" * 20 + b"}"],
)
def test_strict_json(transport, raw):
    with pytest.raises(transport.MeshTransportError, match="mesh_transport_response"):
        transport._decode(raw)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("body", [None, b'{"action":"identity"}'])
def test_redirects_never_follow(transport, server, status, body, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.invalid")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    server.callback = lambda body: (status, {})
    result, _ = transport._wire(
        origin=server.origin, token=None if body is None else "fixture-token", body=body, timeout=2, limit=65536
    )
    assert result == status
    assert len(server.calls) == 1
    if body is None:
        assert server.calls[0][2] is None


@pytest.mark.parametrize("status", [200, 400])
def test_oversized_success_and_error_closed(transport, server, status):
    from brigade.fleet_client import FleetClientError

    server.callback = lambda body: (status, b'{"error":"' + b"x" * 65536 + b'"}')
    with pytest.raises(FleetClientError, match="size limit"):
        transport._wire(origin=server.origin, token=None, body=None, timeout=2, limit=65536)


def test_observation_does_not_confirm_or_initialize(transport, server, tmp_path, monkeypatch):
    from brigade import dirfd
    from brigade import fleet_mesh_crypto as crypto
    from brigade import fleet_mesh_hub_identity as identity

    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    authority = authority_for(monkeypatch, server)
    if not dirfd.posix_available():
        with pytest.raises(crypto.MeshError, match="platform_unsupported"):
            transport.observe_hub_identity(brigade_home=home, authority=authority)
        return
    identity.initialize_identity_store(brigade_home=home)
    signed = signed_identity(server.origin)
    server.callback = lambda body: (200, signed)

    def forbidden(**kwargs):
        raise AssertionError("implicit initialization or confirmation")

    monkeypatch.setattr(identity, "initialize_identity_store", forbidden)
    monkeypatch.setattr(identity, "confirm_identity", forbidden)
    assert transport.observe_hub_identity(brigade_home=home, authority=authority).status == "accepted"
    assert identity.load_identity(brigade_home=home, origin=server.origin).provenance == "observed"
    assert server.calls[0][2] is None
    server.callback = lambda body: (200, {"identity": signed, "migration": {}})
    with pytest.raises(transport.MeshTransportError):
        transport.observe_hub_identity(brigade_home=home, authority=authority)


@pytest.mark.parametrize("mode", ["publish", "existing", "lost", "conflict"])
def test_real_custody_publication_and_reconciliation(transport, prepared, server, mode, monkeypatch):
    if prepared is None:
        return
    import json
    import threading
    from brigade import fleet_mesh_keys as keys

    home, _, state, _ = prepared
    original = {p: p.read_bytes() for p in (home / "fleet" / "mesh-keys").iterdir() if p.is_file()}
    saved = entry(state.active.record) if mode == "existing" else None
    writes = []
    main_thread = threading.current_thread()
    original_load = keys.load_local_keys

    def load(**kwargs):
        assert threading.current_thread() is main_thread
        assert kwargs.get("create") is False
        return original_load(**kwargs)

    monkeypatch.setattr(keys, "load_local_keys", load)

    def callback(body):
        nonlocal saved
        if body["action"] == "identity":
            return 200, {"principal": "node-a"}
        if body["action"] == "keys_get":
            return (200, saved) if saved else (404, {"error": "key not found"})
        writes.append(body)
        saved = entry(body["record"])
        return (0, {}) if mode == "lost" else (409, {}) if mode == "conflict" else (201, saved)

    server.callback = callback
    result = publish(transport, prepared)
    assert (
        result.disposition
        == {"publish": "published", "existing": "already_active", "lost": "reconciled", "conflict": "reconciled"}[mode]
    )
    assert len(writes) == (mode != "existing")
    assert result.record == state.active.record and result.record is not state.active.record
    assert "private" not in json.dumps(writes)
    assert state.retry_key.hex() not in json.dumps(writes)
    assert original == {p: p.read_bytes() for p in original}
    assert publish(transport, prepared).disposition == "already_active"


@pytest.mark.parametrize(
    "change", ["state", "record", "principal_id", "created_at", "retired_at", "revoked_at", "compromised_from", "extra"]
)
def test_same_kid_is_insufficient(transport, prepared, server, change):
    if prepared is None:
        return
    value = entry(prepared[2].active.record)
    value["key"][change] = "wrong"
    server.callback = lambda body: (200, {"principal": "node-a"}) if body["action"] == "identity" else (200, value)
    with pytest.raises(transport.MeshTransportError, match="mesh_transport_key"):
        publish(transport, prepared)
    assert not any(call[3]["action"] == "keys_publish" for call in server.calls)


def test_expected_principal_is_only_a_guard(transport, prepared, server):
    if prepared is None:
        return
    server.callback = lambda body: (200, {"principal": "node-b"})
    with pytest.raises(transport.MeshTransportError, match="mesh_transport_principal"):
        publish(transport, prepared)
    assert len(server.calls) == 1


def test_late_post_can_commit_after_timeout(transport, prepared, server):
    if prepared is None:
        return
    import threading

    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    saved = None

    def callback(body):
        nonlocal saved
        if body["action"] == "identity":
            return 200, {"principal": "node-a"}
        if body["action"] == "keys_get":
            return (200, saved) if saved else (404, {"error": "key not found"})
        entered.set()
        release.wait(3)
        saved = entry(body["record"])
        committed.set()
        return 201, saved

    server.callback = callback
    try:
        with pytest.raises(transport.MeshTransportError) as caught:
            publish(transport, prepared, timeout=1)
        assert entered.is_set()
        assert caught.value.remote_effect == "possible"
    finally:
        release.set()
    assert committed.wait(3)
    assert publish(transport, prepared).disposition == "already_active"
    assert sum(call[3]["action"] == "keys_publish" for call in server.calls) == 1


def pin_anchor(home, hub):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from brigade import fleet_mesh_crypto as crypto
    from brigade import fleet_mesh_trust as trust

    public = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32)))).public_key().public_bytes_raw()
    return trust.pin_manifest_anchor(
        brigade_home=home,
        hub=hub,
        public_key_file=crypto.encode_binary(public).encode(),
        expected_fingerprint=crypto.fingerprint(public),
        expected_current_signer=None,
    )


def test_bundle_requires_independent_anchor_and_prevalidates_all(transport, prepared, server):
    if prepared is None:
        return
    from brigade import fleet_mesh_crypto as crypto
    from brigade import fleet_mesh_statements as statements
    from brigade import fleet_mesh_trust as trust

    home, authority, _, signed = prepared

    def run():
        return transport.import_hub_statements(brigade_home=home, authority=authority, expected_principal="node-a")

    with pytest.raises(transport.MeshTransportError, match="mesh_transport_anchor"):
        run()
    assert not server.calls
    pin_anchor(home, signed["hub_id"])

    def statement(kind):
        return statements.sign_statement(
            {
                "v": 1,
                "hub": signed["hub_id"],
                "seq": 1,
                "created_at": "2026-01-02T00:00:00Z",
                "members" if kind == "membership" else "revocations": [],
            },
            bytes(reversed(range(32))),
            expected_kind=kind,
        )

    membership = statement("membership")
    bundle = {"membership": membership, "revocation": {"invalid": "statement"}}
    server.callback = lambda body: (
        (200, {"principal": "node-a"}) if body["action"] == "identity" else (200, {"statements": bundle})
    )
    baseline = trust.load_trust(brigade_home=home)
    with pytest.raises(crypto.MeshError):
        run()
    assert trust.load_trust(brigade_home=home) == baseline
    bundle["revocation"] = statement("revocation")
    assert [result.status for result in run().values()] == ["accepted", "accepted"]
    assert [result.status for result in run().values()] == ["replay", "replay"]


@pytest.mark.parametrize("point", ["lookup", "publish", "final_identity"])
@pytest.mark.parametrize("drift", ["credential", "trust", "custody", "identity"])
def test_observable_drift_refuses_with_honest_effect(transport, prepared, server, point, drift, monkeypatch):
    if prepared is None:
        return
    from brigade import fleet_mesh_hub_identity as identity
    from brigade import fleet_mesh_keys as keys
    from brigade import fleet_client_message_credentials as credentials

    home, _, state, signed = prepared
    saved, identity_calls = None, 0

    def change():
        if drift == "credential":
            monkeypatch.setattr(
                credentials, "message_settings", lambda *args, **kwargs: (server.origin, "changed" * 8, "local-spool")
            )
        elif drift == "trust":
            pin_anchor(home, signed["hub_id"])
        elif drift == "custody":
            keys.rotate_local_keys(brigade_home=home, principal="node-a", hub_id=signed["hub_id"])
        else:
            identity.confirm_identity(
                brigade_home=home, origin=server.origin, expected_hub_id=signed["hub_id"], source="invite"
            )

    def callback(body):
        nonlocal saved, identity_calls
        if body["action"] == "identity":
            identity_calls += 1
            if point == "final_identity" and identity_calls == 3:
                change()
            return 200, {"principal": "node-a"}
        if body["action"] == "keys_get":
            if point == "lookup":
                change()
            return 404, {"error": "key not found"}
        saved = entry(state.active.record)
        if point == "publish":
            change()
        return 201, saved

    server.callback = callback
    with pytest.raises(transport.MeshTransportError) as caught:
        publish(transport, prepared)
    assert caught.value.code == "mesh_transport_drift"
    assert caught.value.remote_effect == ("none" if point == "lookup" else "confirmed")
    assert sum(call[3]["action"] == "keys_publish" for call in server.calls) == (point != "lookup")


@pytest.mark.parametrize(
    "origin",
    [
        "https://hub.example/path",
        "https://user@hub.example",  # content-guard: allow pii/email
        "https://hub.example?q=1",
        "http://hub.example",
    ],
)
def test_invalid_endpoint_before_network(transport, tmp_path, monkeypatch, origin):
    from brigade.fleet_client_message_credentials import MessageAuthority
    from brigade import fleet_client_message_credentials as credentials

    monkeypatch.setattr(credentials, "message_settings", lambda *args, **kwargs: (origin, "t" * 40, "local"))
    authority = MessageAuthority(origin, "t" * 40, "local", token_file="explicit")
    with pytest.raises(transport.MeshTransportError):
        transport.observe_hub_identity(brigade_home=tmp_path, authority=authority)


def test_implicit_authority_refused(transport, tmp_path):
    from brigade.fleet_client_message_credentials import MessageAuthority

    authority = MessageAuthority("https://hub.example", "t" * 40, "local")
    with pytest.raises(transport.MeshTransportError, match="mesh_transport_authority"):
        transport.observe_hub_identity(brigade_home=tmp_path, authority=authority)
    assert "tttt" not in repr(authority)


def test_missing_crypto_and_native_platform_are_explicit(transport, monkeypatch, tmp_path, server):
    from brigade import fleet_mesh_crypto as crypto
    from brigade import fleet_mesh_hub_identity as identity

    authority = authority_for(monkeypatch, server)

    def missing():
        raise crypto.MeshExtraMissing()

    monkeypatch.setattr(crypto, "probe_backend", missing)
    with pytest.raises(crypto.MeshExtraMissing):
        transport.observe_hub_identity(brigade_home=tmp_path, authority=authority)
    monkeypatch.setattr(crypto, "probe_backend", lambda: None)
    from types import SimpleNamespace

    monkeypatch.setattr(identity, "os", SimpleNamespace(name="nt"))
    with pytest.raises(crypto.MeshError, match="mesh_identity_platform_unsupported"):
        transport.observe_hub_identity(brigade_home=tmp_path, authority=authority)
    assert not server.calls


def test_partial_import_retains_earlier_durable_result(transport, prepared, server, monkeypatch):
    if prepared is None:
        return
    from brigade import fleet_mesh_crypto as crypto
    from brigade import fleet_mesh_statements as statements
    from brigade import fleet_mesh_trust as trust

    home, authority, _, signed = prepared
    pin_anchor(home, signed["hub_id"])
    bundle = {}
    for kind in ("membership", "revocation"):
        bundle[kind] = statements.sign_statement(
            {
                "v": 1,
                "hub": signed["hub_id"],
                "seq": 1,
                "created_at": "2026-01-02T00:00:00Z",
                "members" if kind == "membership" else "revocations": [],
            },
            bytes(reversed(range(32))),
            expected_kind=kind,
        )
    server.callback = lambda body: (
        (200, {"principal": "node-a"}) if body["action"] == "identity" else (200, {"statements": bundle})
    )
    original = trust.import_statement

    def fail_second(**kwargs):
        if kwargs["kind"] == "revocation":
            raise crypto.MeshError("mesh_trust_storage_failure")
        return original(**kwargs)

    monkeypatch.setattr(trust, "import_statement", fail_second)
    with pytest.raises(transport.MeshTransportError, match="mesh_transport_partial_import"):
        transport.import_hub_statements(brigade_home=home, authority=authority, expected_principal="node-a")
    assert len(trust.load_trust(brigade_home=home).data["objects"]) == 1
    monkeypatch.setattr(trust, "import_statement", original)
    results = transport.import_hub_statements(brigade_home=home, authority=authority, expected_principal="node-a")
    assert [r.status for r in results.values()] == ["replay", "accepted"]


@pytest.mark.parametrize("failure", ["missing", "unsafe", "barred"])
def test_missing_unsafe_or_barred_custody_cannot_publish(transport, prepared, server, failure):
    if prepared is None:
        return
    from brigade import fleet_mesh_crypto as crypto
    from brigade import fleet_mesh_trust as trust

    home, _, state, signed = prepared
    custody = [
        p
        for p in (home / "fleet" / "mesh-keys").glob("*.json")
        if p.name not in {"hub-identity-state.json", "trust-state.json"}
    ]
    if failure == "missing":
        for path in custody:
            path.unlink()
    elif failure == "unsafe":
        for path in custody:
            path.chmod(0o644)
    else:
        trust.record_hub_revocation(
            brigade_home=home,
            hub=signed["hub_id"],
            principal="node-a",
            kid=state.active.record["kid"],
            cutoff=state.active.record["created_at"],
        )
    server.callback = lambda body: (200, {"principal": "node-a"})
    with pytest.raises((crypto.MeshError, transport.MeshTransportError)):
        publish(transport, prepared)
    assert all(call[3]["action"] == "identity" for call in server.calls)


def test_token_file_drift_is_detected_by_real_authority(transport, prepared, server, tmp_path, monkeypatch):
    if prepared is None:
        return
    from brigade import fleet_client
    from brigade import fleet_client_message_credentials as credentials

    home, _, _, _ = prepared
    path = tmp_path / "token"
    path.write_text("s" * 40)
    path.chmod(0o600)
    monkeypatch.undo()
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": server.origin})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "local-spool")
    authority = credentials.MessageAuthority.capture(str(path))

    def callback(body):
        path.unlink()
        return 200, {"principal": "node-a"}

    server.callback = callback
    with pytest.raises(transport.MeshTransportError, match="mesh_transport_drift"):
        transport.publish_active_key(brigade_home=home, authority=authority, expected_principal="node-a")
    assert len(server.calls) == 1


def test_import_does_not_load_optional_crypto_backend(tmp_path):
    import subprocess
    import sys

    code = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname == "cryptography" or fullname.startswith("cryptography."):
            raise ImportError("blocked optional backend")
sys.meta_path.insert(0, Block())
import brigade.fleet_mesh_transport
assert "cryptography" not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_truncated_publish_response_reconciles_without_republish(transport, prepared, server, monkeypatch):
    if prepared is None:
        return
    import http.client
    import json

    saved = None

    def callback(body):
        nonlocal saved
        if body["action"] == "identity":
            return 200, {"principal": "node-a"}
        if body["action"] == "keys_get":
            return (200, saved) if saved else (404, {"error": "key not found"})
        saved = entry(body["record"])
        return 201, saved

    server.callback = callback
    original = transport._wire

    def truncated(**kwargs):
        result = original(**kwargs)
        if kwargs["body"] is not None and json.loads(kwargs["body"])["action"] == "keys_publish":
            raise http.client.IncompleteRead(b"", 1)
        return result

    monkeypatch.setattr(transport, "_wire", truncated)
    assert publish(transport, prepared).disposition == "reconciled"
    assert sum(call[3]["action"] == "keys_publish" for call in server.calls) == 1


def test_observation_refuses_concurrent_confirmation(transport, prepared, server, monkeypatch):
    if prepared is None:
        return
    from brigade import fleet_mesh_hub_identity as identity

    home, authority, _, signed = prepared
    original = identity.observe_identity

    def competing(**kwargs):
        result = original(**kwargs)
        identity.confirm_identity(
            brigade_home=home, origin=server.origin, expected_hub_id=signed["hub_id"], source="invite"
        )
        return result

    monkeypatch.setattr(identity, "observe_identity", competing)
    server.callback = lambda body: (200, signed)
    with pytest.raises(transport.MeshTransportError, match="mesh_transport_drift"):
        transport.observe_hub_identity(brigade_home=home, authority=authority)
    assert identity.load_identity(brigade_home=home, origin=server.origin).provenance == "verified"


def test_authenticated_agent_uses_own_custody_not_spool_identity(transport, prepared, server):
    if prepared is None:
        return
    from brigade import fleet_mesh_keys as keys

    home, authority, _, signed = prepared
    own = keys.load_local_keys(brigade_home=home, principal="agent:owner", hub_id=signed["hub_id"], create=True)
    saved = None

    def callback(body):
        nonlocal saved
        if body["action"] == "identity":
            return 200, {"principal": "agent:owner"}
        if body["action"] == "keys_get":
            return (200, saved) if saved else (404, {"error": "key not found"})
        saved = entry(body["record"])
        return 201, saved

    server.callback = callback
    result = transport.publish_active_key(brigade_home=home, authority=authority, expected_principal="agent:owner")
    assert result.record == own.active.record
    assert result.principal == "agent:owner"


@pytest.mark.parametrize("readback", ["missing", "retired", "revoked", "different"])
def test_conflict_requires_exact_active_readback(transport, prepared, server, readback):
    if prepared is None:
        return
    published = False
    value = entry(prepared[2].active.record)
    if readback in ("retired", "revoked"):
        value["key"]["state"] = readback
    elif readback == "different":
        value["key"]["record"] = {}

    def callback(body):
        nonlocal published
        if body["action"] == "identity":
            return 200, {"principal": "node-a"}
        if body["action"] == "keys_get":
            return (200, value) if published and readback != "missing" else (404, {"error": "key not found"})
        published = True
        return 409, {"error": "conflict"}

    server.callback = callback
    with pytest.raises(transport.MeshTransportError) as caught:
        publish(transport, prepared)
    assert caught.value.remote_effect == "possible"
    assert sum(call[3]["action"] == "keys_publish" for call in server.calls) == 1


@pytest.mark.parametrize("unsupported", ["manifest", "future-kind"])
def test_unsupported_bundle_refuses_before_any_local_import(transport, prepared, server, monkeypatch, unsupported):
    """A three-kind Hub bundle must not advance membership while blocking revocations."""
    if prepared is None:
        return
    from brigade import fleet_mesh_statements as statements
    from brigade import fleet_mesh_trust as trust

    home, authority, _, signed = prepared
    pin_anchor(home, signed["hub_id"])
    bundle = {}
    for kind, field in (("membership", "members"), ("manifest", "keys"), ("revocation", "revocations")):
        bundle[kind] = statements.sign_statement(
            {"v": 1, "hub": signed["hub_id"], "seq": 1, "created_at": "2026-01-02T00:00:00Z", field: []},
            bytes(reversed(range(32))),
            expected_kind=kind,
        )
    if unsupported != "manifest":
        bundle[unsupported] = bundle.pop("manifest")
    server.callback = lambda body: (
        (200, {"principal": "node-a"}) if body["action"] == "identity" else (200, {"statements": bundle})
    )
    baseline = trust.load_trust(brigade_home=home)
    files = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    imports = []
    original = trust.import_statement

    def record_import(**kwargs):
        imports.append(kwargs["kind"])
        return original(**kwargs)

    monkeypatch.setattr(trust, "import_statement", record_import)
    for _ in range(2):
        with pytest.raises(transport.MeshTransportError) as caught:
            transport.import_hub_statements(brigade_home=home, authority=authority, expected_principal="node-a")
        assert imports == []
        assert trust.load_trust(brigade_home=home) == baseline
        assert {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()} == files
        assert caught.value.code == "mesh_transport_statements_unsupported"
        assert caught.value.remote_effect == "none"


@pytest.mark.parametrize("status", [200, 400])
def test_operation_reports_oversized_reply_as_response_refusal(transport, prepared, server, status):
    if prepared is None:
        return
    home, authority, _, _ = prepared
    server.callback = lambda body: (status, b'{"error":"' + b"x" * 65536 + b'"}')
    with pytest.raises(transport.MeshTransportError) as caught:
        transport.observe_hub_identity(brigade_home=home, authority=authority)
    assert caught.value.code == "mesh_transport_response"
    assert caught.value.remote_effect == "none"


def test_first_import_failure_preserves_fixed_cause(transport, prepared, server):
    if prepared is None:
        return
    from brigade import fleet_mesh_crypto as crypto
    from brigade import fleet_mesh_statements as statements
    from brigade import fleet_mesh_trust as trust

    home, authority, _, signed = prepared
    pin_anchor(home, signed["hub_id"])
    membership = statements.sign_statement(
        {"v": 1, "hub": signed["hub_id"], "seq": 1, "created_at": "2026-01-02T00:00:00Z", "members": []},
        bytes(reversed(range(32))),
        expected_kind="membership",
    )
    server.callback = lambda body: (
        (200, {"principal": "node-a"})
        if body["action"] == "identity"
        else (200, {"statements": {"membership": membership}})
    )
    newer = statements.sign_statement(
        {"v": 1, "hub": signed["hub_id"], "seq": 2, "created_at": "2026-01-02T00:00:00Z", "members": []},
        bytes(reversed(range(32))),
        expected_kind="membership",
    )
    assert (
        trust.import_statement(
            brigade_home=home, hub=signed["hub_id"], kind="membership", raw=crypto.canonical(newer)
        ).status
        == "accepted"
    )
    baseline = trust.load_trust(brigade_home=home)
    with pytest.raises(crypto.MeshError) as caught:
        transport.import_hub_statements(brigade_home=home, authority=authority, expected_principal="node-a")
    assert caught.value.code == "signed_statement_rollback"
    assert trust.load_trust(brigade_home=home) == baseline


def test_cross_origin_redirect_never_discloses_authority(transport, server):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    hits = []

    class Destination(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            hits.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_GET = do_POST

    destination = ThreadingHTTPServer(("localhost", 0), Destination)
    worker = threading.Thread(target=destination.serve_forever, daemon=True)
    worker.start()
    try:
        # The existing fixture builds Location from its mutable origin; keep
        # the original request bound to the original listener's actual port.
        origin = server.origin
        server.origin = "http://localhost:" + str(destination.server_port)
        server.callback = lambda body: (307, {})
        status, _ = transport._wire(
            origin=origin, token="synthetic-bearer", body=b'{"action":"identity"}', timeout=2, limit=65536
        )
        assert status == 307
        assert len(server.calls) == 1
        assert hits == []
    finally:
        destination.shutdown()
        destination.server_close()
        worker.join()
