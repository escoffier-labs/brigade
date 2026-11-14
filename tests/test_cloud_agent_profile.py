"""Generic connector inventory and authentication boundaries."""

from tests.dot_support import requires_connector_extra, requires_local_state

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_dot_adapter import Fixture

from brigade.dot.contracts import DotError
from brigade.dot.profile import CLOUD_AGENT, runtime_profile
from brigade.dot.runtime_config import CLOUD_AGENT_RUNTIME_SCHEMA, parse_runtime
from brigade.dot.token_auth import TokenVerifier
from tests.cloud_agent_support import _memory_document
from tests.test_dot_lifecycle import config
from brigade.dot import lifecycle


def cloud_document(tmp_path):
    data = _memory_document()
    data["schema"] = CLOUD_AGENT_RUNTIME_SCHEMA
    data["hub"]["principal"] = "agent:cloud-agent"
    data["hub"]["message_token_file"] = str(tmp_path / "hub-token")
    data["paths"]["state_dir"] = str(tmp_path / "state")
    data["paths"]["workspace"] = str(tmp_path / "workspace")
    token = tmp_path / "listener-token"
    token.write_text(("example-listener-credential-" + "0" * 24))
    token.chmod(0o600)
    (tmp_path / "hub-token").write_text(("example-hub-credential-" + "0" * 30))
    (tmp_path / "hub-token").chmod(0o600)
    data["auth"] = {"token_file": str(token)}
    return data


@requires_connector_extra
@pytest.mark.parametrize("enabled", [None, False, True])
def test_inventory_and_direct_refusal(tmp_path, enabled):
    from brigade.dot.adapter import DotTools

    data = cloud_document(tmp_path)
    if enabled is not None:
        data["messaging_enabled"] = enabled
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    profile = runtime_profile(CLOUD_AGENT, False, messaging_enabled=bool(runtime.messaging_enabled))
    expected = {"memory_search", "memory_card", "handoff_write"}
    if enabled:
        expected |= {"inbox_list", "inbox_ack", "inbox_reply", "message_send"}
    listed = asyncio.run(lifecycle._server(config(), SimpleNamespace(), profile).list_tools())
    assert {tool.name for tool in listed} == expected
    assert profile.principal_memory and not profile.fleet_dispatch and not profile.memory_push
    assert runtime.hub.principal == "agent:cloud-agent"
    tools = DotTools(runtime, None, fleet=None, hub=None, memory=None, inbox=None, profile=profile)
    for name in {"fleet_submit", "skill_get", "dot_t3_capabilities", "fleet_overview", "message_status"}:
        with pytest.raises(DotError, match="invalid_request"):
            tools.call("owner@example.test", name, {})
    snapshot = json.loads((Path(__file__).parent / "fixtures/cloud_agent_tools_snapshot.json").read_text())
    assert sorted(expected) == snapshot["messaging" if enabled else "memory"]


@pytest.mark.parametrize(
    "key,value", [("messaging_enabled", 1), ("dispatch_enabled", False), ("events_enabled", False), ("skills_deny", [])]
)
def test_closed_runtime(tmp_path, key, value):
    data = cloud_document(tmp_path)
    data[key] = value
    with pytest.raises(DotError):
        parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)


@pytest.mark.parametrize(
    "headers,accepted",
    [
        ([(b"authorization", b"Bearer " + ("example-listener-credential-" + "0" * 24).encode())], True),
        ([], False),
        ([(b"authorization", b"Bearer wrong")], False),
        ([(b"cf-access-jwt-assertion", ("example-listener-credential-" + "0" * 24).encode())], False),
        ([(b"authorization", b"Bearer " + ("example-listener-credential-" + "0" * 24).encode())] * 2, False),
    ],
)
def test_token_auth(tmp_path, headers, accepted, caplog):
    runtime = parse_runtime(cloud_document(tmp_path), schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    verifier = TokenVerifier(runtime.auth)
    if accepted:
        assert asyncio.run(verifier.verify({"headers": headers})) == runtime.auth.approved_email
    else:
        with pytest.raises(DotError, match="authentication_required"):
            asyncio.run(verifier.verify({"headers": headers}))
    assert ("example-listener-credential-" + "0" * 24) not in caplog.text
    assert ("example-listener-credential-" + "0" * 24) not in repr(runtime)


def test_token_cannot_share_hub_credential(tmp_path):
    data = cloud_document(tmp_path)
    data["auth"]["token_file"] = data["hub"]["message_token_file"]
    with pytest.raises(DotError):
        parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)


def test_access_configuration_unchanged(tmp_path):
    data = cloud_document(tmp_path)
    data["auth"] = {**_memory_document()["auth"], "mode": "access"}
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    assert runtime.auth.mode == "access"
    assert runtime.auth.audience == "c" * 64


