"""Fleet authority proof: plan, activated target denial, and launch admission."""

from __future__ import annotations

import json
from io import StringIO

import pytest

from brigade import (
    cli,
    fleet_hub,
    fleet_hub_model_roster,
    fleet_hub_policy,
    fleet_hub_policy_api,
    fleet_model_admission,
    fleet_model_roster,
    fleet_policy,
    fleet_policy_migration,
    fleet_session_bootstrap,
)


NODE_A = "11111111-1111-4111-8111-111111111111"
SEAT = "seat-alpha"
PROVIDER = "provider-a"
MODEL = "model-a-1"
REPO = "repo/public"
SESSION = "session-proof-1"
DIGEST = "sha256:" + ("ab" * 32)


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _set_seat(conn) -> None:
    status, payload = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "set",
            "expected_revision": 1,
            "seat": SEAT,
            "provider": PROVIDER,
            "model": MODEL,
            "reasoning": "high",
            "enabled": True,
            "limit": 3,
            "brigade_cli": "cli-alpha",
            "t3_instance_id": "inst-alpha",
            "t3_service_tier": "standard",
            "notes": "",
        },
    )
    assert status == 200, payload
    status, payload = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "set-default",
            "expected_revision": 2,
            "consumer": "t3-fleet",
            "seat": SEAT,
        },
    )
    assert status == 200, payload
    status, payload = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "set-default",
            "expected_revision": 3,
            "consumer": "brigade-run",
            "seat": SEAT,
        },
    )
    assert status == 200, payload


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_training": False}},
        "machines": {"worker-linux-1": {"os": "linux", "concurrency": 2, "node_id": NODE_A}},
        "seats": {},
        "consumers": {
            "brigade-run": {"reload": "refreshable", "coverage": "unverified", "notes": "kept-brigade"},
            "t3-fleet": {"reload": "none", "coverage": "unverified", "notes": "kept-t3"},
        },
        "repositories": {REPO: {"privacy": "public"}},
    }


def _activate(conn) -> None:
    _set_seat(conn)
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="seed")
    preview = fleet_policy_migration.preview_migration(
        conn,
        expected_policy_version=fleet_hub_policy.current_policy(conn)["revision"],
        expected_roster_revision=int(
            conn.execute("SELECT revision FROM model_roster_meta WHERE singleton=1").fetchone()[0]
        ),
        annotations={"seats": {SEAT: {"pinned": False, "training_allowed": False}}},
    )
    assert preview["ok"] is True, preview
    fleet_policy_migration.activate_migration(
        conn,
        expected_policy_version=preview["sources"]["policy_revision"],
        expected_roster_revision=preview["sources"]["roster_revision"],
        preview_digest=preview["preview_digest"],
        actor="operator",
        reason="adopt",
        annotations=preview.get("annotations"),
    )


def _admit(conn, *, phase: str, request_id: str, consumer: str = "brigade-run", extra: dict | None = None):
    roster = fleet_hub_model_roster.project_roster(conn, audience_node_id=NODE_A)
    body = {
        "action": "admit",
        "schema": fleet_model_roster.ADMISSION_REQUEST_SCHEMA,
        "consumer": consumer,
        "seat": SEAT,
        "request_id": request_id,
        "phase": phase,
        "expect_revision": roster["revision"],
        "expect_digest": roster["document_sha256"],
    }
    if extra:
        body.update(extra)
    return fleet_hub_model_roster.handle_model_policy(conn, body, caller_node=NODE_A)


def _prepare_and_ack(conn, *, consumer: str = "brigade-run", session_id: str = SESSION, instance_id: str = "cli-alpha"):
    current = fleet_hub_policy.current_policy(conn)
    status, prepared = fleet_hub_policy_api.handle_policy(
        conn,
        {
            "action": "prepare",
            "consumer": consumer,
            "repo_identity": REPO,
            "session_id": session_id,
            "origin": "local",
            "provider": PROVIDER,
            "model": MODEL,
            "instance_id": instance_id,
        },
        caller_node=NODE_A,
        is_admin=False,
    )
    assert status == 200, prepared
    status, ack = fleet_hub_policy_api.handle_policy(
        conn,
        {
            "action": "acknowledge",
            "consumer": consumer,
            "repo_identity": REPO,
            "session_id": session_id,
            "version": current["revision"] if prepared.get("version") is None else prepared["version"],
            "digest": prepared["digest"],
            "status": "applied",
            "context_hash": prepared["context_hash"],
        },
        caller_node=NODE_A,
        is_admin=False,
    )
    assert status == 200, ack
    return prepared, ack


