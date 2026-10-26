"""Sealed storage contracts at the authenticated Hub message boundary."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_messages as messages
from tests.test_fleet_hub_message_keys import binary, publish, record


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@pytest.fixture
def hub(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    for principal, seed in (("node-a", 1), ("node-b", 2), ("node-c", 3)):
        fleet_hub.add_node(conn, principal)
        publish(conn, record(principal, seed=seed))
    yield conn
    conn.close()


def envelope(sender="node-a", recipient="node-b", client_id="sealed-1", **changes):
    kids = {
        "node-a": record("node-a", seed=1)["kid"],
        "node-b": record("node-b", seed=2)["kid"],
        "node-c": record("node-c", seed=3)["kid"],
    }
    rcpt = sorted({kids[sender], kids[recipient]})
    value = {
        "v": 1,
        "suite": "bm1",
        "hub": "h_" + binary(bytes(16)),
        "from": sender,
        "from_kid": kids[sender],
        "to": recipient,
        "kind": "message",
        "priority": "normal",
        "client_id": client_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pad": 1024,
        "rcpt": rcpt,
        "ct_sha256": binary(bytes(32)),
        "wraps_sha256": binary(bytes(32)),
        "rc": binary(bytes(32)),
        "recipients": [{"kid": k, "wrap": binary(bytes(1168))} for k in rcpt],
        "ct": binary(bytes(1040)),
        "sig": binary(bytes(64)),
    }
    value.update(changes)
    return canonical(value)


def send(conn, value, caller="node-a", **extra):
    return messages.handle(conn, {"action": "send", "envelope": value, **extra}, caller=caller)[1]["message"]


def lookup(conn, client_id="sealed-1", caller="node-a"):
    return messages.handle(conn, {"action": "sent_lookup", "client_id": client_id}, caller=caller)


def test_sealed_storage_routing_and_reads(hub):
    raw = envelope(priority="high")
    value = send(hub, raw)
    assert value["envelope"] == raw and value["content_state"] == "sealed"
    stored = hub.execute(
        "SELECT subject,body,reference,thread,sender_label,payload_hash,envelope_bytes FROM fleet_messages"
    ).fetchone()
    assert stored == ("", "", None, None, None, hashlib.sha256(raw.encode()).hexdigest(), len(raw))
    assert (value["recipient"], value["priority"]) == ("node-b", "high")
    assert messages.handle(hub, {"action": "count"}, caller="node-b")[1] == {"unread_count": 1}
    assert messages.handle(hub, {"action": "list"}, caller="node-b")[1]["messages"] == [value]
    assert messages.handle(hub, {"action": "sent"}, caller="node-a")[1]["messages"] == [value]
    assert lookup(hub)[1]["message"] == value
    assert lookup(hub, caller="node-c") == (404, {"error": "message not found", "code": "not_found"})


@pytest.mark.parametrize(
    "field",
    [
        "subject",
        "body",
        "ref",
        "reference",
        "thread",
        "sender_label",
        "to",
        "kind",
        "priority",
        "client_id",
        "enqueued_at",
        "deadline",
    ],
)
def test_envelope_rejects_parallel_plaintext_and_routing(hub, field):
    with pytest.raises(messages.MessageError):
        send(hub, envelope(), **{field: "leak"})
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"from": "node-c"}, "message_sender_mismatch"),
        ({"from_kid": record(seed=9)["kid"]}, "sender_key_stale"),
        ({"rcpt": sorted([record("node-a", seed=1)["kid"], record(seed=9)["kid"]])}, "recipient_key_stale"),
    ],
)
def test_caller_and_current_keys_fenced_without_writes(hub, change, code):
    value = json.loads(envelope())
    value.update(change)
    if "from_kid" in change:
        value["rcpt"] = sorted([value["from_kid"], record("node-b", seed=2)["kid"]])
    value["recipients"] = [{"kid": k, "wrap": binary(bytes(1168))} for k in value["rcpt"]]
    with pytest.raises(messages.MessageError) as caught:
        send(hub, canonical(value))
    assert caught.value.code == code
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
    assert hub.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone()[0] == 0


def test_retry_precedes_rotation_and_conflict_includes_existing_id(hub):
    raw = envelope()
    value = send(hub, raw)
    for principal, seed in (("node-a", 1), ("node-b", 2)):
        publish(
            hub,
            record(principal, seed=seed + 10, prev_kid=record(principal, seed=seed)["kid"], prev_sig=binary(bytes(64))),
        )
    assert send(hub, raw) == value
    changed = json.loads(raw)
    changed["rc"] = binary(bytes([1]) * 32)
    with pytest.raises(messages.MessageConflict) as caught:
        send(hub, canonical(changed))
    assert caught.value.code == "message_client_id_conflict" and caught.value.message_id == value["id"]
    assert hub.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone()[0] == 1


def test_reply_presence_and_sealed_reply(hub):
    raw = envelope()
    original = send(hub, raw)
    link = {"hub": json.loads(raw)["hub"], "from": "node-a", "client_id": "sealed-1"}
    with pytest.raises(messages.MessageError):
        send(hub, envelope("node-b", "node-a", "reply-1"), caller="node-b", reply_to=original["id"])
    with pytest.raises(messages.MessageError):
        send(hub, envelope("node-b", "node-a", "reply-1", in_reply_to=link), caller="node-b")
    reply = messages.handle(
        hub,
        {
            "action": "reply",
            "id": original["id"],
            "envelope": envelope("node-b", "node-a", "reply-1", in_reply_to=link),
        },
        caller="node-b",
    )[1]["message"]
    assert reply["reply_to"] == original["id"] and reply["subject"] == ""


def test_retention_purges_only_content_and_keeps_retry_hash(hub):
    raw = envelope()
    value = send(hub, raw)
    messages.handle(hub, {"action": "ack", "id": value["id"]}, caller="node-b")
    before = hub.execute("SELECT payload_hash FROM fleet_messages").fetchone()[0]
    hub.execute("UPDATE fleet_messages SET acked_at=?", ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
    hub.commit()
    tomb = lookup(hub)[1]["message"]
    assert tomb["content_state"] == "content_expired"
    header = json.loads(tomb["envelope"])
    assert "ct" not in header and "recipients" not in header
    assert header == {k: v for k, v in json.loads(raw).items() if k not in ("ct", "recipients")}
    assert hub.execute("SELECT payload_hash,envelope_bytes FROM fleet_messages").fetchone() == (
        before,
        len(tomb["envelope"]),
    )
    assert send(hub, raw) == tomb
    hub.execute(
        "UPDATE fleet_messages SET created_at=?", ((datetime.now(timezone.utc) - timedelta(days=91)).isoformat(),)
    )
    hub.commit()
    assert lookup(hub)[0] == 404


@pytest.mark.parametrize(
    ("limit", "code"),
    [
        ("sender_storage_bytes", "sender_storage_quota"),
        ("fleet_storage_bytes", "hub_storage_full"),
        ("max_envelope_bytes", "message_too_large"),
    ],
)
def test_storage_limits_refuse_atomically(hub, limit, code):
    from brigade.fleet_hub_sealed import StoragePolicy

    policy = StoragePolicy(**{limit: 1})
    with pytest.raises(messages.MessageError) as caught:
        messages.handle(hub, {"action": "send", "envelope": envelope()}, caller="node-a", storage_policy=policy)
    assert caught.value.code == code
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
    assert hub.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone()[0] == 0


def test_require_sealed_preserves_legacy_reads(hub):
    from brigade.fleet_hub_sealed import StoragePolicy

    legacy = messages.handle(
        hub, {"action": "send", "to": "node-b", "subject": "Legacy", "body": "Legacy"}, caller="node-a"
    )[1]["message"]
    policy = StoragePolicy(require_sealed=True)
    with pytest.raises(messages.MessageError) as caught:
        messages.handle(
            hub,
            {"action": "send", "to": "node-b", "subject": "Plain", "body": "Plain"},
            caller="node-a",
            storage_policy=policy,
        )
    assert caught.value.code == "message_sealing_required"
    assert messages.handle(hub, {"action": "list"}, caller="node-b", storage_policy=policy)[1]["messages"] == [legacy]


def test_concurrent_retry_one_charge(hub, monkeypatch):
    from brigade import fleet_hub_events as events

    monkeypatch.setattr(events, "_CONFIG", events.EventsConfig(frozenset({"callback.invalid"})))
    hub.execute(
        "INSERT INTO fleet_event_subscriptions(id,principal_id,name,arguments,filter_value,url,secret,created_at) "
        "VALUES('sub-fixture','node-b','message.created','{}','node-b','https://callback.invalid/events','fixture-only',0)"
    )
    hub.commit()
    raw = envelope()
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    barrier = threading.Barrier(2)

    def attempt(_):
        conn = sqlite3.connect(path)
        try:
            barrier.wait(timeout=5)
            return send(conn, raw)["id"]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(attempt, range(2)))
    assert ids[0] == ids[1]
    event_rows = hub.execute("SELECT data FROM fleet_events WHERE name='message.created'").fetchall()
    assert len(event_rows) == 1
    event = json.loads(event_rows[0][0])
    assert event["message_id"] == str(ids[0])
    assert not ({"envelope", "ct", "recipients", "wrap", "body", "subject"} & event.keys())
    assert sum(raw in sql for sql in hub.iterdump()) == 1
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 1
    assert hub.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone()[0] == 1


@pytest.mark.parametrize("via_environment", [False, True])
def test_http_policy_and_retry_conflict(hub, monkeypatch, via_environment):
    from tests.test_fleet_hub_message_keys import request

    if via_environment:
        monkeypatch.setenv("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED", "1")
    _, token = fleet_hub.add_node(hub, "node-http")
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin", require_sealed=not via_environment)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        address = server.server_address
        status, payload = request(
            address, "POST", "/messages", token, {"action": "send", "to": "node-b", "subject": "Plain", "body": "Plain"}
        )
        assert status == 400 and payload["code"] == "message_sealing_required"
        # A node token is only returned at enrollment, so create this caller's key.
        publish(hub, record("node-http", seed=4))
        raw = json.loads(envelope())
        raw["from"] = "node-http"
        raw["from_kid"] = record("node-http", seed=4)["kid"]
        raw["rcpt"] = sorted([raw["from_kid"], record("node-b", seed=2)["kid"]])
        raw["recipients"] = [{"kid": k, "wrap": binary(bytes(1168))} for k in raw["rcpt"]]
        status, value = request(address, "POST", "/messages", token, {"action": "send", "envelope": canonical(raw)})
        assert status == 201, value
        raw["rc"] = binary(bytes([1]) * 32)
        status, conflict = request(address, "POST", "/messages", token, {"action": "send", "envelope": canonical(raw)})
        assert status == 409 and conflict["code"] == "message_client_id_conflict"
        assert conflict["id"] == value["message"]["id"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_require_sealed_reaches_serve(monkeypatch):
    from brigade import cli

    seen = {}
    monkeypatch.setattr(fleet_hub, "run", lambda **kwargs: seen.update(kwargs) or 0)
    assert cli.main(["fleet", "serve", "--host", "127.0.0.1", "--require-sealed"]) == 0
    assert seen["require_sealed"] is True


def test_startup_repairs_schema_without_request_time_ddl(hub):
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    legacy = messages.handle(
        hub, {"action": "send", "to": "node-b", "subject": "Keep", "body": "Keep"}, caller="node-a"
    )[1]["message"]
    # Simulate the older schema, which had neither sealed columns nor its index.
    hub.execute("DROP INDEX fleet_messages_sealed_acked")
    for name in ("envelope", "envelope_bytes", "content_state"):
        hub.execute(f"ALTER TABLE fleet_messages DROP COLUMN {name}")
    hub.commit()
    repaired = fleet_hub.init_db(path)
    try:
        assert repaired.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        statements = []
        repaired.set_trace_callback(statements.append)
        assert messages.handle(repaired, {"action": "sent"}, caller="node-a")[1]["messages"] == [legacy]
        assert send(repaired, envelope())["id"] > legacy["id"]
        assert not any(sql.lstrip().upper().startswith(("CREATE ", "ALTER ")) for sql in statements)
    finally:
        repaired.close()


def test_quota_reclaims_purged_content_and_retains_tombstone_charge(hub):
    from brigade.fleet_hub_sealed import StoragePolicy

    first_raw, second_raw = envelope(), envelope(client_id="sealed-2")
    policy = StoragePolicy(sender_storage_bytes=len(first_raw) + len(second_raw) - 1)
    first = messages.handle(hub, {"action": "send", "envelope": first_raw}, caller="node-a", storage_policy=policy)[1][
        "message"
    ]
    with pytest.raises(messages.MessageQuotaError) as caught:
        messages.handle(hub, {"action": "send", "envelope": second_raw}, caller="node-a", storage_policy=policy)
    assert caught.value.code == "sender_storage_quota"
    hub.execute(
        "UPDATE fleet_messages SET acked_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), first["id"]),
    )
    hub.commit()
    second = messages.handle(hub, {"action": "send", "envelope": second_raw}, caller="node-a", storage_policy=policy)[
        1
    ]["message"]
    assert second["id"] != first["id"]
    assert hub.execute("SELECT SUM(envelope_bytes) FROM fleet_messages").fetchone()[0] == len(second_raw) + len(
        lookup(hub)[1]["message"]["envelope"]
    )


def test_unacked_ttl_and_seven_day_purge_boundary(hub, monkeypatch):
    raw = envelope()
    value = send(hub, raw)
    hub.execute("UPDATE fleet_messages SET acked_at=?", ((datetime.now(timezone.utc) - timedelta(days=6)).isoformat(),))
    hub.commit()
    assert lookup(hub)[1]["message"]["envelope"] == raw
    hub.execute(
        "UPDATE fleet_messages SET acked_at=NULL,created_at=?",
        ((datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),),
    )
    hub.commit()
    assert lookup(hub)[1]["message"]["id"] == value["id"]
    monkeypatch.setattr(messages, "RETENTION_DAYS", 3)
    assert lookup(hub)[0] == 404


@pytest.mark.parametrize("self_send", [False, True])
def test_current_key_recipient_and_grants_apply_to_sealed(hub, self_send):
    if self_send:
        assert send(hub, envelope(recipient="node-a"))["recipient"] == "node-a"
        return
    enrolled = messages.add_principal(hub, {"name": "limited"})
    key = record(enrolled["principal_id"], seed=4)
    publish(hub, key)
    value = json.loads(envelope())
    value["from"], value["from_kid"] = enrolled["principal_id"], key["kid"]
    value["rcpt"] = sorted([key["kid"], record("node-b", seed=2)["kid"]])
    value["recipients"] = [{"kid": k, "wrap": binary(bytes(1168))} for k in value["rcpt"]]
    with pytest.raises(messages.MessageForbidden):
        send(hub, canonical(value), caller=enrolled["principal_id"])
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
    fleet_hub.revoke_node(hub, "node-b")
    with pytest.raises(messages.MessageRecipientInactive):
        send(hub, envelope())


def test_concurrent_quota_admission_has_no_partial_charge(hub):
    from brigade.fleet_hub_sealed import StoragePolicy

    raws = [envelope(client_id=f"quota-{i}") for i in range(2)]
    policy = StoragePolicy(sender_storage_bytes=max(map(len, raws)))
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    barrier = threading.Barrier(2)

    def attempt(raw):
        conn = sqlite3.connect(path)
        try:
            barrier.wait(timeout=5)
            return messages.handle(conn, {"action": "send", "envelope": raw}, caller="node-a", storage_policy=policy)[0]
        except messages.MessageQuotaError as exc:
            return exc.code
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, raws))
    assert outcomes.count(201) == 1 and outcomes.count("sender_storage_quota") == 1
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 1
    assert hub.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone()[0] == 1


def test_sent_lookup_rechecks_authenticated_generation(hub):
    send(hub, envelope())
    with pytest.raises(messages.MessageAuthError):
        messages.handle(
            hub,
            {"action": "sent_lookup", "client_id": "sealed-1"},
            caller="node-a",
            authenticated_generation="replaced-credential",
        )


def test_environment_storage_cap_is_enforced_over_http(hub, monkeypatch):
    from tests.test_fleet_hub_message_keys import request

    monkeypatch.setenv("BRIGADE_FLEET_MESSAGES_FLEET_STORAGE_BYTES", "1")
    _, token = fleet_hub.add_node(hub, "node-http")
    publish(hub, record("node-http", seed=4))
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        value = json.loads(envelope())
        value["from"], value["from_kid"] = "node-http", record("node-http", seed=4)["kid"]
        value["rcpt"] = sorted([value["from_kid"], record("node-b", seed=2)["kid"]])
        value["recipients"] = [{"kid": k, "wrap": binary(bytes(1168))} for k in value["rcpt"]]
        status, payload = request(
            server.server_address, "POST", "/messages", token, {"action": "send", "envelope": canonical(value)}
        )
        assert status == 429 and payload["code"] == "hub_storage_full"
        assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
        assert hub.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "mutation", ["newline", "duplicate", "nonascii", "deep", "missing", "tombstone", "unordered", "size"]
)
def test_malformed_raw_envelopes_never_write(hub, mutation):
    raw = envelope()
    if mutation == "newline":
        raw += "\n"
    elif mutation == "duplicate":
        raw = raw.replace('{"client_id":', '{"v":1,"client_id":', 1)
    elif mutation == "nonascii":
        raw = raw.replace('"normal"', '"nörmal"')
    elif mutation == "deep":
        raw = '{"extra":[[[[[]]]]],"envelope":' + raw + "}"
    elif mutation == "size":
        raw = " " * (96 * 1024 + 1)
    else:
        value = json.loads(raw)
        if mutation == "missing":
            del value["from"]
        elif mutation == "tombstone":
            del value["ct"]
            del value["recipients"]
        else:
            value = dict(reversed(list(value.items())))
        raw = json.dumps(value, separators=(",", ":"))
    with pytest.raises(messages.MessageError) as caught:
        send(hub, raw)
    assert caught.value.code == ("message_too_large" if mutation == "size" else "envelope_malformed")
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0


def test_sealed_operations_without_crypto_modules(tmp_path):
    import subprocess
    import sys

    code = """
