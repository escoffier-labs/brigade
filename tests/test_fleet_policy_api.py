"""HTTP /policy transport for fleet control-plane slice 2."""

from __future__ import annotations

import copy
import http.client
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from brigade import fleet_hub, fleet_hub_policy, fleet_policy


NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
ADMIN_TOKEN = "test-admin-token-policy"


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"roles": {"impl": "seat-alpha"}, "data": {"allow_training": False}},
        "machines": {
            "worker-linux-1": {"os": "linux", "node_id": NODE_A, "concurrency": 2},
        },
        "seats": {
            "seat-alpha": {
                "provider": "provider-a",
                "model": "model-a-1",
                "effort": "high",
                "enabled": True,
                "bindings": {
                    "brigade": {"cli": "cursor-agent"},
                    "t3_fleet": {"instance_id": "cursor", "service_tier": "standard"},
                    "native": {"instance_id": "grok-native", "model": "model-a-1"},
                },
            },
            "seat-beta": {
                "provider": "provider-b",
                "model": "model-b-1",
                "bindings": {
                    "native": {"instance_id": "other-native", "model": "model-b-1"},
                },
            },
            "seat-off": {
                "provider": "provider-a",
                "model": "model-a-2",
                "enabled": False,
                "bindings": {"native": {"instance_id": "disabled-native", "model": "model-a-2"}},
            },
            "seat-train": {
                "provider": "provider-c",
                "model": "model-c-free",
                "training_allowed": True,
                "bindings": {"native": {"instance_id": "train-native", "model": "model-c-free"}},
            },
        },
        "consumers": {
            "t3-code": {"reload": "refreshable", "coverage": "verified"},
            "consumer-one": {"reload": "refreshable", "coverage": "verified"},
        },
        "repositories": {"repo/public": {"privacy": "public"}},
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


def _enroll(hub, node_id: str = NODE_A) -> str:
    db = fleet_hub.open_db(hub[2])
    try:
        _node, node_token = fleet_hub.add_node(db, node_id, "node-a")
    finally:
        db.close()
    return node_token


def _save_policy(hub) -> dict:
    status, payload = _request(
        hub,
        "POST",
        "/policy",
        token=ADMIN_TOKEN,
        body={
            "action": "save",
            "document": _document(),
            "expected_version": 1,
            "reason": "install control-plane policy",
        },
    )
    assert status == 200, payload
    return payload


def test_policy_requires_auth_and_never_returns_a_token(tmp_path):
    with _hub(tmp_path) as hub:
        status, payload = _request(hub, "GET", "/policy")
        assert status == 401
        assert payload["error"]["code"] == "auth-failed"
        dumped = json.dumps(payload)
        assert ADMIN_TOKEN not in dumped
        assert "Bearer" not in dumped

        node_token = _enroll(hub)
        status, payload = _request(hub, "GET", "/policy", token=node_token)
        assert status == 200
        assert payload["schema"] == fleet_policy.POLICY_SCHEMA
        assert "token" not in payload
        assert node_token not in json.dumps(payload)


def test_models_v1_stays_available_and_policy_does_not_claim_synced_authority(tmp_path):
    with _hub(tmp_path) as hub:
        status, models = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        assert "seats" in models
        status, policy = _request(hub, "GET", "/policy", token=ADMIN_TOKEN)
        assert status == 200
        assert policy.get("compatibility_synced") not in {True, "synced"}
        assert "migrated" not in json.dumps(policy)


def test_admin_owns_document_writes_and_nodes_cannot_save(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        status, payload = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={"action": "save", "document": _document(), "expected_version": 1, "reason": "nope"},
        )
        assert status == 403
        assert payload["error"]["code"] == "auth-failed"

        preview = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "preview", "document": _document(), "expected_version": 1, "reason": "preview first"},
        )
        assert preview[0] == 200
        assert preview[1]["ok"] is True
        saved = _save_policy(hub)
        assert saved["revision"] == 2
        status, history = _request(hub, "GET", "/policy?history=1", token=ADMIN_TOKEN)
        assert status == 200
        assert [row["revision"] for row in history["revisions"]] == [2, 1]