def test_authority_parser_rejects_non_positive_version_and_short_digest():
    assert fleet_model_roster.parse_fleet_policy_authority(None) == (None, None)
    assert fleet_model_roster.parse_fleet_policy_authority({"active": True, "version": 0, "digest": DIGEST})[1] == (
        "malformed-authority"
    )
    assert fleet_model_roster.parse_fleet_policy_authority({"active": True, "version": -1, "digest": DIGEST})[1] == (
        "malformed-authority"
    )
    short = {"active": True, "version": 1, "digest": "sha256:abcd"}
    assert fleet_model_roster.parse_fleet_policy_authority(short)[1] == "malformed-authority"
    good = {"active": True, "version": 1, "digest": DIGEST}
    meta, error = fleet_model_roster.parse_fleet_policy_authority(good)
    assert error is None
    assert meta == good


def test_legacy_cache_mac_unchanged_without_authority_metadata():
    payload = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": 1,
        "revision_updated_at": "2026-09-05T00:00:00Z",
        "issued_at": "2026-09-05T00:00:00Z",
        "expires_at": "2026-09-05T00:15:00Z",
        "audience_node_id": NODE_A,
        "document_sha256": DIGEST,
        "seats": [],
        "consumer_defaults": {},
        "retired_models": [],
    }
    without = fleet_model_roster.roster_mac("token", payload)
    activated = dict(payload)
    activated["fleet_policy"] = {"active": True, "version": 1, "digest": DIGEST}
    with_meta = fleet_model_roster.roster_mac("token", activated)
    assert without != with_meta
    dropped = dict(activated)
    dropped.pop("fleet_policy")
    assert fleet_model_roster.roster_mac("token", dropped) == without
    assert fleet_model_roster.roster_mac("token", dropped) != with_meta


