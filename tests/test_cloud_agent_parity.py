"""Fixed generic contracts retained after the paired profile deletion gate."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from brigade import cli, fleet_client, memory_hub_client as hubc
from brigade.dot import lifecycle
from brigade.dot.memory_tools import MemoryTools
from brigade.dot.process import CommandResult
from brigade.dot.profile import CLOUD_AGENT, runtime_profile
from tests.dot_support import requires_local_state
from tests.test_dot_adapter import HANDOFF, NOW, FakeRunner, lint_report
from tests.test_dot_lifecycle import config
from tests.test_dot_runtime import _document
from brigade.dot.runtime_config import parse_runtime
from tests.test_memory_hub_client import FakeHub, HUB_URL, _put, workspace
from tests.test_fleet_messages import message_hub  # noqa: F401


class CliRunner:
    """Execute the real memory CLI with the connector's protected child environment."""

    def run(self, argv, *, env, **kwargs):
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = cli.main(argv[1:])
        return CommandResult(code, output.getvalue().encode())


def inventory(profile):
    listed = asyncio.run(lifecycle._server(config(), SimpleNamespace(), profile).list_tools())
    return {tool.name: tool.model_dump(mode="json", by_alias=True)["inputSchema"] for tool in listed}


def test_cloud_agent_memory_parity():
    snapshot = json.loads((Path(__file__).parent / "fixtures/dot_tools_snapshot.json").read_text())["tools"]
    assert inventory(CLOUD_AGENT) == {name: snapshot[name]["inputSchema"] for name in CLOUD_AGENT.tools}
    assert set(inventory(CLOUD_AGENT)) == {"memory_search", "memory_card", "handoff_write"}


def test_cloud_agent_message_schema_parity():
    enabled = runtime_profile(CLOUD_AGENT, False, messaging_enabled=True)
    snapshot = json.loads((Path(__file__).parent / "fixtures/dot_tools_snapshot.json").read_text())["tools"]
    names = {"memory_search", "memory_card", "handoff_write", "inbox_list", "inbox_ack", "inbox_reply", "message_send"}
    assert inventory(enabled) == {name: snapshot[name]["inputSchema"] for name in names}


@pytest.fixture
def paired_memory(dot_private_dir, monkeypatch):
    dot_private_dir.mkdir(mode=0o700)
    fake = FakeHub()
    monkeypatch.setattr(hubc, "urllib_transport", fake)
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": HUB_URL})
    target = workspace(dot_private_dir.parent / "workspace", read_scopes=("fleet",))
    _put(fake, "fleet", "public-card", "release notes")
    _put(fake, "principal:other", "private-card", "release private notes")
    for card in fake.cards.values():
        card["trust"] = "reviewed"
    tools = []
    for profile in (CLOUD_AGENT,):
        root = dot_private_dir / profile.pack_id
        root.mkdir(mode=0o700, parents=True)
        token = root / "token"
        credential = "-".join(("fixture", profile.pack_id, "credential")) * 3
        token.write_text(credential)
        token.chmod(0o600)
        principal = "agent:" + profile.pack_id
        fake.principals[credential] = principal
        fake.grants[principal] = {"fleet"}
        runtime = parse_runtime(_document())
        runtime = replace(
            runtime,
            paths=replace(runtime.paths, workspace=target, state_dir=root),
            hub=replace(runtime.hub, principal=principal, message_token_file=token),
        )
        memory = MemoryTools(runtime, commands=CliRunner(), profile=profile)
        memory.clock = lambda: NOW
        tools.append(memory)
    return tools, fake, target


@requires_local_state
def test_cloud_agent_search_and_card_parity(paired_memory):
    tools, fake, _ = paired_memory
    answers = [(tool.memory_search("release"), tool.memory_card("public-card", scope="fleet")) for tool in tools]
    assert len(answers) == 1
    search, card = answers[0]
    assert [row["card_id"] for row in search["matches"]] == ["public-card"]
    assert search["matches"][0]["content_trust"] == "untrusted"
    assert card["content_trust"] == "untrusted" and card["content"] == "release notes\n"
    assert {fake.principals[token] for token in fake.tokens} >= {"agent:cloud-agent"}


@requires_local_state
def test_cloud_agent_private_cards_withheld(paired_memory):
    tools, _, _ = paired_memory
    answers = []
    for tool in tools:
        found = tool.memory_search("private")
        assert found["matches"] == []
        denied = tool.memory_card("private-card", scope="principal:other")
        assert "content" not in denied and "error" in denied
        answers.append((found, denied))
    assert len(answers) == 1


