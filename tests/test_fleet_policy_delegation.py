"""Tests for Brigade-to-T3 Fleet policy delegation and decision-scoped prepare."""

from __future__ import annotations

import copy
import http.client
import json
import threading
from contextlib import contextmanager

import pytest

from brigade import fleet_hub, fleet_policy, fleet_policy_delegation
from brigade.fleet_hub import FleetHubError, FleetHubForbidden

NODE_ORIGIN = "11111111-1111-4111-8111-111111111111"
NODE_TARGET = "22222222-2222-4222-8222-222222222222"
NODE_OTHER = "33333333-3333-4333-8333-333333333333"
ADMIN_TOKEN = "test-admin-token-delegation"


def _test_document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"roles": {"impl": "seat-alpha"}, "data": {"allow_training": False}},
        "machines": {
            "origin-box": {"os": "linux", "node_id": NODE_ORIGIN, "concurrency": 2},
            "remote-target": {"os": "linux", "node_id": NODE_TARGET, "concurrency": 2},
            "other-box": {"os": "linux", "node_id": NODE_OTHER, "concurrency": 2},
        },
        "seats": {
            "seat-alpha": {
                "provider": "provider-a",
                "model": "model-a-canonical",
                "effort": "high",
                "enabled": True,
                "eligible_machines": ["origin-box", "remote-target", "other-box"],
                "concurrency": 2,
                "bindings": {
                    "brigade": {"cli": "cursor-agent", "model": "model-a-canonical"},
                    "t3_fleet": {"instance_id": "t3-instance", "service_tier": "standard"},
                    "native": {"instance_id": "native/t3-instance", "model": "xai/model-a-native"},
                },
            },
            "seat-beta": {
                "provider": "provider-b",
                "model": "model-b-canonical",
                "enabled": True,
                "eligible_machines": ["origin-box", "remote-target", "other-box"],
                "concurrency": 2,
                "bindings": {
                    "brigade": {"cli": "cursor-agent", "model": "model-b-canonical"},
                    "t3_fleet": {"instance_id": "inst-b", "service_tier": "standard"},
                    "native": {"instance_id": "inst-b", "model": "model-b-canonical"},
                },
            },
            "seat-disabled": {
                "provider": "provider-a",
                "model": "model-a-disabled",
                "enabled": False,
                "eligible_machines": ["origin-box", "remote-target", "other-box"],
                "bindings": {
                    "native": {"instance_id": "inst-disabled", "model": "model-a-disabled"},
                },
            },
        },
        "consumers": {
            "brigade-run": {"reload": "refreshable", "coverage": "verified"},
            "t3-code": {"reload": "refreshable", "coverage": "verified"},
            "t3-fleet": {"reload": "refreshable", "coverage": "verified"},
        },
        "routing": {
            "enabled": True,
            "telemetry_ttl_seconds": 300,
            "reservation_ttl_seconds": 300,
            "quota_reserve_percent": 4,
            "workload_requirements": {
                "development": {"os": "linux", "capabilities": []},
            },
        },
        "repositories": {
            "example.com/org/repo": {"privacy": "private"},
            "example.com/org/other": {"privacy": "private"},
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


def _save_policy(hub, doc: dict | None = None):
    policy = doc or _test_document()
    status, res = _request(
        hub,
        "POST",
        "/policy",
        token=ADMIN_TOKEN,
        body={"action": "save", "document": policy, "expected_version": 1, "reason": "test setup"},
    )
    assert status == 200, res
    return status, res


def _enroll_node(hub, node_id: str, label: str) -> str:
    db = fleet_hub.open_db(hub[2])
    try:
        _node, token = fleet_hub.add_node(db, node_id, label)
        return token
    finally:
        db.close()


def _observe_telemetry(hub, token: str, node_id: str = NODE_TARGET, seats: list[str] | None = None):
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat()
    status, observed = _request(
        hub,
        "POST",
        "/policy",
        token=token,
        body={
            "action": "telemetry-observe",
            "node_id": node_id,
            "observed_at": stamp,
            "ttl_seconds": 300,
            "status": "available",
            "load": 0.0,
            "running": {"session_ids": [], "run_ids": []},
            "active_claims": [],
            "usable_seats": seats or ["seat-alpha", "seat-beta"],
            "credential_state": "ok",
        },
    )
    assert status == 200, observed


def _route_decision(hub, token_origin: str, *, session_id: str = "sess-1", repo: str = "example.com/org/repo") -> dict:
    status, decision = _request(
        hub,
        "POST",
        "/policy",
        token=token_origin,
        body={
            "action": "route",
            "consumer": "brigade-run",
            "repo_identity": repo,
            "session_id": session_id,
            "origin": "origin-box",
            "workload": "development",
            "machine": "remote-target",
            "seat": "seat-alpha",
            "override_reason": "test route to remote target",
        },
    )
    assert status == 200, decision
    return decision


def test_delegation_full_lifecycle_and_exact_native_binding(tmp_path):
    """Full lifecycle: route -> delegation create -> show -> prepare exact native -> ack."""
    with _hub(tmp_path) as hub:
        token_origin = _enroll_node(hub, NODE_ORIGIN, "origin")
        token_target = _enroll_node(hub, NODE_TARGET, "target")
        _save_policy(hub)
        _observe_telemetry(hub, token_target)

        # 1. Origin routes a decision
        decision = _route_decision(hub, token_origin)
        decision_id = decision["decision_id"]
        reservation_id = decision["reservation_id"]
        assert reservation_id is not None

        # Check reservation count before delegation
        db = fleet_hub.open_db(hub[2])
        try:
            res_count_before = db.execute("SELECT COUNT(*) FROM fleet_reservations").fetchone()[0]
        finally:
            db.close()

        # 2. Origin creates delegation
        valid_sha = "0123456789abcdef0123456789abcdef01234567"
        status, del_result = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={
                "action": "delegation-create",
                "decision_id": decision_id,
                "source_revision": valid_sha,
                "parent_request_id": "parent-req-001",
            },
        )
        assert status == 200, del_result
        assert del_result["schema"] == "brigade.fleet_policy_delegation.v1"
        delegation_id = del_result["delegation_id"]
        assert delegation_id.startswith("del-")
        assert del_result["launch_authorized"] is False
        assert del_result["consumer"] == "brigade-run"
        assert del_result["adapter"] == "t3-fleet"
        assert del_result["parent_request_id"] == "parent-req-001"
        assert del_result["decision_id"] == decision_id
        assert del_result["reservation_id"] == reservation_id
        assert del_result["session_id"] == "sess-1"
        assert del_result["repo_identity"] == "example.com/org/repo"
        assert del_result["seat"] == "seat-alpha"
        assert del_result["workload"] == "development"
        assert del_result["policy_version"] == 2
        assert del_result["policy_digest"].startswith("sha256:")
        assert del_result["origin_node"] == NODE_ORIGIN
        assert del_result["target_machine"] == "remote-target"
        assert del_result["target_node"] == NODE_TARGET
        assert del_result["source_revision"] == valid_sha
        assert "created_at" in del_result
        assert "expires_at" in del_result
        assert set(del_result) == set(fleet_policy_delegation.DELEGATION_PUBLIC_KEYS)

        # Verify no second reservation was created
        db = fleet_hub.open_db(hub[2])
        try:
            res_count_after = db.execute("SELECT COUNT(*) FROM fleet_reservations").fetchone()[0]
        finally:
            db.close()
        assert res_count_after == res_count_before == 1

        # Verify no token in result
        raw_json = json.dumps(del_result)
        assert token_origin not in raw_json
        assert token_target not in raw_json
        assert ADMIN_TOKEN not in raw_json

        # 3. Target shows delegation
        status, show_result = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={"action": "delegation-show", "delegation_id": delegation_id},
        )
        assert status == 200, show_result
        assert show_result == del_result

        # Origin can also show delegation
        status, show_origin = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={"action": "delegation-show", "delegation_id": delegation_id},
        )
        assert status == 200, show_origin
        assert show_origin == del_result

        # 4. Target prepares exact native identity
        # In seat-alpha, the native binding has model: "xai/model-a-native" and
        # instance_id: "native/t3-instance", while canonical seat.model is "model-a-canonical".
        status, prepared = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "prepare",
                "consumer": "brigade-run",
                "repo_identity": "example.com/org/repo",
                "session_id": "sess-1",
                "origin": "origin-box",
                "delegation_id": delegation_id,
                "provider": "provider-a",
                "model": "xai/model-a-native",
                "instance_id": "native/t3-instance",
                "reasoning": "high",
            },
        )
        assert status == 200, prepared
        selected = prepared["selected"]
        assert selected["seat"] == "seat-alpha"
        assert selected["provider"] == "provider-a"
        assert selected["model"] == "model-a-canonical"  # canonical admission seat.model!
        assert selected["launch_model"] == "xai/model-a-native"  # exact validated native request.model!
        assert selected["instance_id"] == "native/t3-instance"
        assert selected["reasoning"] == "high"
        assert prepared["context_hash"].startswith("sha256:")
        assert prepared["ack_required"] is True

        # 5. Target acknowledges session
        status, acked = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "acknowledge",
                "consumer": "brigade-run",
                "repo_identity": "example.com/org/repo",
                "session_id": "sess-1",
                "version": prepared["version"],
                "digest": prepared["digest"],
                "status": "applied",
                "context_hash": prepared["context_hash"],
            },
        )
        assert status == 200, acked
        assert acked["applied"] is True
        assert acked["state"] == "current"
        assert acked["context_hash"] == prepared["context_hash"]
        assert set(acked) == {
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


def test_delegation_failure_modes(tmp_path):
    """Test wrong node, consumer, repo, seat, sourceSHA, expiry, release, policy change, conflict."""
    with _hub(tmp_path) as hub:
        token_origin = _enroll_node(hub, NODE_ORIGIN, "origin")
        token_target = _enroll_node(hub, NODE_TARGET, "target")
        token_other = _enroll_node(hub, NODE_OTHER, "other")
        _save_policy(hub)
        _observe_telemetry(hub, token_target)

        decision = _route_decision(hub, token_origin)
        decision_id = decision["decision_id"]
        valid_sha = "aabbccddeeff00112233445566778899aabbccdd"

        # Case: Wrong node on create (target cannot create delegation for origin decision)
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "delegation-create",
                "decision_id": decision_id,
                "source_revision": valid_sha,
                "parent_request_id": "parent-fail-1",
            },
        )
        assert status == 403
        assert err["error"]["code"] == "auth-failed"

        # Case: Wrong sourceSHA (non-40 hex)
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={
                "action": "delegation-create",
                "decision_id": decision_id,
                "source_revision": "invalid-sha",
                "parent_request_id": "parent-fail-2",
            },
        )
        assert status == 400
        assert err["error"]["code"] == "invalid-request"

        # Case: Successful create
        status, del_row = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={
                "action": "delegation-create",
                "decision_id": decision_id,
                "source_revision": valid_sha,
                "parent_request_id": "parent-success",
            },
        )
        assert status == 200
        delegation_id = del_row["delegation_id"]

        # Case: Idempotent retry with exact same context
        status, retry = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={
                "action": "delegation-create",
                "decision_id": decision_id,
                "source_revision": valid_sha,
                "parent_request_id": "parent-success",
            },
        )
        assert status == 200
        assert retry["delegation_id"] == delegation_id

        # Case: Conflict with different sourceSHA on same parent_request_id
        different_sha = "1111111111111111111111111111111111111111"
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={
                "action": "delegation-create",
                "decision_id": decision_id,
                "source_revision": different_sha,
                "parent_request_id": "parent-success",
            },
        )
        assert status == 409
        assert err["error"]["code"] == "revision-conflict"

        # Case: Wrong node on show (third-party node_other)
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_other,
            body={"action": "delegation-show", "delegation_id": delegation_id},
        )
        assert status == 403
        assert err["error"]["code"] == "auth-failed"

        # Case: Wrong node on prepare (origin cannot prepare on behalf of target)
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={
                "action": "prepare",
                "delegation_id": delegation_id,
                "provider": "provider-a",
                "model": "model-a-native",
                "instance_id": "t3-instance",
            },
        )
        assert status == 403
        assert err["error"]["code"] == "auth-failed"

        # Case: Wrong consumer in prepare
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "prepare",
                "delegation_id": delegation_id,
                "consumer": "t3-code",
                "provider": "provider-a",
                "model": "model-a-native",
                "instance_id": "t3-instance",
            },
        )
        assert status == 400
        assert err["error"]["code"] == "invalid-request"

        # Case: Wrong repo in prepare
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "prepare",
                "delegation_id": delegation_id,
                "repo_identity": "example.com/org/wrong",
                "provider": "provider-a",
                "model": "model-a-native",
                "instance_id": "t3-instance",
            },
        )
        assert status == 400
        assert err["error"]["code"] == "invalid-request"

        # Case: Wrong seat / model mismatch in prepare
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "prepare",
                "delegation_id": delegation_id,
                "provider": "provider-a",
                "model": "wrong-model",
                "instance_id": "t3-instance",
            },
        )
        assert status == 400
        assert err["error"]["code"] == "seat-unresolved"

        # Case: Wrong instance_id in prepare
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "prepare",
                "delegation_id": delegation_id,
                "provider": "provider-a",
                "model": "model-a-native",
                "instance_id": "wrong-instance",
            },
        )
        assert status == 400
        assert err["error"]["code"] == "seat-unresolved"


