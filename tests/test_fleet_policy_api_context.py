"""HTTP regressions for prepared/acknowledged Fleet policy context hashes."""

from __future__ import annotations

import http.client
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from brigade import fleet_hub, fleet_hub_policy, fleet_policy


NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
ADMIN_TOKEN = "test-admin-token-policy-context"
CONTEXT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
ACK_KEYS = {
    "schema",
    "session_id",
    "consumer",
    "repo_identity",
    "version",
    "digest",
    "state",
    "loaded_at",
    "applied",
    "context_hash",
}
PREPARE_KEYS = {
    "schema",
    "version",
    "digest",
    "effective",
    "sources",
    "ack_required",
    "selected",
    "instructions",
    "repo_identity",
    "context_hash",
}


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"roles": {"impl": "seat-alpha"}, "data": {"allow_training": False}},
        "machines": {
            "worker-linux-1": {"os": "linux", "node_id": NODE_A, "concurrency": 2},
            "worker-linux-2": {"os": "linux", "node_id": NODE_B, "concurrency": 2},
        },
        "seats": {
            "seat-alpha": {
                "provider": "provider-a",
                "model": "model-a-1",
                "effort": "high",
                "enabled": True,
                "eligible_machines": ["worker-linux-1", "worker-linux-2"],
                "bindings": {
                    "brigade": {"cli": "cursor-agent"},
                    "t3_fleet": {"instance_id": "cursor", "service_tier": "standard"},
                    "native": {"instance_id": "native/slash-id", "model": "xai/model-a-1"},
                },
            },
            "seat-beta": {
                "provider": "provider-b",
                "model": "model-b-1",
                "effort": "high",
                "enabled": True,
                "eligible_machines": ["worker-linux-1", "worker-linux-2"],
                "bindings": {
                    "native": {"instance_id": "other-native", "model": "model-b-1"},
                },
            },
        },
        "consumers": {
            "t3-code": {"reload": "refreshable", "coverage": "verified"},
            "brigade-run": {"reload": "refreshable", "coverage": "verified"},
        },
        "routing": {
            "enabled": True,
            "telemetry_ttl_seconds": 300,
            "reservation_ttl_seconds": 300,
            "quota_reserve_percent": 4,
            "workload_requirements": {"general": {"os": "linux", "capabilities": []}},
        },
        "repositories": {
            "repo/public": {"privacy": "public"},
            "repo/other": {"privacy": "public"},
        },
    }


@contextmanager
def _hub(tmp_path):
    server = fleet_hub.make_server(
        "127.0.0.1", 0, tmp_path / "fleet.db", ADMIN_TOKEN
    )  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_address[1], tmp_path / "fleet.db"  # content-guard: allow loopback-ipv4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, method: str, path: str, *, token: str | None = None, body: dict | None = None):
    host, port, _db = hub
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection.request(
        method, path, body=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers
    )
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    payload = json.loads(raw) if raw else {}
    connection.close()
    return response.status, payload


def _enroll(hub, node_id: str, label: str = "node") -> str:
    db = fleet_hub.open_db(hub[2])
    try:
        _node, node_token = fleet_hub.add_node(db, node_id, label)
    finally:
        db.close()
    return node_token


def _save_policy(hub, document: dict | None = None) -> dict:
    status, payload = _request(
        hub,
        "POST",
        "/policy",
        token=ADMIN_TOKEN,
        body={
            "action": "save",
            "document": document or _document(),
            "expected_version": 1,
            "reason": "install control-plane policy",
        },
    )
    assert status == 200, payload
    return payload


def _prepare(
    hub,
    token: str,
    *,
    session_id: str,
    consumer: str = "t3-code",
    repo: str = "repo/public",
    origin: str = "local",
    provider: str = "provider-a",
    model: str = "xai/model-a-1",
    instance_id: str = "native/slash-id",
    reasoning: str = "high",
    overrides: dict | None = None,
    override_reason: str | None = None,
):
    body: dict = {
        "action": "prepare",
        "consumer": consumer,
        "repo_identity": repo,
        "session_id": session_id,
        "origin": origin,
        "provider": provider,
        "model": model,
        "instance_id": instance_id,
        "reasoning": reasoning,
    }
    if overrides is not None:
        body["overrides"] = overrides
        body["override_reason"] = override_reason or "bounded test override"
    return _request(hub, "POST", "/policy", token=token, body=body)