import sys
sys.modules['cryptography'] = None
sys.modules['brigade.fleet_mesh_crypto'] = None
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory
ns = runpy.run_path('tests/test_fleet_hub_sealed.py')
with TemporaryDirectory() as directory:
    conn = ns['fleet_hub'].init_db(Path(directory) / 'hub.db')
    for principal, seed in (('node-a', 1), ('node-b', 2)):
        ns['fleet_hub'].add_node(conn, principal)
        ns['publish'](conn, ns['record'](principal, seed=seed))
    message = ns['send'](conn, ns['envelope']())
    assert message['content_state'] == 'sealed'
    assert ns['lookup'](conn)[1]['message'] == message
    from brigade import fleet_admin_stats
    from datetime import datetime, timedelta, timezone
    conn.execute('UPDATE fleet_messages SET acked_at=?', ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
    conn.commit()
    fleet_admin_stats.maintain(Path(directory) / 'hub.db')
    assert conn.execute('SELECT content_state FROM fleet_messages').fetchone()[0] == 'content_expired'
    conn.close()
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("advertised,enforced", [(False, True), (True, False)])
def test_required_policy_preserves_signed_identity(hub, advertised, enforced):
    from brigade import fleet_hub_identity
    from brigade.fleet_hub_sealed import StoragePolicy
    from tests.test_fleet_hub_message_keys import identity

    statement = {**identity(), "require_sealed": advertised}
    fleet_hub_identity.upload_identity(hub, statement)
    before = hub.execute("SELECT value FROM hub_meta WHERE key='identity'").fetchone()[0]
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    with pytest.raises(fleet_hub.FleetHubError, match="admin-signed"):
        fleet_hub.make_server(
            "127.0.0.1", 0, path, "fixture-admin", storage_policy=StoragePolicy(require_sealed=enforced)
        )
    assert fleet_hub_identity.identity(hub) == statement
    assert hub.execute("SELECT value FROM hub_meta WHERE key='identity'").fetchone()[0] == before


@pytest.mark.parametrize("action", ["question_create", "question_desktop"])
def test_required_sealing_blocks_legacy_question_writes(hub, monkeypatch, action):
    from brigade.fleet_hub_questions import _generation
    from brigade.fleet_hub_sealed import StoragePolicy

    messages.add_principal(hub, {"name": "operator", "may_send_to": ["node-a"]})
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", "agent:operator")
    raw = {"action": "question_create", "question": "Proceed?", "request_id": "legacy-question"}
    generation = _generation(hub, "node-a")
    if action == "question_desktop":
        question = messages.handle(hub, raw, caller="node-a", authenticated_generation=generation)[1]
        raw = {"action": action, "question_id": question["question_id"], "answer": "Legacy answer"}
    before = hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0]
    with pytest.raises(messages.MessageError) as caught:
        messages.handle(
            hub,
            raw,
            caller="node-a",
            authenticated_generation=generation,
            storage_policy=StoragePolicy(require_sealed=True),
        )
    assert caught.value.code == "message_sealing_required"
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == before
    assert hub.execute("SELECT answer FROM fleet_questions WHERE answer IS NOT NULL").fetchall() == []