def test_inactive_target_still_admits_without_proof(conn):
    _set_seat(conn)
    status, payload = _admit(conn, phase="target", request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert status == 200
    assert payload["schema"] == fleet_model_roster.ADMISSION_SCHEMA
    assert payload["state"] == "authoritative"


def test_activated_target_without_proof_is_denied(conn):
    _activate(conn)
    status, payload = _admit(conn, phase="target", request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert status == 409
    assert payload["error"] == "policy-proof-missing"


def test_activated_resolve_is_not_launch_proof(conn):
    _activate(conn)
    status, resolved = fleet_hub_policy_api.handle_policy(
        conn,
        {
            "action": "resolve",
            "consumer": "brigade-run",
            "repo_identity": REPO,
            "session_id": SESSION,
            "origin": "local",
        },
        caller_node=NODE_A,
        is_admin=False,
    )
    assert status == 200, resolved
    current = fleet_hub_policy.current_policy(conn)
    status, payload = _admit(
        conn,
        phase="target",
        request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        extra={
            "policy_session_id": SESSION,
            "policy_version": current["revision"],
            "policy_digest": current["digest"],
            "repo_identity": REPO,
        },
    )
    assert status == 409
    assert payload["error"] == "policy-proof-not-prepared"


def test_plan_is_read_only_and_not_admitted(monkeypatch):
    snapshot = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": 7,
        "document_sha256": DIGEST,
        "expires_at": "2099-09-05T00:15:00Z",
        "seats": [
            {
                "seat": "cursor_grok",
                "provider": "cursor",
                "model": "cursor-grok-4.6-high-fast",
                "reasoning": "high",
                "enabled": True,
                "bindings": {
                    "brigade": {"cli": "cursor-agent"},
                    "t3_fleet": {"instance_id": "cursor", "service_tier": "standard"},
                    "native": {"instance_id": "cursor", "model": "cursor/native-id"},
                },
            }
        ],
        "consumer_defaults": {"t3-fleet": "cursor_grok"},
        "retired_models": [],
        "source": "hub",
    }
    monkeypatch.setattr(
        fleet_model_admission,
        "fetch_versioned_roster",
        lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(True, 0, "hub", snapshot),
    )
    out = StringIO()
    err = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    rc = cli.main(["fleet", "models", "plan", "--consumer", "t3-fleet", "--seat", "cursor_grok", "--json"])
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["schema"] == fleet_model_roster.PLAN_SCHEMA
    assert payload["launch_authorized"] is False
    assert payload["source"] == "hub"
    assert payload["model"] == "cursor-grok-4.6-high-fast"
    assert payload["binding"]["model"] == "cursor/native-id"
    assert "state" not in payload
    assert payload["schema"] != fleet_model_roster.ADMISSION_SCHEMA


def test_valid_post_ack_launch_succeeds_and_replays(conn):
    _activate(conn)
    prepared, ack = _prepare_and_ack(conn)
    assert ack["applied"] is True
    extra = {
        "policy_session_id": SESSION,
        "policy_version": prepared["version"],
        "policy_digest": prepared["digest"],
        "repo_identity": REPO,
        "policy_context_hash": prepared["context_hash"],
    }
    status, first = _admit(conn, phase="launch", request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", extra=extra)
    assert status == 200, first
    assert first["schema"] == fleet_model_roster.ADMISSION_SCHEMA
    assert first["state"] == "authoritative"
    status, replay = _admit(conn, phase="launch", request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", extra=extra)
    assert status == 200
    assert replay == first
    status, reused = _admit(conn, phase="launch", request_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", extra=extra)
    assert status == 409
    assert reused["error"] == "launch-proof-replay"
    drifted = dict(extra, policy_digest="sha256:" + ("cd" * 32))
    status, changed = _admit(conn, phase="launch", request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", extra=drifted)
    assert status == 409
    assert changed["error"] == "admission-conflict"


def test_t3_launch_without_decision_is_denied(conn):
    _activate(conn)
    prepared, _ack = _prepare_and_ack(conn, consumer="t3-fleet", instance_id="inst-alpha")
    status, payload = _admit(
        conn,
        phase="launch",
        request_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        consumer="t3-fleet",
        extra={
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "repo_identity": REPO,
            "policy_context_hash": prepared["context_hash"],
        },
    )
    assert status == 409
    assert payload["error"] == "decision-proof-missing"


def test_delegation_id_fails_closed(conn):
    _activate(conn)
    status, no_proof = _admit(
        conn,
        phase="launch",
        request_id="99999999-9999-4999-8999-999999999999",
        extra={"delegation_id": "del-opaque"},
    )
    assert status == 409
    assert no_proof["error"] == "policy-proof-missing"
    prepared, _ack = _prepare_and_ack(conn)
    status, payload = _admit(
        conn,
        phase="launch",
        request_id="99999999-9999-4999-8999-999999999998",
        extra={
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "repo_identity": REPO,
            "policy_context_hash": prepared["context_hash"],
            "delegation_id": "del-opaque",
        },
    )
    assert status == 409
    assert payload["error"] == "delegation-unavailable"


def test_activated_lease_requires_launch_proof_not_ack_only(conn):
    _activate(conn)
    status, denied = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "acquire",
            "lease_id": "lease-no-proof",
            "node_id": NODE_A,
            "holder": "holder-alpha",
            "seat": SEAT,
            "provider": PROVIDER,
            "model": MODEL,
            "ttl_seconds": 60,
        },
        caller_node=NODE_A,
    )
    assert status == 409
    assert denied["error"] == "policy-proof-missing"
    prepared, _ack = _prepare_and_ack(conn)
    status, ack_only = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "acquire",
            "lease_id": "lease-ack-only",
            "node_id": NODE_A,
            "holder": "holder-alpha",
            "seat": SEAT,
            "provider": PROVIDER,
            "model": MODEL,
            "ttl_seconds": 60,
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "request_id": SESSION,
            "repo_identity": REPO,
            "policy_context_hash": prepared["context_hash"],
        },
        caller_node=NODE_A,
    )
    assert status == 409
    assert ack_only["error"] == "policy-proof-missing"
    extra = {
        "policy_session_id": SESSION,
        "policy_version": prepared["version"],
        "policy_digest": prepared["digest"],
        "repo_identity": REPO,
        "policy_context_hash": prepared["context_hash"],
    }
    status, launched = _admit(conn, phase="launch", request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", extra=extra)
    assert status == 200, launched
    status, ok = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "acquire",
            "lease_id": "lease-with-proof",
            "node_id": NODE_A,
            "holder": "holder-alpha",
            "seat": SEAT,
            "provider": PROVIDER,
            "model": MODEL,
            "ttl_seconds": 60,
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "request_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "repo_identity": REPO,
            "policy_context_hash": prepared["context_hash"],
            "consumer": "brigade-run",
        },
        caller_node=NODE_A,
    )
    assert status == 200, ok
    assert ok["acquired"] is True
    status, replay = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "acquire",
            "lease_id": "lease-with-proof",
            "node_id": NODE_A,
            "holder": "holder-alpha",
            "seat": SEAT,
            "provider": PROVIDER,
            "model": MODEL,
            "ttl_seconds": 60,
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "request_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "repo_identity": REPO,
            "policy_context_hash": prepared["context_hash"],
            "consumer": "brigade-run",
        },
        caller_node=NODE_A,
    )
    assert status == 200, replay
    status, second = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "acquire",
            "lease_id": "lease-second",
            "node_id": NODE_A,
            "holder": "holder-beta",
            "seat": SEAT,
            "provider": PROVIDER,
            "model": MODEL,
            "ttl_seconds": 60,
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "request_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "repo_identity": REPO,
            "policy_context_hash": prepared["context_hash"],
            "consumer": "brigade-run",
        },
        caller_node=NODE_A,
    )
    assert status == 409
    assert second["error"] == "launch-proof-replay"