@requires_connector_extra
@pytest.mark.parametrize(
    "header,status",
    [(b"Bearer " + ("example-listener-credential-" + "0" * 24).encode(), 200), (b"Bearer wrong", 401), (None, 401)],
)
def test_listener_token_gate(tmp_path, header, status):
    from tests.test_dot_lifecycle import request

    runtime = parse_runtime(cloud_document(tmp_path), schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    calls = []

    async def app(scope, receive, send):
        calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    gate = lifecycle._DotGate(app, config(), TokenVerifier(runtime.auth), profile=CLOUD_AGENT)
    headers = [(b"host", b"dot.example.test")]
    if header is not None:
        headers.append((b"authorization", header))
    else:
        headers.append((b"cf-access-jwt-assertion", b"fake.signed.assertion"))
    result = asyncio.run(request(gate, {"method": "tools/list"}, headers=headers))
    assert result[0] == status
    assert len(calls) == int(status == 200)
    if status == 401:
        assert result[2] == {"error": "request_denied"}


def test_cloud_agent_no_node_token_fallback(tmp_path, monkeypatch):
    from brigade import fleet_client

    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: pytest.fail("node fallback"))
    data = cloud_document(tmp_path)
    Path(data["auth"]["token_file"]).unlink()
    with pytest.raises(DotError):
        parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)


def test_cloud_agent_rejects_wrong_audience(tmp_path):
    from brigade.dot.access_auth import AccessVerifier
    from tests.test_dot_access_auth import SignatureChecks, AUTH

    helper = SignatureChecks()
    helper.setUpClass()
    data = cloud_document(tmp_path)
    from dataclasses import asdict

    data["auth"] = {k: v for k, v in asdict(AUTH).items() if k != "token_file"}
    runtime = parse_runtime(data, schema=CLOUD_AGENT_RUNTIME_SCHEMA)

    async def fetch():
        return json.dumps({"keys": [helper.public]}).encode()

    verifier = AccessVerifier(runtime.auth, fetch=fetch)
    assert asyncio.run(helper.verify(verifier, helper.token())) == AUTH.approved_email
    with pytest.raises(DotError):
        asyncio.run(helper.verify(verifier, helper.token(changes={"aud": ["b" * 64]})))


@requires_local_state
class TestCloudAgentMessages(Fixture):
    def make_adapter(self, enabled):
        from dataclasses import replace
        from brigade.dot.adapter import DotTools
        from brigade.dot.hub_inbox import HubInbox
        from brigade.dot.inbox_tools import InboxTools
        from tests.test_dot_adapter import NOW

        self.runtime = replace(
            self.runtime,
            schema=CLOUD_AGENT_RUNTIME_SCHEMA,
            dispatch_enabled=False,
            events_enabled=False,
            messaging_enabled=True,
            memory_push=False,
            hub=replace(self.runtime.hub, principal="agent:cloud-agent", recipients={"operator": "agent:operator"}),
        )
        profile = runtime_profile(CLOUD_AGENT, False, messaging_enabled=True)
        inbox = InboxTools(
            self.runtime, self.state, HubInbox(self.state, self.runtime.hub, profile=profile, messaging_enabled=True)
        )
        return DotTools(
            self.runtime, self.state, fleet=None, hub=None, memory=None, inbox=inbox, profile=profile, clock=lambda: NOW
        )

    def test_cloud_agent_message_grants_and_retry(self):
        with pytest.raises(DotError) as caught:
            self.call("message_send", to="ungranted", subject="subject", body="body")
        assert caught.value.hub_code == "message_recipient_not_permitted"
        assert not self.messages.calls
        first = self.call("message_send", to="operator", subject="subject", body="body")
        again = self.call("message_send", to="operator", subject="subject", body="body")
        assert first["id"] == again["id"]
        sends = [c for c in self.messages.calls if c["action"] == "send"]
        assert sends[0]["client_id"] == sends[-1]["client_id"]

    def test_shared_hourly_limit(self):
        # FakeMessages authenticates explicit credentials as agent:dot; this test exercises the shared send budget.
        self.messages.add(recipient="agent:dot")
        for i in range(10):
            self.call("message_send", to="operator", subject=f"subject {i}", body="body")
            self.call("inbox_reply", msg_id=1, body=f"reply {i}")
        with pytest.raises(DotError, match="send_limit_reached"):
            self.call("message_send", to="operator", subject="last", body="body")
        with pytest.raises(DotError, match="send_limit_reached"):
            self.call("inbox_reply", msg_id=1, body="last")

    def test_failed_ack_leaves_pending(self):
        from brigade import fleet_client_messages
        from unittest.mock import patch

        self.messages.add(recipient="agent:dot")
        original = fleet_client_messages.list_messages

        def list_as_cloud(*args, **kwargs):
            # Adapt the shared Dot fake's authenticated recipient to this profile.
            value = original(*args, **kwargs)
            for row in value["messages"]:
                row["recipient"] = "agent:cloud-agent"
            return value

        with patch.object(fleet_client_messages, "list_messages", list_as_cloud):
            assert len(self.call("inbox_list")["messages"]) == 1
            self.messages.fail["ack"] = dict(error="unavailable", status=503, code="hub_unavailable")
            with pytest.raises(DotError, match="hub_unavailable"):
                self.call("inbox_ack", ids=[1])
            assert len(self.call("inbox_list")["messages"]) == 1
        assert any(c["action"] == "ack" for c in self.messages.calls)
        assert self.messages.rows[0]["acked_at"] is None


def test_token_watch_checks_protected_origin(tmp_path, monkeypatch):
    from brigade.dot import watch

    runtime = parse_runtime(cloud_document(tmp_path), schema=CLOUD_AGENT_RUNTIME_SCHEMA)
    probes = []

    def probe(url, **kwargs):
        probes.append((url, kwargs))
        return 401, None

    monkeypatch.setattr(watch, "_probe", probe)
    assert watch.probe_route(config(), runtime) == []
    assert probes == [("http://127.0.0.1:8779/mcp", {"method": "POST"})]