def test_retention_batches_and_repairs_current_schema(hub):
    from brigade import fleet_hub_sealed as sealed

    raw = envelope()
    first = send(hub, raw)
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    hub.execute("UPDATE fleet_messages SET acked_at=?", (old,))
    for i in range(70):
        hub.execute(
            "INSERT INTO fleet_messages(sender,recipient,subject,body,priority,client_id,payload_hash,created_at,received_at,acked_at,envelope,envelope_bytes,content_state) "
            "SELECT sender,recipient,subject,body,priority,?,payload_hash,created_at,received_at,acked_at,envelope,envelope_bytes,content_state FROM fleet_messages WHERE id=?",
            (f"batch-{i}", first["id"]),
        )
    hub.commit()
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    hub.execute("DROP INDEX IF EXISTS fleet_messages_sealed_acked")
    hub.commit()
    repaired = fleet_hub.init_db(path)
    try:
        indexes = {r[1] for r in repaired.execute("PRAGMA index_list(fleet_messages)")}
        assert "fleet_messages_sealed_acked" in indexes
        plan = repaired.execute(
            "EXPLAIN QUERY PLAN SELECT id,envelope FROM fleet_messages WHERE content_state='sealed' AND acked_at<=? ORDER BY acked_at,id LIMIT 32",
            (old,),
        ).fetchall()
        assert any("fleet_messages_sealed_acked" in str(row) for row in plan)
        before = repaired.execute("SELECT COUNT(*) FROM fleet_messages WHERE content_state='sealed'").fetchone()[0]
        with repaired:
            sealed.purge_content(repaired)
        after = repaired.execute("SELECT COUNT(*) FROM fleet_messages WHERE content_state='sealed'").fetchone()[0]
        assert before - after == 32
        from pathlib import Path
        from brigade import fleet_admin_stats as stats

        statements = []
        open_db = fleet_hub.open_db
        from unittest.mock import patch

        def traced_open(db_path):
            conn = open_db(db_path)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(fleet_hub, "open_db", traced_open):
            stats.maintain(Path(path))
        assert repaired.execute("SELECT COUNT(*) FROM fleet_messages WHERE content_state='sealed'").fetchone()[0] == 0
        batch_sizes, updates = [], 0
        for sql in statements:
            if sql.startswith("UPDATE fleet_messages SET envelope="):
                updates += 1
            elif sql == "COMMIT":
                batch_sizes.append(updates)
                updates = 0
        assert sum(batch_sizes) == after
        assert max(batch_sizes) <= 32 and len([n for n in batch_sizes if n]) >= 2
        assert repaired.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        repaired.close()