def test_resolve_returns_wire_envelope_and_does_not_mark_loaded(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _save_policy(hub)
        status, payload = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "resolve",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-1",
                "origin": "local",
            },
        )
        assert status == 200, payload
        assert payload["schema"] == fleet_policy.POLICY_SCHEMA
        assert payload["version"] == 2
        assert payload["digest"].startswith("sha256:")
        assert payload["ack_required"] is True
        assert "effective" in payload
        assert "sources" in payload
        assert set(payload) == {"schema", "version", "digest", "effective", "sources", "ack_required"}
        db = fleet_hub.open_db(hub[2])
        try:
            states = fleet_hub_policy.list_session_states(db)
        finally:
            db.close()
        assert states == []


def test_resolve_rejects_overrides_without_reason_and_spoofed_node_fields(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _save_policy(hub)
        status, payload = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "resolve",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-1",
                "origin": "local",
                "overrides": {"roles": {"impl": "seat-beta"}},
            },
        )
        assert status == 400
        assert payload["error"]["code"] == "invalid-request"

        status, payload = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "resolve",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-1",
                "origin": NODE_B,
                "node_id": NODE_B,
            },
        )
        assert status in {400, 403}
        assert payload["error"]["code"] in {"invalid-request", "auth-failed"}


def test_unknown_consumer_is_coverage_unknown_and_enrollment_required(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _save_policy(hub)
        status, payload = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "resolve",
                "consumer": "not-enrolled",
                "repo_identity": "repo/public",
                "session_id": "session-1",
                "origin": "local",
            },
        )
        assert status == 400
        assert payload["error"]["code"] == "enrollment-required"
        assert payload.get("coverage") == "unknown"


def test_repeated_resolve_conflicts_when_expected_snapshot_changed(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        first = _save_policy(hub)
        status, resolved = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "resolve",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-repeat",
                "origin": "local",
            },
        )
        assert status == 200, resolved
        changed = _document()
        changed["defaults"]["roles"]["impl"] = "seat-beta"
        status, saved = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "save", "document": changed, "expected_version": 2, "reason": "switch impl"},
        )
        assert status == 200, saved
        status, conflict = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "resolve",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-repeat",
                "origin": "local",
                "expected_version": first["revision"],
                "expected_digest": first["digest"],
            },
        )
        assert status == 409
        assert conflict["error"]["code"] == "revision-conflict"
        status, fresh = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "resolve",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-fresh",
                "origin": "local",
            },
        )
        assert status == 200
        assert fresh["version"] == 3


def test_prepare_exact_match_persists_pending_overrides_and_refuses_bad_seats(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _save_policy(hub)
        status, prepared = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-prep",
                "origin": "local",
                "provider": "provider-a",
                "model": "model-a-1",
                "instance_id": "grok-native",
                "reasoning": "high",
                "overrides": {"execution": {"concurrency": 2}},
                "override_reason": "bounded test override",
            },
        )
        assert status == 200, prepared
        assert prepared["ack_required"] is True
        assert prepared["selected"] == {
            "seat": "seat-alpha",
            "provider": "provider-a",
            "model": "model-a-1",
            "reasoning": "high",
            "instance_id": "grok-native",
        }
        assert "instructions" in prepared and "sha256:" in prepared["instructions"]
        assert prepared["effective"]["execution"]["concurrency"] == 2
        assert prepared["repo_identity"] == "repo/public"
        assert isinstance(prepared["context_hash"], str)
        assert prepared["context_hash"].startswith("sha256:")
        assert len(prepared["context_hash"]) == 71
        assert set(prepared) == {
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
        db = fleet_hub.open_db(hub[2])
        try:
            assert fleet_hub_policy.list_session_states(db) == []
        finally:
            db.close()

        status, missing = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-missing",
                "origin": "local",
                "provider": "provider-a",
                "model": "model-a-1",
                "instance_id": "no-such-instance",
            },
        )
        assert status == 400
        assert missing["error"]["code"] == "seat-unresolved"

        status, disabled = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-off",
                "origin": "local",
                "provider": "provider-a",
                "model": "model-a-2",
                "instance_id": "disabled-native",
            },
        )
        assert status == 400
        assert disabled["error"]["code"] == "seat-disabled"

        status, training = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "repo/private-unlisted",
                "session_id": "session-train",
                "origin": "local",
                "provider": "provider-c",
                "model": "model-c-free",
                "instance_id": "train-native",
            },
        )
        assert status == 400
        assert training["error"]["code"] == "training-disallowed"


