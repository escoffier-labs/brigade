"""Agent send policy, admin grants, and the version 30 migration."""

from datetime import datetime, timezone

import pytest

from brigade import fleet_hub, fleet_hub_messages as messages
from tests.test_fleet_messages import ADMIN, NODE_A, NODE_B, _post_path, _request

pytest_plugins = ["tests._fleet_message_fixtures"]


def enroll(base, **kwargs):
    status, result = _post_path(base, ADMIN, "/message-principals", {"action": "add", "name": "fixture", **kwargs})
    assert status == 201
    return result["principal"]


def send(base, token, recipient=NODE_A, **kwargs):
    return _request(base, token, {"action": "send", "to": recipient, "subject": "Task", "body": "Work", **kwargs})


def set_grants(base, patterns):
    return _post_path(
        base, ADMIN, "/message-principals", {"action": "grants", "name": "fixture", "may_send_to": patterns}
    )


def test_empty_grants_forbid_agent_send(hub):
    base, _, _, _ = hub
    principal = enroll(base)
    status, result = send(base, principal["token"])
    assert status == 403
    assert result["code"] == "message_recipient_not_permitted"


@pytest.mark.parametrize(
    "patterns,recipient,expected",
    [
        ([NODE_A], NODE_A, 201),
        (["node:*"], NODE_A, 201),
        (["node:*"], "agent:other", 403),
        (["agent:*"], "agent:other", 201),
        (["agent:*"], NODE_A, 403),
        (["*"], NODE_A, 201),
        (["*"], "agent:other", 201),
        (["agent:other"], "agent:other", 201),
        (["*"], "agent:fixture", 403),
    ],
)
def test_grant_patterns(hub, patterns, recipient, expected):
    base, _, _, _ = hub
    _post_path(base, ADMIN, "/message-principals", {"name": "other"})
    principal = enroll(base, may_send_to=patterns)
    assert send(base, principal["token"], recipient)[0] == expected


def test_grants_replacement_and_idempotent_replay(hub):
    base, _, _, _ = hub
    principal = enroll(base, may_send_to=[NODE_A])
    first = send(base, principal["token"], client_id="stable")
    assert first[0] == 201
    assert set_grants(base, [])[0] == 200
    assert send(base, principal["token"], client_id="stable") == first
    assert send(base, principal["token"])[0] == 403


def test_inbound_reply_requires_grants_and_root_retry_conflicts(hub):
    base, token_a, _, _ = hub
    principal = enroll(base)
    parent = send(base, token_a, "agent:fixture")[1]["message"]
    status, result = _request(base, principal["token"], {"action": "reply", "id": parent["id"], "body": "Reply"})
    assert status == 403
    assert result["code"] == "message_recipient_not_permitted"
    set_grants(base, [NODE_A])
    root = send(base, principal["token"], client_id="root")[1]["message"]
    set_grants(base, [])
    status, _ = _request(
        base, principal["token"], {"action": "reply", "id": root["id"], "body": "Work", "client_id": "root"}
    )
    # The root digest includes reply_to=None, so this retry conflicts before delivery.
    assert status == 409


def test_agent_recipient_cap_and_daily_quota(hub, monkeypatch):
    base, _, _, _ = hub
    principal = enroll(base, may_send_to=["*"])
    for index in range(200):
        assert send(base, principal["token"], client_id=f"cap-{index}")[0] == 201
    status, result = send(base, principal["token"])
    assert status == 429
    assert result["code"] == "message_sender_recipient_quota"
    monkeypatch.setattr(messages, "SENDER_DAILY_QUOTA", 200)
    status, result = send(base, principal["token"], NODE_B)
    assert status == 429
    assert result["code"] == "message_sender_quota"


def test_node_send_ignores_grants(hub):
    base, token_a, _, _ = hub
    assert send(base, token_a, NODE_B)[0] == 201


@pytest.mark.parametrize("action", ["add", "grants"])
def test_grants_admin_only(hub, action):
    base, token_a, _, _ = hub
    principal = enroll(base)
    body = {"action": action, "name": "fixture", "may_send_to": [NODE_A]}
    assert _post_path(base, token_a, "/message-principals", body)[0] == 403
    # Agent lookup is intentionally restricted to /messages.
    assert _post_path(base, principal["token"], "/message-principals", body)[0] == 401