@pytest.mark.parametrize("phase", ["startup", "idle"])
def test_retention_without_message_operations(hub, monkeypatch, phase):
    import time
    from pathlib import Path
    from brigade import fleet_admin_stats as stats, fleet_hub_sealed as sealed

    clock = [datetime.now(timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(sealed, "datetime", Clock)
    raw = envelope()
    send(hub, raw)
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    path = Path(hub.execute("PRAGMA database_list").fetchone()[2])
    if phase == "startup":
        hub.execute("UPDATE fleet_messages SET acked_at=?", (old,))
        hub.commit()
        server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin")
        server.server_close()
    else:
        # Cross the seven-day cutoff after startup. The recurring production
        # loop must purge with no message operation to trigger cleanup.
        hub.execute("UPDATE fleet_messages SET acked_at=?", ((clock[0] - timedelta(days=6)).isoformat(),))
        hub.commit()
        monkeypatch.setattr(stats, "RETENTION_INTERVAL_SECONDS", 0.01)
        stop = threading.Event()
        worker = stats.start_retention_sweeper(path, stop=stop)
        try:
            assert hub.execute("SELECT content_state FROM fleet_messages").fetchone()[0] == "sealed"
            clock[0] += timedelta(days=2)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if hub.execute("SELECT content_state FROM fleet_messages").fetchone()[0] == "content_expired":
                    break
                stop.wait(0.01)
        finally:
            stop.set()
            worker.join(timeout=2)
    text, size, state = hub.execute("SELECT envelope,envelope_bytes,content_state FROM fleet_messages").fetchone()
    assert state == "content_expired"
    value = json.loads(raw)
    del value["ct"], value["recipients"]
    assert text == canonical(value) and size == len(text)
    assert hub.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("upgrade", [False, True])
def test_sealed_reply_hint_survives_parent_expiry(hub, upgrade):
    from brigade import fleet_mesh_crypto as mesh

    # Real signed bm1 header: the Hub still only parses/stores these bytes.
    now = datetime.now(timezone.utc)
    hub_id = "h_" + binary(bytes(16))
    keys = [mesh.PrivateKeys(bytes([i]) * 32, bytes([i + 2]) * 32) for i in (1, 2)]
    records = [
        mesh.build_key_record(k, principal=p, hub=hub_id, created_at=now.isoformat())
        for k, p in zip(keys, ("node-a", "node-b"), strict=True)
    ]
    hub.execute("DELETE FROM fleet_message_keys")
    hub.commit()
    for key in records:
        publish(hub, key)
    payload = {"subject": "subject", "body": "body", "ref": None, "thread": None, "sender_label": None}
    original_raw = canonical(
        mesh.seal(
            payload,
            sender=keys[0],
            sender_record=records[0],
            recipient_record=records[1],
            retry_key=bytes(32),
            client_id="original",
            created_at=(now - timedelta(days=89)).isoformat(),
        )
    )
    original = send(hub, original_raw)
    link = {"hub": hub_id, "from": "node-a", "client_id": "original"}
    raw = canonical(
        mesh.seal(
            payload,
            sender=keys[1],
            sender_record=records[1],
            recipient_record=records[0],
            retry_key=bytes(32),
            client_id="reply",
            created_at=now.isoformat(),
            in_reply_to=link,
        )
    )
    reply = send(hub, raw, caller="node-b", reply_to=original["id"])
    if upgrade:
        # A current-version DB can already have sealed columns and candidate
        # replies while still lacking the new immutable hint.
        if "sealed_reply_to" in {r[1] for r in hub.execute("PRAGMA table_info(fleet_messages)")}:
            hub.execute("ALTER TABLE fleet_messages DROP COLUMN sealed_reply_to")
        hub.commit()
        repaired = fleet_hub.init_db(hub.execute("PRAGMA database_list").fetchone()[2])
        repaired.close()
    hub.execute("PRAGMA foreign_keys=ON")
    hub.execute(
        "UPDATE fleet_messages SET created_at=? WHERE id=?", ((now - timedelta(days=91)).isoformat(), original["id"])
    )
    hub.commit()
    stored = lookup(hub, "reply", "node-b")[1]["message"]
    assert stored["reply_to"] == original["id"]
    assert stored["envelope"] == raw
    assert mesh.verify_signed_header(raw.encode("ascii"), records[1])["in_reply_to"] == link
    assert hub.execute("SELECT 1 FROM fleet_messages WHERE id=?", (original["id"],)).fetchone() is None
    assert messages.handle(hub, {"action": "list"}, caller="node-a")[1]["messages"] == [stored]
    assert messages.handle(hub, {"action": "sent"}, caller="node-b")[1]["messages"] == [stored]
    assert send(hub, raw, caller="node-b", reply_to=original["id"]) == stored
    assert stored["id"] == reply["id"]


@pytest.mark.parametrize("field,value", [("require_sealed", True), ("max_envelope_bytes", 32768)])
def test_identity_replacement_must_match_enforcement(hub, field, value):
    from brigade import fleet_hub_identity
    from tests.test_fleet_hub_message_keys import identity, request

    statement = identity()
    fleet_hub_identity.upload_identity(hub, statement)
    before = hub.execute("SELECT value FROM hub_meta WHERE key='identity'").fetchone()[0]
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        replacement = {**statement, "seq": statement["seq"] + 1, field: value}
        status, response = request(server.server_address, "POST", "/mesh/identity", "fixture-admin", replacement)
        assert status == 400 and "admin-signed" in response["error"]
        assert request(server.server_address, "GET", "/mesh/identity") == (200, statement)
        assert hub.execute("SELECT value FROM hub_meta WHERE key='identity'").fetchone()[0] == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("require_sealed", 1),
        ("require_sealed", "false"),
        ("max_envelope_bytes", True),
        ("max_envelope_bytes", 0),
        ("max_envelope_bytes", 98305),
    ],
)
def test_identity_policy_fields_are_exact_bounded_types(hub, field, value):
    from brigade import fleet_hub_identity
    from tests.test_fleet_hub_message_keys import identity

    with pytest.raises(messages.MessageError):
        fleet_hub_identity.upload_identity(hub, {**identity(), field: value})
    assert fleet_hub_identity.identity(hub) is None


