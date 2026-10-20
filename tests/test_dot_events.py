"""Event protocol and webhook transport regressions ported from the bridge."""

from tests.dot_support import requires_local_state

import asyncio
import base64
import hashlib
import hmac
import json
import time
import importlib.util
import unittest
from typing import Any
import pytest
from brigade.dot.state import BridgeState
from brigade.dot.events import EventService

OWNER = "owner-example"
REQUEST = "request-example"
HOSTS = frozenset({"connectors.api.openai.com"})


class FakeHub:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.sent = True

    def sync(self) -> None:
        self.calls.append("sync")

    def reconcile(self) -> None:
        self.calls.append("reconcile")

    def flush_pending(self) -> None:
        self.calls.append("flush")

    def send_completion(self, request_id: str, generation: int, state: str) -> Any:
        self.calls.append((request_id, generation, state))
        if self.fail:
            raise OSError("unavailable")
        return self.sent


class EventFixture(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def private_state(self, dot_private_dir):
        with BridgeState(dot_private_dir) as state:
            self.state = state
            yield

    def setUp(self):
        pass

    def notify(self, sender, body, priority="normal"):
        # Stand in for an external inbox writer; delivery reads the real durable queue.
        from brigade.dot.events import enqueue

        with self.state.transaction() as c:
            cursor = c.execute(
                "INSERT INTO inbox(created_at,sender,body,priority) VALUES (?,?,?,?)",
                ("2026-01-01T00:00:00+00:00", sender, body, priority),
            )
            msg_id = cursor.lastrowid
            enqueue(c, "fleet.inbox.message", {"msg_id": msg_id, "priority": priority}, priority, f"inbox:{msg_id}")
        return {"id": msg_id}

    def messages(self):
        with self.state.transaction() as c:
            messages = [dict(row) for row in c.execute("SELECT * FROM inbox WHERE acked_at IS NULL")]
        return {"messages": messages, "unacked_total": len(messages)}

    def service(self, post):
        return EventService(
            self.state,
            fleet_status=lambda request: {"ok": True, "result": {"state": "succeeded"}},
            hub=None,
            hub_poll_seconds=30,
            callback_hosts=HOSTS,
            post=post,
        )


class EventChecks(unittest.TestCase):
    def test_standard_webhook_signature(self):
        from brigade.dot.webhooks import signed_headers

        key = b"k" * 32
        secret = "whsec_" + base64.b64encode(key).decode()
        headers = signed_headers(secret, "evt1", b'{"a":1}', "sub1", now=123)
        expected = base64.b64encode(hmac.new(key, b'evt1.123.{"a":1}', hashlib.sha256).digest()).decode()
        self.assertEqual(headers["webhook-signature"], "v1," + expected)

    def test_callback_ssrf_denied(self):
        from brigade.dot.webhooks import callback_url

        for url in (
            "http://connectors.api.openai.com/",
            "https://127.0.0.1/",
            "https://connectors.api.openai.com.evil.test/",
            "https://user@example.com/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                callback_url(url, HOSTS | {"example.com"})


class EventIntegrationChecks(EventFixture):
    def setUp(self):
        EventFixture.setUp(self)

        self.deliveries = []
        self.secret = "whsec_" + base64.b64encode(b"k" * 32).decode()

        async def subscriber(url, body, headers):
            expected = base64.b64encode(
                hmac.new(
                    b"k" * 32,
                    (headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode() + body,
                    hashlib.sha256,
                ).digest()
            ).decode()
            self.assertIn("v1," + expected, headers["webhook-signature"])
            payload = json.loads(body)
            if payload.get("type") == "verification":
                return 200, json.dumps({"challenge": payload["challenge"]}).encode()
            self.deliveries.append((payload, headers))
            return 200, b"{}"

        self.events = self.service(subscriber)

    async def subscribe(self, name="fleet.inbox.message", **options):
        return await self.events.rpc(
            OWNER,
            "events/subscribe",
            dict(
                name=name,
                arguments={},
                delivery={
                    "mode": "webhook",
                    "url": "https://connectors.api.openai.com/test",
                    "secret": self.secret,
                },
                **options,
            ),
        )

    @requires_local_state
    def test_unsubscribe_by_documented_key_and_categorized_failure(self):
        from brigade.dot.events import EventError

        async def scenario():
            await self.subscribe()
            with self.state.transaction() as c:
                self.assertEqual(c.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 1)
            # ChatGPT's documented unsubscribe shape: name, arguments, delivery.url (no secret).
            await self.events.rpc(
                OWNER,
                "events/unsubscribe",
                dict(
                    name="fleet.inbox.message",
                    arguments={},
                    delivery={"mode": "webhook", "url": "https://connectors.api.openai.com/test"},
                ),
            )
            with self.state.transaction() as c:
                self.assertEqual(c.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 0)

            async def forbidden(url, body, headers):
                return 403, b"{}"

            self.events = self.service(forbidden)
            with self.assertRaises(EventError) as caught:
                await self.subscribe()
            self.assertEqual((caught.exception.code, caught.exception.reason), (-32015, "http_4xx"))

        asyncio.run(scenario())

    @requires_local_state
    def test_fake_subscriber_verification_idempotence_delivery_and_unsubscribe(self):
        async def scenario():
            sub = await self.subscribe()
            self.assertEqual(sub["id"], (await self.subscribe())["id"])
            first = self.notify("agent", "normal")
            self.notify("agent", "second normal")
            now = time.time()
            await self.events.deliver(now)
            self.assertEqual(len(self.deliveries), 1)
            self.assertEqual(self.deliveries[0][0]["data"]["msg_id"], first["id"])
            self.notify("agent", "urgent", priority="urgent")
            await self.events.deliver(now + 1)
            self.assertEqual(len(self.deliveries), 2)
            await self.events.rpc(OWNER, "events/unsubscribe", {"id": sub["id"]})
            self.notify("agent", "new", priority="urgent")
            await self.events.deliver(now + 2)
            self.assertEqual(len(self.deliveries), 2)

        asyncio.run(scenario())

    @requires_local_state
    def test_hourly_cap_and_normal_debounce(self):
        async def scenario():
            await self.subscribe()
            now = time.time()
            self.notify("agent", "normal")
            await self.events.deliver(now)
            self.notify("agent", "later normal")
            await self.events.deliver(now + 1)
            self.assertEqual(len(self.deliveries), 1)
            await self.events.deliver(now + 301)
            self.assertEqual(len(self.deliveries), 2)
            for i in range(40):
                self.notify("agent", "urgent " + str(i), priority="urgent")
            await self.events.deliver(now + 302)
            self.assertEqual(len(self.deliveries), 30)
            await self.events.deliver(now + 303)
            self.assertEqual(len(self.deliveries), 30)

        asyncio.run(scenario())

    @requires_local_state
    def test_throttled_deliveries_are_deferred_to_the_window_end(self):
        # A throttled row must not be re-selected (and rewritten) every second until its window ends.
        def pending():
            with self.state.transaction() as c:
                return [r[0] for r in c.execute("SELECT next_attempt FROM event_deliveries WHERE state='pending'")]

        async def scenario():
            await self.subscribe()
            now = time.time()
            self.notify("agent", "normal")
            await self.events.deliver(now)
            self.notify("agent", "later normal")
            await self.events.deliver(now + 1)
            self.assertEqual(pending(), [now + 300])
            await self.events.deliver(now + 301)
            for i in range(40):
                self.notify("agent", "urgent " + str(i), priority="urgent")
            await self.events.deliver(now + 302)
            self.assertEqual(len(self.deliveries), 30)
            # The ceiling frees one slot when the oldest attempt (at now) leaves the hour.
            self.assertTrue(pending())
            self.assertTrue(all(value == now + 3600 for value in pending()), pending())

        asyncio.run(scenario())

    @requires_local_state
    def test_completion_only_for_tracked_requests_and_followup_generation(self):
        operation = self.state.begin_operation("fleet_submit", {})
        self.state.finish_operation(operation, REQUEST, "ok")

        async def scenario():
            await self.subscribe("fleet.request.completed")
            await self.events.poll_requests()
            await self.events.poll_requests()
            messages = self.messages()["messages"]
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["ref"], REQUEST)
            await self.events.deliver()
            self.assertEqual(len(self.deliveries), 1)
            operation = self.state.begin_operation("fleet_followup", {})
            self.state.finish_operation(operation, REQUEST, "ok")
            await self.events.poll_requests()
            self.assertEqual(self.messages()["unacked_total"], 2)

        asyncio.run(scenario())

    @requires_local_state
    def test_callback_failure_and_invalid_filters_are_refused(self):
        from brigade.dot.events import EventError

        async def scenario():
            async def broken(*args):
                return 200, b'{"challenge":"wrong"}'

            self.events.post = broken
            with self.assertRaises(EventError) as caught:
                await self.subscribe()
            self.assertEqual(caught.exception.code, -32015)
            with self.state.transaction() as c:
                self.assertEqual(c.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 0)
            with self.assertRaises(EventError):
                await self.events.rpc(
                    OWNER,
                    "events/subscribe",
                    {
                        "name": "fleet.inbox.message",
                        "arguments": {"unknown": True},
                        "delivery": {},
                    },
                )

        asyncio.run(scenario())

    @requires_local_state
    def test_410_and_413_not_retried_transient_keeps_event_id(self):
        async def scenario():
            await self.subscribe()
            self.notify("agent", "urgent", priority="urgent")
            now = time.time()
            attempts = []

            async def transient(url, body, headers):
                attempts.append(headers)
                return 503, b""

            self.events.post = transient
            await self.events.deliver(now)
            await self.events.deliver(now + 61)
            self.assertEqual(attempts[0]["webhook-id"], attempts[1]["webhook-id"])
            self.assertNotEqual(attempts[0]["webhook-timestamp"], attempts[1]["webhook-timestamp"])

            async def gone(url, body, headers):
                attempts.append(headers)
                return 413, b""

            self.events.post = gone
            await self.events.deliver(now + 182)
            await self.events.deliver(now + 1000)
            self.assertEqual(len(attempts), 3)

        asyncio.run(scenario())


class EventRotationChecks(EventFixture):
    setUp = EventIntegrationChecks.setUp
    subscribe = EventIntegrationChecks.subscribe

    @requires_local_state
    def test_rotated_secret_keeps_grace_after_identical_refresh(self):
        async def scenario():
            sub = await self.subscribe()
            self.secret = "whsec_" + base64.b64encode(b"n" * 32).decode()

            async def echo(url, body, headers):
                return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode()

            self.events.post = echo
            await self.subscribe()
            await self.subscribe()
            with self.state.transaction() as c:
                row = c.execute("SELECT previous_secret FROM subscriptions WHERE id=?", (sub["id"],)).fetchone()
            self.assertEqual(row["previous_secret"], "whsec_" + base64.b64encode(b"k" * 32).decode())

        asyncio.run(scenario())


class WebhookTransportChecks(unittest.TestCase):
    """Exercise the real webhook_post framing; the other event tests inject a fake post."""

    def run_post(self, reply):
        from unittest import mock
        import anyio
        from brigade.dot import webhooks

        sent = bytearray()

        class Stream:
            def __init__(self):
                self.chunks = [reply[i : i + 7] for i in range(0, len(reply), 7)]

            async def send(self, data):
                sent.extend(data)

            async def receive(self, size):
                if not self.chunks:
                    raise anyio.EndOfStream
                return self.chunks.pop(0)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        async def connect_tcp(*args, **kwargs):
            return Stream()

        async def getaddrinfo(*args, **kwargs):
            return [(None, None, None, None, ("8.8.8.8", 443))]

        async def scenario():
            loop = asyncio.get_running_loop()
            with (
                mock.patch.object(anyio, "connect_tcp", connect_tcp),
                mock.patch.object(loop, "getaddrinfo", getaddrinfo),
            ):
                return await webhooks.webhook_post(
                    "https://connectors.api.openai.com/cb",
                    b'{"a":1}',
                    {"Content-Type": "application/json", "webhook-id": "msg_1"},
                )

        result = asyncio.run(scenario())
        return result, bytes(sent)

    @unittest.skipUnless(
        importlib.util.find_spec("anyio") and importlib.util.find_spec("h11"), "requires the grokbot extra"
    )
    def test_real_post_sends_headers_and_reads_length_body(self):
        (status, body), sent = self.run_post(
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{"challenge":1}'
        )
        self.assertEqual((status, body), (200, b'{"challenge":1}'))
        self.assertIn(b"webhook-id: msg_1\r\n", sent)
        self.assertIn(b"host: connectors.api.openai.com\r\n", sent.lower())

    @unittest.skipUnless(
        importlib.util.find_spec("anyio") and importlib.util.find_spec("h11"), "requires the grokbot extra"
    )
    def test_real_post_reads_body_ended_by_close(self):
        (status, body), _ = self.run_post(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"challenge":"x"}')
        self.assertEqual((status, body), (200, b'{"challenge":"x"}'))


@requires_local_state
@pytest.mark.parametrize("failure", ["raises", "false"])
def test_completion_send_failure_leaves_request_pending(dot_private_dir, failure):
    async def scenario():
        with BridgeState(dot_private_dir) as state:
            operation = state.begin_operation("fleet_submit", {})
            state.finish_operation(operation, REQUEST, "ok")
            hub = FakeHub()
            hub.fail = failure == "raises"
            hub.sent = False
            service = EventService(
                state,
                fleet_status=lambda request: {"ok": True, "result": {"state": "succeeded"}},
                hub=hub,
                hub_poll_seconds=30,
                callback_hosts=HOSTS,
            )
            await service.poll_requests()
            with state.transaction() as c:
                assert c.execute("SELECT completed FROM tracked_requests").fetchone()[0] == 0
                assert c.execute("SELECT count(*) FROM event_queue").fetchone()[0] == 0
            hub.fail, hub.sent = False, True
            await service.poll_requests()
            with state.transaction() as c:
                assert c.execute("SELECT completed FROM tracked_requests").fetchone()[0] == 1
            assert hub.calls == [(REQUEST, 0, "succeeded")] * 2

    asyncio.run(scenario())


@requires_local_state
def test_hub_poll_cadence_and_invalid_credential(dot_private_dir, caplog):
    from brigade.dot.contracts import DotError

    async def scenario():
        with BridgeState(dot_private_dir) as state:
            hub = FakeHub()
            service = EventService(
                state, fleet_status=lambda request: {}, hub=hub, hub_poll_seconds=30, callback_hosts=HOSTS
            )
            for _ in range(20):
                await service.poll_hub()
            assert hub.calls == ["sync", "flush"] * 19 + ["sync", "reconcile", "flush"]

            def invalid():
                raise DotError("hub_credential_invalid")

            hub.sync = invalid
            await service.poll_hub()
            assert hub.calls[-1] == "flush"
            assert "hub_credential_invalid" in caplog.text

    asyncio.run(scenario())


@requires_local_state
def test_gone_subscription_and_thirty_day_pruning(dot_private_dir):
    from brigade.dot.events import enqueue

    async def scenario():
        with BridgeState(dot_private_dir) as state:

            async def gone(url, body, headers):
                payload = json.loads(body)
                return (
                    (200, json.dumps({"challenge": payload["challenge"]}).encode())
                    if "challenge" in payload
                    else (410, b"")
                )

            service = EventService(
                state, fleet_status=lambda request: {}, hub=None, hub_poll_seconds=30, callback_hosts=HOSTS, post=gone
            )
            await service.rpc(
                OWNER,
                "events/subscribe",
                {
                    "name": "fleet.inbox.message",
                    "delivery": {
                        "mode": "webhook",
                        "url": "https://connectors.api.openai.com/test",
                        "secret": "whsec_" + base64.b64encode(b"k" * 32).decode(),
                    },
                },
            )
            now = time.time()
            with state.transaction() as c:
                enqueue(c, "fleet.inbox.message", {}, "urgent", "old", now - 31 * 86400)
                enqueue(c, "fleet.inbox.message", {}, "urgent", "recent", now)
            await service.deliver(now)
            with state.transaction() as c:
                assert c.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 0
                assert [row[0] for row in c.execute("SELECT source FROM event_queue")] == ["recent"]
                assert c.execute("SELECT count(*) FROM event_deliveries").fetchone()[0] == 0

    asyncio.run(scenario())


@requires_local_state
@pytest.mark.parametrize("enabled", [False, True])
def test_events_switch_controls_rpc_capabilities_and_workers(dot_private_dir, enabled):
    from unittest.mock import AsyncMock
    from brigade.dot.events import EventsApp

    async def scenario():
        service = AsyncMock()
        passed = []

        async def inner(scope, receive, send):
            message = await receive()
            passed.append((scope, message))
            if scope["type"] == "lifespan":
                await send({"type": "lifespan.startup.complete"})
                await asyncio.sleep(0)
                await send({"type": "lifespan.shutdown.complete"})
            else:
                method = json.loads(message["body"])["method"]
                reply = (
                    {"result": {"capabilities": {"tools": {}}}}
                    if method == "initialize"
                    else {"error": {"code": -32601}}
                )
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": json.dumps(reply).encode()})

        app = EventsApp(inner, service, enabled=enabled)
        send = AsyncMock()
        receive = AsyncMock(return_value={"type": "lifespan.startup"})
        await app({"type": "lifespan"}, receive, send)
        assert service.run.call_count == int(enabled)
        for method in ("initialize", "server/discover", "events/list", "events/subscribe", "events/unsubscribe"):
            passed.clear()
            send.reset_mock()
            message = {
                "type": "http.request",
                "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}).encode(),
            }
            receive = AsyncMock(return_value=message)
            service.rpc.return_value = {"events": []}
            await app({"type": "http", "headers": []}, receive, send)
            response = json.loads(send.call_args_list[-1].args[0]["body"])
            if method == "initialize":
                assert ("events" in response["result"]["capabilities"]) is enabled
            elif enabled:
                assert "result" in response
                assert passed == []
            else:
                assert response == {"error": {"code": -32601}}
            if not enabled:
                assert passed == [({"type": "http", "headers": []}, message)]
        assert service.rpc.call_count == (3 if enabled else 0)

    asyncio.run(scenario())