def test_enrollment_replay_and_reenrollment_grants(hub):
    base, _, _, _ = hub
    request = {"request_id": "r" * 32, "token": "t" * 40, "may_send_to": [NODE_B, NODE_A, NODE_A]}
    principal = enroll(base, **request)
    assert principal["may_send_to"] == sorted([NODE_A, NODE_B])
    assert (
        _post_path(base, ADMIN, "/message-principals", {"action": "get", "name": "fixture"})[1]["principal"]["revoked"]
        is False
    )
    status, _ = _post_path(
        base, ADMIN, "/message-principals", {"action": "add", "name": "fixture", **request, "may_send_to": []}
    )
    assert status == 409
    _post_path(
        base,
        ADMIN,
        "/message-principals",
        {"action": "revoke", "name": "fixture", "expected_enrollment_id": principal["enrollment_id"]},
    )
    assert (
        _post_path(base, ADMIN, "/message-principals", {"action": "get", "name": "fixture"})[1]["principal"]["revoked"]
        is True
    )
    assert enroll(base, expected_enrollment_id=principal["enrollment_id"])["may_send_to"] == []


@pytest.mark.parametrize(
    "patterns", [["unknown"], ["agent:Bad"], ["bad/id"], [str(i) for i in range(65)], "node:*", None, [1]]
)
def test_invalid_patterns(hub, patterns):
    base, _, _, _ = hub
    assert _post_path(base, ADMIN, "/message-principals", {"name": "fixture", "may_send_to": patterns})[0] == 400


def test_v30_migration_preserves_message_digest(tmp_path):
    db = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(db)
    conn.execute("DROP TABLE fleet_messages")
    conn.execute("DROP TABLE IF EXISTS fleet_message_send_grants")
    conn.execute("""CREATE TABLE fleet_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, recipient TEXT NOT NULL,
        subject TEXT NOT NULL, body TEXT NOT NULL, reference TEXT, priority TEXT NOT NULL,
        client_id TEXT NOT NULL, payload_hash TEXT NOT NULL, reply_to INTEGER REFERENCES fleet_messages(id),
        created_at TEXT NOT NULL, received_at TEXT NOT NULL, acked_at TEXT, acked_by TEXT)""")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO fleet_messages(sender,recipient,subject,body,priority,client_id,payload_hash,created_at,received_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (NODE_A, NODE_B, "Old", "Body", "normal", "old", "unchanged-digest", now, now),
    )
    conn.execute("PRAGMA user_version=30")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(fleet_messages)")}
        assert {"sender_label", "thread", "kind"} <= columns
        assert conn.execute("SELECT payload_hash FROM fleet_messages").fetchone()[0] == "unchanged-digest"
        assert conn.execute("SELECT COUNT(*) FROM fleet_message_send_grants").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION > 30
    finally:
        conn.close()


def test_agent_replies_share_recipient_cap_with_initial_sends(hub):
    base, token_a, _, _ = hub
    principal = enroll(base, may_send_to=[NODE_A])
    parent = send(base, token_a, "agent:fixture")[1]["message"]
    assert send(base, principal["token"], client_id="initial")[0] == 201
    request = {"action": "reply", "id": parent["id"], "body": "Reply"}
    for index in range(199):
        assert _request(base, principal["token"], {**request, "client_id": f"reply-{index}"})[0] == 201
    status, result = _request(base, principal["token"], {**request, "client_id": "over-cap"})
    assert status == 429
    assert result["code"] == "message_sender_recipient_quota"
    # Accepted retries neither consume quota nor require a current grant.
    set_grants(base, [])
    assert _request(base, principal["token"], {**request, "client_id": "reply-0"})[0] == 201
    assert _request(base, principal["token"], {**request, "client_id": "new-reply"})[0] == 403


def test_ungranted_agent_cannot_probe_recipient_state(hub):
    base, _, _, _ = hub
    principal = enroll(base)
    other = _post_path(base, ADMIN, "/message-principals", {"name": "other"})[1]["principal"]
    _post_path(
        base,
        ADMIN,
        "/message-principals",
        {"action": "revoke", "name": "other", "expected_enrollment_id": other["enrollment_id"]},
    )
    responses = [send(base, principal["token"], recipient) for recipient in (NODE_A, "agent:other", "missing-node")]
    assert (
        responses
        == [(403, {"error": "recipient is not permitted by send grants", "code": "message_recipient_not_permitted"})]
        * 3
    )


def test_enrollment_replay_reports_changed_admin_grants(hub):
    base, _, _, _ = hub
    request = {"name": "fixture", "request_id": "r" * 32, "token": "t" * 40, "may_send_to": [NODE_A]}
    assert _post_path(base, ADMIN, "/message-principals", request)[0] == 201
    assert set_grants(base, [NODE_B])[0] == 200
    status, result = _post_path(base, ADMIN, "/message-principals", request)
    assert status == 409
    assert result["code"] == "principal_enrollment_grants_changed"
    assert "grants" in result["error"]
    assert _post_path(base, ADMIN, "/message-principals", {"action": "get", "name": "fixture"})[1]["principal"][
        "may_send_to"
    ] == [NODE_B]


def test_grant_validation_does_not_load_hub_server():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from brigade.fleet_hub_message_grants import normalize_patterns; import sys; assert normalize_patterns(['node-a']) == ['node-a']; assert 'brigade.fleet_hub' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
