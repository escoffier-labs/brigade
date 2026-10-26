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


def test_authorization_upgrade_from_intact_v45_via_init_db(tmp_path):
    import re

    from tests.test_fleet_hub_message_keys import publish, record
    from tests.test_fleet_hub_sealed import envelope

    db = tmp_path / "upgrade.db"
    conn = fleet_hub.init_db(db)
    for name, seed in (("node-a", 1), ("node-b", 2)):
        fleet_hub.add_node(conn, name)
        publish(conn, record(name, seed=seed))
    messages.handle(conn, {"action": "send", "to": "node-b", "subject": "Plain", "body": "Body"}, caller="node-a")
    messages.handle(conn, {"action": "send", "envelope": envelope()}, caller="node-a")
    active = messages.add_principal(conn, {"name": "active", "may_send_to": ["agent:revoked"]})
    revoked = messages.add_principal(conn, {"name": "revoked"})
    messages.revoke_principal(
        conn, {"action": "revoke", "name": "revoked", "expected_enrollment_id": revoked["enrollment_id"]}
    )
    conn.execute("UPDATE fleet_message_principals SET enrollment_id=NULL WHERE name='revoked'")
    # Reconstruct the intact v45 principal layout, leaving all other v45 schema intact.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fleet_message_principals)")}
    for column in ("authorization_enrollment_epoch", "grant_epoch"):
        if column in columns:
            conn.execute(f"ALTER TABLE fleet_message_principals DROP COLUMN {column}")
    prior_principals = conn.execute("SELECT * FROM fleet_message_principals ORDER BY name").fetchall()
    prior_grants = conn.execute("SELECT * FROM fleet_message_send_grants").fetchall()
    prior_messages = conn.execute("SELECT * FROM fleet_messages ORDER BY id").fetchall()
    conn.execute("PRAGMA user_version=45")
    conn.commit()
    assert not fleet_hub._missing_additive_columns(conn)
    conn.close()

    conn = fleet_hub.init_db(db)
    try:
        columns = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(fleet_message_principals)")}
        assert columns.get("authorization_enrollment_epoch") == "TEXT"
        assert columns.get("grant_epoch") == "TEXT"
        epochs = conn.execute(
            "SELECT authorization_enrollment_epoch,grant_epoch FROM fleet_message_principals ORDER BY name"
        ).fetchall()
        assert len({value for pair in epochs for value in pair}) == 4
        assert all(re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) for pair in epochs for value in pair)
        assert active["enrollment_id"] not in {value for pair in epochs for value in pair}
        assert (
            conn.execute(
                "SELECT principal_id,name,label,token_hash,created_at,revoked_at,enrollment_id "
                "FROM fleet_message_principals ORDER BY name"
            ).fetchall()
            == prior_principals
        )
        assert conn.execute("SELECT * FROM fleet_message_send_grants").fetchall() == prior_grants
        assert conn.execute("SELECT * FROM fleet_messages ORDER BY id").fetchall() == prior_messages
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 46
    finally:
        conn.close()
    conn = fleet_hub.init_db(db)
    try:
        assert (
            conn.execute(
                "SELECT authorization_enrollment_epoch,grant_epoch FROM fleet_message_principals ORDER BY name"
            ).fetchall()
            == epochs
        )
    finally:
        conn.close()


def test_epoch_generator_collision_is_bounded(monkeypatch):
    from brigade import fleet_hub_message_grants as grants

    calls = []
    values = iter(["a" * 32, "b" * 32])

    def generate(size):
        calls.append(size)
        return next(values)

    monkeypatch.setattr(messages.secrets, "token_urlsafe", generate)
    assert grants.new_authorization_epoch(excluded=("a" * 32,)) == "b" * 32
    assert calls == [24, 24]
    calls.clear()

    def collide(size):
        calls.append(size)
        return "a" * 32

    monkeypatch.setattr(messages.secrets, "token_urlsafe", collide)
    with pytest.raises(messages.MessageForbidden) as exc:
        grants.new_authorization_epoch(excluded=("a" * 32,))
    assert exc.value.code == "message_authorization_unavailable"
    assert calls == [24] * 4


def _public_epochs(conn, name="fixture"):
    return conn.execute(
        "SELECT authorization_enrollment_epoch,grant_epoch FROM fleet_message_principals WHERE name=?", (name,)
    ).fetchone()


def test_public_epochs_independent_of_request_and_token(tmp_path):
    import re

    conn = fleet_hub.init_db(tmp_path / "epochs.db")
    try:
        request = {"name": "fixture", "request_id": "a" * 40, "token": "a" * 40}
        principal = messages.add_principal(conn, request)
        epochs = _public_epochs(conn)
        assert all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) for value in epochs)
        assert len(set(epochs)) == 2
        assert not set(epochs).intersection(
            {principal["token"], principal["enrollment_id"], fleet_hub.hash_node_token(principal["token"])}
        )
    finally:
        conn.close()