def test_startup_refuses_advertised_cap_disagreement(hub):
    from brigade import fleet_hub_identity
    from tests.test_fleet_hub_message_keys import identity

    statement = {**identity(), "max_envelope_bytes": 32768}
    fleet_hub_identity.upload_identity(hub, statement)
    before = hub.execute("SELECT value FROM hub_meta WHERE key='identity'").fetchone()[0]
    with pytest.raises(fleet_hub.FleetHubError, match="admin-signed"):
        fleet_hub.make_server("127.0.0.1", 0, hub.execute("PRAGMA database_list").fetchone()[2], "fixture-admin")
    assert hub.execute("SELECT value FROM hub_meta WHERE key='identity'").fetchone()[0] == before


@pytest.mark.parametrize("action", ["reply", "send"])
def test_sealed_reply_cannot_complete_legacy_operator_question(hub, monkeypatch, action):
    from brigade.fleet_hub_questions import _generation

    operator = messages.add_principal(hub, {"name": "operator", "may_send_to": ["node-a"]})["principal_id"]
    operator_record = record(operator, seed=7)
    publish(hub, operator_record)
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", operator)
    question = messages.handle(
        hub,
        {"action": "question_create", "question": "Proceed?", "request_id": "question"},
        caller="node-a",
        authenticated_generation=_generation(hub, "node-a"),
    )[1]
    message_id = int(question["message_id"])
    value = json.loads(envelope("node-b", "node-a", "sealed-answer"))
    value.update(
        {
            "from": operator,
            "from_kid": operator_record["kid"],
            "in_reply_to": {"hub": value["hub"], "from": "node-a", "client_id": "question"},
        }
    )
    value["rcpt"] = sorted([operator_record["kid"], record("node-a", seed=1)["kid"]])
    value["recipients"] = [{"kid": k, "wrap": binary(bytes(1168))} for k in value["rcpt"]]
    raw = {"action": action, "envelope": canonical(value), "id" if action == "reply" else "reply_to": message_id}
    tables = ("fleet_messages", "fleet_events", "fleet_message_sends")
    before = tuple(hub.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables)
    with pytest.raises(messages.MessageError) as caught:
        messages.handle(hub, raw, caller=operator)
    assert caught.value.code == "sealed_question_reply_unavailable"
    assert tuple(hub.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables) == before
    assert hub.execute("SELECT status,answer FROM fleet_questions").fetchone() == ("pending", None)


@pytest.mark.parametrize("action", ["send", "reply"])
def test_sealed_outer_schema_rejects_contradictory_routing(hub, action):
    if action == "send":
        raw = {"action": "send", "envelope": envelope(), "id": 999}
        caller = "node-a"
    else:
        original = send(hub, envelope())
        link = {"hub": json.loads(original["envelope"])["hub"], "from": "node-a", "client_id": "sealed-1"}
        raw = {
            "action": "reply",
            "id": original["id"],
            "reply_to": 999,
            "envelope": envelope("node-b", "node-a", "reply", in_reply_to=link),
        }
        caller = "node-b"
    before = hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0]
    with pytest.raises(messages.MessageError):
        messages.handle(hub, raw, caller=caller)
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == before


def test_stored_sealed_reply_retry_precedes_question_guard(hub):
    original = send(hub, envelope())
    link = {"hub": json.loads(original["envelope"])["hub"], "from": "node-a", "client_id": "sealed-1"}
    raw = envelope("node-b", "node-a", "old-reply", in_reply_to=link)
    stored = send(hub, raw, caller="node-b", reply_to=original["id"])
    # Reconstruct the historical state accepted before sealed-question refusal.
    hub.execute("UPDATE fleet_messages SET kind='operator_question' WHERE id=?", (original["id"],))
    hub.commit()
    assert send(hub, raw, caller="node-b", reply_to=original["id"]) == stored
    assert hub.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone()[0] == 2


def test_compatible_identity_and_opaque_fields_are_preserved(hub):
    from brigade import fleet_hub_identity
    from brigade.fleet_hub_sealed import StoragePolicy
    from tests.test_fleet_hub_message_keys import identity

    for statement, policy in (
        ({**identity(), "require_sealed": True}, StoragePolicy(require_sealed=True)),
        ({**identity(), "max_envelope_bytes": 32768}, StoragePolicy(max_envelope_bytes=32768)),
        (identity(), StoragePolicy(max_envelope_bytes=98305)),
        ({k: v for k, v in identity().items() if k not in ("require_sealed", "max_envelope_bytes")}, StoragePolicy()),
    ):
        statement["future"] = {"opaque": "unchanged"}
        fleet_hub_identity.upload_identity(hub, statement)
        before = hub.execute("SELECT value FROM hub_meta WHERE key='identity'").fetchone()[0]
        server = fleet_hub.make_server(
            "127.0.0.1", 0, hub.execute("PRAGMA database_list").fetchone()[2], "fixture-admin", storage_policy=policy
        )
        server.server_close()
        assert fleet_hub_identity.identity(hub) == statement
        assert hub.execute("SELECT value FROM hub_meta WHERE key='identity'").fetchone()[0] == before


@pytest.mark.parametrize("days", [90, 30, 180])
def test_routing_retention_admission_and_visibility(hub, monkeypatch, days):
    from brigade import fleet_hub_sealed as sealed

    policy = sealed.StoragePolicy(retention_days=days)
    now = datetime.now(timezone.utc)
    retained = []
    for index, sealed_row in enumerate((False, True)):
        stamp = (now - timedelta(days=days - 1)).isoformat()
        raw = (
            {"envelope": envelope(client_id=f"retained-{index}", created_at=stamp)}
            if sealed_row
            else {"to": "node-b", "subject": "Legacy", "body": "Body", "enqueued_at": stamp}
        )
        retained.append(
            messages.handle(hub, {"action": "send", **raw}, caller="node-a", storage_policy=policy)[1]["message"]
        )
        expired = (now - timedelta(days=days + 1)).isoformat()
        raw = (
            {"envelope": envelope(client_id=f"expired-{index}", created_at=expired)}
            if sealed_row
            else {"to": "node-b", "subject": "Legacy", "body": "Body", "enqueued_at": expired}
        )
        with pytest.raises(messages.MessageError, match="expired before delivery"):
            messages.handle(hub, {"action": "send", **raw}, caller="node-a", storage_policy=policy)
    # Tombstones obey the same routing lifetime. Simulate a cleanup backlog to
    # prove visibility itself, rather than relying on deletion to hide rows.
    hub.execute(
        "UPDATE fleet_messages SET acked_at=? WHERE id=?", ((now - timedelta(days=8)).isoformat(), retained[1]["id"])
    )
    hub.commit()
    messages._prune_expired(hub, storage_policy=policy)
    assert (
        hub.execute("SELECT content_state FROM fleet_messages WHERE id=?", (retained[1]["id"],)).fetchone()[0]
        == "content_expired"
    )
    monkeypatch.setattr(messages, "_prune_expired", lambda *args, **kwargs: None)
    for expired in (False, True):
        if expired:
            hub.execute("UPDATE fleet_messages SET created_at=?", ((now - timedelta(days=days + 1)).isoformat(),))
            hub.commit()

        def call(raw, caller="node-b"):
            return messages.handle(hub, raw, caller=caller, storage_policy=policy)

        assert call({"action": "count"})[1]["unread_count"] == (0 if expired else 1)
        assert len(call({"action": "list"})[1]["messages"]) == (0 if expired else 2)
        assert len(call({"action": "sent"}, "node-a")[1]["messages"]) == (0 if expired else 2)
        assert call({"action": "sent_lookup", "client_id": "retained-1"}, "node-a")[0] == (404 if expired else 200)
        assert messages.unread_counts(hub, storage_policy=policy) == ({} if expired else {"node-b": 1})
    assert call({"action": "ack", "id": retained[0]["id"]})[0] == 404
    assert call({"action": "reply", "id": retained[0]["id"], "body": "Reply"})[0] == 404
    with pytest.raises(messages.MessageError, match="reply_to"):
        call({"action": "send", "to": "node-a", "subject": "Reply", "body": "Body", "reply_to": retained[0]["id"]})
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 2
    hub.execute("UPDATE fleet_messages SET created_at=?", ((now - timedelta(days=days - 1)).isoformat(),))
    hub.commit()
    assert call({"action": "ack", "id": retained[0]["id"]})[0] == 200
    status, result = call({"action": "reply", "id": retained[1]["id"], "body": "Retained reply"})
    assert status == 201 and result["message"]["reply_to"] == retained[1]["id"]