def test_acknowledge_records_loaded_policy_from_pending_not_reconstructed(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _save_policy(hub)
        status, prepared = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-ack",
                "origin": "local",
                "provider": "provider-a",
                "model": "model-a-1",
                "instance_id": "grok-native",
                "overrides": {"execution": {"concurrency": 4}},
                "override_reason": "session cap",
            },
        )
        assert status == 200, prepared
        other = _enroll(hub, NODE_B)
        status, forbidden = _request(
            hub,
            "POST",
            "/policy",
            token=other,
            body={
                "action": "acknowledge",
                "session_id": "session-ack",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "version": prepared["version"],
                "digest": prepared["digest"],
            },
        )
        assert status == 403

        status, acked = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "acknowledge",
                "session_id": "session-ack",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "version": prepared["version"],
                "digest": prepared["digest"],
                "status": "applied",
                "context_hash": prepared["context_hash"],
            },
        )
        assert status == 200, acked
        assert acked == {
            "schema": "brigade.fleet_policy_session.v1",
            "session_id": "session-ack",
            "consumer": "t3-code",
            "repo_identity": "repo/public",
            "version": prepared["version"],
            "digest": prepared["digest"],
            "state": "current",
            "loaded_at": acked["loaded_at"],
            "applied": True,
            "context_hash": prepared["context_hash"],
        }
        assert acked["loaded_at"]
        db = fleet_hub.open_db(hub[2])
        try:
            rows = fleet_hub_policy.list_session_states(db)
            assert len(rows) == 1
            assert rows[0]["state"] == "current"
            assert rows[0]["consumer"] == "t3-code"
            assert rows[0]["refresh_state"] in {"none", "applied"}
            pending = fleet_hub_policy.get_pending_policy(
                db,
                node_id=NODE_A,
                consumer="t3-code",
                repo_identity="repo/public",
                session_id="session-ack",
            )
            assert pending["selected"]["seat"] == "seat-alpha"
            assert pending["effective"]["execution"]["concurrency"] == 4
            assert pending["sources"]
            assert pending["context_hash"]
        finally:
            db.close()


def test_refresh_marks_requested_only_and_failed_ack_is_not_current(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _save_policy(hub)
        status, prepared = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-refresh",
                "origin": "local",
                "provider": "provider-a",
                "model": "model-a-1",
                "instance_id": "grok-native",
            },
        )
        assert status == 200, prepared
        status, _acked = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "acknowledge",
                "session_id": "session-refresh",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "version": prepared["version"],
                "digest": prepared["digest"],
                "context_hash": prepared["context_hash"],
            },
        )
        assert status == 200
        status, refreshed = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={
                "action": "refresh",
                "session_id": "session-refresh",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
            },
        )
        assert status == 200, refreshed
        assert refreshed["refresh_state"] == "requested"
        assert refreshed["state"] != "current"

        status, failed = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "acknowledge",
                "session_id": "session-refresh",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "version": prepared["version"],
                "digest": prepared["digest"],
                "status": "failed",
                "reason": "provider busy",
            },
        )
        assert status == 200, failed
        assert failed["schema"] == "brigade.fleet_policy_session.v1"
        assert failed["applied"] is False
        assert failed["state"] != "current"
        assert failed["state"] is not None
        assert failed["version"] == prepared["version"]
        assert failed["context_hash"] == prepared["context_hash"]
        assert set(failed) == {
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


def test_session_keys_do_not_let_one_consumer_claim_another(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _save_policy(hub)
        for consumer in ("t3-code", "consumer-one"):
            status, payload = _request(
                hub,
                "POST",
                "/policy",
                token=node_token,
                body={
                    "action": "resolve",
                    "consumer": consumer,
                    "repo_identity": "repo/public",
                    "session_id": "shared-session",
                    "origin": "local",
                },
            )
            assert status == 200, payload
            status, acked = _request(
                hub,
                "POST",
                "/policy",
                token=node_token,
                body={
                    "action": "acknowledge",
                    "session_id": "shared-session",
                    "consumer": consumer,
                    "repo_identity": "repo/public",
                    "version": payload["version"],
                    "digest": payload["digest"],
                },
            )
            assert status == 200, acked
        db = fleet_hub.open_db(hub[2])
        try:
            consumers = sorted(row["consumer"] for row in fleet_hub_policy.list_session_states(db))
        finally:
            db.close()
        assert consumers == ["consumer-one", "t3-code"]


def test_rollback_and_revision_conflict_are_distinct(tmp_path):
    with _hub(tmp_path) as hub:
        _save_policy(hub)
        status, rolled = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "rollback", "revision": 1, "expected_version": 2, "reason": "undo"},
        )
        assert status == 200
        assert rolled["revision"] == 3
        status, conflict = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "rollback", "revision": 1, "expected_version": 2, "reason": "stale"},
        )
        assert status == 409
        assert conflict["error"]["code"] == "revision-conflict"