def test_delegation_policy_change_invalidates(tmp_path):
    """When policy document is updated, delegation operations must fail with revision-conflict 409."""
    with _hub(tmp_path) as hub:
        token_origin = _enroll_node(hub, NODE_ORIGIN, "origin")
        token_target = _enroll_node(hub, NODE_TARGET, "target")
        _save_policy(hub)
        _observe_telemetry(hub, token_target)

        decision = _route_decision(hub, token_origin)
        decision_id = decision["decision_id"]
        valid_sha = "0123456789abcdef0123456789abcdef01234567"

        status, del_row = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={
                "action": "delegation-create",
                "decision_id": decision_id,
                "source_revision": valid_sha,
                "parent_request_id": "parent-pol-change",
            },
        )
        assert status == 200
        delegation_id = del_row["delegation_id"]

        # Now save a new policy revision
        new_doc = copy.deepcopy(_test_document())
        new_doc["defaults"]["data"]["allow_training"] = True
        status, _saved = _request(
            hub,
            "POST",
            "/policy",
            token=ADMIN_TOKEN,
            body={"action": "save", "document": new_doc, "expected_version": 2, "reason": "bump policy version"},
        )
        assert status == 200

        # Show delegation should fail with 409
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={"action": "delegation-show", "delegation_id": delegation_id},
        )
        assert status == 409
        assert err["error"]["code"] == "revision-conflict"

        # Prepare delegation should fail with 409
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "prepare",
                "delegation_id": delegation_id,
                "provider": "provider-a",
                "model": "model-a-native",
                "instance_id": "t3-instance",
            },
        )
        assert status == 409
        assert err["error"]["code"] == "revision-conflict"


