"""Endpoint changes cannot lend cached message authority to another Hub."""

import hashlib
import json
import unittest

import pytest

from brigade import fleet_client, fleet_client_messages as messages
from brigade.dot.contracts import DotError
from tests.dot_support import requires_local_state
from tests import test_dot_inbox_tools as dot_support


@pytest.mark.parametrize("interrupted", [False, True])
def test_spool_origin_survives_endpoint_switch(tmp_path, monkeypatch, caplog, interrupted):
    settings = dict(hub_url="https://a.invalid", node_token="fixture-node", admin_token="fixture-admin")
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(settings))
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: dict(settings))
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-a")
    calls = []

    def offline(hub, token, body, **kwargs):
        calls.append((hub, body["action"]))
        if interrupted:
            settings["hub_url"] = "https://b.invalid"
            raise KeyboardInterrupt
        raise fleet_client.FleetClientError("offline")

    monkeypatch.setattr(messages, "_request", offline)

    def send():
        return messages.send(
            recipient="node-b", subject="s", body="b", reference=None, priority="normal", client_id="send-origin"
        )

    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            send()
        settings["hub_url"] = "https://a.invalid"
    else:
        assert send()["queued"]
        assert messages.reply(
            1, body="reply", subject=None, reference=None, priority="normal", client_id="reply-origin"
        )["queued"]
    directory = fleet_client.spool_path("node-a").parent
    original = {p: p.read_bytes() for p in directory.glob("*.jsonl")}
    calls.clear()
    settings["hub_url"] = "https://b.invalid"

    def delivered(hub, token, body, **kwargs):
        calls.append((hub.rstrip("/"), body["action"]))
        return 200, {"message": {"client_id": body["client_id"]}}

    monkeypatch.setattr(messages, "_request", delivered)
    assert messages.flush_credential() == 0
    assert calls == []
    assert all(p.read_bytes() == content for p, content in original.items())
    legacy = directory / ("messages-" + hashlib.sha256(b"fixture-node").hexdigest() + ".jsonl")
    legacy.write_text(json.dumps(dict(action="reply", id=1, client_id="legacy")) + "\n")
    node_legacy = directory / "node-a.messages.jsonl"
    node_legacy.write_text(legacy.read_text())
    legacy_bytes = legacy.read_bytes()
    assert messages.flush_credential() == 0
    assert calls == []
    assert legacy.read_bytes() == node_legacy.read_bytes() == legacy_bytes
    assert "fixture-node" not in caplog.text

    # Validation may reread current settings, but cannot adopt B or replay A while B is current.
    from brigade.fleet_client_message_credentials import FleetMessageHubError

    with pytest.raises(FleetMessageHubError, match="message credential changed"):
        messages.flush("node-a", hub_url="https://a.invalid/", token="fixture-node")
    assert calls == []
    assert all(p.read_bytes() == content for p, content in original.items())
    settings["hub_url"] = "https://a.invalid"
    assert messages.flush("node-a", hub_url="https://a.invalid/", token="fixture-node") == (1 if interrupted else 2)
    assert all(hub == "https://a.invalid" for hub, _ in calls)
    assert messages.flush_credential() == 0
    assert legacy.read_bytes() == node_legacy.read_bytes() == legacy_bytes