def _ack(
    hub,
    token: str,
    prepared: dict,
    *,
    session_id: str,
    consumer: str = "t3-code",
    repo: str = "repo/public",
    context_hash: str | None = ...,
    status: str = "applied",
    reason: str | None = None,
):
    body = {
        "action": "acknowledge",
        "session_id": session_id,
        "consumer": consumer,
        "repo_identity": repo,
        "version": prepared["version"],
        "digest": prepared["digest"],
        "status": status,
    }
    if context_hash is not ...:
        if context_hash is not None:
            body["context_hash"] = context_hash
    elif prepared.get("context_hash"):
        body["context_hash"] = prepared["context_hash"]
    if reason:
        body["reason"] = reason
    return _request(hub, "POST", "/policy", token=token, body=body)


def _loaded_rows(hub, session_id: str) -> list[dict]:
    db = fleet_hub.open_db(hub[2])
    try:
        return [row for row in fleet_hub_policy.list_session_states(db) if row.get("external_session_id") == session_id]
    finally:
        db.close()


def test_prepare_emits_context_hash_and_slash_native_binding(tmp_path):
    with _hub(tmp_path) as hub:
        token = _enroll(hub, NODE_A, "node-a")
        _save_policy(hub)
        status, prepared = _prepare(hub, token, session_id="session-slash")
        assert status == 200, prepared
        assert set(prepared) == PREPARE_KEYS
        assert CONTEXT_HASH.fullmatch(prepared["context_hash"])
        assert prepared["selected"]["model"] == "model-a-1"
        assert prepared["selected"]["launch_model"] == "xai/model-a-1"
        assert prepared["selected"]["instance_id"] == "native/slash-id"
        status, again = _prepare(hub, token, session_id="session-slash")
        assert status == 200, again
        assert again["context_hash"] == prepared["context_hash"]
        assert again["digest"] == prepared["digest"]


def test_same_version_override_or_model_changes_context_hash(tmp_path):
    with _hub(tmp_path) as hub:
        token = _enroll(hub, NODE_A, "node-a")
        saved = _save_policy(hub)
        status, first = _prepare(
            hub,
            token,
            session_id="session-override-a",
            overrides={"execution": {"concurrency": 2}},
            override_reason="cap a",
        )
        assert status == 200, first
        status, second = _prepare(
            hub,
            token,
            session_id="session-override-b",
            overrides={"execution": {"concurrency": 4}},
            override_reason="cap b",
        )
        assert status == 200, second
        assert first["version"] == second["version"] == saved["revision"]
        assert first["digest"] == second["digest"]
        assert first["context_hash"] != second["context_hash"]
        status, other_model = _prepare(
            hub,
            token,
            session_id="session-model-b",
            provider="provider-b",
            model="model-b-1",
            instance_id="other-native",
        )
        assert status == 200, other_model
        assert other_model["version"] == first["version"]
        assert other_model["context_hash"] != first["context_hash"]
        assert other_model["selected"]["model"] == "model-b-1"
        assert "launch_model" not in other_model["selected"]


def test_prepare_hash_ignores_telemetry(tmp_path):
    with _hub(tmp_path) as hub:
        token = _enroll(hub, NODE_A, "node-a")
        _save_policy(hub)
        status, first = _prepare(hub, token, session_id="session-tele")
        assert status == 200, first
        digest = first["digest"]
        stamp = datetime.now(timezone.utc).isoformat()
        status, observed = _request(
            hub,
            "POST",
            "/policy",
            token=token,
            body={
                "action": "telemetry-observe",
                "node_id": NODE_A,
                "observed_at": stamp,
                "ttl_seconds": 300,
                "status": "available",
                "load": 0.1,
                "running": {"session_ids": [], "run_ids": []},
                "active_claims": [],
                "usable_seats": ["seat-alpha"],
                "credential_state": "ok",
            },
        )
        assert status == 200, observed
        status, second = _prepare(hub, token, session_id="session-tele")
        assert status == 200, second
        assert second["digest"] == digest
        assert second["context_hash"] == first["context_hash"]