def test_enrollment_replay_preserves_authorization_epochs(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "replay.db")
    try:
        request = {"name": "fixture", "request_id": "r" * 32, "token": "t" * 40, "may_send_to": [NODE_A]}
        messages.add_principal(conn, request)
        before = _public_epochs(conn)
        assert all(isinstance(value, str) for value in before)
        grants_before = conn.execute("SELECT * FROM fleet_message_send_grants").fetchall()
        messages.add_principal(conn, request)
        assert _public_epochs(conn) == before
        assert conn.execute("SELECT * FROM fleet_message_send_grants").fetchall() == grants_before
        for change in ({"token": "u" * 40}, {"label": "Changed"}, {"may_send_to": []}):
            with pytest.raises(messages.MessageConflict):
                messages.add_principal(conn, {**request, **change})
            assert _public_epochs(conn) == before
            assert not conn.in_transaction
        messages.revoke_principal(
            conn, {"action": "revoke", "name": "fixture", "expected_enrollment_id": request["request_id"]}
        )
        assert _public_epochs(conn) == before
        with pytest.raises(messages.MessageConflict) as exc:
            messages.add_principal(conn, request)
        assert exc.value.code == "principal_enrollment_revoked"
        assert _public_epochs(conn) == before
    finally:
        conn.close()


def test_reenrollment_reused_token_and_request_aba_changes_public_epoch(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "aba.db")
    try:
        token = "t" * 40
        seen = set()
        previous = None
        for request_id in ("a" * 32, "b" * 32, "a" * 32):
            messages.add_principal(
                conn, {"name": "fixture", "request_id": request_id, "token": token, "expected_enrollment_id": previous}
            )
            epochs = _public_epochs(conn)
            assert all(isinstance(value, str) for value in epochs)
            assert not seen.intersection(epochs)
            seen.update(epochs)
            # Token reuse remains permitted by the existing enrollment policy.
            assert messages.lookup_principal_token(conn, token) == "agent:fixture"
            messages.revoke_principal(
                conn, {"action": "revoke", "name": "fixture", "expected_enrollment_id": request_id}
            )
            assert _public_epochs(conn) == epochs
            previous = request_id
    finally:
        conn.close()


def test_equal_empty_restored_grants_advance_epoch(tmp_path):
    from brigade import fleet_hub_message_grants as grants

    conn = fleet_hub.init_db(tmp_path / "grants.db")
    try:
        messages.add_principal(conn, {"name": "fixture", "may_send_to": [NODE_A]})
        enrollment, current = _public_epochs(conn)
        assert isinstance(enrollment, str) and isinstance(current, str)
        seen = {current}
        for patterns in ([NODE_A], [], [], [NODE_A]):
            grants.set_grants(conn, {"action": "grants", "name": "fixture", "may_send_to": patterns})
            new_enrollment, current = _public_epochs(conn)
            assert new_enrollment == enrollment
            assert current not in seen
            seen.add(current)
            assert grants.grants_for(conn, "agent:fixture") == patterns
    finally:
        conn.close()


@pytest.mark.parametrize("operation", ["grants", "reenroll"])
def test_epoch_and_grant_rollback(tmp_path, operation):
    import sqlite3

    from brigade import fleet_hub_message_grants as grants

    conn = fleet_hub.init_db(tmp_path / "rollback.db")
    try:
        original = messages.add_principal(conn, {"name": "fixture", "may_send_to": [NODE_A]})
        if operation == "reenroll":
            messages.revoke_principal(
                conn, {"action": "revoke", "name": "fixture", "expected_enrollment_id": original["enrollment_id"]}
            )
        before = conn.execute("SELECT * FROM fleet_message_principals").fetchall()
        rows = conn.execute("SELECT * FROM fleet_message_send_grants").fetchall()
        conn.execute(
            "CREATE TRIGGER fail_epoch BEFORE UPDATE OF grant_epoch ON fleet_message_principals BEGIN SELECT RAISE(ABORT,'fixture epoch failure'); END"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="fixture epoch failure"):
            if operation == "grants":
                grants.set_grants(conn, {"action": "grants", "name": "fixture", "may_send_to": [NODE_B]})
            else:
                messages.add_principal(
                    conn,
                    {"name": "fixture", "expected_enrollment_id": original["enrollment_id"], "may_send_to": [NODE_B]},
                )
        assert conn.execute("SELECT * FROM fleet_message_principals").fetchall() == before
        assert conn.execute("SELECT * FROM fleet_message_send_grants").fetchall() == rows
        assert not conn.in_transaction
    finally:
        conn.close()