@pytest.mark.parametrize("value", ["0", "-1", "True", "1.5", " 30", "+30", "3651", ""])
def test_routing_retention_invalid_environment_refuses_startup(tmp_path, monkeypatch, value):
    monkeypatch.setenv("BRIGADE_FLEET_MESSAGES_RETENTION_DAYS", value)
    with pytest.raises(fleet_hub.FleetHubError, match="RETENTION_DAYS"):
        fleet_hub.make_server("127.0.0.1", 0, tmp_path / "hub.db", "fixture-admin")
    assert not (tmp_path / "hub.db").exists()


def test_routing_retention_policy_types_and_bounds():
    from brigade.fleet_hub_sealed import StoragePolicy, policy_from_environment

    assert StoragePolicy().retention_days == 90
    for days in (1, 30, 180, 3650):
        assert policy_from_environment({"BRIGADE_FLEET_MESSAGES_RETENTION_DAYS": str(days)}).retention_days == days
    for days in (True, False, 0, -1, 3651, 30.0, "30"):
        with pytest.raises(ValueError, match="retention"):
            StoragePolicy(retention_days=days)


def test_routing_retention_restart_migration_preserves_older_rows(hub, monkeypatch):
    from brigade import fleet_hub_sealed as sealed

    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    value = send(hub, envelope())
    hub.execute("UPDATE fleet_messages SET created_at=?", (old,))
    hub.execute("PRAGMA user_version=0")
    hub.commit()
    monkeypatch.setenv("BRIGADE_FLEET_MESSAGES_RETENTION_DAYS", "180")
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin")
    server.server_close()
    assert (
        messages.handle(
            hub,
            {"action": "sent_lookup", "client_id": value["client_id"]},
            caller="node-a",
            storage_policy=sealed.StoragePolicy(retention_days=180),
        )[0]
        == 200
    )


@pytest.mark.parametrize("days", [30, 180])
def test_routing_retention_question_adapter_and_maintenance(hub, monkeypatch, days):
    from pathlib import Path
    from brigade import fleet_admin_stats as stats, fleet_hub_questions as questions, fleet_hub_sealed as sealed

    policy = sealed.StoragePolicy(retention_days=days)
    messages.add_principal(hub, {"name": "operator"})
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", "agent:operator")
    prior = send(hub, envelope())
    old = (datetime.now(timezone.utc) - timedelta(days=days - 1)).isoformat()
    hub.execute("UPDATE fleet_messages SET created_at=?", (old,))
    hub.commit()
    generation = questions._generation(hub, "node-a")
    result = messages.handle(
        hub,
        {"action": "question_create", "question": "Proceed?", "request_id": "retention-question"},
        caller="node-a",
        authenticated_generation=generation,
        storage_policy=policy,
    )[1]
    assert hub.execute("SELECT 1 FROM fleet_messages WHERE id=?", (prior["id"],)).fetchone()
    hub.execute("UPDATE fleet_questions SET created_at=?", (old,))
    hub.commit()
    stats.maintain(Path(hub.execute("PRAGMA database_list").fetchone()[2]), storage_policy=policy)
    assert hub.execute("SELECT COUNT(*) FROM fleet_questions").fetchone()[0] == 1
    assert (
        messages.handle(
            hub,
            {"action": "question_status", "question_id": result["question_id"]},
            caller="node-a",
            authenticated_generation=generation,
            storage_policy=policy,
        )[0]
        == 200
    )
    expired = (datetime.now(timezone.utc) - timedelta(days=days + 1)).isoformat()
    hub.execute("UPDATE fleet_questions SET created_at=?", (expired,))
    hub.execute("UPDATE fleet_messages SET created_at=?", (expired,))
    hub.commit()
    stats.maintain(Path(hub.execute("PRAGMA database_list").fetchone()[2]), storage_policy=policy)
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
    assert hub.execute("SELECT COUNT(*) FROM fleet_questions").fetchone()[0] == 0


def test_routing_retention_two_frozen_hubs_with_longpoll_and_sweeper(tmp_path, monkeypatch):
    import time
    from brigade import fleet_admin_stats as stats, fleet_hub_sealed as sealed
    from tests.test_fleet_hub_message_keys import request

    servers, threads, connections, tokens = [], [], [], []
    stop = threading.Event()
    sweepers = []
    try:
        for days in (30, 180):
            path = tmp_path / f"hub-{days}.db"
            conn = fleet_hub.init_db(path)
            connections.append(conn)
            _, sender_token = fleet_hub.add_node(conn, "node-a")
            _, recipient_token = fleet_hub.add_node(conn, "node-b")
            tokens.append((sender_token, recipient_token))
            monkeypatch.setenv("BRIGADE_FLEET_MESSAGES_RETENTION_DAYS", str(days))
            server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin")
            servers.append(server)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            threads.append(thread)
            thread.start()
        monkeypatch.setenv("BRIGADE_FLEET_MESSAGES_RETENTION_DAYS", "1")
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        raw = {"action": "send", "to": "node-b", "subject": "Older", "body": "Body", "enqueued_at": old}
        assert request(servers[0].server_address, "POST", "/messages", tokens[0][0], raw)[0] == 400
        assert request(servers[1].server_address, "POST", "/messages", tokens[1][0], raw)[0] == 201
        after = connections[1].execute("SELECT MAX(id) FROM fleet_messages").fetchone()[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            waits = [
                pool.submit(
                    request,
                    server.server_address,
                    "POST",
                    "/messages",
                    token[1],
                    {"action": "list", "after": after if index else 0, "wait_ms": 1500},
                )
                for index, (server, token) in enumerate(zip(servers, tokens, strict=True))
            ]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with messages._WAKE_LOCK:
                    if messages._ACTIVE_WAITERS.get("node-b", 0) == 2:
                        break
                stop.wait(0.005)
            else:
                pytest.fail("both Hub long polls did not enter waiting")
            for conn in connections:
                # The older envelope becomes visible only on a reopened poll
                # connection. Cleanup must retain it only on the 180-day Hub.
                messages.send(
                    conn,
                    {**raw, "client_id": "wake-older"},
                    sender="node-a",
                    storage_policy=sealed.StoragePolicy(retention_days=180),
                )
            assert waits[0].result(timeout=3)[1]["messages"] == []
            assert len(waits[1].result(timeout=3)[1]["messages"]) == 1
        # Existing hourly maintenance uses each handler's frozen policy even
        # after the environment has changed. Inject rows to age without a read.
        for conn in connections:
            messages.send(
                conn,
                {**raw, "client_id": "maintenance-older"},
                sender="node-a",
                storage_policy=sealed.StoragePolicy(retention_days=180),
            )
        monkeypatch.setattr(stats, "RETENTION_INTERVAL_SECONDS", 0.01)
        for index, server in enumerate(servers):
            sweepers.append(
                stats.start_retention_sweeper(
                    tmp_path / f"hub-{(30, 180)[index]}.db",
                    stop=stop,
                    storage_policy=server.RequestHandlerClass.message_storage_policy,
                )
            )
        deadline = time.monotonic() + 3
        while (
            time.monotonic() < deadline and connections[0].execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0]
        ):
            stop.wait(0.01)
        assert connections[0].execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
        assert connections[1].execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 3
    finally:
        stop.set()
        for sweeper in sweepers:
            sweeper.join(timeout=3)
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3)
        for conn in connections:
            conn.close()