def test_stale_ack_after_reprepare_refuses_and_correct_ack_persists(tmp_path):
    with _hub(tmp_path) as hub:
        token = _enroll(hub, NODE_A, "node-a")
        _save_policy(hub)
        status, prep_a = _prepare(
            hub,
            token,
            session_id="session-reprepare",
            overrides={"execution": {"concurrency": 2}},
            override_reason="first cap",
        )
        assert status == 200, prep_a
        hash_a = prep_a["context_hash"]
        status, prep_b = _prepare(
            hub,
            token,
            session_id="session-reprepare",
            overrides={"execution": {"concurrency": 8}},
            override_reason="second cap",
        )
        assert status == 200, prep_b
        assert prep_b["version"] == prep_a["version"]
        assert prep_b["context_hash"] != hash_a
        status, stale = _ack(hub, token, prep_a, session_id="session-reprepare", context_hash=hash_a)
        assert status == 400, stale
        assert stale["error"]["code"] == "invalid-request"
        assert _loaded_rows(hub, "session-reprepare") == []
        status, acked = _ack(hub, token, prep_b, session_id="session-reprepare")
        assert status == 200, acked
        assert acked["applied"] is True
        assert acked["state"] == "current"
        assert acked["context_hash"] == prep_b["context_hash"]
        assert set(acked) == ACK_KEYS
        rows = _loaded_rows(hub, "session-reprepare")
        assert len(rows) == 1
        loaded = rows[0]["loaded_snapshot"]
        assert loaded["effective"] == prep_b["effective"]
        assert loaded["sources"] == prep_b["sources"]
        assert loaded["selected"] == prep_b["selected"]
        db = fleet_hub.open_db(hub[2])
        try:
            pending = fleet_hub_policy.get_pending_policy(
                db,
                node_id=NODE_A,
                consumer="t3-code",
                repo_identity="repo/public",
                session_id="session-reprepare",
            )
        finally:
            db.close()
        assert pending["instructions"] == prep_b["instructions"]
        assert loaded["overrides"] == pending["overrides"]
        assert loaded["override_reason"] == pending["override_reason"]


def test_wrong_node_same_external_id_separate_repository(tmp_path):
    with _hub(tmp_path) as hub:
        token_a = _enroll(hub, NODE_A, "node-a")
        token_b = _enroll(hub, NODE_B, "node-b")
        _save_policy(hub)
        status, prep_a = _prepare(hub, token_a, session_id="session-shared", repo="repo/public")
        assert status == 200, prep_a
        status, prep_b = _prepare(hub, token_b, session_id="session-shared", repo="repo/other")
        assert status == 200, prep_b
        assert prep_a["context_hash"] != prep_b["context_hash"]
        status, stolen = _ack(
            hub,
            token_b,
            prep_a,
            session_id="session-shared",
            repo="repo/public",
            context_hash=prep_a["context_hash"],
        )
        assert status == 403, stolen
        status, mismatched = _ack(
            hub,
            token_a,
            prep_b,
            session_id="session-shared",
            repo="repo/other",
            context_hash=prep_b["context_hash"],
        )
        assert status == 403, mismatched
        status, acked_a = _ack(hub, token_a, prep_a, session_id="session-shared", repo="repo/public")
        assert status == 200, acked_a
        assert acked_a["state"] == "current"
        status, acked_b = _ack(hub, token_b, prep_b, session_id="session-shared", repo="repo/other")
        assert status == 200, acked_b
        assert acked_b["state"] == "current"
        assert acked_a["context_hash"] == prep_a["context_hash"]
        assert acked_b["context_hash"] == prep_b["context_hash"]