def test_legacy_digest_and_mac_unchanged_without_consumer_launch_bindings():
    payload = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": 1,
        "revision_updated_at": "2026-09-05T00:00:00Z",
        "issued_at": "2026-09-05T00:00:00Z",
        "expires_at": "2026-09-05T00:15:00Z",
        "audience_node_id": NODE_A,
        "document_sha256": DIGEST,
        "seats": [],
        "consumer_defaults": {},
        "retired_models": [],
    }
    without = fleet_model_roster.roster_digest(payload)
    mac_without = fleet_model_roster.roster_mac("token", payload)
    activated = dict(payload)
    activated["consumer_launch_bindings"] = {
        "brigade-run": {
            SEAT: {
                "brigade": {"cli": "cli-alpha"},
                "t3_fleet": {},
                "native": {},
            }
        }
    }
    assert fleet_model_roster.roster_digest(activated) != without
    assert fleet_model_roster.roster_mac("token", activated) != mac_without
    dropped = dict(activated)
    dropped.pop("consumer_launch_bindings")
    assert fleet_model_roster.roster_digest(dropped) == without
    assert fleet_model_roster.roster_mac("token", dropped) == mac_without


def test_consumer_launch_bindings_are_projected_and_not_inherited(conn):
    _activate(conn)
    current = fleet_hub_policy.current_policy(conn)
    document = dict(current["document"])
    seats = dict(document["seats"])
    seat = dict(seats[SEAT])
    bindings = dict(seat.get("bindings") or {})
    bindings["native"] = {"instance_id": "inst-alpha", "model": "provider-a/model-slash-id"}
    bindings["brigade"] = {"cli": "cli-alpha", "model": "provider-a/model-slash-id"}
    seat["bindings"] = bindings
    seats[SEAT] = seat
    document["seats"] = seats
    consumers = dict(document["consumers"])
    brigade = dict(consumers["brigade-run"])
    brigade["seat_bindings"] = {SEAT: {"brigade": {"cli": "cli-alpha", "model": "provider-a/model-slash-id"}}}
    t3 = dict(consumers["t3-fleet"])
    t3["seat_bindings"] = {SEAT: {"native": {"instance_id": "inst-alpha", "model": "provider-a/model-slash-id"}}}
    consumers["brigade-run"] = brigade
    consumers["t3-fleet"] = t3
    document["consumers"] = consumers
    fleet_hub_policy.save_policy(conn, document, expected_version=current["revision"], actor="operator", reason="bind")
    roster = fleet_hub_model_roster.project_roster(conn, audience_node_id=NODE_A)
    projected = roster["consumer_launch_bindings"]
    assert "unknown-consumer" not in projected
    assert projected["brigade-run"][SEAT]["brigade"]["model"] == "provider-a/model-slash-id"
    assert projected["t3-fleet"][SEAT]["native"]["model"] == "provider-a/model-slash-id"
    assert projected["brigade-run"][SEAT]["native"]["model"] == "provider-a/model-slash-id"
    assert fleet_model_roster.roster_digest(roster) == roster["document_sha256"]
    assert fleet_model_roster.validate_roster_rows(roster) is None
    assert "unknown-consumer" not in projected
    copied = dict(projected["brigade-run"][SEAT])
    assert fleet_model_roster.adapter_plan_binding("other", copied) is None