def test_prepare_refuses_ambiguous_native_bindings(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        duplicate = _document()
        duplicate["seats"]["seat-clone"] = copy.deepcopy(duplicate["seats"]["seat-alpha"])
        status, _saved = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "save", "document": duplicate, "expected_version": 1, "reason": "duplicate binding"},
        )
        assert status == 200
        status, payload = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-amb",
                "origin": "local",
                "provider": "provider-a",
                "model": "model-a-1",
                "instance_id": "grok-native",
            },
        )
        assert status == 400
        assert payload["error"]["code"] == "ambiguous-seat"


def test_ack_after_policy_change_without_reload_is_conflict(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _save_policy(hub)
        status, prepared = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-stale-ack",
                "origin": "local",
                "provider": "provider-a",
                "model": "model-a-1",
                "instance_id": "grok-native",
            },
        )
        assert status == 200, prepared
        changed = _document()
        changed["defaults"]["roles"]["impl"] = "seat-beta"
        status, saved = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "save", "document": changed, "expected_version": 2, "reason": "switch impl"},
        )
        assert status == 200, saved
        status, conflict = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "acknowledge",
                "session_id": "session-stale-ack",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "version": prepared["version"],
                "digest": prepared["digest"],
            },
        )
        assert status == 409
        assert conflict["error"]["code"] == "revision-conflict"