def test_delegation_released_or_expired_reservation(tmp_path):
    """When reservation is released or expired, delegation create, show, and prepare must fail."""
    with _hub(tmp_path) as hub:
        token_origin = _enroll_node(hub, NODE_ORIGIN, "origin")
        token_target = _enroll_node(hub, NODE_TARGET, "target")
        _save_policy(hub)
        _observe_telemetry(hub, token_target)

        decision = _route_decision(hub, token_origin)
        decision_id = decision["decision_id"]
        reservation_id = decision["reservation_id"]
        valid_sha = "0123456789abcdef0123456789abcdef01234567"

        status, del_row = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={
                "action": "delegation-create",
                "decision_id": decision_id,
                "source_revision": valid_sha,
                "parent_request_id": "parent-release-test",
            },
        )
        assert status == 200
        delegation_id = del_row["delegation_id"]

        # Release the reservation
        db = fleet_hub.open_db(hub[2])
        try:
            db.execute("UPDATE fleet_reservations SET state='released' WHERE reservation_id=?", (reservation_id,))
            db.commit()
        finally:
            db.close()

        # Show delegation fails
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={"action": "delegation-show", "delegation_id": delegation_id},
        )
        assert status == 400
        assert err["error"]["code"] == "invalid-request"

        # Prepare delegation fails
        status, err = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "prepare",
                "delegation_id": delegation_id,
                "provider": "provider-a",
                "model": "model-a-native",
                "instance_id": "t3-instance",
            },
        )
        assert status == 400
        assert err["error"]["code"] == "invalid-request"