def test_launch_requires_repo_and_policy_context_hash(conn):
    _activate(conn)
    prepared, _ack = _prepare_and_ack(conn)
    status, missing_hash = _admit(
        conn,
        phase="launch",
        request_id="12121212-1212-4121-8121-121212121212",
        extra={
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "repo_identity": REPO,
        },
    )
    assert status == 409
    assert missing_hash["error"] == "policy-proof-missing"
    status, missing_repo = _admit(
        conn,
        phase="launch",
        request_id="13131313-1313-4131-8131-131313131313",
        extra={
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "policy_context_hash": prepared["context_hash"],
        },
    )
    assert status == 409
    assert missing_repo["error"] == "policy-proof-missing"


def test_reprepare_after_ack_changes_hash_and_blocks_launch(conn):
    _activate(conn)
    prepared, _ack = _prepare_and_ack(conn)
    first_hash = prepared["context_hash"]
    status, again = fleet_hub_policy_api.handle_policy(
        conn,
        {
            "action": "prepare",
            "consumer": "brigade-run",
            "repo_identity": REPO,
            "session_id": SESSION,
            "origin": "local",
            "provider": PROVIDER,
            "model": MODEL,
            "instance_id": "cli-alpha",
            "overrides": {"data": {"retention": "none"}},
            "override_reason": "session-test",
        },
        caller_node=NODE_A,
        is_admin=False,
    )
    assert status == 200, again
    assert again["context_hash"] != first_hash
    status, payload = _admit(
        conn,
        phase="launch",
        request_id="14141414-1414-4141-8141-141414141414",
        extra={
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "repo_identity": REPO,
            "policy_context_hash": first_hash,
        },
    )
    assert status == 409
    assert payload["error"] in {"policy-proof-mismatch", "policy-stale", "policy-ack-missing"}


def test_canonical_lease_model_rejects_slash_native_as_request_model(conn):
    _activate(conn)
    prepared, _ack = _prepare_and_ack(conn)
    extra = {
        "policy_session_id": SESSION,
        "policy_version": prepared["version"],
        "policy_digest": prepared["digest"],
        "repo_identity": REPO,
        "policy_context_hash": prepared["context_hash"],
    }
    status, launched = _admit(conn, phase="launch", request_id="15151515-1515-4151-8151-151515151515", extra=extra)
    assert status == 200, launched
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub._validate_model_lease_request(
            {
                "action": "acquire",
                "lease_id": "lease-slash",
                "node_id": NODE_A,
                "holder": "holder-alpha",
                "seat": SEAT,
                "provider": PROVIDER,
                "model": "provider-a/model-slash-id",
                "ttl_seconds": 60,
            }
        )
    request = fleet_hub._validate_model_lease_request(
        {
            "action": "acquire",
            "lease_id": "lease-canonical",
            "node_id": NODE_A,
            "holder": "holder-alpha",
            "seat": SEAT,
            "provider": PROVIDER,
            "model": MODEL,
            "launch_model": "provider-a/model-slash-id",
            "ttl_seconds": 60,
            "policy_session_id": SESSION,
            "policy_version": prepared["version"],
            "policy_digest": prepared["digest"],
            "request_id": "15151515-1515-4151-8151-151515151515",
            "repo_identity": REPO,
            "policy_context_hash": prepared["context_hash"],
            "consumer": "brigade-run",
        }
    )
    assert request["model"] == MODEL
    assert request["launch_model"] == "provider-a/model-slash-id"


