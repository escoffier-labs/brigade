"""Fleet Hub versioned model roster contract (schema v1)."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_model_roster, fleet_model_roster


NODE_A = "11111111-1111-4111-8111-111111111111"
ADMIN_TOKEN = "test-admin-token"
NODE_BEARER = "node-bearer-for-roster-mac"
SEAT = {
    "action": "set",
    "provider": "cursor",
    "model": "cursor-grok-4.6-high-fast",
    "seat": "cursor_grok",
    "enabled": True,
    "reasoning": "high",
    "brigade_cli": "cursor-agent",
    "t3_instance_id": "cursor",
    "t3_service_tier": "standard",
}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest_body(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: payload[key]
        for key in (
            "schema",
            "revision",
            "revision_updated_at",
            "seats",
            "consumer_defaults",
            "retired_models",
        )
        if key in payload
    }


def _cache_envelope(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: payload[key]
        for key in (
            "schema",
            "revision",
            "revision_updated_at",
            "issued_at",
            "expires_at",
            "audience_node_id",
            "roster_digest",
            "seats",
            "consumer_defaults",
            "retired_models",
        )
        if key in payload
    }


def _expected_digest(payload: dict[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(_digest_body(payload)).encode("ascii")).hexdigest()


def _expected_mac(raw_bearer: str, payload: dict[str, object]) -> str:
    message = b"brigade.fleet-model-roster.lkg.v1\0" + _canonical(_cache_envelope(payload)).encode("ascii")
    return hmac.new(raw_bearer.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _dump(conn: sqlite3.Connection) -> str:
    return "\n".join(conn.iterdump())


def _revision(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT revision FROM model_roster_meta WHERE singleton=1").fetchone()[0])


def _v14_database(path, *, node_id: str, raw_token: str) -> None:
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE model_policy ("
        "seat TEXT NOT NULL PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, "
        "enabled INTEGER NOT NULL, limit_count INTEGER, notes TEXT, updated_at TEXT NOT NULL)"
    )
    old.execute(
        "INSERT INTO model_policy VALUES ('coder', 'openai', 'gpt-5.6-terra', 1, 2, 'kept', '2026-01-01T00:00:00+00:00')"
    )
    old.execute(
        "CREATE TABLE nodes ("
        "node_id TEXT PRIMARY KEY, token_sha256 TEXT UNIQUE, label TEXT, created_at TEXT, revoked_at TEXT)"
    )
    old.execute(
        "INSERT INTO nodes VALUES (?, ?, 'node-a', 't', NULL)",
        (node_id, hashlib.sha256(raw_token.encode("utf-8")).hexdigest()),
    )
    old.execute("PRAGMA user_version=14")
    old.commit()
    old.close()


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


def _request(hub, method: str, path: str, *, token: str | None = None, body: dict[str, object] | None = None):
    host, port, _db = hub
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection.request(
        method, path, body=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers
    )
    response = connection.getresponse()
    result = response.status, json.loads(response.read().decode("utf-8"))
    connection.close()
    return result


def _enroll(hub, node_id: str = NODE_A) -> str:
    db = fleet_hub.open_db(hub[2])
    try:
        _node, node_token = fleet_hub.add_node(db, node_id, "node-a")
    finally:
        db.close()
    return node_token


def _current_revision(hub) -> int:
    status, payload = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
    assert status == 200
    return int(payload["revision"])


def _admin_set(hub, **fields: object):
    body = {"expected_revision": _current_revision(hub), **SEAT, **fields}
    return _request(hub, "POST", "/models", token=ADMIN_TOKEN, body=body)


@pytest.mark.parametrize(
    "model",
    ["gpt-5.4", "openai/gpt-5.4", "gpt-5.4-high", "gpt-5.5:preview"],
)
def test_permanent_retired_openai_families_match_structurally(model):
    assert fleet_model_roster.retired_reason("openai", model) == "permanently-retired"


def test_retired_family_match_does_not_catch_gpt_5_40():
    assert fleet_model_roster.retired_reason("openai", "gpt-5.40") is None


def test_retired_family_normalizes_stored_family_and_cursor_prefixed_model():
    rows = (
        {
            "provider": "cursor",
            "family": "cursor/cursor-grok-4.6",
            "reason_code": "operator-retired",
        },
    )
    assert fleet_model_roster.normalize_model("cursor", "cursor-grok-4.6-high-fast") == "cursor-grok-4.6-high-fast"
    assert fleet_model_roster.normalize_model("cursor", "cursor/cursor-grok-4.6") == "cursor-grok-4.6"
    assert fleet_model_roster.retired_reason("cursor", "cursor-grok-4.6-high-fast", rows) == "operator-retired"
    assert fleet_model_roster.retired_reason("cursor", "cursor/cursor-grok-4.6", rows) == "operator-retired"


def test_roster_digest_is_stable_across_freshness_and_audience():
    body = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": 2,
        "revision_updated_at": "2026-08-30T13:52:00Z",
        "seats": [],
        "consumer_defaults": {"t3-fleet": "cursor_grok"},
        "retired_models": [],
    }
    first = {
        **body,
        "issued_at": "2026-08-30T14:00:00Z",
        "expires_at": "2026-08-30T14:15:00Z",
        "audience_node_id": NODE_A,
        "roster_digest": "sha256:" + ("aa" * 32),
    }
    second = {
        **body,
        "issued_at": "2026-08-30T14:20:00Z",
        "expires_at": "2026-08-30T14:35:00Z",
        "audience_node_id": "22222222-2222-4222-8222-222222222222",
        "roster_digest": "sha256:" + ("bb" * 32),
    }
    assert fleet_model_roster.roster_digest(first) == fleet_model_roster.roster_digest(second)
    assert fleet_model_roster.roster_digest(first) == _expected_digest(body)
    assert fleet_model_roster.roster_mac(NODE_BEARER, first) != fleet_model_roster.roster_mac(NODE_BEARER, second)
    assert hmac.compare_digest(fleet_model_roster.roster_mac(NODE_BEARER, first), _expected_mac(NODE_BEARER, first))


def test_schema_migration_preserves_rows_seeds_retirements_and_never_stores_raw_token(tmp_path):
    db = tmp_path / "legacy.db"
    _v14_database(db, node_id=NODE_A, raw_token=NODE_BEARER)
    assert fleet_hub.SCHEMA_VERSION > 14
    migrated = fleet_hub.init_db(db)
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        row = migrated.execute(
            "SELECT seat, provider, model, enabled, limit_count, notes, reasoning, brigade_cli, "
            "t3_instance_id, t3_service_tier FROM model_policy WHERE seat='coder'"
        ).fetchone()
        assert row == ("coder", "openai", "gpt-5.6-terra", 1, 2, "kept", "none", "", "", "")
        families = set(
            migrated.execute("SELECT provider, family, permanent, reason_code FROM retired_models").fetchall()
        )
        assert families == {
            ("openai", "gpt-5.4", 1, "permanently-retired"),
            ("openai", "gpt-5.5", 1, "permanently-retired"),
        }
        assert _revision(migrated) == 1
        consumers = set(migrated.execute("SELECT consumer FROM model_consumer_defaults").fetchall())
        assert consumers == {("brigade-run",), ("t3-fleet",)}
        assert migrated.execute("SELECT COUNT(*) FROM model_admission_audit").fetchone()[0] == 0
        dumped = _dump(migrated)
        assert NODE_BEARER not in dumped
        assert ADMIN_TOKEN not in dumped
    finally:
        migrated.close()


def test_admin_set_requires_revision_and_stale_set_is_byte_identical(tmp_path):
    with _hub(tmp_path) as hub:
        missing = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "set",
                "provider": "cursor",
                "model": "cursor-grok-4.6-high-fast",
                "seat": "cursor_grok",
                "enabled": True,
                "reasoning": "high",
                "brigade_cli": "cursor-agent",
                "t3_instance_id": "cursor",
            },
        )
        assert missing[0] == 400
        status, payload = _admin_set(hub)
        assert status == 200
        policy = payload["policy"]
        assert policy["reasoning"] == "high"
        assert policy["brigade_cli"] == "cursor-agent"
        assert policy["t3_instance_id"] == "cursor"
        assert policy["t3_service_tier"] == "standard"
        assert payload.get("revision") == 2
        db = fleet_hub.open_db(hub[2])
        try:
            assert _revision(db) == 2
            before = _dump(db)
        finally:
            db.close()
        stale = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={**SEAT, "expected_revision": 1, "model": "composer-2.5"},
        )
        assert stale[0] == 409
        assert stale[1]["error"] == "roster_revision_conflict"
        db = fleet_hub.open_db(hub[2])
        try:
            assert _dump(db) == before
            assert _revision(db) == 2
            assert db.execute("SELECT model FROM model_policy WHERE seat='cursor_grok'").fetchone()[0] == (
                "cursor-grok-4.6-high-fast"
            )
        finally:
            db.close()


def test_set_default_and_retire_require_revision_and_protect_permanent_rows(tmp_path):
    with _hub(tmp_path) as hub:
        assert _admin_set(hub)[0] == 200
        assert (
            _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={"action": "set-default", "consumer": "t3-fleet", "seat": "cursor_grok"},
            )[0]
            == 400
        )
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "set-default",
                "consumer": "t3-fleet",
                "seat": "cursor_grok",
                "expected_revision": _current_revision(hub),
            },
        )
        assert status == 200
        assert payload["revision"] == 3
        assert (
            _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={"action": "retire", "provider": "openai", "family": "gpt-4", "permanent": False},
            )[0]
            == 400
        )
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "retire",
                "provider": "openai",
                "family": "gpt-4",
                "permanent": False,
                "reason_code": "operator-retired",
                "expected_revision": _current_revision(hub),
            },
        )
        assert status == 200
        assert payload["revision"] == 4
        revision = _current_revision(hub)
        for body in (
            {
                "action": "retire",
                "provider": "openai",
                "family": "gpt-5.4",
                "permanent": False,
                "expected_revision": revision,
            },
            {
                "action": "retire",
                "provider": "openai",
                "family": "gpt-5.4",
                "match_kind": "exact",
                "expected_revision": revision,
            },
        ):
            status, payload = _request(hub, "POST", "/models", token=ADMIN_TOKEN, body=body)
            assert status == 409
            assert payload["error"] in {
                "permanent-retirement-immutable",
                "roster_revision_conflict",
            }
        db = fleet_hub.open_db(hub[2])
        try:
            row = db.execute(
                "SELECT permanent, match_kind, family FROM retired_models WHERE provider='openai' AND family='gpt-5.4'"
            ).fetchone()
            assert row == (1, "family-prefix", "gpt-5.4")
            assert _revision(db) == 4
        finally:
            db.close()


def test_node_get_is_mac_bound_and_admin_get_cannot_seed_lkg(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        assert _admin_set(hub)[0] == 200
        assert (
            _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={
                    "action": "set-default",
                    "consumer": "t3-fleet",
                    "seat": "cursor_grok",
                    "expected_revision": _current_revision(hub),
                },
            )[0]
            == 200
        )
        status, node_payload = _request(hub, "GET", "/models", token=node_token)
        assert status == 200
        assert node_payload["schema"] == "brigade.fleet_model_roster.v1"
        assert node_payload["audience_node_id"] == NODE_A
        assert node_payload["models"]
        assert {row["seat"] for row in node_payload["seats"]} == {"cursor_grok"}
        assert node_payload["consumer_defaults"]["t3-fleet"] == "cursor_grok"
        families = {(row["provider"], row["family"]) for row in node_payload["retired_models"]}
        assert families == {("openai", "gpt-5.4"), ("openai", "gpt-5.5")}
        assert node_payload["mac"]["algorithm"] == "hmac-sha256-node-bearer-v1"
        assert hmac.compare_digest(node_payload["roster_digest"], _expected_digest(node_payload))
        assert hmac.compare_digest(node_payload["mac"]["value"], _expected_mac(node_token, node_payload))
        assert node_token not in json.dumps(node_payload)
        status, admin_payload = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        assert admin_payload["schema"] == "brigade.fleet_model_roster.v1"
        assert "audience_node_id" not in admin_payload
        assert "mac" not in admin_payload
        assert admin_payload["models"] == node_payload["models"]


def test_admit_is_node_bound_idempotent_and_conflicts_on_replay_drift(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        assert _admin_set(hub)[0] == 200
        assert (
            _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={
                    "action": "set-default",
                    "consumer": "t3-fleet",
                    "seat": "cursor_grok",
                    "expected_revision": _current_revision(hub),
                },
            )[0]
            == 200
        )
        status, roster = _request(hub, "GET", "/models", token=node_token)
        assert status == 200
        admit = {
            "action": "admit",
            "schema": "brigade.model_admission_request.v1",
            "consumer": "t3-fleet",
            "seat": None,
            "request_id": "c833a6f6-02fd-4eb2-92cb-d44d3cd29b66",
            "phase": "controller",
            "expect_revision": roster["revision"],
            "expect_digest": roster["roster_digest"],
        }
        status, first = _request(hub, "POST", "/models", token=node_token, body=admit)
        assert status == 200
        assert first == {
            "schema": "brigade.model_admission.v1",
            "state": "authoritative",
            "source": "hub",
            "roster_revision": roster["revision"],
            "roster_digest": roster["roster_digest"],
            "seat": "cursor_grok",
            "provider": "cursor",
            "model": "cursor-grok-4.6-high-fast",
            "reasoning": "high",
            "binding": {"instance_id": "cursor", "service_tier": "standard"},
            "expires_at": first["expires_at"],
        }
        status, replay = _request(hub, "POST", "/models", token=node_token, body=admit)
        assert status == 200
        assert replay == first
        drifted = dict(admit, expect_digest="sha256:" + ("ab" * 32))
        status, conflict = _request(hub, "POST", "/models", token=node_token, body=drifted)
        assert status == 409
        assert conflict["error"] == "admission-conflict"
        stale = dict(admit, request_id="11111111-1111-4111-8111-111111111111", expect_revision=1)
        status, revision_conflict = _request(hub, "POST", "/models", token=node_token, body=stale)
        assert status == 409
        assert revision_conflict["error"] == "roster_revision_conflict"
        assert (
            _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={
                    "action": "set",
                    "expected_revision": _current_revision(hub),
                    "provider": "openai",
                    "model": "gpt-5.6-terra",
                    "seat": "coder",
                    "enabled": True,
                    "reasoning": "none",
                    "brigade_cli": "codex",
                    "t3_instance_id": "openai",
                },
            )[0]
            == 200
        )
        assert (
            _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={
                    "action": "retire",
                    "provider": "openai",
                    "family": "gpt-5.6-terra",
                    "permanent": False,
                    "reason_code": "operator-retired",
                    "expected_revision": _current_revision(hub),
                },
            )[0]
            == 200
        )
        status, retired_roster = _request(hub, "GET", "/models", token=node_token)
        assert status == 200
        retired = {
            "action": "admit",
            "schema": "brigade.model_admission_request.v1",
            "consumer": "t3-fleet",
            "seat": "coder",
            "request_id": "22222222-2222-4222-8222-222222222222",
            "phase": "controller",
            "expect_revision": retired_roster["revision"],
            "expect_digest": retired_roster["roster_digest"],
        }
        status, denied = _request(hub, "POST", "/models", token=node_token, body=retired)
        assert status == 409
        assert denied["error"] == "retired-model"
        assert denied["roster_revision"] == retired_roster["revision"]
        assert denied["roster_digest"] == retired_roster["roster_digest"]
        assert denied["seat"] == "coder"
        assert denied["provider"] == "openai"
        assert denied["model"] == "gpt-5.6-terra"
        status, replay_denied = _request(hub, "POST", "/models", token=node_token, body=retired)
        assert status == 409
        assert replay_denied == denied
        drifted_denied = dict(retired, expect_digest="sha256:" + ("cd" * 32))
        status, denial_conflict = _request(hub, "POST", "/models", token=node_token, body=drifted_denied)
        assert status == 409
        assert denial_conflict["error"] == "admission-conflict"
        dumped = json.dumps(first) + json.dumps(denied)
        assert node_token not in dumped
        assert ADMIN_TOKEN not in dumped
        db = fleet_hub.open_db(hub[2])
        try:
            rows = db.execute(
                "SELECT node_id, request_id, phase, consumer, decision, provider, model, reasoning "
                "FROM model_admission_audit ORDER BY created_at, request_id"
            ).fetchall()
            assert rows == [
                (
                    NODE_A,
                    "c833a6f6-02fd-4eb2-92cb-d44d3cd29b66",
                    "controller",
                    "t3-fleet",
                    "admitted",
                    "cursor",
                    "cursor-grok-4.6-high-fast",
                    "high",
                ),
                (
                    NODE_A,
                    "22222222-2222-4222-8222-222222222222",
                    "controller",
                    "t3-fleet",
                    "retired-model",
                    "openai",
                    "gpt-5.6-terra",
                    "none",
                ),
            ]
            assert NODE_BEARER not in _dump(db)
            assert node_token not in _dump(db)
        finally:
            db.close()


def test_fresh_envelope_after_ttl_keeps_stable_digest_and_revision(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(fleet_hub_model_roster, "utc_now", lambda: clock["now"])
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        assert _admin_set(hub)[0] == 200
        status, first = _request(hub, "GET", "/models", token=node_token)
        assert status == 200
        assert first["revision_updated_at"]
        assert first["issued_at"] == "2026-08-30T14:00:00Z"
        assert first["expires_at"] == "2026-08-30T14:15:00Z"
        assert hmac.compare_digest(first["roster_digest"], _expected_digest(first))
        assert hmac.compare_digest(first["mac"]["value"], _expected_mac(node_token, first))
        clock["now"] = clock["now"] + timedelta(seconds=901)
        status, second = _request(hub, "GET", "/models", token=node_token)
        assert status == 200
        assert second["revision"] == first["revision"]
        assert second["revision_updated_at"] == first["revision_updated_at"]
        assert second["roster_digest"] == first["roster_digest"]
        assert second["issued_at"] == "2026-08-30T14:15:01Z"
        assert second["expires_at"] == "2026-08-30T14:30:01Z"
        assert hmac.compare_digest(second["mac"]["value"], _expected_mac(node_token, second))
        assert second["mac"]["value"] != first["mac"]["value"]
        status, admin_payload = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        assert admin_payload["roster_digest"] == first["roster_digest"]
        assert "audience_node_id" not in admin_payload
        assert "mac" not in admin_payload


def test_set_and_set_default_reject_retired_models_before_revision_bump(tmp_path):
    with _hub(tmp_path) as hub:
        assert _admin_set(hub)[0] == 200
        before = _current_revision(hub)
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                **SEAT,
                "expected_revision": before,
                "provider": "openai",
                "model": "gpt-5.4",
                "seat": "coder",
                "reasoning": "none",
                "brigade_cli": "codex",
                "t3_instance_id": "openai",
            },
        )
        assert status == 409
        assert payload["error"] == "retired-model"
        assert _current_revision(hub) == before
        assert (
            _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={
                    "action": "retire",
                    "provider": "cursor",
                    "family": "cursor-grok-4.6",
                    "permanent": False,
                    "reason_code": "operator-retired",
                    "expected_revision": before,
                },
            )[0]
            == 200
        )
        retired_revision = _current_revision(hub)
        status, payload = _admin_set(hub)
        assert status == 409
        assert payload["error"] == "retired-model"
        assert _current_revision(hub) == retired_revision
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "set-default",
                "consumer": "t3-fleet",
                "seat": "cursor_grok",
                "expected_revision": retired_revision,
            },
        )
        assert status == 409
        assert payload["error"] == "retired-model"
        assert _current_revision(hub) == retired_revision


def test_new_set_requires_explicit_reasoning(tmp_path):
    with _hub(tmp_path) as hub:
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "set",
                "expected_revision": _current_revision(hub),
                "provider": "cursor",
                "model": "cursor-grok-4.6-high-fast",
                "seat": "cursor_grok",
                "enabled": True,
                "brigade_cli": "cursor-agent",
                "t3_instance_id": "cursor",
            },
        )
        assert status == 400
        assert "reasoning" in payload["error"]
        assert _current_revision(hub) == 1


def test_retire_rejects_replace_family_without_mutation(tmp_path):
    with _hub(tmp_path) as hub:
        revision = _current_revision(hub)
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "retire",
                "provider": "openai",
                "family": "gpt-4",
                "permanent": False,
                "reason_code": "operator-retired",
                "replace_family": "gpt-5.4",
                "expected_revision": revision,
            },
        )
        assert status == 400
        assert "replace_family" in payload["error"]
        assert _current_revision(hub) == revision


def test_repeated_init_db_preserves_reasoning_bindings_and_revision(tmp_path):
    with _hub(tmp_path) as hub:
        assert _admin_set(hub)[0] == 200
        db_path = hub[2]
    first = fleet_hub.init_db(db_path)
    try:
        row = first.execute(
            "SELECT reasoning, brigade_cli, t3_instance_id, t3_service_tier FROM model_policy WHERE seat='cursor_grok'"
        ).fetchone()
        assert row == ("high", "cursor-agent", "cursor", "standard")
        revision = _revision(first)
        assert revision == 2
    finally:
        first.close()
    second = fleet_hub.init_db(db_path)
    try:
        assert (
            second.execute(
                "SELECT reasoning, brigade_cli, t3_instance_id, t3_service_tier FROM model_policy "
                "WHERE seat='cursor_grok'"
            ).fetchone()
            == row
        )
        assert _revision(second) == revision
    finally:
        second.close()


def test_admission_denial_without_seat_persists_nullable_fields(tmp_path):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        status, roster = _request(hub, "GET", "/models", token=node_token)
        assert status == 200
        admit = {
            "action": "admit",
            "schema": "brigade.model_admission_request.v1",
            "consumer": "t3-fleet",
            "seat": None,
            "request_id": "33333333-3333-4333-8333-333333333333",
            "phase": "controller",
            "expect_revision": roster["revision"],
            "expect_digest": roster["roster_digest"],
        }
        status, denied = _request(hub, "POST", "/models", token=node_token, body=admit)
        assert status == 409
        assert denied["error"] == "default-missing"
        assert denied["roster_revision"] == roster["revision"]
        assert denied["roster_digest"] == roster["roster_digest"]
        assert denied.get("seat") is None
        assert denied.get("provider") is None
        assert denied.get("model") is None
        assert denied.get("reasoning") is None
        status, replay = _request(hub, "POST", "/models", token=node_token, body=admit)
        assert status == 409
        assert replay == denied
        db = fleet_hub.open_db(hub[2])
        try:
            row = db.execute(
                "SELECT decision, seat, provider, model, reasoning, consumer_binding, roster_revision, roster_digest "
                "FROM model_admission_audit WHERE request_id=?",
                (admit["request_id"],),
            ).fetchone()
            assert row[0] == "default-missing"
            assert row[1] is None
            assert row[2] is None
            assert row[3] is None
            assert row[4] is None
            assert row[5] is None
            assert row[6] == roster["revision"]
            assert row[7] == roster["roster_digest"]
        finally:
            db.close()


def test_admin_token_cannot_acquire_or_release_model_lease(tmp_path):
    with _hub(tmp_path) as hub:
        assert _admin_set(hub)[0] == 200
        acquire = {
            "action": "acquire",
            "lease_id": "model-a",
            "node_id": NODE_A,
            "holder": "fence-a",
            "seat": "cursor_grok",
            "provider": "cursor",
            "model": "cursor-grok-4.6-high-fast",
            "ttl_seconds": 60,
        }
        status, payload = _request(hub, "POST", "/models", token=ADMIN_TOKEN, body=acquire)
        assert status == 403
        assert "node" in payload["error"]
        db = fleet_hub.open_db(hub[2])
        try:
            assert db.execute("SELECT COUNT(*) FROM model_leases").fetchone()[0] == 0
        finally:
            db.close()
        node_token = _enroll(hub)
        status, payload = _request(hub, "POST", "/models", token=node_token, body=acquire)
        assert status == 200
        assert payload["acquired"] is True
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={"action": "release", "lease_id": "model-a", "node_id": NODE_A, "holder": "fence-a"},
        )
        assert status == 403
        db = fleet_hub.open_db(hub[2])
        try:
            assert db.execute("SELECT COUNT(*) FROM model_leases WHERE released_at IS NULL").fetchone()[0] == 1
        finally:
            db.close()