def test_failed_ack_without_a_loaded_receipt_uses_loaded_at_none(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _save_policy(hub)
        status, prepared = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-failed-first",
                "origin": "local",
                "provider": "provider-a",
                "model": "model-a-1",
                "instance_id": "grok-native",
            },
        )
        assert status == 200, prepared
        status, failed = _request(
            hub,
            "POST",
            "/policy",
            token=node_token,
            body={
                "action": "acknowledge",
                "session_id": "session-failed-first",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "version": prepared["version"],
                "digest": prepared["digest"],
                "status": "failed",
                "reason": "harness rejected selection",
            },
        )
        assert status == 200, failed
        assert failed["applied"] is False
        assert failed["loaded_at"] is None
        assert failed["state"] != "current"
        assert failed["context_hash"] is None
        assert set(failed) == {
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


def test_isolated_hub_route_status_and_quota_transport_two_nodes(tmp_path):
    from brigade import fleet_hub_routing, fleet_quota

    with _hub(tmp_path) as hub:
        node_a = _enroll(hub, NODE_A)
        node_b = _enroll(hub, NODE_B)
        routed = _document()
        routed["routing"] = {
            "enabled": True,
            "telemetry_ttl_seconds": 300,
            "reservation_ttl_seconds": 300,
            "quota_reserve_percent": 4,
            "workload_requirements": {"general": {"os": "linux", "capabilities": []}},
            "quota_pools": {
                "pool-a": {
                    "account_id": "acct-generic-a",
                    "provider": "provider-a",
                    "reserve_percent": 4,
                    "collector_node": NODE_A,
                }
            },
        }
        routed["seats"]["seat-alpha"]["quota_pool"] = "pool-a"
        routed["seats"]["seat-alpha"]["eligible_machines"] = ["worker-linux-1"]
        routed["machines"]["worker-linux-1"]["preferred_workloads"] = ["general"]
        status, saved = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "save", "document": routed, "expected_version": 1, "reason": "enable routing"},
        )
        assert status == 200, saved
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        stamp = now.isoformat()
        reset = (now + timedelta(hours=1)).isoformat()
        status, observed = _request(
            hub,
            "POST",
            "/policy",
            token=node_a,
            body={
                "action": "telemetry-observe",
                "node_id": NODE_A,
                "observed_at": stamp,
                "ttl_seconds": 300,
                "status": "available",
                "load": 0.0,
                "running": {"session_ids": [], "run_ids": []},
                "active_claims": [],
                "usable_seats": ["seat-alpha"],
                "credential_state": "ok",
            },
        )
        assert status == 200, observed
        status, quota = _request(
            hub,
            "POST",
            "/policy",
            token=node_a,
            body={
                "action": "quota-ingest",
                "schema": fleet_quota.QUOTA_SCHEMA,
                "collected_at": stamp,
                "observations": [
                    {
                        "observation_id": "obs-a-1",
                        "account_id": "acct-generic-a",
                        "pool_id": "pool-a",
                        "provider": "provider-a",
                        "source": "crossusage-probe",
                        "collected_at": stamp,
                        "provider_time": None,
                        "status": "current",
                        "windows": [
                            {
                                "window_id": "hourly",
                                "label": "hourly",
                                "used": 10.0,
                                "limit": 100.0,
                                "unit": "percent",
                                "resets_at": reset,
                            }
                        ],
                    }
                ],
            },
        )
        assert status == 200, quota
        status, forbidden = _request(
            hub,
            "POST",
            "/policy",
            token=node_b,
            body={
                "action": "quota-ingest",
                "schema": fleet_quota.QUOTA_SCHEMA,
                "collected_at": stamp,
                "observations": [
                    {
                        "observation_id": "obs-b-1",
                        "account_id": "acct-generic-a",
                        "pool_id": "pool-a",
                        "provider": "provider-a",
                        "source": "crossusage-probe",
                        "collected_at": stamp,
                        "status": "current",
                        "windows": [
                            {
                                "window_id": "hourly",
                                "label": "hourly",
                                "used": 10.0,
                                "limit": 100.0,
                                "unit": "percent",
                            }
                        ],
                    }
                ],
            },
        )
        assert status == 403
        status, decision = _request(
            hub,
            "POST",
            "/policy",
            token=node_a,
            body={
                "action": "route",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-http-route",
                "origin": "worker-linux-1",
                "workload": "general",
            },
        )
        assert status == 200, decision
        assert set(decision) == {
            "schema",
            "decision_id",
            "policy_version",
            "policy_digest",
            "selected",
            "reason",
            "candidates",
            "reservation_id",
            "expires_at",
        }
        assert decision["schema"] == fleet_hub_routing.ROUTE_SCHEMA
        for row in decision["candidates"]:
            assert set(row) == {"machine", "seat", "eligible", "reason"}
        status, stolen = _request(
            hub,
            "POST",
            "/policy",
            token=node_b,
            body={
                "action": "route",
                "consumer": "t3-code",
                "repo_identity": "repo/public",
                "session_id": "session-http-route",
                "origin": "worker-linux-1",
                "workload": "general",
            },
        )
        assert status in {403, 400}
        status, projection = _request(hub, "GET", "/policy/status", token=node_a)
        assert status == 200, projection
        assert projection["schema"] == "brigade.fleet_control_plane.v1"
        assert projection["authority"]["active"] is False
        assert "quota_effective" in projection
        dumped = json.dumps(projection)
        assert node_a not in dumped
        assert ADMIN_TOKEN not in dumped