def test_enrollment_downgrade_from_omitted_authority_is_denied(monkeypatch):
    enrolled = {
        "state": "authoritative",
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "fleet_policy": {"active": True, "version": 1, "digest": DIGEST},
    }
    assert fleet_session_bootstrap.classify_enrollment(enrolled) == "enrolled"
    monkeypatch.setattr(fleet_model_admission, "enrollment_activation_observed", lambda: True)
    legacy = {
        "state": "authoritative",
        "schema": fleet_model_roster.ROSTER_SCHEMA,
    }
    assert fleet_session_bootstrap.classify_enrollment(legacy) == "denied"


def test_models_bindings_cli_projects_grouped_envelope(monkeypatch):
    snapshot = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "source": "hub",
        "revision": 9,
        "document_sha256": DIGEST,
        "expires_at": "2099-09-05T00:15:00Z",
        "fleet_policy": {"active": True, "version": 4, "digest": DIGEST},
        "seats": [
            {
                "seat": SEAT,
                "provider": PROVIDER,
                "model": MODEL,
                "enabled": True,
                "reasoning": "high",
                "bindings": {
                    "brigade": {"cli": "cli-alpha"},
                    "t3_fleet": {"instance_id": "inst-alpha", "service_tier": "standard"},
                },
            }
        ],
        "consumer_launch_bindings": {
            "t3-fleet": {
                SEAT: {
                    "brigade": {"cli": "cli-alpha"},
                    "t3_fleet": {"instance_id": "inst-alpha", "service_tier": "standard"},
                    "native": {"instance_id": "inst-alpha", "model": "provider-a/model-slash-id"},
                }
            }
        },
        "consumer_defaults": {"t3-fleet": SEAT},
        "retired_models": [],
    }
    monkeypatch.setattr(
        fleet_model_admission,
        "fetch_versioned_roster",
        lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(True, 0, "hub", snapshot),
    )
    out = StringIO()
    err = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    rc = cli.main(["fleet", "models", "bindings", "--consumer", "t3-fleet", "--json"])
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["schema"] == fleet_model_roster.BINDINGS_SCHEMA
    assert payload["source"] == "hub"
    assert payload["consumer"] == "t3-fleet"
    assert payload["bindings"][SEAT]["native"]["model"] == "provider-a/model-slash-id"
    assert payload["bindings"][SEAT]["model"] == MODEL


def test_plan_uses_consumer_launch_bindings_not_seat_native_inference(monkeypatch):
    snapshot = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": 7,
        "document_sha256": DIGEST,
        "expires_at": "2099-09-05T00:15:00Z",
        "source": "hub",
        "seats": [
            {
                "seat": "cursor_grok",
                "provider": "cursor",
                "model": "cursor-grok-4.6-high-fast",
                "reasoning": "high",
                "enabled": True,
                "bindings": {
                    "brigade": {"cli": "cursor-agent"},
                    "t3_fleet": {"instance_id": "cursor", "service_tier": "standard"},
                },
            }
        ],
        "consumer_launch_bindings": {
            "t3-fleet": {
                "cursor_grok": {
                    "brigade": {"cli": "cursor-agent"},
                    "t3_fleet": {"instance_id": "cursor", "service_tier": "standard"},
                    "native": {"instance_id": "cursor", "model": "cursor/native-id"},
                }
            }
        },
        "consumer_defaults": {"t3-fleet": "cursor_grok"},
        "retired_models": [],
    }
    monkeypatch.setattr(
        fleet_model_admission,
        "fetch_versioned_roster",
        lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(True, 0, "hub", snapshot),
    )
    out = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", StringIO())
    rc = cli.main(["fleet", "models", "plan", "--consumer", "t3-fleet", "--seat", "cursor_grok", "--json"])
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["model"] == "cursor-grok-4.6-high-fast"
    assert payload["binding"]["model"] == "cursor/native-id"
    assert payload["binding"]["instance_id"] == "cursor"
