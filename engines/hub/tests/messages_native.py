"""Native messages preserve Python wire bytes, storage, the sealed lifecycle and long polls."""

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc  # noqa: E402 - repository-local drive helper
import storage_parity as sp  # noqa: E402 - deterministic key records and envelopes

BINARY = Path(os.environ.get("BRIGADE_HUB_BINARY", ROOT / "engines/hub/target/debug/brigade-hub"))
START = 1767225600
DAY = 86_400
OPERATOR = "agent:ops"
# Rows the parity run compares. Random client ids and question ids are left out.
STORAGE = {
    "fleet_messages": (
        "SELECT id,sender,recipient,subject,body,reference,priority,"
        "CASE WHEN kind='operator_question' THEN '' ELSE client_id END,"
        "CASE WHEN kind='operator_question' THEN '' ELSE payload_hash END,"
        "reply_to,created_at,received_at,acked_at,acked_by,sender_label,thread,kind,envelope,envelope_bytes,"
        "content_state,sealed_reply_to FROM fleet_messages ORDER BY id"
    ),
    "fleet_message_sends": "SELECT sender,received_at FROM fleet_message_sends ORDER BY id",
    "fleet_questions": (
        "SELECT caller,request_id,payload_hash,operator,question,message_id,created_at,deadline,status,answer,"
        "answer_id,source,answered_by,answered_at,late_answer_message_id,late_answered_at "
        "FROM fleet_questions ORDER BY created_at,request_id"
    ),
    "sqlite_sequence": "SELECT name,seq FROM sqlite_sequence WHERE name LIKE 'fleet_message%' ORDER BY name",
}


def send(hub, method, path, token=None, body=None, raw=None, timeout=30):
    origin = urlsplit(hub.url)
    connection = http.client.HTTPConnection(origin.hostname, origin.port, timeout=timeout)
    try:
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection.request(method, path, body=payload, headers=headers)
        received = connection.getresponse()
        data = received.read()
        return SimpleNamespace(
            status=received.status, body=data, headers={k.lower(): v for k, v in received.getheaders()}
        )
    finally:
        connection.close()


def launch(state, clock, native):
    state.mkdir()
    template = (
        f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}} "
        f"--test-clock-file {clock} --route-mode registration=native --route-mode claims=native"
    )
    if native:
        template += " --route-mode messages=native"
    original = hc.hub_env

    def hub_env(*args):
        # The drive drops every BRIGADE_* variable; opt this hub into events and questions.
        env = original(*args)
        env["BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS"] = "events.example.test"
        env["BRIGADE_FLEET_OPERATOR_RECIPIENT"] = OPERATOR
        return env

    hc.hub_env = hub_env
    try:
        hub = hc.launch_hub(template, brigade=[sys.executable, "-m", "brigade"], state_dir=state, startup_timeout=30)
    finally:
        hc.hub_env = original
    hub.db = state / "hub.db"
    hub.tokens = {"admin": hub.admin_token, "bogus": "not-a-real-token"}
    return hub