def _inventory_policy(node_id: str) -> dict:
    document = _document()
    document["routing"] = {
        "enabled": True,
        "inventory_collectors": {
            "agy-a": {
                "node_id": node_id,
                "source": "cli:agy",
                "provider": "provider-a",
                "harness": "agy",
                "account_id": "acct-generic",
            },
            "browser-a": {
                "node_id": node_id,
                "source": "manual:browser",
                "provider": "provider-b",
                "harness": "browser",
                "account_id": "acct-browser",
            },
        },
    }
    return document


def _inventory_payload(**overrides) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "schema": "brigade.fleet_model_inventory.v1",
        "source": "cli:secret-client",
        "provider": "provider-a",
        "harness": "agy",
        "account_id": "acct-generic",
        "scope": "complete",
        "status": "ok",
        "captured_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": ["model-a-1", "model-a-3"],
    }
    payload.update(overrides)
    return payload


def test_inventory_ingest_uses_configured_source_and_denies_mismatches(tmp_path):
    with _hub(tmp_path) as hub:
        node_a = _enroll(hub, NODE_A)
        node_b = _enroll(hub, NODE_B)
        status, saved = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={
                "action": "save",
                "document": _inventory_policy(NODE_A),
                "expected_version": 1,
                "reason": "collectors",
            },
        )
        assert status == 200, saved
        status, denied = _request(
            hub,
            "POST",
            "/policy",
            token=node_a,
            body={"action": "inventory-ingest", **_inventory_payload()},
        )
        # Missing map is not this case; the map exists. Client source is ignored.
        assert status == 200, denied
        assert denied["status"] == "ok"
        assert "token" not in json.dumps(denied)
        status, stolen = _request(
            hub,
            "POST",
            "/policy",
            token=node_b,
            body={"action": "inventory-ingest", **_inventory_payload()},
        )
        assert stolen["error"]["code"] == "auth-failed"
        status, admin_cli = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "inventory-ingest", **_inventory_payload()},
        )
        assert admin_cli["error"]["code"] == "auth-failed"
        status, projection = _request(hub, "GET", "/policy/inventory", token=node_a)
        assert status == 200, projection
        assert projection["schema"] == "brigade.fleet_model_inventory.snapshot.v1"
        assert projection["providers"]["provider-a"]["state"] == "fresh"
        assert "model-a-1" in projection["providers"]["provider-a"]["available"]


def test_inventory_ingest_manual_browser_is_admin_only_with_operator(tmp_path):
    with _hub(tmp_path) as hub:
        node_a = _enroll(hub, NODE_A)
        status, saved = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={
                "action": "save",
                "document": _inventory_policy(NODE_A),
                "expected_version": 1,
                "reason": "collectors",
            },
        )
        assert status == 200, saved
        manual = _inventory_payload(
            source="manual:browser",
            provider="provider-b",
            harness="browser",
            account_id="acct-browser",
            evidence_type="manual_browser",
            models=["model-b-1"],
        )
        status, node_manual = _request(
            hub, "POST", "/policy", token=node_a, body={"action": "inventory-ingest", **manual}
        )
        assert node_manual["error"]["code"] == "auth-failed"
        status, missing_operator = _request(
            hub, "POST", "/policy", token=ADMIN_TOKEN, body={"action": "inventory-ingest", **manual}
        )
        assert missing_operator["error"]["code"] == "invalid-request"
        assert "operator" in missing_operator["error"]["message"]
        status, accepted = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "inventory-ingest", **manual, "operator": "operator-alice"},
        )
        assert status == 200, accepted
        assert accepted["status"] == "ok"


def test_inventory_ingest_without_collectors_is_denied(tmp_path):
    with _hub(tmp_path) as hub:
        node_a = _enroll(hub, NODE_A)
        _save_policy(hub)
        status, denied = _request(
            hub,
            "POST",
            "/policy",
            token=node_a,
            body={"action": "inventory-ingest", **_inventory_payload()},
        )
        assert denied["error"]["code"] == "auth-failed"
        assert "not configured" in denied["error"]["message"]
