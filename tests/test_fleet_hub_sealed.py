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
    for name in ("envelope", "envelope_bytes", "content_state"):
        hub.execute(f"ALTER TABLE fleet_messages DROP COLUMN {name}")
    hub.commit()
    repaired = fleet_hub.init_db(path)
    try:
        assert repaired.execute("PRAGMA user_version").fetchone()[0] == 45
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
    conn.close()
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_required_policy_preserves_signed_identity(hub):
    from brigade import fleet_hub_identity
    from tests.test_fleet_hub_message_keys import identity, request

    statement = identity()
    fleet_hub_identity.upload_identity(hub, statement)
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin", require_sealed=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        assert request(server.server_address, "GET", "/mesh/identity") == (200, statement)
        assert fleet_hub_identity.identity(hub) == statement
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