def test_normal_t3_nondelegated_decision_prepare(tmp_path):
    """Normal T3 nondelegated decision prepare with --decision-id must still work."""
    with _hub(tmp_path) as hub:
        token_target = _enroll_node(hub, NODE_TARGET, "target")
        _save_policy(hub)
        _observe_telemetry(hub, token_target)

        # Route a decision under t3-code
        status, decision = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "route",
                "consumer": "t3-code",
                "repo_identity": "example.com/org/repo",
                "session_id": "sess-t3-direct",
                "origin": "remote-target",
                "workload": "development",
                "machine": "remote-target",
                "seat": "seat-alpha",
                "override_reason": "direct t3 route",
            },
        )
        assert status == 200, decision
        decision_id = decision["decision_id"]

        # Prepare using decision_id
        status, prepared = _request(
            hub,
            "POST",
            "/policy",
            token=token_target,
            body={
                "action": "prepare",
                "consumer": "t3-code",
                "repo_identity": "example.com/org/repo",
                "session_id": "sess-t3-direct",
                "origin": "remote-target",
                "decision_id": decision_id,
                "provider": "provider-a",
                "model": "xai/model-a-native",
                "instance_id": "native/t3-instance",
                "reasoning": "high",
            },
        )
        assert status == 200, prepared
        assert prepared["selected"]["seat"] == "seat-alpha"
        assert prepared["selected"]["model"] == "model-a-canonical"
        assert prepared["selected"]["launch_model"] == "xai/model-a-native"
        assert prepared["selected"]["instance_id"] == "native/t3-instance"
        assert prepared["context_hash"].startswith("sha256:")