def test_authorization_migration_rollback_and_concurrent_startup(tmp_path, monkeypatch):
    import sqlite3
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from brigade import fleet_hub_message_grants as grants

    db = tmp_path / "concurrent-upgrade.db"
    conn = fleet_hub.init_db(db)
    messages.add_principal(conn, {"name": "fixture"})
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fleet_message_principals)")}
    for column in ("authorization_enrollment_epoch", "grant_epoch"):
        if column in columns:
            conn.execute(f"ALTER TABLE fleet_message_principals DROP COLUMN {column}")
    conn.execute("PRAGMA user_version=45")
    conn.commit()
    before = conn.execute("SELECT * FROM fleet_message_principals").fetchall()
    conn.close()
    generate = grants.new_authorization_epoch
    calls = []

    def fail_second(**kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise messages.MessageForbidden("fixture migration failure", code="message_authorization_unavailable")
        return generate(**kwargs)

    monkeypatch.setattr(grants, "new_authorization_epoch", fail_second)
    with pytest.raises(messages.MessageForbidden, match="fixture migration failure"):
        fleet_hub.init_db(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 45
        assert conn.execute("SELECT * FROM fleet_message_principals").fetchall() == before
        assert "authorization_enrollment_epoch" not in {
            row[1] for row in conn.execute("PRAGMA table_info(fleet_message_principals)")
        }
    finally:
        conn.close()
    monkeypatch.setattr(grants, "new_authorization_epoch", generate)
    start = threading.Barrier(2)

    def initialize():
        start.wait(timeout=5)
        local = fleet_hub.init_db(db)
        try:
            return _public_epochs(local), local.execute("PRAGMA user_version").fetchone()[0]
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(initialize), pool.submit(initialize)
        result = left.result(timeout=10)
        assert right.result(timeout=10) == result
    assert result[1] == 46 and all(isinstance(value, str) for value in result[0])


def test_authorization_backfill_preserves_nonnull_metadata(tmp_path):
    db = tmp_path / "partial-metadata.db"
    conn = fleet_hub.init_db(db)
    try:
        principal = messages.add_principal(conn, {"name": "fixture"})
        messages.revoke_principal(
            conn, {"action": "revoke", "name": "fixture", "expected_enrollment_id": principal["enrollment_id"]}
        )
        # Non-null corruption is preserved for fail-closed reads, not repaired.
        conn.execute("UPDATE fleet_message_principals SET authorization_enrollment_epoch='bad',grant_epoch=NULL")
        conn.execute("PRAGMA user_version=45")
        conn.commit()
    finally:
        conn.close()
    conn = fleet_hub.init_db(db)
    try:
        enrollment, grant = _public_epochs(conn)
        assert enrollment == "bad" and isinstance(grant, str)
        conn.execute("UPDATE fleet_message_principals SET authorization_enrollment_epoch=NULL")
        conn.execute("PRAGMA user_version=45")
        conn.commit()
    finally:
        conn.close()
    conn = fleet_hub.init_db(db)
    try:
        enrollment, preserved = _public_epochs(conn)
        assert isinstance(enrollment, str) and enrollment != grant
        assert preserved == grant
    finally:
        conn.close()


def test_epoch_replacements_retry_current_values_and_fail_atomically(tmp_path, monkeypatch):
    from brigade import fleet_hub_message_grants as grants

    conn = fleet_hub.init_db(tmp_path / "epoch-collisions.db")
    try:
        principal = messages.add_principal(
            conn, {"name": "fixture", "request_id": "r" * 32, "token": "t" * 40, "may_send_to": [NODE_A]}
        )
        enrollment, grant = _public_epochs(conn)
        calls = []
        values = iter([grant, "g" * 32])

        def generate(size):
            calls.append(size)
            return next(values)

        monkeypatch.setattr(messages.secrets, "token_urlsafe", generate)
        grants.set_grants(conn, {"action": "grants", "name": "fixture", "may_send_to": [NODE_B]})
        assert _public_epochs(conn) == (enrollment, "g" * 32)
        assert calls == [24, 24]
        messages.revoke_principal(
            conn, {"action": "revoke", "name": "fixture", "expected_enrollment_id": principal["enrollment_id"]}
        )
        before = conn.execute("SELECT * FROM fleet_message_principals").fetchall()
        rows = conn.execute("SELECT * FROM fleet_message_send_grants").fetchall()
        calls.clear()
        # Enrollment changes first, but grant collision exhaustion must roll
        # the entire re-enrollment and its credential change back.
        values = iter([enrollment, "e" * 32, "g" * 32, "g" * 32, "g" * 32, "g" * 32])
        with pytest.raises(messages.MessageForbidden) as exc:
            messages.add_principal(
                conn,
                {
                    "name": "fixture",
                    "request_id": "s" * 32,
                    "token": "u" * 40,
                    "expected_enrollment_id": principal["enrollment_id"],
                    "may_send_to": [],
                },
            )
        assert exc.value.code == "message_authorization_unavailable"
        assert calls == [24] * 6
        assert conn.execute("SELECT * FROM fleet_message_principals").fetchall() == before
        assert conn.execute("SELECT * FROM fleet_message_send_grants").fetchall() == rows
        assert not conn.in_transaction
    finally:
        conn.close()