def enroll(hub, *, nodes=(), agents=()):
    for node in nodes:
        added = send(hub, "POST", "/nodes", hub.admin_token, {"action": "add", "node_id": node})
        assert added.status == 200, added.body
        hub.tokens[node] = json.loads(added.body)["token"]
    for name, grants in agents:
        short = name.removeprefix("agent:")
        added = send(hub, "POST", "/message-principals", hub.admin_token, {"action": "add", "name": short})
        assert added.status == 201, added.body
        hub.tokens[name] = json.loads(added.body)["principal"]["token"]
        granted = send(
            hub,
            "POST",
            "/message-principals",
            hub.admin_token,
            {"action": "grants", "name": short, "may_send_to": grants},
        )
        assert granted.status == 200, granted.body


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class Messages(unittest.TestCase):
    def test_python_byte_parity(self):
        self.maxDiff = None
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            clock = work / "clock"
            clock.write_text(str(START), encoding="utf-8")
            hubs = []
            try:
                for native in (False, True):
                    hubs.append(launch(work / ("native" if native else "proxy"), clock, native))
                for hub in hubs:
                    enroll(
                        hub,
                        nodes=("node-a", "node-b", "node-c"),
                        agents=(("agent:bot", ["node:*"]), (OPERATOR, ["node:*"]), ("agent:peer", ["agent:bot"])),
                    )
                    db = sqlite3.connect(hub.db)
                    db.execute(
                        "INSERT INTO fleet_event_subscriptions(id,principal_id,name,arguments,url,secret,created_at) "
                        "VALUES('sub-messages','node-b','message.created','{}','https://events.example.test/hook','fake',?)",
                        (START,),
                    )
                    db.commit()
                    db.close()
                offset = [0.0]

                def at(seconds):
                    offset[0] = seconds
                    clock.write_text(str(START + seconds), encoding="utf-8")

                # Each hub draws its own question ids: label -> [proxy id, native id].
                qids = {}

                def compare(who=None, body=None, raw=None, path="/messages", record=None):
                    bodies = [body(i) if callable(body) else body for i in range(len(hubs))]
                    responses = [
                        send(hub, "POST", path, hub.tokens.get(who) if who else None, bodies[i], raw)
                        for i, hub in enumerate(hubs)
                    ]
                    case = (who, bodies[0], raw)
                    if record is not None:
                        qids[record] = [json.loads(r.body)["question_id"] for r in responses]
                    texts = [r.body for r in responses]
                    for label, ids in qids.items():
                        texts = [t.replace(ids[i].encode(), label.encode()) for i, t in enumerate(texts)]
                    self.assertEqual(
                        responses[0].status, responses[1].status, (case, responses[0].body, responses[1].body)
                    )
                    self.assertEqual(texts[0], texts[1], case)
                    for header in ("content-type", "cache-control", "referrer-policy"):
                        self.assertEqual(responses[0].headers.get(header), responses[1].headers.get(header), case)
                    return json.loads(responses[0].body)

                def masked(who, body):
                    """Compare an identity answer with its random epochs masked."""
                    responses = [send(hub, "POST", "/messages", hub.tokens[who], body) for hub in hubs]
                    self.assertEqual(responses[0].status, responses[1].status, body)

                    def mask(value):
                        if isinstance(value, dict):
                            return {k: ("*" if k.endswith("_epoch") and v else mask(v)) for k, v in value.items()}
                        return [mask(v) for v in value] if isinstance(value, list) else value

                    decoded = [json.loads(r.body) for r in responses]
                    self.assertEqual(mask(decoded[0]), mask(decoded[1]), body)
                    # Key order and spacing still match byte for byte.
                    self.assertEqual(
                        [list(d) for d in decoded], [list(d) for d in decoded[::-1]], body
                    )
                    self.assertEqual(len(responses[0].body), len(responses[1].body), body)

                def message(who, *parts, **fields):
                    body = {}
                    for part in parts:
                        body.update(part)
                    return compare(who, {**body, **fields})

                # Authentication and the request envelope.
                compare(None, {"action": "count"})
                compare("bogus", {"action": "count"})
                compare("admin", {"action": "count"})
                compare("node-a", [1])
                compare("node-a", raw=b"{")
                compare("node-a", raw=b'{"action": "count", "x": NaN}')
                compare("node-a", {"action": 5})
                compare("node-a", {"action": "shout"})
                compare("node-a", {"action": "count", "after": 1})
                message("node-b", action="count")
                # Send validation, in Python's order.
                base = {"action": "send", "to": "node-b", "subject": "hi", "body": "hello"}
                for change in (
                    {"to": None},
                    {"to": "  "},
                    {"subject": "s" * 201},
                    {"subject": "é" * 101},
                    {"body": 5},
                    {"body": "x" * 70_000},
                    {"ref": 7},
                    {"client_id": "c" * 65},
                    {"priority": "now"},
                    {"priority": None},
                    {"enqueued_at": "2026-01-01T00:00:00"},
                    {"enqueued_at": "yesterday"},
                    {"enqueued_at": "2026-01-01T00:10:00+00:00"},
                    {"enqueued_at": "2025-01-01T00:00:00Z"},
                    {"enqueued_at": ""},
                    {"kind": "operator_question"},
                    {"kind": "memo"},
                    {"thread": "-x"},
                    {"sender_label": "bad label"},
                    {"to": "node-z"},
                    {"to": "agent:nobody"},
                    {"envelope": "x", "subject": "s"},
                    {"reply_to": "x"},
                    {"reply_to": 99},
                ):
                    compare("node-a", {**base, "client_id": "v1", **change})
                compare("agent:bot", {**base, "sender_label": "bot"})
                compare("agent:peer", {**base, "client_id": "denied"})
                sent = message(
                    "node-a",
                    **base,
                    client_id="m1",
                    ref="ref-1",
                    priority="high",
                    thread="t/1#a",
                    sender_label="lab",
                    kind="question",
                    enqueued_at="2025-12-31T23:59:00.250Z",
                )
                first = sent["message"]["id"]
                message("node-a", base, client_id="m1", ref="ref-1", priority="high", thread="t/1#a", sender_label="lab", kind="question", enqueued_at="2025-12-31T23:59:00.250Z")
                message("node-a", base, {"body": "changed"}, client_id="m1")
                at(1.25)
                message("node-a", base, {"body": "unicode é \x7f   \U0001f389"}, client_id="m2")
                message("node-a", base, to="agent:bot", client_id="m3", enqueued_at="2025-12-31T20:00:00-03:30")
                message("agent:bot", base, client_id="m4")
                # Python hashes enqueued_at forms it alone reads; the native hub forwards them.
                message("node-a", base, client_id="m5", enqueued_at="2026-01-01 00:00:00+00:00")
                # Reads.
                for body in (
                    {"action": "list", "after": -1},
                    {"action": "list", "after": None},
                    {"action": "list", "unacked": 1},
                    {"action": "list", "wait_ms": 20_001},
                    {"action": "list", "wait_ms": True},
                    {"action": "list"},
                    {"action": "list", "after": first, "unacked": True, "extra": 1},
                    {"action": "sent"},
                    {"action": "sent", "after": 1, "ids": [1]},
                    {"action": "sent", "ids": []},
                    {"action": "sent", "ids": [0]},
                    {"action": "sent", "ids": [first, first, 999]},
                    {"action": "sent", "x": 1},
                    {"action": "sent_lookup", "client_id": "m1"},
                    {"action": "sent_lookup", "client_id": "zzz"},
                    {"action": "sent_lookup", "client_id": "m1", "x": 1},
                    {"action": "sent_lookup", "client_id": ""},
                    {"action": "identity"},
                    {"action": "identity", "peers": ["agent:bot"]},
                    {"action": "identity", "peers": []},
                    {"action": "manifests_get"},
                    {"action": "manifests_get", "x": 1},
                    {"action": "keys_get", "kid": "nope"},
                    {"action": "keys_get", "kid": "k1_AAAA"},
                    {"action": "keys_get", "kid": "k1_AAAAAAAAAAAAAAAAAAAAAA"},
                ):
                    compare("node-a", body)
                    compare("node-b", body)
                masked("agent:bot", {"action": "identity", "peers": ["agent:peer", "agent:ops"]})
                compare("agent:bot", {"action": "identity", "peers": ["agent:bot"]})
                masked("agent:peer", {"action": "identity", "peers": ["agent:bot"]})
                compare("agent:bot", {"action": "list"})
                compare("agent:bot", {"action": "sent"})
                # Acks.
                for body in ({"action": "ack"}, {"action": "ack", "id": True}, {"action": "ack", "id": 999}):
                    compare("node-b", body)
                compare("node-a", {"action": "ack", "id": first})
                at(2)
                compare("node-b", {"action": "ack", "id": first})
                at(3)
                compare("node-b", {"action": "ack", "id": first})
                message("node-b", action="count")
                message("node-b", action="list", unacked=True)
                # Replies.
                compare("node-b", {"action": "reply", "id": first, "body": "re", "client_id": "r1"})
                compare("node-b", {"action": "reply", "id": first, "body": "re", "client_id": "r1"})
                compare("node-b", {"action": "reply", "id": first, "body": "re2", "client_id": "r2", "subject": "own", "ref": None, "thread": None})
                compare("node-b", {"action": "reply", "id": first, "body": "re3", "client_id": "r3", "subject": "x" * 201})
                compare("node-b", {"action": "reply", "id": first, "client_id": "r4"})
                compare("node-a", {"action": "reply", "id": first, "body": "not yours", "client_id": "r5"})
                compare("node-a", {"action": "reply", "id": first, "body": "not yours"})
                compare("node-b", {"action": "reply", "id": "x", "body": "x"})
                compare("node-b", {"action": "reply", "id": first, "body": "x", "kind": "memo"})
                compare("node-b", {"action": "send", "to": "node-a", "subject": "s", "body": "via send", "client_id": "r6", "reply_to": first})
                compare("node-c", {"action": "send", "to": "node-a", "subject": "s", "body": "wrong parent", "client_id": "r7", "reply_to": first})
                message("node-a", action="list")
                message("node-a", action="sent", after=first)
                # Questions, answered by the operator and from the desktop.
                ask = {"action": "question_create", "question": "Ship it?", "timeout_secs": 30, "request_id": "q1"}
                for change in ({"timeout_secs": 0}, {"timeout_secs": True}, {"request_id": "-x"}, {"question": ""}, {"x": 1}):
                    compare("node-a", {**ask, **change})
                created = compare("node-a", ask, record="QUESTION-ONE")
                compare("node-a", ask)
                compare("node-a", {**ask, "question": "Other?"})

                def status(label, **extra):
                    return lambda i: {"action": "question_status", "question_id": qids[label][i], **extra}

                compare("node-b", status("QUESTION-ONE"))
                compare("node-a", {"action": "question_status", "question_id": "nope"})
                compare("node-a", status("QUESTION-ONE", x=1))
                compare("node-a", lambda i: {"action": "question_desktop", "question_id": qids["QUESTION-ONE"][i]})
                asked = int(created["message_id"])
                compare(OPERATOR, {"action": "reply", "id": asked, "body": "yes", "client_id": "q1-answer"})
                compare("node-a", status("QUESTION-ONE"))
                compare("node-a", {**ask, "request_id": "q2"}, record="QUESTION-TWO")
                compare("node-a", {**ask, "request_id": "q3"}, record="QUESTION-THREE")
                compare("node-a", {**ask, "request_id": "q4"})
                compare(
                    "node-a",
                    lambda i: {"action": "question_desktop", "question_id": qids["QUESTION-TWO"][i], "answer": "done"},
                )
                compare("node-a", status("QUESTION-TWO"))
                compare("agent:ops", {**ask, "request_id": "q5"})
                at(40)
                compare("node-a", {**ask, "request_id": "q4"}, record="QUESTION-FOUR")
                # Sealed mail: keys go through the Python upstream on both hubs.
                kids = {}
                for node in ("node-a", "node-b"):
                    record = sp.key_record(node, iso(START + offset[0]).replace("+00:00", "Z"))
                    kids[node] = record["kid"]
                    compare(node, {"action": "keys_publish", "record": record})
                compare("node-a", {"action": "keys_get", "kid": kids["node-b"]})
                compare("agent:bot", {"action": "keys_get", "kid": kids["node-b"]})
                stamp = datetime.fromtimestamp(START + offset[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                value = sp.envelope(
                    sender="node-a",
                    sender_kid=kids["node-a"],
                    recipient="node-b",
                    recipient_kid=kids["node-b"],
                    client_id="s1",
                    created_at=stamp,
                )
                envelope = hc.canonical(value)
                for broken in (
                    {"action": "send", "envelope": envelope, "subject": "x"},
                    {"action": "send", "envelope": 5},
                    {"action": "send", "envelope": envelope.replace('"v":1', '"v":2')},
                    {"action": "send", "envelope": envelope.replace("{", "{ ", 1)},
                    {"action": "send", "envelope": hc.canonical({**value, "from": "node-b"})},
                    {"action": "send", "envelope": hc.canonical({**value, "rcpt": list(reversed(value["rcpt"]))})},
                    {"action": "send", "envelope": envelope, "reply_to": first},
                    {"action": "send", "envelope": hc.canonical({**value, "client_id": "s0", "from_kid": kids["node-b"]})},
                ):
                    compare("node-a", broken)
                sealed = compare("node-a", {"action": "send", "envelope": envelope})["message"]["id"]
                compare("node-a", {"action": "send", "envelope": envelope})
                compare("node-b", {"action": "list", "after": sealed - 1})
                compare("node-b", {"action": "reply", "id": sealed, "body": "plain"})
                compare("node-b", {"action": "ack", "id": sealed})
                answer = hc.canonical(
                    sp.envelope(
                        sender="node-b",
                        sender_kid=kids["node-b"],
                        recipient="node-a",
                        recipient_kid=kids["node-a"],
                        client_id="s2",
                        created_at=stamp,
                        in_reply_to={"hub": sp.HUB_ID, "from": "node-a", "client_id": "s1"},
                    )
                )
                compare("node-b", {"action": "reply", "id": sealed, "envelope": answer})
                compare("node-b", {"action": "reply", "id": sealed, "envelope": answer, "thread": "x"})
                # The Python dispatcher prunes old events in the background once the
                # clock jumps, so the outbox is compared before the jump.
                events = []
                for hub in hubs:
                    db = sqlite3.connect(hub.db)
                    events.append(
                        db.execute(
                            "SELECT name,subject,producer,data,created_at FROM fleet_events ORDER BY created_at,data"
                        ).fetchall()
                    )
                    db.close()
                self.assertEqual(events[0], events[1])
                self.assertGreater(len(events[1]), 5, "the subscribed message.created events were recorded")
                # Past the 7-day content lifetime: reads show the tombstone on both
                # hubs, the native one by projection without a purge.
                at(8 * DAY)
                compare("node-b", {"action": "list", "after": sealed - 1})
                compare("node-b", {"action": "sent", "ids": [sealed + 1]})
                compare("node-a", {"action": "sent", "ids": [sealed]})
                compare("node-a", {"action": "sent_lookup", "client_id": "s1"})
                message("node-a", base, client_id="m6")
                # Past retention: old mail and its reply links are gone.
                at(91 * DAY)
                compare("node-b", {"action": "list"})
                compare("node-a", {"action": "list"})
                compare("node-a", {"action": "count"})
                compare("node-b", {"action": "reply", "id": first, "body": "late", "client_id": "r1"})
                message("node-a", base, client_id="m7")
                compare("node-a", status("QUESTION-ONE"))
                compare("node-a", status("QUESTION-THREE"))
                compare("admin", {"action": "revoke", "node_id": "node-c"}, path="/nodes")
                compare("node-a", {**base, "to": "node-c", "client_id": "m8"})
                compare("node-c", {"action": "count"})

                dumps = []
                for hub in hubs:
                    db = sqlite3.connect(hub.db)
                    dumps.append({name: db.execute(sql).fetchall() for name, sql in STORAGE.items()})
                    db.close()
                for name in STORAGE:
                    self.assertEqual(dumps[0][name], dumps[1][name], name)
                self.assertTrue(dumps[1]["fleet_questions"])
            finally:
                for hub in hubs:
                    hc.stop_hub(hub)

    def test_long_polls_wake_and_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            clock = work / "clock"
            clock.write_text(str(START), encoding="utf-8")
            hub = launch(work / "native", clock, True)
            try:
                enroll(hub, nodes=("node-a", "node-b"), agents=(("agent:bot", ["node:*"]),))
                results = {}

                def poll(name, token, after=0, wait_ms=10_000):
                    started = time.monotonic()
                    body = {"action": "list", "after": after, "wait_ms": wait_ms}
                    response = send(hub, "POST", "/messages", token, body, timeout=40)
                    results[name] = (response, time.monotonic() - started)

                def start(name, token, **kwargs):
                    thread = threading.Thread(target=poll, args=(name, token), kwargs=kwargs)
                    thread.start()
                    return thread

                def parked(token, after=0):
                    deadline = time.monotonic() + 10
                    probe = {"action": "list", "after": after, "wait_ms": 1}
                    while time.monotonic() < deadline:
                        if send(hub, "POST", "/messages", token, probe).status == 429:
                            return True
                        time.sleep(0.02)
                    return False

                b = hub.tokens["node-b"]
                # An empty client_id gets a fresh random one, as secrets.token_hex(16) does.
                random_id = send(hub, "POST", "/messages", b, {"action": "send", "to": "node-a", "subject": "s", "body": "b", "client_id": ""})
                generated = json.loads(random_id.body)["message"]["client_id"]
                self.assertRegex(generated, r"^[0-9a-f]{32}$")
                # An empty wait holds for about wait_ms.
                poll("empty", b, wait_ms=300)
                response, waited = results["empty"]
                self.assertEqual(response.body, b'{"messages": [], "next_cursor": 0}')
                self.assertGreaterEqual(waited, 0.25)
                # Two waiters per principal; the third is refused; a native send wakes both.
                threads = [start("w1", b), start("w2", b)]
                # The probe itself takes a slot while it waits, so let both waiters park first.
                time.sleep(0.5)
                self.assertTrue(parked(b))
                refused = send(hub, "POST", "/messages", b, {"action": "list", "wait_ms": 1})
                self.assertEqual(
                    refused.body, b'{"error": "message wait capacity reached", "code": "message_wait_capped"}'
                )
                body = {"action": "send", "to": "node-b", "subject": "wake", "body": "wake", "client_id": "w1"}
                woken = time.monotonic()
                self.assertEqual(send(hub, "POST", "/messages", hub.tokens["node-a"], body).status, 201)
                for thread in threads:
                    thread.join()
                for name in ("w1", "w2"):
                    response, waited = results[name]
                    page = json.loads(response.body)
                    self.assertEqual([m["client_id"] for m in page["messages"]], ["w1"], name)
                    self.assertLess(time.monotonic() - woken, 2, name)
                cursor = page["next_cursor"]
                # A commit by the Python upstream wakes a native waiter through data_version.
                thread = start("external", b, after=cursor)
                time.sleep(0.3)
                external = dict(body, client_id="w2", enqueued_at=iso(START).replace("T", " "))
                woken = time.monotonic()
                self.assertEqual(send(hub, "POST", "/messages", hub.tokens["node-a"], external).status, 201)
                thread.join()
                response, _ = results["external"]
                self.assertEqual([m["client_id"] for m in json.loads(response.body)["messages"]], ["w2"])
                self.assertLess(time.monotonic() - woken, 2)
                # A revocation while waiting answers the v1 auth error on the next wake.
                bot = hub.tokens["agent:bot"]
                thread = start("revoked", bot, wait_ms=1500)
                time.sleep(0.3)
                revoke = send(hub, "POST", "/message-principals", hub.admin_token, {"action": "get", "name": "bot"})
                enrollment = json.loads(revoke.body)["principal"]["enrollment_id"]
                done = send(
                    hub,
                    "POST",
                    "/message-principals",
                    hub.admin_token,
                    {"action": "revoke", "name": "bot", "expected_enrollment_id": enrollment},
                )
                self.assertEqual(done.status, 200, done.body)
                wake = dict(body, to="agent:bot", client_id="w3")
                self.assertEqual(send(hub, "POST", "/messages", hub.tokens["node-a"], wake).status, 400)
                thread.join()
                response, _ = results["revoked"]
                # The send was refused and the wait ran out; its last re-read
                # still checks the principal and answers the v1 auth error.
                self.assertEqual(
                    response.body,
                    b'{"error": "sender is not an active hub principal", "code": "message_sender_inactive"}',
                )
            finally:
                hc.stop_hub(hub)


if __name__ == "__main__":
    unittest.main()