@requires_local_state
def test_cloud_agent_handoff_untrusted(paired_memory, monkeypatch):
    from tests.test_cloud_agent_memory_contract import FakeHubClient

    tools, _, target = paired_memory
    monkeypatch.setattr(hubc, "MemoryHubClient", FakeHubClient)
    FakeHubClient.calls = []
    FakeHubClient.outcome = {"handoff_id": "handoff-fixture", "log_epoch": 1}
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    results = []
    for tool in tools:
        tool.commands = FakeRunner(lambda argv, stdin: CommandResult(0, lint_report()))
        result = tool.handoff_write("parity", HANDOFF)
        assert result["state"] == "pending_review" and result["destination"] == "memory_handoffs"
        assert result == tool.handoff_write("parity", HANDOFF)
        results.append(result)
    assert len(results) == 1
    assert FakeHubClient.calls == [FakeHubClient.calls[0]] * 2
    assert FakeHubClient.calls[0]["text"] == HANDOFF
    assert set(FakeHubClient.calls[0]) == {"action", "text", "kind", "client_event_id"}
    assert {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()} == before


def normalized(value, principal):
    """Normalize only identities and generated message metadata, preserving content and trust."""
    if isinstance(value, dict):
        return {
            key: (["message-id"] * len(item) if key in {"acked", "not_pending"} else normalized(item, principal))
            for key, item in value.items()
            if key not in {"id", "created_at", "acked_at", "next_cursor", "client_id", "reply_to", "thread", "msg_id"}
        }
    if isinstance(value, list):
        return [normalized(item, principal) for item in value]
    return "agent:profile" if value == principal else value


@requires_local_state
def test_cloud_agent_inbox_parity(dot_private_dir, monkeypatch, message_hub):  # noqa: F811
    from brigade import fleet_hub, fleet_hub_messages
    from brigade.dot.adapter import DotTools
    from brigade.dot.hub_inbox import HubInbox
    from brigade.dot.inbox_tools import InboxTools
    from brigade.dot.state import BridgeState
    from tests.test_dot_adapter import EMAIL
    from tests.test_fleet_messages import NODE_A, _request

    dot_private_dir.mkdir(mode=0o700)
    base, node_token, _, db = message_hub
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": base})
    profiles = (runtime_profile(CLOUD_AGENT, False, messaging_enabled=True),)
    outcomes = []
    for profile in profiles:
        principal = "agent:" + profile.pack_id
        with fleet_hub.init_db(db) as connection:
            created = fleet_hub_messages.add_principal(
                connection, {"name": profile.pack_id, "label": "Parity fixture", "may_send_to": [NODE_A]}
            )
        root = dot_private_dir / profile.pack_id
        root.mkdir(mode=0o700, parents=True)
        token = root / "token"
        token.write_text(created["token"])
        token.chmod(0o600)
        runtime = parse_runtime(_document())
        runtime = replace(
            runtime,
            messaging_enabled=True,
            dispatch_enabled=False,
            paths=replace(runtime.paths, state_dir=root),
            hub=replace(runtime.hub, principal=principal, message_token_file=token, recipients={"operator": NODE_A}),
        )
        status, incoming = _request(
            base, node_token, {"action": "send", "to": principal, "subject": "Parity", "body": "Review notes"}
        )
        assert status == 201
        with BridgeState(root) as state:
            inbox = InboxTools(runtime, state, HubInbox(state, runtime.hub, profile=profile, messaging_enabled=True))
            adapter = DotTools(
                runtime,
                state,
                fleet=None,
                hub=None,
                memory=None,
                inbox=inbox,
                profile=profile,
                clock=lambda: datetime.now(timezone.utc),
            )

            def call(name, adapter=adapter, **arguments):
                return adapter.call(EMAIL, name, arguments)

            listed = call("inbox_list")
            assert len(listed["messages"]) == 1 and listed["content_trust"] == "untrusted"
            sent = call("message_send", to="operator", subject="Parity", body="Ready")
            assert sent["id"] == call("message_send", to="operator", subject="Parity", body="Ready")["id"]
            reply = call("inbox_reply", msg_id=incoming["message"]["id"], body="Reviewed")
            ack = call("inbox_ack", ids=[incoming["message"]["id"]])
            assert call("inbox_list")["messages"] == []
            outcomes.append(normalized([listed, sent, reply, ack], principal))
    assert len(outcomes) == 1