def test_missing_and_malformed_hash_and_failed_initial_apply(tmp_path):
    with _hub(tmp_path) as hub:
        token = _enroll(hub, NODE_A, "node-a")
        _save_policy(hub)
        status, prepared = _prepare(hub, token, session_id="session-hash-errors")
        assert status == 200, prepared
        status, missing = _ack(hub, token, prepared, session_id="session-hash-errors", context_hash=None)
        assert status == 400, missing
        assert missing["error"]["code"] == "invalid-request"
        status, malformed = _ack(hub, token, prepared, session_id="session-hash-errors", context_hash="not-a-digest")
        assert status == 400, malformed
        assert malformed["error"]["code"] == "invalid-request"
        status, uppercase = _ack(
            hub,
            token,
            prepared,
            session_id="session-hash-errors",
            context_hash="sha256:" + ("A" * 64),
        )
        assert status == 400, uppercase
        assert _loaded_rows(hub, "session-hash-errors") == []
        status, failed = _ack(
            hub,
            token,
            prepared,
            session_id="session-hash-errors",
            context_hash=None,
            status="failed",
            reason="harness rejected selection",
        )
        assert status == 200, failed
        assert failed["applied"] is False
        assert failed["loaded_at"] is None
        assert failed["context_hash"] is None
        assert failed["state"] != "current"
        assert set(failed) == ACK_KEYS
        assert _loaded_rows(hub, "session-hash-errors") == []


def test_duplicate_exact_ack_preserves_first_loaded_at(tmp_path):
    with _hub(tmp_path) as hub:
        token = _enroll(hub, NODE_A, "node-a")
        _save_policy(hub)
        status, prepared = _prepare(hub, token, session_id="session-dup")
        assert status == 200, prepared
        status, first = _ack(hub, token, prepared, session_id="session-dup")
        assert status == 200, first
        assert first["applied"] is True
        first_loaded = first["loaded_at"]
        status, second = _ack(hub, token, prepared, session_id="session-dup")
        assert status == 200, second
        assert second["applied"] is True
        assert second["loaded_at"] == first_loaded
        assert second["context_hash"] == prepared["context_hash"]
        assert second["state"] == "current"


def test_failed_ack_preserves_prior_loaded_snapshot(tmp_path):
    with _hub(tmp_path) as hub:
        token = _enroll(hub, NODE_A, "node-a")
        _save_policy(hub)
        status, prepared = _prepare(hub, token, session_id="session-failed-later")
        assert status == 200, prepared
        status, acked = _ack(hub, token, prepared, session_id="session-failed-later")
        assert status == 200, acked
        loaded_at = acked["loaded_at"]
        status, failed = _ack(
            hub,
            token,
            prepared,
            session_id="session-failed-later",
            status="failed",
            reason="provider busy",
        )
        assert status == 200, failed
        assert failed["applied"] is False
        assert failed["state"] != "current"
        assert failed["loaded_at"] == loaded_at
        assert failed["context_hash"] == prepared["context_hash"]
        rows = _loaded_rows(hub, "session-failed-later")
        assert len(rows) == 1
        assert rows[0]["loaded_snapshot"]["context_hash"] == prepared["context_hash"]
        assert rows[0]["loaded_snapshot"]["selected"] == prepared["selected"]


def test_ordinary_prepare_keeps_canonical_model(tmp_path):
    with _hub(tmp_path) as hub:
        token = _enroll(hub, NODE_A, "node-a")
        document = _document()
        document["seats"]["seat-alpha"]["bindings"]["native"] = {
            "instance_id": "grok-native",
            "model": "model-a-1",
        }
        _save_policy(hub, document)
        status, prepared = _prepare(
            hub,
            token,
            session_id="session-ordinary",
            model="model-a-1",
            instance_id="grok-native",
        )
        assert status == 200, prepared
        assert prepared["selected"]["model"] == "model-a-1"
        assert "launch_model" not in prepared["selected"]
        assert CONTEXT_HASH.fullmatch(prepared["context_hash"])
        status, acked = _ack(hub, token, prepared, session_id="session-ordinary")
        assert status == 200, acked
        assert acked["context_hash"] == prepared["context_hash"]
        assert acked["state"] == "current"