@pytest.fixture
def startup_foreign_keys(monkeypatch):
    connect = sqlite3.connect

    def connect_with_foreign_keys(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect_with_foreign_keys)


@pytest.mark.parametrize("schema", ["older", "repair", "current"])
@pytest.mark.parametrize(
    "advertised", [{"require_sealed": True}, {"max_envelope_bytes": 1024}, {"require_sealed": "invalid"}]
)
def test_startup_policy_refusal_preserves_retention_rows(hub, monkeypatch, schema, advertised, startup_foreign_keys):
    from brigade import fleet_hub_identity, fleet_hub_questions as questions
    from brigade.fleet_hub_sealed import StoragePolicy
    from tests.test_fleet_hub_message_keys import identity

    hub.execute("PRAGMA foreign_keys=ON")
    send(hub, envelope(client_id="retained-envelope"))
    tomb = send(hub, envelope(client_id="retained-tombstone"))
    messages.handle(hub, {"action": "ack", "id": tomb["id"]}, caller="node-b")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    hub.execute("UPDATE fleet_messages SET acked_at=? WHERE id=?", (old, tomb["id"]))
    hub.commit()
    assert lookup(hub, "retained-tombstone")[1]["message"]["content_state"] == "content_expired"
    messages.add_principal(hub, {"name": "operator"})
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", "agent:operator")
    messages.handle(
        hub,
        {"action": "question_create", "question": "Proceed?", "request_id": "startup-question"},
        caller="node-a",
        authenticated_generation=questions._generation(hub, "node-a"),
    )
    fleet_hub_identity.upload_identity(hub, identity())
    hub.execute("UPDATE hub_meta SET value=? WHERE key='identity'", (json.dumps({**identity(), **advertised}),))
    hub.execute("UPDATE fleet_messages SET created_at=?,acked_at=?", (old, old))
    hub.execute("UPDATE fleet_message_sends SET received_at=?", (old,))
    hub.execute("UPDATE fleet_questions SET created_at=?", (old,))
    if schema == "older":
        hub.execute("PRAGMA user_version=0")
    elif schema == "repair":
        hub.execute("DROP INDEX fleet_messages_sealed_acked")
    hub.commit()
    tables = ("fleet_messages", "fleet_message_sends", "fleet_questions", "hub_meta")
    before = {table: hub.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    assert all(before.values())
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    with pytest.raises(fleet_hub.FleetHubError, match="identity"):
        fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin", storage_policy=StoragePolicy(retention_days=1))
    assert {table: hub.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables} == before
    assert hub.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("compatible", [False, True])
def test_startup_stats_history_migration_defers_retention_until_identity_admission(hub, compatible):
    from brigade import fleet_admin_stats as stats, fleet_hub_identity
    from tests.test_fleet_hub_message_keys import identity

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=500)
    # Store the recent row first so both valid snapshots survive fixture setup.
    for taken_at in (now - timedelta(days=1), old):
        value = stats.snapshot(
            taken_at=taken_at,
            source_set=["d1"],
            weeks=[
                {
                    "week": taken_at.strftime("%G-W%V"),
                    "partial": True,
                    "active_sources": 20,
                    "retained_2d_sources": 10,
                    "retained_3d_sources": 5,
                    "versions": [],
                    "upgrades": [],
                }
            ],
            daily=[],
            downloads={"pypi": None, "npm": None},
        )
        stats.store(hub, value, now=taken_at)
    tables = ("admin_stats_snapshots", "admin_stats_weeks")
    before = {table: hub.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    assert all(len(rows) == 2 for rows in before.values())
    fleet_hub_identity.upload_identity(hub, {**identity(), "require_sealed": not compatible})
    signed = hub.execute("SELECT * FROM hub_meta ORDER BY rowid").fetchall()
    hub.execute("DROP TABLE admin_stats_weeks")
    hub.execute("PRAGMA user_version=34")
    hub.commit()
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    if compatible:
        server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin")
        server.server_close()
        # Accepted startup still applies the separate 400-day stats policy.
        before = {table: rows[:1] for table, rows in before.items()}
    else:
        with pytest.raises(fleet_hub.FleetHubError, match="identity"):
            fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin")
    assert {table: hub.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables} == before
    assert hub.execute("SELECT * FROM hub_meta ORDER BY rowid").fetchall() == signed


@pytest.mark.parametrize("legacy", [False, True])
def test_startup_compatible_upgrade_applies_selected_retention(hub, legacy, startup_foreign_keys):
    from brigade import fleet_hub_identity
    from brigade.fleet_hub_sealed import StoragePolicy
    from tests.test_fleet_hub_message_keys import identity

    hub.execute("PRAGMA foreign_keys=ON")
    send(hub, envelope())
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    hub.execute("UPDATE fleet_messages SET created_at=?", (old,))
    hub.commit()
    if legacy:
        hub.execute("DROP TABLE hub_meta")
    else:
        fleet_hub_identity.upload_identity(hub, {**identity(), "require_sealed": False})
    hub.execute("PRAGMA user_version=0")
    hub.commit()
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    server = fleet_hub.make_server(
        "127.0.0.1", 0, path, "fixture-admin", storage_policy=StoragePolicy(retention_days=1)
    )
    server.server_close()
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
    assert hub.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    assert (
        fleet_hub_identity.identity(hub) is None
        if legacy
        else fleet_hub_identity.identity(hub)["require_sealed"] is False
    )
    assert hub.execute("PRAGMA foreign_key_check").fetchall() == []


def test_startup_policy_validation_serializes_identity_replacement(hub, monkeypatch, startup_foreign_keys):
    from brigade import fleet_hub_identity
    from tests.test_fleet_hub_message_keys import identity

    first = send(hub, envelope())
    for i in range(70):
        hub.execute(
            "INSERT INTO fleet_messages(sender,recipient,subject,body,priority,client_id,payload_hash,created_at,received_at,envelope,envelope_bytes,content_state) "
            "SELECT sender,recipient,subject,body,priority,?,payload_hash,created_at,received_at,envelope,envelope_bytes,content_state FROM fleet_messages WHERE id=?",
            (f"startup-batch-{i}", first["id"]),
        )
    hub.execute("UPDATE fleet_messages SET acked_at=?", ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
    hub.commit()
    statements = []
    init = fleet_hub.init_db

    def traced_init(*args, **kwargs):
        conn = init(*args, **kwargs)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(fleet_hub, "init_db", traced_init)
    fleet_hub_identity.upload_identity(hub, {**identity(), "require_sealed": False})
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    original = fleet_hub_identity.validate_storage_policy
    attempts = []

    def validate(raw, policy):
        original(raw, policy)

        def replace():
            conn = sqlite3.connect(path, timeout=0)
            try:
                conn.execute(
                    "UPDATE hub_meta SET value=? WHERE key='identity'",
                    (json.dumps({**identity(), "require_sealed": True}),),
                )
                conn.commit()
                return "replaced"
            except sqlite3.OperationalError as exc:
                return str(exc)
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            attempts.append(pool.submit(replace).result())

    monkeypatch.setattr(fleet_hub_identity, "validate_storage_policy", validate)
    server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin")
    server.server_close()
    assert attempts and all("locked" in result for result in attempts)
    assert fleet_hub_identity.identity(hub)["require_sealed"] is False
    batches, updates = [], 0
    for sql in statements:
        if sql.startswith("UPDATE fleet_messages SET envelope="):
            updates += 1
        elif sql == "COMMIT":
            batches.append(updates)
            updates = 0
    assert sum(batches) == 71 and max(batches) <= 32
    assert len(attempts) == len(batches) >= 3
    assert hub.execute("SELECT COUNT(*) FROM fleet_messages WHERE content_state='sealed'").fetchone()[0] == 0


def test_startup_retention_rolls_back_on_failure(hub, monkeypatch, startup_foreign_keys):
    from brigade.fleet_hub_sealed import StoragePolicy

    send(hub, envelope())
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    hub.execute("UPDATE fleet_messages SET created_at=?", (old,))
    hub.execute("PRAGMA user_version=0")
    hub.commit()
    before = hub.execute("SELECT * FROM fleet_messages").fetchall()
    prune = messages._prune_expired

    def fail_after_pruning(conn, *, storage_policy=None):
        prune(conn, storage_policy=storage_policy)
        raise sqlite3.OperationalError("injected startup retention failure")

    monkeypatch.setattr(messages, "_prune_expired", fail_after_pruning)
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    with pytest.raises(sqlite3.OperationalError, match="injected startup"):
        fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin", storage_policy=StoragePolicy(retention_days=1))
    assert hub.execute("SELECT * FROM fleet_messages").fetchall() == before


@pytest.mark.parametrize("action", ["send", "reply"])
@pytest.mark.parametrize("mismatch", ["from", "client_id", None])
def test_sealed_reply_link_matches_routed_parent(hub, action, mismatch):
    hub.execute("PRAGMA foreign_keys=ON")
    parent = send(hub, envelope(client_id="parent"))
    other = send(hub, envelope(client_id="other-parent"))
    link = {"hub": json.loads(parent["envelope"])["hub"], "from": "node-a", "client_id": "parent"}
    if mismatch == "from":
        link["from"] = "node-c"
    elif mismatch == "client_id":
        link["client_id"] = other["client_id"]
    raw = {
        "action": action,
        "id" if action == "reply" else "reply_to": parent["id"],
        "envelope": envelope("node-b", "node-a", "bound-reply", in_reply_to=link),
    }
    tables = ("fleet_messages", "fleet_message_sends", "fleet_events")
    before = {table: hub.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    charge = hub.execute("SELECT SUM(envelope_bytes) FROM fleet_messages").fetchone()[0]
    if mismatch is not None:
        with pytest.raises(messages.MessageError) as caught:
            messages.handle(hub, raw, caller="node-b")
        assert caught.value.code == "envelope_reply_mismatch"
        assert {table: hub.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables} == before
        assert hub.execute("SELECT SUM(envelope_bytes) FROM fleet_messages").fetchone()[0] == charge
        return
    status, result = messages.handle(hub, raw, caller="node-b")
    assert status == 201
    reply = result["message"]
    assert reply["reply_to"] == parent["id"] and reply["envelope"] == raw["envelope"]
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    hub.execute("UPDATE fleet_messages SET created_at=? WHERE id=?", (old, parent["id"]))
    hub.commit()
    for principal, seed in (("node-a", 1), ("node-b", 2)):
        publish(
            hub,
            record(principal, seed=seed + 10, prev_kid=record(principal, seed=seed)["kid"], prev_sig=binary(bytes(64))),
        )
    assert messages.handle(hub, raw, caller="node-b")[1]["message"] == reply
    assert hub.execute("SELECT 1 FROM fleet_messages WHERE id=?", (parent["id"],)).fetchone() is None
    assert hub.execute("SELECT COUNT(*) FROM fleet_message_sends WHERE sender='node-b'").fetchone()[0] == 1


@pytest.mark.parametrize(("value", "required"), [(None, False), ("", False), ("0", False), ("1", True)])
def test_require_sealed_environment_valid_values(value, required):
    from brigade.fleet_hub_sealed import policy_from_environment

    environ = {} if value is None else {"BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED": value}
    assert policy_from_environment(environ).require_sealed is required


@pytest.mark.parametrize("value", ["true", "yes", "True", "TRUE", " 1", "1 ", "0 ", " ", "false", "2"])
def test_require_sealed_environment_rejects_noncanonical_values(value):
    from brigade.fleet_hub_sealed import policy_from_environment

    with pytest.raises(ValueError, match="REQUIRE_SEALED.*0.*1"):
        policy_from_environment({"BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED": value})


@pytest.mark.parametrize("provided_policy", [False, True])
@pytest.mark.parametrize("existing_db", [False, True])
def test_require_sealed_invalid_startup_precedes_database_and_socket(
    hub, tmp_path, monkeypatch, provided_policy, existing_db
):
    from pathlib import Path

    from brigade.fleet_hub_sealed import StoragePolicy

    send(hub, envelope())
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    hub.execute("UPDATE fleet_messages SET created_at=?,acked_at=?", (old, old))
    hub.commit()
    path = Path(hub.execute("PRAGMA database_list").fetchone()[2]) if existing_db else tmp_path / "uncreated.db"
    before = path.read_bytes() if existing_db else None
    monkeypatch.setenv("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED", "1 ")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid sealing environment reached database initialization or socket construction")

    monkeypatch.setattr(fleet_hub, "init_db", forbidden)
    monkeypatch.setattr(fleet_hub, "BoundedThreadingHTTPServer", forbidden)
    with pytest.raises(fleet_hub.FleetHubError, match="REQUIRE_SEALED.*0.*1"):
        fleet_hub.make_server(
            "127.0.0.1", 0, path, "fixture-admin", storage_policy=StoragePolicy() if provided_policy else None
        )
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize(
    ("value", "base_required", "flag", "required"),
    [
        (None, False, False, False),
        ("", True, False, True),
        ("0", True, False, True),
        ("1", False, False, True),
        ("0", False, True, True),
    ],
)
def test_require_sealed_environment_preserves_explicit_policy_overlay(
    tmp_path, monkeypatch, value, base_required, flag, required
):
    from brigade.fleet_hub_sealed import StoragePolicy

    if value is None:
        monkeypatch.delenv("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED", raising=False)
    else:
        monkeypatch.setenv("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED", value)
    # Explicit policy still overrides numeric environment settings.
    monkeypatch.setenv("BRIGADE_FLEET_MESSAGES_RETENTION_DAYS", "invalid")
    policy = StoragePolicy(require_sealed=base_required, retention_days=180)
    server = fleet_hub.make_server(
        "127.0.0.1", 0, tmp_path / "overlay.db", "fixture-admin", storage_policy=policy, require_sealed=flag
    )
    try:
        effective = server.RequestHandlerClass.message_storage_policy
        assert effective.require_sealed is required
        assert effective.retention_days == 180
        assert policy.require_sealed is base_required
    finally:
        server.server_close()