def test_validate_delegation_helper_phases(tmp_path):
    """Direct tests for the validate_delegation helper contract across read, prepare, and launch phases."""
    with _hub(tmp_path) as hub:
        token_origin = _enroll_node(hub, NODE_ORIGIN, "origin")
        token_target = _enroll_node(hub, NODE_TARGET, "target")
        _save_policy(hub)
        _observe_telemetry(hub, token_target)

        decision = _route_decision(hub, token_origin)
        valid_sha = "1234567890abcdef1234567890abcdef12345678"

        status, del_row = _request(
            hub,
            "POST",
            "/policy",
            token=token_origin,
            body={
                "action": "delegation-create",
                "decision_id": decision["decision_id"],
                "source_revision": valid_sha,
                "parent_request_id": "parent-helper-test",
            },
        )
        assert status == 200
        del_id = del_row["delegation_id"]

        db = fleet_hub.open_db(hub[2])
        try:
            # phase='read': both origin and target nodes allowed
            env_read_origin = fleet_policy_delegation.validate_delegation(db, del_id, NODE_ORIGIN, phase="read")
            assert env_read_origin["delegation_id"] == del_id
            env_read_target = fleet_policy_delegation.validate_delegation(db, del_id, NODE_TARGET, phase="read")
            assert env_read_target["delegation_id"] == del_id

            # other node forbidden
            with pytest.raises(FleetHubForbidden):
                fleet_policy_delegation.validate_delegation(db, del_id, NODE_OTHER, phase="read")

            # phase='prepare': only target node allowed
            env_prep = fleet_policy_delegation.validate_delegation(db, del_id, NODE_TARGET, phase="prepare")
            assert env_prep["delegation_id"] == del_id
            with pytest.raises(FleetHubForbidden):
                fleet_policy_delegation.validate_delegation(db, del_id, NODE_ORIGIN, phase="prepare")

            # phase='launch': requires reservation to be accepted
            # Before accepted:
            with pytest.raises(FleetHubError, match="not been accepted"):
                fleet_policy_delegation.validate_delegation(db, del_id, NODE_TARGET, phase="launch")

            # Mark accepted
            db.execute("UPDATE fleet_reservations SET accepted=1 WHERE reservation_id=?", (del_row["reservation_id"],))
            db.commit()

            # Now launch phase succeeds
            env_launch = fleet_policy_delegation.validate_delegation(db, del_id, NODE_TARGET, phase="launch")
            assert env_launch["delegation_id"] == del_id
        finally:
            db.close()