def test_admin_retry_state_endpoint_walk(tmp_path, monkeypatch):
    settings = dict(hub_url="https://a.invalid", node_token="", admin_token="fixture-admin")
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(settings))
    first = messages._pending_principal_request(
        "fixture", "fixture-admin", None, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    revoke = messages._pending_principal_revocation_request(
        "fixture", "fixture-admin", expected_enrollment_id="a", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    settings["hub_url"] = "https://b.invalid"
    second = messages._pending_principal_request(
        "fixture", "fixture-admin", None, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    other_revoke = messages._pending_principal_revocation_request(
        "fixture", "fixture-admin", expected_enrollment_id="b", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    assert first["request_id"] != second["request_id"]
    assert revoke["request_id"] != other_revoke["request_id"]
    assert other_revoke["expected_enrollment_id"] == "b"
    settings["hub_url"] = "https://a.invalid/"
    assert (
        messages._pending_principal_request(
            "fixture", "fixture-admin", None, hub_url=fleet_client.load_fleet_settings()["hub_url"]
        )
        == first
    )
    assert (
        messages._pending_principal_revocation_request(
            "fixture", "fixture-admin", hub_url=fleet_client.load_fleet_settings()["hub_url"]
        )
        == revoke
    )


class TestDotEndpointScope(unittest.TestCase):
    setup_hub = dot_support.HubFixture.setup_hub

    @requires_local_state
    def test_mirror_cursors_ack_flags_and_wakes_endpoint_walk(self):
        self.runner.settings["hub_url"] = "https://a.invalid"
        self.runner.add(id=50, body="from-a")
        self.hub.sync()
        self.hub.ack([50])
        self.runner.queued = True
        self.hub.send("node-b", "s", "b")
        self.runner.settings["hub_url"] = "https://b.invalid"
        self.runner.rows.clear()
        self.runner.add(id=1, body="from-b")
        self.runner.add(id=50, body="overlap-b")
        listing = self.hub.list()
        self.assertEqual([r["body"] for r in listing["messages"]], ["from-b", "overlap-b"])
        self.hub.flush_pending()
        self.assertFalse(any(c["action"] == "flush_credential" for c in self.runner.calls))
        self.hub.ack([1, 50])
        self.runner.settings["hub_url"] = "https://a.invalid/"
        self.runner.fail["list_messages"] = OSError("offline")
        self.assertEqual(self.hub.list(False)["messages"][0]["body"], "from-a")
        self.hub.flush_pending()
        self.assertEqual(sum(c["action"] == "flush_credential" for c in self.runner.calls), 1)
        with self.state.transaction() as c:
            sources = [r[0] for r in c.execute("SELECT source FROM event_queue")]
        self.assertEqual(len(sources), 3)
        self.assertEqual(len(set(sources)), 3)

    @requires_local_state
    def test_endpoint_switch_during_fetch_or_ack_fails_closed(self):
        self.runner.add()
        self.hub.sync()
        for action in ("list_messages", "ack"):
            self.runner.settings["hub_url"] = "https://hub.invalid"

            def switch(name, args, kwargs, action=action):
                if name == action:
                    self.runner.settings["hub_url"] = "https://b.invalid"

            self.runner.on_call = switch
            with self.assertRaisesRegex(DotError, "hub_credential_changed"):
                self.hub.sync(after=0) if action == "list_messages" else self.hub.ack([1])

    @requires_local_state
    def test_node_sweep_and_pending_flags_have_separate_endpoint_scope(self):
        from datetime import timedelta
        from tests.test_dot_inbox_tools import NOW

        self.runner.add(
            id=50,
            sender="agent:dot",
            recipient="node-a",
            reply_to=1,
            created_at=(NOW - timedelta(hours=25)).isoformat(),
        )
        self.hub.reconcile()
        self.runner.queued = True
        self.hub.send_completion("request", 0, "completed")
        self.runner.settings["hub_url"] = "https://b.invalid"
        self.runner.rows.clear()
        self.runner.add(
            id=1, sender="agent:dot", recipient="node-a", reply_to=1, created_at=(NOW - timedelta(hours=25)).isoformat()
        )
        self.hub.reconcile()
        self.assertIsNotNone(self.runner.rows[0]["acked_at"])
        self.hub.flush_pending()
        self.assertFalse(any(c["action"] == "flush_credential" for c in self.runner.calls))
        self.runner.settings["hub_url"] = "https://hub.invalid/"
        self.hub.flush_pending()
        self.assertEqual(sum(c["action"] == "flush_credential" for c in self.runner.calls), 1)

    @requires_local_state
    def test_reply_wait_discards_batch_after_endpoint_changes_during_ack(self):
        self.runner.add(sender="agent:dot", recipient="node-a", reply_to=1)

        def switch(action, args, kwargs):
            if action == "ack":
                self.runner.settings["hub_url"] = "https://b.invalid"

        self.runner.on_call = switch
        with self.assertRaisesRegex(DotError, "hub_credential_changed"):
            self.hub.wait_replies(1, 0)

    @requires_local_state
    def test_wake_delivery_retains_inactive_scope_and_does_not_coalesce_it(self):
        import asyncio
        import base64
        import time
        from brigade.dot.events import EventService

        self.runner.add(id=50)
        self.hub.sync()
        with self.state.transaction() as c:
            c.execute(
                "INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "sub",
                    "owner",
                    "fleet.inbox.message",
                    "{}",
                    "https://callback.invalid",
                    "whsec_" + base64.b64encode(b"k" * 32).decode(),
                    None,
                    None,
                    None,
                    0,
                ),
            )
        self.runner.settings["hub_url"] = "https://b.invalid"
        self.runner.rows.clear()
        self.runner.add(id=1)
        self.hub.sync()
        posted = []

        async def post(url, body, headers):
            posted.append(json.loads(body)["data"]["msg_id"])
            return 200, b""

        service = EventService(
            self.state,
            fleet_status=lambda _: {},
            hub=self.hub,
            hub_poll_seconds=30,
            callback_hosts=frozenset(),
            post=post,
        )
        now = time.time()
        asyncio.run(service.deliver(now=now))
        self.assertEqual(posted, [1])
        self.runner.settings["hub_url"] = "https://hub.invalid/"
        asyncio.run(service.deliver(now=now + 301))
        self.assertEqual(posted, [1, 50])

    @requires_local_state
    def test_unscoped_mirror_is_archived_without_lending_ack_authority(self):
        with self.state.transaction() as c:
            c.execute("DROP TABLE hub_inbox")
            c.execute("""CREATE TABLE hub_inbox (
                id INTEGER PRIMARY KEY, sender TEXT NOT NULL, sender_label TEXT,
                subject TEXT, body TEXT NOT NULL, ref TEXT, thread TEXT, kind TEXT,
                priority TEXT, reply_to INTEGER, created_at TEXT NOT NULL,
                acked_at TEXT, synced_at TEXT NOT NULL)""")
            c.execute(
                "INSERT INTO hub_inbox(id,sender,body,created_at,synced_at) VALUES (1,'node-a','legacy',?,?)",
                ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            self.state.hub_set("cursor", 99, connection=c)
        # Reopening runs the real schema initializer, as on upgrade.
        from brigade.dot.state import BridgeState

        directory = self.state.directory
        self.state.close()
        self.state = BridgeState(directory)
        self.hub.state = self.state
        self.runner.add(id=1, body="current")
        self.assertEqual(self.hub.list()["messages"][0]["body"], "current")
        with self.state.transaction() as c:
            self.assertEqual(c.execute("SELECT body FROM hub_inbox_legacy").fetchone()[0], "legacy")
        self.assertEqual(self.state.hub_get("cursor"), "99")

    @requires_local_state
    def test_reconcile_cursor_endpoint_walk_preserves_inactive_progress(self):
        self.runner.page_size = 1
        for ident in range(50, 60):
            self.runner.add(id=ident, body="from-a")
        self.hub.reconcile()  # Ten pages leave a nonzero durable reconcile cursor.
        self.runner.settings["hub_url"] = "https://b.invalid"
        self.runner.rows.clear()
        self.runner.add(id=1, body="from-b")
        self.runner.calls.clear()
        self.hub.reconcile()
        first = next(c for c in self.runner.calls if c["action"] == "list_messages" and c["token_file"])
        self.assertEqual(first["after"], 0)
        self.assertEqual(self.hub.list()["messages"][0]["body"], "from-b")
        self.runner.settings["hub_url"] = "https://hub.invalid/"
        self.runner.fail["list_messages"] = OSError("offline")
        self.assertEqual(len(self.hub.list()["messages"]), 10)
        self.assertEqual(self.state.hub_get("reconcile_cursor", scope=self.hub.scope()), "59")

    @requires_local_state
    def test_invalid_agent_file_does_not_starve_node_flush(self):
        self.runner.queued = True
        self.hub.send_completion("request", 0, "completed")
        self.token.unlink()
        with self.assertRaisesRegex(DotError, "hub_credential_invalid"):
            self.hub.flush_pending()
        self.assertEqual([c["token_file"] for c in self.runner.calls if c["action"] == "flush_credential"], [None])

    @requires_local_state
    def test_rotation_during_event_signing_stops_before_post(self):
        import asyncio
        import base64
        from unittest.mock import patch
        from brigade.dot import events

        self.runner.add()
        self.hub.sync()
        with self.state.transaction() as c:
            c.execute(
                "INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "sub",
                    "owner",
                    "fleet.inbox.message",
                    "{}",
                    "https://callback.invalid",
                    "whsec_" + base64.b64encode(b"k" * 32).decode(),
                    None,
                    None,
                    None,
                    0,
                ),
            )
        posted = []

        async def post(*args):
            posted.append(args)
            return 200, b""

        service = events.EventService(
            self.state,
            fleet_status=lambda _: {},
            hub=self.hub,
            hub_poll_seconds=30,
            callback_hosts=frozenset(),
            post=post,
        )
        original = events.signed_headers

        def sign(*args, **kwargs):
            self.runner.settings["hub_url"] = "https://other.invalid"
            return original(*args, **kwargs)

        with patch.object(events, "signed_headers", side_effect=sign):
            asyncio.run(service.deliver())
        self.assertEqual(posted, [])


def test_drain_rejects_mismatched_origin_before_lookup_or_replay(tmp_path, monkeypatch):
    from brigade.fleet_client_message_credentials import MessageAuthority

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: dict(hub_url="https://b.invalid", node_token="fixture-node", admin_token=""),
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-a")
    message_authority = MessageAuthority.capture()
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    path = messages._spool_path("node-a", "fixture-node", hub_url="https://a.invalid")
    with fleet_client._spool_lock(path) as fd:
        fleet_client._write_spool_atomic(path, [dict(action="reply", id=1)], dir_fd=fd)
    original = path.read_bytes()
    from unittest.mock import patch

    with patch.object(messages, "_request") as request:
        with pytest.raises(fleet_client.FleetClientError, match="origin does not match"):
            messages._flush_locked(path, message_authority)
        request.assert_not_called()
    assert path.read_bytes() == original


@pytest.mark.parametrize("node_prefix", [False, True])
def test_unknown_admin_retry_is_preserved_and_never_adopted(tmp_path, monkeypatch, node_prefix):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(hub_url="https://b.invalid"))
    directory = tmp_path / fleet_client.SPOOL_DIRNAME
    directory.mkdir()
    name_hash = hashlib.sha256(b"fixture").hexdigest()[:16]
    token_hash = hashlib.sha256(b"fixture-admin").hexdigest()
    legacy = directory / (("node-a." if node_prefix else "") + f"principal-{name_hash}-{token_hash}.jsonl")
    legacy.write_text(json.dumps(dict(name="fixture", request_id="old-origin", expected_enrollment_id="old")) + "\n")
    original = legacy.read_bytes()
    for operation in (
        lambda: messages._pending_principal_request(
            "fixture", "fixture-admin", None, hub_url=fleet_client.load_fleet_settings()["hub_url"]
        ),
        lambda: messages._pending_principal_revocation_request(
            "fixture",
            "fixture-admin",
            expected_enrollment_id="new",
            hub_url=fleet_client.load_fleet_settings()["hub_url"],
        ),
    ):
        with pytest.raises(fleet_client.FleetClientError, match="unproven endpoint scope") as error:
            operation()
        assert "fixture-admin" not in str(error.value)
        assert legacy.read_bytes() == original
