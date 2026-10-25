"""The duet connector profile on the Dot connector code (#1943)."""

from tests.dot_support import requires_local_state

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from brigade import fleet_client, fleet_client_messages, fleet_hub, fleet_hub_messages, grokbot_packs
from brigade.dot import lifecycle as m
from brigade.dot import watch as w
from brigade.dot.access_auth import AccessVerifier, AuthError
from brigade.dot.adapter import DotTools, build_tools, validate_arguments
from brigade.dot.contracts import DotError, TOOLS
from brigade.dot.events import EventService
from brigade.dot.fleet_tools import FleetTools
from brigade.dot.hub_tools import HubTools
from brigade.dot.inbox_tools import HubInbox, InboxTools
from brigade.dot.memory_tools import MemoryTools
from brigade.dot.process import CommandResult
from brigade.dot.profile import DOT, DUET, DUET_EXCLUDED_TOOLS, profile_for
from brigade.dot.runtime_config import DUET_RUNTIME_SCHEMA, RUNTIME_SCHEMA, AuthConfig, parse_runtime
from brigade.dot.state import BridgeState
from brigade.fleet_client_message_credentials import message_scope, message_settings
from tests.test_dot_adapter import BRIGADE, GH, HANDOFF, NOW, Fixture, lint_report
from tests.test_dot_lifecycle import config, request
from tests.test_dot_runtime import _document, _write_private
from tests.test_fleet_messages import NODE_A, message_hub as message_hub

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "dot_tools_snapshot.json").read_text())["tools"]
ISSUE_TOOLS = {
    "duet_capabilities",
    "memory_search",
    "memory_card",
    "handoff_write",
    "fleet_policy",
    "skill_search",
    "skill_get",
    "inbox_list",
    "inbox_ack",
    "inbox_reply",
    "message_send",
    "message_status",
    "fleet_overview",
    "fleet_sessions",
    "fleet_work_list",
    "fleet_work_show",
    "fleet_work_next",
    "fleet_work_burn",
    "fleet_models",
    "fleet_cloud",
    "fleet_hub_policy",
    "fleet_digest",
}
DISPATCH_AND_T3 = {
    "fleet_submit",
    "fleet_followup",
    "fleet_answer",
    "fleet_approve",
    "fleet_interrupt",
    "fleet_capacity",
    "fleet_doctor",
    "fleet_status",
    "fleet_wait",
    "fleet_recent",
}


def _duet_document() -> dict[str, object]:
    return {
        "schema": DUET_RUNTIME_SCHEMA,
        "auth": {
            "issuer": "https://team.example.test",
            "team_domain": "team.example.test",
            "audience": "b" * 64,
            "approved_email": "duet@example.test",
            "resource": "https://duet.example.test/mcp",
        },
        "paths": {
            "state_dir": str(Path("/srv/duet/state").absolute()),
            "workspace": str(Path("/srv/duet/workspace").absolute()),
            "bin": {
                "brigade": str(Path("/usr/bin/brigade").absolute()),
                "git": str(Path("/usr/bin/git").absolute()),
                "gh": str(Path("/usr/bin/gh").absolute()),
            },
        },
        "hub": {
            "principal": "agent:duet",
            "node_id": "00000000-0000-4000-8000-000000000003",
            "message_token_file": str(Path("/srv/duet/secrets/message-token").absolute()),
            "poll_seconds": 30,
            "recipients": {"operator": "agent:operator", "miller": "agent:miller"},
        },
        "messaging_enabled": True,
        "digest_pr_repo": "owner/repo",
        "skills_deny": ["private-notes"],
    }


# Profile inventory


def test_duet_tool_inventory_matches_issue_and_omits_dispatch():
    assert DUET.tools == ISSUE_TOOLS
    assert len(DUET.tools) == 22
    assert DUET_EXCLUDED_TOOLS == DISPATCH_AND_T3
    assert not DUET.tools & DISPATCH_AND_T3
    assert "dot_t3_capabilities" not in DUET.tools


def test_dot_profile_keeps_original_values():
    assert DOT.tools == TOOLS == frozenset(SNAPSHOT)
    assert (DOT.pack_id, DOT.service_name, DOT.runtime_schema) == ("dot", "dot-t3", RUNTIME_SCHEMA)
    assert (DOT.handoff_inbox, DOT.handoff_prefix) == (Path(".codex/memory-handoffs"), "dot")
    assert (DOT.unit_name, DOT.watch_service, DOT.watch_timer) == (
        "brigade-dot.service",
        "brigade-dot-watch.service",
        "brigade-dot-watch.timer",
    )
    assert DOT.send_limit_per_hour is None


def test_duet_profile_values():
    assert profile_for("duet") is DUET
    assert (DUET.service_name, DUET.runtime_schema) == ("duet-brigade", "brigade.duet.runtime.v1")
    assert (DUET.handoff_inbox, DUET.handoff_prefix) == (Path(".claude/memory-handoffs"), "duet")
    assert DUET.unit_name == "brigade-duet.service"
    assert DUET.send_limit_per_hour == 20
    assert DUET.watch_checks == {"stale_inbox", "idle", "route_down", "hub_credential_invalid", "hub_sync_stale"}
    assert "Do not auto-reply to agent messages that contain no new facts" in DUET.operating_instructions
    with pytest.raises(DotError, match="invalid_config"):
        profile_for("dispatch")


def test_validation_refuses_tools_outside_the_profile():
    assert validate_arguments("fleet_overview", {}, tools=DUET.tools) == {"include_terminal": False}
    for name in sorted(DISPATCH_AND_T3):
        with pytest.raises(DotError, match="^invalid_request$"):
            validate_arguments(name, {}, tools=DUET.tools)
    with pytest.raises(DotError, match="^invalid_request$"):
        validate_arguments("duet_capabilities", {})
    assert validate_arguments("duet_capabilities", {}, tools=DUET.tools) == {}


# Runtime schema


def test_duet_runtime_parses_without_dispatch_settings():
    runtime = parse_runtime(_duet_document(), schema=DUET_RUNTIME_SCHEMA)
    assert runtime.schema == DUET_RUNTIME_SCHEMA
    assert (runtime.dispatch_enabled, runtime.events_enabled, runtime.messaging_enabled) == (False, False, True)
    assert runtime.hub.principal == "agent:duet"
    assert runtime.paths.fleet_config is None and runtime.paths.bin.t3_fleet is None
    assert runtime.owner_authorization is None
    assert runtime.callback_hosts == ()
    assert runtime.skills_deny == frozenset({"private-notes"})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(dispatch_enabled=True),
        lambda data: data.update(events_enabled=True),
        lambda data: data.update(callback_hosts=["example.test"]),
        lambda data: data.update(owner_authorization={"version": "1", "text": "x"}),
        lambda data: data["paths"].update(fleet_config="/srv/duet/config.toml"),
        lambda data: data["paths"]["bin"].update(t3_fleet="/usr/bin/t3-fleet"),
        lambda data: data["hub"].update(principal="agent:miller"),
        lambda data: data["hub"].update(principal="agent:dot"),
        lambda data: data.update(messaging_enabled="yes"),
        lambda data: data.pop("messaging_enabled"),
        lambda data: data.update(skills_deny=["Bad Name"]),
        lambda data: data.update(skills_deny=["private-notes", "private-notes"]),
        lambda data: data.update(skills_deny=[f"skill-{i:03d}" for i in range(257)]),
        lambda data: data.update(schema=RUNTIME_SCHEMA),
    ],
    ids=[
        "dispatch-key",
        "events-key",
        "callback-hosts",
        "owner-authorization",
        "fleet-config",
        "t3-fleet-bin",
        "shared-principal",
        "dot-principal",
        "messaging-not-bool",
        "messaging-missing",
        "deny-bad-name",
        "deny-duplicate",
        "deny-too-many",
        "dot-schema",
    ],
)
def test_duet_runtime_rejects_dispatch_and_shared_identity(mutate):
    data = _duet_document()
    mutate(data)
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(data, schema=DUET_RUNTIME_SCHEMA)


def test_runtime_documents_never_cross_schemas():
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(_duet_document())
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(_document(), schema=DUET_RUNTIME_SCHEMA)
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(_document(), schema="brigade.other.runtime.v1")


def test_dot_messaging_gate_is_optional_and_defaults_to_dispatch():
    assert parse_runtime(_document()).messaging_enabled is None
    data = _document()
    data.update(dispatch_enabled=False, messaging_enabled=True)
    runtime = parse_runtime(data)
    assert (runtime.dispatch_enabled, runtime.messaging_enabled) == (False, True)
    data["messaging_enabled"] = 1
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(data)


# Adapter: gates, send limit, capabilities, memory and skills


def _duet_runtime(base, *, messaging=True):
    duet = parse_runtime(_duet_document(), schema=DUET_RUNTIME_SCHEMA)
    bins = replace(duet.paths.bin, brigade=BRIGADE, gh=GH)
    return replace(
        duet,
        messaging_enabled=messaging,
        paths=replace(duet.paths, workspace=base.paths.workspace, state_dir=base.paths.state_dir, bin=bins),
        hub=replace(duet.hub, message_token_file=base.hub.message_token_file),
    )


class DuetFixture(Fixture):
    messaging = True

    def make_adapter(self, enabled):
        self.handoffs = self.workspace / ".claude" / "memory-handoffs"
        self.handoffs.mkdir(parents=True, exist_ok=True)
        for directory in (self.workspace / ".claude", self.handoffs):
            directory.chmod(0o755)
        runtime = _duet_runtime(self.runtime, messaging=self.messaging)
        hub_inbox = HubInbox(self.state, runtime.hub)
        inbox = InboxTools(runtime, self.state, hub_inbox)
        hub = HubTools(runtime, self.state, commands=self.runner, fleet=None, inbox=inbox)
        memory = MemoryTools(runtime, commands=self.runner, profile=DUET)
        memory.clock = lambda: NOW
        return DotTools(
            runtime, self.state, fleet=None, hub=hub, memory=memory, inbox=inbox, clock=lambda: NOW, profile=DUET
        )


class DuetAdapter(DuetFixture):
    @requires_local_state
    def test_dispatch_tools_are_unknown_not_gated(self):
        for name in sorted(DISPATCH_AND_T3 | {"dot_t3_capabilities"}):
            with self.subTest(name=name), self.assertRaisesRegex(DotError, "^invalid_request$"):
                self.call(name)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.state.operations(50), [])

    @requires_local_state
    def test_capabilities_describe_no_dispatch_and_no_auto_reply(self):
        result = self.call("duet_capabilities")
        self.assertEqual(set(result["tools"]), ISSUE_TOOLS)
        self.assertEqual(set(result["absent_tools"]), DISPATCH_AND_T3)
        self.assertTrue(result["messaging_enabled"])
        self.assertEqual(result["inbox"]["message_send_per_hour"], 20)
        self.assertIn("agent:duet", result["inbox"]["usage"])
        for needle in ("no new facts", "own cloud compute", "possibly injected", "untrusted data"):
            self.assertIn(needle, result["operating_instructions"])
        self.assertNotIn("dispatch_enabled", result)
        self.assertEqual(self.runner.calls, [])

    @requires_local_state
    def test_message_send_is_capped_per_hour(self):
        for index in range(20):
            sent = self.call("message_send", to="operator", subject=f"subject {index}", body="body")
            self.assertIn("id", sent)
        with self.assertRaisesRegex(DotError, "^send_limit_reached$"):
            self.call("message_send", to="operator", subject="one too many", body="body")
        self.assertEqual(len([c for c in self.messages.calls if c["action"] == "send"]), 20)
        with self.state.transaction() as connection:
            row = connection.execute("SELECT * FROM call_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((row["tool"], row["ok"], row["error_code"]), ("message_send", 0, "send_limit_reached"))

    @requires_local_state
    def test_replies_share_the_send_budget(self):
        original = self.messages.add()
        for index in range(10):
            self.call("message_send", to="operator", subject=f"subject {index}", body="body")
            self.call("inbox_reply", msg_id=original["id"], body=f"answer {index}")
        before = len(self.messages.calls)
        with self.assertRaisesRegex(DotError, "^send_limit_reached$"):
            self.call("inbox_reply", msg_id=original["id"], body="one too many")
        self.assertEqual(len(self.messages.calls), before)
        with self.assertRaisesRegex(DotError, "^send_limit_reached$"):
            self.call("message_send", to="operator", subject="one too many", body="body")

    @requires_local_state
    def test_only_configured_aliases_reach_transport_or_consume_budget(self):
        for recipient in ("unknown", "agent:operator", "00000000-0000-4000-8000-000000000003"):
            with self.subTest(recipient=recipient), self.assertRaisesRegex(DotError, "^invalid_request$"):
                self.call("message_send", to=recipient, subject="subject", body="body")
        self.assertEqual(self.messages.calls, [])
        for index in range(20):
            self.call("message_send", to="operator", subject=f"subject {index}", body="body")
        self.assertEqual(len([c for c in self.messages.calls if c["action"] == "send"]), 20)

    @requires_local_state
    def test_send_budget_rolls_over_after_an_hour(self):
        start = 1_000_000.0
        self.assertTrue(all(self.state.reserve_send(2, 3600, now=start + i) for i in range(2)))
        self.assertFalse(self.state.reserve_send(2, 3600, now=start + 10))
        self.assertFalse(self.state.reserve_send(2, 3600, now=start + 3599))
        # The first send ages out at exactly one hour, freeing one slot and only one.
        self.assertTrue(self.state.reserve_send(2, 3600, now=start + 3600))
        self.assertFalse(self.state.reserve_send(2, 3600, now=start + 3600.5))

    @requires_local_state
    def test_handoff_lands_in_claude_inbox_with_duet_prefix(self):
        self.runner.handler = lambda argv, stdin: CommandResult(0, lint_report(True))
        result = self.call("handoff_write", slug="duet-probe", content=HANDOFF)
        name = f"{NOW.astimezone():%Y-%m-%d-%H%M}-duet-duet-probe.md"
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], name)
        self.assertEqual([path.name for path in self.handoffs.iterdir()], [name])
        self.assertEqual(list(self.inbox.iterdir()), [])  # Dot's .codex inbox is untouched.
        self.assertEqual(self.runner.argvs()[0][:4], [BRIGADE, "handoff", "lint", "--json"])

    @requires_local_state
    def test_denied_skill_reads_as_missing_and_is_filtered_from_search(self):
        self.assertEqual(self.call("skill_get", name="private-notes"), {"error": "skill_not_found"})
        self.assertEqual(self.runner.calls, [])
        # Rows are shaped as `brigade skills search --json` prints them; either name of a denied entry hides it.
        kept = {"skill_dir": "/registry/brigade-work", "metadata": {"id": "brigade-work"}}
        rows = [
            {"skill_dir": "/registry/private-notes", "metadata": {"id": "private-notes"}},
            {"skill_dir": "/registry/private-notes", "metadata": {"id": "renamed"}},
            {"skill_dir": "/registry/other", "metadata": {"id": "private-notes"}},
            kept,
        ]
        found = {"skills": rows, "count": 4}
        self.runner.handler = lambda argv, stdin: CommandResult(0, json.dumps(found).encode())
        self.assertEqual(self.call("skill_search", query="notes"), {"skills": [kept], "count": 1})


class DuetMessagingOff(DuetFixture):
    messaging = False

    @requires_local_state
    def test_messaging_gate_is_separate_from_dispatch(self):
        with self.assertRaisesRegex(DotError, "^messaging_disabled$"):
            self.call("message_send", to="operator", subject="subject", body="body")
        self.assertEqual(self.messages.calls, [])
        self.assertFalse(self.call("duet_capabilities")["messaging_enabled"])

    @requires_local_state
    def test_reply_obeys_messaging_gate(self):
        original = self.messages.add()
        with self.assertRaisesRegex(DotError, "^messaging_disabled$"):
            self.call("inbox_reply", msg_id=original["id"], body="answer")
        self.assertEqual(self.messages.calls, [])


class DotMessagingWithoutDispatch(Fixture):
    @requires_local_state
    def test_dot_can_message_with_dispatch_off(self):
        self.runtime = replace(self.runtime, messaging_enabled=True)
        self.adapter = self.make_adapter(False)
        self.assertIn("id", self.call("message_send", to="review", subject="subject", body="body"))
        with self.assertRaisesRegex(DotError, "^dispatch_disabled$"):
            self.call("fleet_submit", repository="brigade", title="t", prompt="p", host="alpha", seat="sol61")
        capabilities = self.call("dot_t3_capabilities")
        self.assertTrue(capabilities["messaging_enabled"])
        self.assertNotIn("message_send", capabilities["dispatch_gated_tools"])

    @requires_local_state
    def test_dot_messaging_off_with_dispatch_on(self):
        self.runtime = replace(self.runtime, messaging_enabled=False)
        self.adapter = self.make_adapter(True)
        with self.assertRaisesRegex(DotError, "^messaging_disabled$"):
            self.call("message_send", to="review", subject="subject", body="body")
        capabilities = self.call("dot_t3_capabilities")
        self.assertFalse(capabilities["messaging_enabled"])
        self.assertNotIn("message_send", capabilities["dispatch_gated_tools"])

    @requires_local_state
    def test_dot_messaging_without_override_follows_dispatch(self):
        for dispatch in (False, True):
            with self.subTest(dispatch=dispatch):
                self.adapter = self.make_adapter(dispatch)
                capabilities = self.call("dot_t3_capabilities")
                self.assertEqual(capabilities["messaging_enabled"], dispatch)
                self.assertIn("message_send", capabilities["dispatch_gated_tools"])


@requires_local_state
def test_build_tools_never_constructs_a_fleet_client(dot_private_dir):
    runtime = parse_runtime(_duet_document(), schema=DUET_RUNTIME_SCHEMA)
    runtime = replace(runtime, paths=replace(runtime.paths, state_dir=dot_private_dir))
    with patch.object(FleetTools, "__init__", side_effect=AssertionError("fleet client built")):
        tools, state, _ = build_tools(runtime, DUET)
    try:
        assert tools.fleet is None and tools.profile is DUET
    finally:
        state.close()
    with pytest.raises(DotError, match="^invalid_config$"):
        build_tools(runtime, DOT)


# Registration and HTTP gate


def test_registered_duet_tools_are_the_profile_and_keep_dot_schemas():
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    listed = asyncio.run(m._server(config(), SimpleNamespace(), DUET).list_tools())
    assert {tool.name for tool in listed} == ISSUE_TOOLS
    for tool in listed:
        value = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        if tool.name == "duet_capabilities":
            assert value["annotations"] == {"readOnlyHint": True}
            continue
        # Shared tools keep Dot's wire schemas; only a few descriptions are reworded for Duet.
        assert value["inputSchema"] == SNAPSHOT[tool.name]["inputSchema"]
        assert value["annotations"] == SNAPSHOT[tool.name]["annotations"]
        if tool.name not in DUET.descriptions:
            assert value["description"] == SNAPSHOT[tool.name]["description"]
        assert "dot" not in value["description"].lower().replace("dot-", "")


def test_dot_registration_is_unchanged_by_profiles():
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    listed = asyncio.run(m._server(config(), SimpleNamespace()).list_tools())
    assert {tool.name for tool in listed} == TOOLS
    for tool in listed:
        value = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        expected = dict(SNAPSHOT[tool.name])
        if tool.name == "dot_t3_capabilities":
            value.pop("description")
            expected.pop("description")
        assert value == expected


def _duet_verifier():
    result = AccessVerifier(parse_runtime(_duet_document(), schema=DUET_RUNTIME_SCHEMA).auth)
    result.verify = AsyncMock(return_value="duet@example.test")
    return result


def _event_service(state):
    return EventService(
        state, fleet_status=lambda rid: {}, hub=None, hub_poll_seconds=30, callback_hosts=frozenset({"example.test"})
    )


@requires_local_state
def test_gate_lists_duet_tools_refuses_dispatch_and_logs_client_name(dot_private_dir):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    runtime = parse_runtime(_duet_document(), schema=DUET_RUNTIME_SCHEMA)
    with BridgeState(dot_private_dir) as state:
        handler = SimpleNamespace(fleet_overview=lambda **kwargs: {"ok": True})
        tools = DotTools(runtime, state, fleet=None, hub=handler, memory=handler, inbox=handler, profile=DUET)
        app = m.build_app(config(), tools, _event_service(state), _duet_verifier(), events_enabled=False, profile=DUET)

        async def scenario():
            async with app.app.router.lifespan_context(app.app):
                initialize = dict(
                    protocolVersion="2025-06-18", capabilities={}, clientInfo={"name": "Claude Duet", "version": "1"}
                )
                for method, params in [("initialize", initialize), ("tools/list", {})]:
                    status, _, result = await request(app, dict(jsonrpc="2.0", id=1, method=method, params=params))
                    assert status == 200
                    if method == "tools/list":
                        assert {t["name"] for t in result["result"]["tools"]} == ISSUE_TOOLS
                for name in sorted(DISPATCH_AND_T3):
                    call = dict(jsonrpc="2.0", id=2, method="tools/call", params=dict(name=name, arguments={}))
                    assert (await request(app, call))[0] == 400
                call = dict(jsonrpc="2.0", id=3, method="tools/call", params=dict(name="fleet_overview", arguments={}))
                status, _, result = await request(app, call)
                assert status == 200 and "error" not in result

        asyncio.run(scenario())
        with state.transaction() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM call_log ORDER BY id")]
    assert rows[0]["tool"] == "initialize" and rows[0]["client_name"] == "Claude Duet"
    assert [row["tool"] for row in rows[2:12]] == ["tools/call"] * 10
    assert all(row["error_code"] == "invalid_request" for row in rows[2:12])
    assert rows[-1]["tool"] == "fleet_overview" and rows[-1]["ok"] == 1


@requires_local_state
def test_client_name_is_bounded_and_printable(dot_private_dir):
    with BridgeState(dot_private_dir) as state:
        state.log_call("initialize", True, 1, client_name="claude-ai", clock=lambda: NOW)
        state.log_call("initialize", True, 1, client_name="bad\nname", clock=lambda: NOW)
        state.log_call("initialize", True, 1, client_name="x" * 200, clock=lambda: NOW)
        with state.transaction() as connection:
            names = [row[0] for row in connection.execute("SELECT client_name FROM call_log ORDER BY id")]
    assert names == ["claude-ai", None, "x" * 64]


# Access audiences: a Dot token never reaches Duet, and the reverse


class TestAudienceSeparation:
    @classmethod
    def setup_class(cls):
        pytest.importorskip("cryptography")
        pytest.importorskip("anyio")
        from cryptography.hazmat.primitives.asymmetric import rsa

        cls.jwt = pytest.importorskip("jwt")
        # Isolated ephemeral test signing key. Never persisted or printed.
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(cls.jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwks = json.dumps(
            {"keys": [dict(kid="fixture", kty=public["kty"], alg="RS256", use="sig", n=public["n"], e=public["e"])]}
        ).encode()

    def token(self, auth: AuthConfig) -> str:
        now = int(datetime.now(timezone.utc).timestamp())
        claims = dict(
            iss=auth.issuer,
            aud=[auth.audience],
            sub="synthetic-subject",
            email=auth.approved_email,
            type="app",
            iat=now - 10,
            nbf=now - 10,
            exp=now + 300,
        )
        return self.jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "fixture"})

    def verifier(self, auth: AuthConfig) -> AccessVerifier:
        async def fetch():
            return self.jwks

        return AccessVerifier(auth, fetch=fetch, monotonic=lambda: 1000)

    def test_tokens_are_bound_to_their_own_audience(self):
        dot_auth = parse_runtime(_document()).auth
        duet_auth = parse_runtime(_duet_document(), schema=DUET_RUNTIME_SCHEMA).auth
        assert dot_auth.audience != duet_auth.audience

        async def verify(auth, token):
            header = (b"cf-access-jwt-assertion", token.encode("ascii"))
            return await self.verifier(auth).verify({"headers": [header]})

        async def scenario():
            assert await verify(duet_auth, self.token(duet_auth)) == duet_auth.approved_email
            assert await verify(dot_auth, self.token(dot_auth)) == dot_auth.approved_email
            with pytest.raises(AuthError):
                await verify(duet_auth, self.token(dot_auth))
            with pytest.raises(AuthError):
                await verify(dot_auth, self.token(duet_auth))
            # Same audience but the other connector's approved identity is refused too.
            with pytest.raises(AuthError):
                await verify(duet_auth, self.token(replace(duet_auth, approved_email=dot_auth.approved_email)))

        asyncio.run(scenario())


# Lifecycle, units, watch and pack registry


@pytest.fixture
def duet_configured(dot_private_dir):
    root = dot_private_dir.parent
    data = _duet_document()
    data["paths"].update(state_dir=str(dot_private_dir), workspace=str(root / "workspace"))
    token = root / "duet.token"
    token.write_text("fixture-duet-credential-" * 2)
    token.chmod(0o600)
    data["hub"]["message_token_file"] = str(token)
    runtime_path = root / "duet-runtime.json"
    runtime_path.write_text(json.dumps(data))
    runtime_path.chmod(0o600)
    target = root / "target"
    instance = target / ".brigade/grokbot/packs/duet.json"
    instance.parent.mkdir(parents=True, exist_ok=True)
    instance.write_text(
        json.dumps(
            dict(
                schema="brigade.grokbot.connector-instance.v1",
                pack_id="duet",
                pack_version="1.0.0",
                bind="127.0.0.1:8780",
                allowed_hosts=["duet.example.test"],
                allowed_origins=["https://claude.ai"],
                public_route="",
                runtime_path=str(runtime_path),
            )
        )
    )
    return target, parse_runtime(data, schema=DUET_RUNTIME_SCHEMA), instance


@requires_local_state
def test_duet_units_name_the_pack(duet_configured, tmp_path):
    target, _, _ = duet_configured
    unit = m.render_unit(target, python="/usr/bin/python3", pack_id="duet")
    assert "# Unit: brigade-duet.service" in unit
    assert "Description=Brigade Duet MCP listener" in unit
    assert "--pack duet" in unit and "--bind 127.0.0.1:8780" in unit
    service, timer = m.render_watch_units(target, python="/usr/bin/python3", pack_id="duet")
    assert "brigade.dot watch --target" in service and " -I -c " in service
    assert service.rstrip().count("--pack duet") == 1
    assert "Unit=brigade-duet-watch.service" in timer
    written = m.write_watch_units(target, tmp_path, python="/usr/bin/python3", pack_id="duet")
    assert [path.name for path in written] == ["brigade-duet-watch.service", "brigade-duet-watch.timer"]
    assert m.write_unit(target, tmp_path, python="/usr/bin/python3", pack_id="duet").name == "brigade-duet.service"


@requires_local_state
def test_dot_target_cannot_load_a_duet_runtime(duet_configured):
    target, _, instance = duet_configured
    dot_instance = instance.with_name("dot.json")
    payload = json.loads(instance.read_text())
    payload.update(pack_id="dot", bind="127.0.0.1:8779")
    dot_instance.write_text(json.dumps(payload))
    with pytest.raises(grokbot_packs.PackError):
        m._load_config(target)


@requires_local_state
def test_duet_canary_checks_its_own_inventory(duet_configured):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    target, runtime, _ = duet_configured
    metadata = {"resource": runtime.auth.resource, "authorization_servers": ["https://duet.example.test"]}
    with patch.object(m, "_probe", side_effect=[(401, {}), (200, metadata)]):
        assert m.canary(target, pack_id="duet") == {"ok": True, "pack_id": "duet"}
    with (
        patch.object(m, "_probe", side_effect=[(401, {}), (200, metadata)]),
        patch.object(m, "_registered_names", return_value=set(TOOLS)),
    ):
        assert m.canary(target, pack_id="duet")["reason"] == "inventory-mismatch"


@requires_local_state
def test_duet_doctor_checks_isolation_instead_of_t3_fleet(authenticated_duet):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    target, _, _ = authenticated_duet
    with patch.object(m, "_probe", return_value=(401, None)):
        checks = m.doctor(target, pack_id="duet")
    assert [c["check"] for c in checks] == [
        "dependency",
        "config",
        "state",
        "access-config",
        "hub-token",
        "hub-identity",
        "isolation",
        "endpoint",
    ]
    assert all(c["status"] == "ok" for c in checks)


@requires_local_state
def test_duet_isolation_fails_when_sharing_dot_identity(duet_configured, dot_private_dir):
    target, runtime, instance = duet_configured
    dot_data = _document()
    shared = dot_private_dir.parent / "dot-state"
    shared.mkdir(mode=0o700)
    dot_data["paths"].update(state_dir=str(shared))
    dot_data["hub"]["message_token_file"] = str(runtime.hub.message_token_file)
    dot_runtime = dot_private_dir.parent / "dot-runtime.json"
    dot_runtime.write_text(json.dumps(dot_data))
    dot_runtime.chmod(0o600)
    payload = json.loads(instance.read_text())
    payload.update(pack_id="dot", bind="127.0.0.1:8779", runtime_path=str(dot_runtime))
    instance.with_name("dot.json").write_text(json.dumps(payload))
    assert m._isolated_from_dot(target, runtime) is False
    with pytest.raises(DotError) as refused:
        m.build_listener_from_target(target, pack_id="duet")
    assert refused.value.code == "not_isolated_from_dot"
    dot_data["hub"]["message_token_file"] = str(dot_private_dir.parent / "dot.token")
    dot_runtime.write_text(json.dumps(dot_data))
    assert m._isolated_from_dot(target, runtime) is True
    _, tools, _ = m.build_listener_from_target(target, pack_id="duet")
    tools.state.close()
    # Another spelling of Duet's own token file, or a link to it, is still the same credential.
    token = runtime.hub.message_token_file
    dot_data["hub"]["message_token_file"] = str(token.parent / "x" / ".." / token.name)
    dot_runtime.write_text(json.dumps(dot_data))
    assert m._isolated_from_dot(target, runtime) is False
    link = dot_private_dir.parent / "dot-token-link"
    link.symlink_to(token)
    dot_data["hub"]["message_token_file"] = str(link)
    dot_runtime.write_text(json.dumps(dot_data))
    assert m._isolated_from_dot(target, runtime) is False
    # A separate file holding Duet's token is still Duet's credential.
    copy = dot_private_dir.parent / "dot-copy.token"
    copy.write_text(token.read_text())
    copy.chmod(0o600)
    dot_data["hub"]["message_token_file"] = str(copy)
    dot_runtime.write_text(json.dumps(dot_data))
    assert m._isolated_from_dot(target, runtime) is False
    copy.write_text("fixture-dot-credential-" * 2)
    assert m._isolated_from_dot(target, runtime) is True
    # A token that exists but cannot be read cannot be shown to differ.
    copy.chmod(0o644)
    assert m._isolated_from_dot(target, runtime) is False


@pytest.fixture
def duet_hub(duet_configured, monkeypatch):
    _, runtime, _ = duet_configured
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: dict(hub_url="https://hub.invalid", node_token="n")
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-a")
    return message_scope(*message_settings(str(runtime.hub.message_token_file))[:2])


@pytest.fixture
def authenticated_duet(duet_configured, message_hub, monkeypatch):
    target, runtime, _ = duet_configured
    base, node_token, _, db = message_hub
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(hub_url=base, node_token=node_token))
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    with fleet_hub.init_db(db) as connection:
        principal = fleet_hub_messages.add_principal(
            connection, {"name": "duet", "label": "Fixture", "may_send_to": [NODE_A]}
        )
    runtime.hub.message_token_file.write_text(principal["token"])
    return target, runtime, db


@pytest.fixture
def listener_dependencies(monkeypatch):
    # These tests replace the app and server, so they need no optional server packages.
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=Mock()))
    monkeypatch.setattr(m.access_auth, "_jwt", Mock())


@requires_local_state
@pytest.mark.parametrize("evidence", ["matching", "dot", "other", "revoked", "unavailable", "malformed", "old-hub"])
def test_duet_startup_authenticates_identity_before_registration(
    authenticated_duet, monkeypatch, evidence, listener_dependencies
):
    target, runtime, db = authenticated_duet
    if evidence in {"dot", "other"}:
        with fleet_hub.init_db(db) as connection:
            principal = fleet_hub_messages.add_principal(connection, {"name": evidence, "label": "Fixture"})
        runtime.hub.message_token_file.write_text(principal["token"])
    elif evidence == "revoked":
        with fleet_hub.init_db(db) as connection:
            connection.execute("UPDATE fleet_message_principals SET revoked_at='2026-01-01'")
    elif evidence == "unavailable":

        def unavailable(*args, **kwargs):
            raise fleet_client.FleetClientError("fixture unavailable")

        monkeypatch.setattr(fleet_client_messages, "_request", unavailable)
    elif evidence == "old-hub":
        monkeypatch.setattr(fleet_client_messages, "_request", lambda *a, **kw: (400, {"error": "unsupported action"}))
    elif evidence == "malformed":
        monkeypatch.setattr(fleet_client_messages, "_request", lambda *a, **kw: (200, {"principal": ["agent:duet"]}))
    cfg, tools, runtime = m.build_listener_from_target(target, pack_id="duet")
    with patch.object(m, "build_app", return_value="app") as build, patch("uvicorn.run") as run:
        if evidence == "matching":
            m.run_listener(cfg, tools, runtime)
            build.assert_called_once()
            run.assert_called_once()
        else:
            with pytest.raises(
                DotError, match="^hub_unavailable$" if evidence == "unavailable" else "^hub_identity_invalid$"
            ):
                m.run_listener(cfg, tools, runtime)
            build.assert_not_called()
            run.assert_not_called()
    with patch.object(w, "probe_route", return_value=[]):
        watched = w.run_watch_once(target, now=NOW, dry_run=True, pack_id="duet")
    kinds = {alert["kind"] for alert in watched["alerts"]}
    assert ("hub_credential_invalid" in kinds) == (evidence not in {"matching", "unavailable"})
    if evidence == "unavailable":
        assert "hub_sync_stale" in kinds
    with patch.object(m, "_probe", return_value=(401, None)):
        checks = {item["check"]: item for item in m.doctor(target, pack_id="duet")}
    assert checks["hub-identity"]["status"] == ("ok" if evidence == "matching" else "fail")
    if evidence != "matching":
        assert checks["hub-identity"]["reason"] == (
            "hub_unavailable" if evidence == "unavailable" else "hub_identity_invalid"
        )


@requires_local_state
def test_duet_lifespan_polls_real_hub_without_event_delivery(authenticated_duet, listener_dependencies):
    from brigade.dot.event_app import EventsApp

    target, runtime, _ = authenticated_duet
    fleet_client_messages.send(
        recipient="agent:duet", subject="question", body="fixture body", reference=None, priority="normal"
    )
    cfg, tools, runtime = m.build_listener_from_target(target, pack_id="duet")
    now = datetime.now(timezone.utc)
    tools.state.log_call("fleet_digest", True, 5, clock=lambda: now)
    captured = []

    def build(config, adapter, service, verifier, **kwargs):
        captured.append(service)
        return "app"

    async def scenario(service):
        polled = asyncio.Event()
        original = service.poll_hub
        assert service.hub is tools.inbox.hub
        service._hub_polls = 19  # Exercise the scheduled reconciliation on this poll.

        async def poll():
            await original()
            polled.set()

        service.poll_hub = poll
        service.post = AsyncMock(side_effect=AssertionError("callback must stay disabled"))
        service.deliver = AsyncMock(side_effect=AssertionError("delivery must stay disabled"))
        service.poll_requests = AsyncMock(side_effect=AssertionError("request polling must stay disabled"))

        async def inner(scope, receive, send):
            await send({"type": "lifespan.startup.complete"})
            try:
                await asyncio.wait_for(polled.wait(), 2)
                with tools.state.transaction() as connection:
                    rows = connection.execute("SELECT * FROM hub_inbox").fetchall()
                assert len(rows) == 1 and rows[0]["subject"] == "question"
            finally:
                await send({"type": "lifespan.shutdown.complete"})

        with (
            patch.object(service.hub, "reconcile", wraps=service.hub.reconcile) as reconcile,
            patch.object(service.hub, "flush_pending", wraps=service.hub.flush_pending) as flush,
        ):
            await EventsApp(inner, service, enabled=False)({"type": "lifespan"}, AsyncMock(), AsyncMock())
        reconcile.assert_called_once()
        flush.assert_called_once()
        service.post.assert_not_called()
        service.deliver.assert_not_called()
        service.poll_requests.assert_not_called()

    def serve(*args, **kwargs):
        asyncio.run(scenario(captured[0]))

    with patch.object(m, "build_app", side_effect=build), patch("uvicorn.run", side_effect=serve):
        m.run_listener(cfg, tools, runtime)
    with patch.object(w, "probe_route", return_value=[]), patch.object(w, "send") as send:
        result = w.run_watch_once(target, now=now + timedelta(hours=4), pack_id="duet")
    assert {alert["kind"] for alert in result["alerts"]} >= {"stale_inbox", "duet_idle"}
    send.assert_not_called()


@requires_local_state
def test_duet_watch_runs_only_its_checks(authenticated_duet, capsys):
    target, runtime, _ = authenticated_duet
    duet_hub = message_scope(*message_settings(str(runtime.hub.message_token_file))[:2])
    with BridgeState(runtime.paths.state_dir) as state:
        state.log_call("fleet_digest", True, 5, clock=lambda: NOW)
        with state.transaction() as c:
            c.execute(
                "INSERT INTO hub_inbox(scope,id,sender,body,created_at,synced_at,subject) VALUES (?,?,?,?,?,?,?)",
                (duet_hub, 1, "agent:miller", "SECRET-BODY", NOW.isoformat(), NOW.isoformat(), "question"),
            )
            c.execute(
                "INSERT INTO wake_attempts(timestamp,priority,source) VALUES (?, 'normal', ?)",
                ((NOW + timedelta(minutes=1)).timestamp(), "hub-inbox:" + duet_hub + ":1"),
            )
    with patch.object(w, "send", return_value=0) as send, patch.object(w, "probe_route", return_value=["edge"]):
        result = w.run_watch_once(target, now=NOW + timedelta(hours=4), pack_id="duet")
    kinds = {alert["kind"] for alert in result["alerts"]}
    assert kinds == {"stale_inbox", "duet_idle", "route_down", "hub_sync_stale"}
    assert all(alert["title"].startswith("Duet bridge:") for alert in result["alerts"])
    # The fixture runtime sets no notify_command, so alerts are recorded but nothing is sent.
    assert {alert["result"] for alert in result["alerts"]} == {"skipped"}
    send.assert_not_called()
    assert "SECRET" not in json.dumps(result)


def test_watch_send_payload_names_the_profile():
    captured = []

    def run(argv, **kwargs):
        captured.append(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=0)

    alert = {"title": "Duet bridge: stale inbox", "text": "text"}
    with patch.object(w.subprocess, "run", side_effect=run):
        assert w.send(("/usr/bin/notifier",), alert, profile=DUET) == 0
        assert w.send(("/usr/bin/notifier",), alert) == 0
    assert (captured[0]["source"], captured[0]["tags"]) == ("duet-inbox-watch", ["duet", "duet-bridge"])
    assert (captured[1]["source"], captured[1]["tags"]) == ("dot-inbox-watch", ["dot", "dot-bridge"])


def test_registry_lists_duet_pack():
    shown = grokbot_packs.show_pack("duet")
    assert shown["kind"] == "connector"
    assert set(shown["tools"]) == ISSUE_TOOLS
    assert shown["default_bind"] == "127.0.0.1:8780"
    assert shown["public_route"] == ""


@pytest.fixture
def duet_runtime_file(dot_private_dir):
    runtime = dot_private_dir.parent / "duet-runtime.json"
    _write_private(runtime, json.dumps(_duet_document()).encode())
    return runtime


@requires_local_state
def test_duet_setup_takes_a_duet_runtime_and_no_bearer(tmp_path, duet_runtime_file, dot_private_dir):
    grokbot_packs.apply_setup(tmp_path, "duet", runtime_path=duet_runtime_file)
    stored = grokbot_packs._load_instance_config(tmp_path, "duet")
    assert stored["runtime_path"] == str(duet_runtime_file)
    assert "bearer" not in stored
    with pytest.raises(grokbot_packs.PackError, match="unexpected-key"):
        grokbot_packs.preview_setup(tmp_path, "duet", runtime_path=duet_runtime_file, bearer_env="TEST_BEARER")
    dot_runtime = dot_private_dir.parent / "dot-runtime.json"
    _write_private(dot_runtime, json.dumps(_document()).encode())
    with pytest.raises(grokbot_packs.PackError, match="unsafe-path"):
        grokbot_packs.preview_setup(tmp_path / "other", "duet", runtime_path=dot_runtime)
    with pytest.raises(grokbot_packs.PackError, match="unsafe-path"):
        grokbot_packs.preview_setup(tmp_path / "other", "dot", runtime_path=duet_runtime_file)


def test_lifecycle_delegates_duet_with_its_pack_id(tmp_path, monkeypatch):
    from brigade import dot

    calls = []
    monkeypatch.setattr(dot, "doctor", lambda target, **kw: calls.append(("doctor", kw)) or [])
    monkeypatch.setattr(dot, "canary", lambda target, **kw: calls.append(("canary", kw)) or {"ok": True})
    monkeypatch.setattr(dot, "render_unit", lambda target, **kw: calls.append(("render", kw)) or "unit")
    grokbot_packs.doctor(tmp_path, "duet")
    grokbot_packs.canary(tmp_path, "duet")
    grokbot_packs.render_install_service(tmp_path, "duet")
    assert [(name, kw.get("pack_id")) for name, kw in calls] == [
        ("doctor", "duet"),
        ("canary", "duet"),
        ("render", "duet"),
    ]


def test_cli_serve_duet_passes_its_pack_id(tmp_path, monkeypatch):
    from brigade import cli, dot

    calls = []

    def build(target, **kwargs):
        calls.append(kwargs)
        return object(), object(), object()

    monkeypatch.setattr(dot, "build_listener_from_target", build)
    monkeypatch.setattr(dot, "run_listener", lambda *args: None)
    assert cli.main(["run", "cloud", "grokbot", "serve", "--pack", "duet", "--target", str(tmp_path)]) == 0
    assert calls == [{"bind": None, "allowed_hosts": None, "allowed_origins": None, "pack_id": "duet"}]
    argv = ["run", "cloud", "grokbot", "serve", "--pack", "duet", "--target", str(tmp_path), "--bearer-env", "X"]
    assert cli.main(argv) == 2


@requires_local_state
@pytest.mark.parametrize("change", ["other", "rotation", "endpoint"])
@pytest.mark.parametrize(
    "operation", ["send", "reply", "status", "sync", "reconcile", "flush", "supplied", "supplied-node"]
)
def test_duet_startup_scope_stays_bound(authenticated_duet, monkeypatch, change, operation):
    from brigade.fleet_client_message_credentials import MessageAuthority

    target, runtime, db = authenticated_duet
    _, tools, _ = m.build_listener_from_target(target, pack_id="duet")
    hub = tools.inbox.hub
    try:
        hub.require_identity()
        if change == "endpoint":
            settings = fleet_client.load_fleet_settings()
            monkeypatch.setattr(
                fleet_client, "load_fleet_settings", lambda: dict(settings, hub_url="https://changed.invalid")
            )
        else:
            with fleet_hub.init_db(db) as connection:
                if change == "rotation":
                    fleet_hub_messages.revoke_principal(
                        connection,
                        {
                            "action": "revoke",
                            "name": "duet",
                            "expected_enrollment_id": fleet_hub_messages.get_principal(
                                connection, {"action": "get", "name": "duet"}
                            )["enrollment_id"],
                        },
                    )
                principal = fleet_hub_messages.add_principal(
                    connection,
                    {
                        "name": "duet" if change == "rotation" else "other",
                        "label": "Fixture",
                        "expected_enrollment_id": fleet_hub_messages.get_principal(
                            connection, {"action": "get", "name": "duet"}
                        )["enrollment_id"]
                        if change == "rotation"
                        else None,
                    },
                )
            runtime.hub.message_token_file.write_text(principal["token"])
        replacement = (
            MessageAuthority.capture(node_only=True)
            if operation == "supplied-node"
            else MessageAuthority.capture(str(runtime.hub.message_token_file))
        )
        actions = {
            "send": lambda: hub.send("operator", "subject", "body"),
            "reply": lambda: hub.reply(1, "body"),
            "status": lambda: hub.status([1]),
            "sync": hub.sync,
            "reconcile": hub.reconcile,
            "flush": hub.flush_pending,
            "supplied": lambda: hub.sync(authority=replacement),
            "supplied-node": lambda: hub.sync(authority=replacement),
        }
        with patch.object(fleet_client_messages, "_request", wraps=fleet_client_messages._request) as transport:
            with pytest.raises(DotError, match="^hub_credential_changed$"):
                actions[operation]()
            transport.assert_not_called()
        with tools.state.transaction() as connection:
            assert connection.execute("SELECT count(*) FROM hub_inbox").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM hub_state").fetchone()[0] == 0
        if change == "rotation":
            # Only a new listener's startup can adopt a legitimately rotated token.
            _, restarted, _ = m.build_listener_from_target(target, pack_id="duet")
            try:
                restarted.inbox.hub.require_identity()
                restarted.inbox.hub.sync()
            finally:
                restarted.state.close()
    finally:
        tools.state.close()


@requires_local_state
def test_duet_maintenance_leaves_shared_node_queue_alone(authenticated_duet, monkeypatch, tmp_path):
    from brigade.fleet_client_message_credentials import MessageAuthority

    _, runtime, _ = authenticated_duet
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path / "client")
    tools, state, hub = build_tools(replace(runtime, hub=replace(runtime.hub, recipients={"operator": NODE_A})), DUET)
    try:
        hub.require_identity()
        node = MessageAuthority.capture(node_only=True)
        agent = hub.capture()
        with patch.object(fleet_client_messages, "_request", side_effect=OSError("fixture offline")):
            assert fleet_client_messages.send(
                recipient="agent:duet", subject="node backlog", body="body", reference=None, priority="normal"
            )["queued"]
            assert hub.send("operator", "duet backlog", "body")["queued"]
        assert fleet_client_messages.has_pending(node) and fleet_client_messages.has_pending(agent)
        seen = []
        real_request = fleet_client_messages._request

        def request(base, token, body, **kwargs):
            seen.append((token == node.token, body["action"]))
            return real_request(base, token, body, **kwargs)

        node_path = fleet_client_messages._spool_path(node.node_id, node.token, hub_url=node.hub)
        real_stat = Path.stat

        def stat_path(path, *args, **kwargs):
            assert path != node_path, "Duet maintenance inspected shared node spool metadata"
            return real_stat(path, *args, **kwargs)

        with (
            patch.object(hub, "capture", wraps=hub.capture) as capture,
            patch.object(fleet_client_messages, "_request", side_effect=request),
            patch.object(Path, "stat", stat_path),
        ):
            hub.reconcile()
            hub.flush_pending()
        assert all(call.kwargs.get("agent", True) for call in capture.call_args_list)
        assert not any(is_node for is_node, _ in seen)
        assert fleet_client_messages.has_pending(node)
        assert not fleet_client_messages.has_pending(agent)
        # Dot retains the shared node flush behavior.
        dot = HubInbox(state, runtime.hub)
        dot.flush_pending()
        assert not fleet_client_messages.has_pending(node)
    finally:
        state.close()


@requires_local_state
def test_duet_disabled_restart_holds_queued_send_and_reply(authenticated_duet, monkeypatch, tmp_path):
    _, runtime, _ = authenticated_duet
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path / "client")
    runtime = replace(runtime, messaging_enabled=True, hub=replace(runtime.hub, recipients={"operator": NODE_A}))
    original = fleet_client_messages.send(
        recipient="agent:duet", subject="question", body="question", reference=None, priority="normal"
    )["message"]
    tools, state, hub = build_tools(runtime, DUET)
    try:
        hub.require_identity()
        with patch.object(fleet_client_messages, "_request", side_effect=OSError("fixture offline")):
            assert hub.send("operator", "queued send", "send body")["queued"]
            assert hub.reply(original["id"], "reply body")["queued"]
    finally:
        state.close()
    disabled = replace(runtime, messaging_enabled=False)
    tools, state, hub = build_tools(disabled, DUET)
    try:
        hub.require_identity()
        service = EventService(
            state, fleet_status=lambda rid: {}, hub=hub, hub_poll_seconds=30, callback_hosts=frozenset()
        )
        service._hub_polls = 19
        asyncio.run(service.poll_hub())
        asyncio.run(service.poll_hub())
        assert fleet_client_messages.has_pending(hub.capture())
        assert hub.list()["unacked_total"] == 1
        assert hub.ack([original["id"]])["acked"] == [original["id"]]
        for name, args in (
            ("message_send", dict(to="operator", subject="new", body="body")),
            ("inbox_reply", dict(msg_id=original["id"], body="new")),
        ):
            with pytest.raises(DotError, match="^messaging_disabled$"):
                tools.call("fixture-owner", name, args)
        assert fleet_client_messages.list_messages()["messages"] == []
        assert fleet_client_messages.has_pending(hub.capture())
    finally:
        state.close()
    tools, state, hub = build_tools(runtime, DUET)
    try:
        hub.require_identity()
        hub.flush_pending()
        assert not fleet_client_messages.has_pending(hub.capture())
        delivered = fleet_client_messages.list_messages()["messages"]
        assert {row["body"] for row in delivered} == {"send body", "reply body"}
    finally:
        state.close()


@requires_local_state
def test_duet_watch_alerts_on_real_poll_outage_with_empty_mirror(authenticated_duet):
    target, runtime, _ = authenticated_duet
    _, tools, _ = m.build_listener_from_target(target, pack_id="duet")
    hub = tools.inbox.hub
    hub.clock = lambda: NOW
    try:
        hub.require_identity()
        service = EventService(
            tools.state, fleet_status=lambda rid: {}, hub=hub, hub_poll_seconds=30, callback_hosts=frozenset()
        )
        asyncio.run(service.poll_hub())
        assert hub.list()["unacked_total"] == 0
        real_request = fleet_client_messages._request

        def outage(base, token, body, **kwargs):
            if body["action"] == "list":
                return 503, {"code": "hub_unavailable"}
            return real_request(base, token, body, **kwargs)

        with (
            patch.object(fleet_client_messages, "_request", side_effect=outage),
            patch.object(w, "_probe", return_value=(200, {"resource": runtime.auth.resource})),
        ):
            asyncio.run(service.poll_hub())
            result = w.run_watch_once(target, now=NOW + timedelta(minutes=16), dry_run=True, pack_id="duet")
        assert [alert["kind"] for alert in result["alerts"]] == ["hub_sync_stale"]
    finally:
        tools.state.close()


@requires_local_state
@pytest.mark.parametrize("minutes", [1, 15, 16])
def test_duet_watch_reports_identity_outage_after_fresh_success(authenticated_duet, minutes):
    target, runtime, _ = authenticated_duet
    _, tools, _ = m.build_listener_from_target(target, pack_id="duet")
    hub = tools.inbox.hub
    hub.clock = lambda: NOW
    try:
        hub.require_identity()
        service = EventService(
            tools.state, fleet_status=lambda rid: {}, hub=hub, hub_poll_seconds=30, callback_hosts=frozenset()
        )
        asyncio.run(service.poll_hub())
        assert hub.list()["unacked_total"] == 0
        real_request = fleet_client_messages._request

        def outage(base, token, body, **kwargs):
            if body["action"] == "identity":
                raise OSError("fixture private transport detail")
            return real_request(base, token, body, **kwargs)

        with (
            patch.object(fleet_client_messages, "_request", side_effect=outage),
            patch.object(w, "_probe", return_value=(200, {"resource": runtime.auth.resource})),
        ):
            result = w.run_watch_once(target, now=NOW + timedelta(minutes=minutes), dry_run=True, pack_id="duet")
        assert result["ok"] is False
        assert result["error"] == "hub_unavailable"
        assert "fixture private transport detail" not in json.dumps(result)
        assert result["unacked"] == 0
        assert result["open_requests"] == 0
        assert [alert["kind"] for alert in result["alerts"]] == (["hub_sync_stale"] if minutes > 15 else [])
    finally:
        tools.state.close()


@requires_local_state
def test_duet_changed_scope_poll_failure_is_visible_and_local(authenticated_duet, caplog):
    target, runtime, db = authenticated_duet
    _, tools, _ = m.build_listener_from_target(target, pack_id="duet")
    hub = tools.inbox.hub
    try:
        hub.require_identity()
        service = EventService(
            tools.state, fleet_status=lambda rid: {}, hub=hub, hub_poll_seconds=30, callback_hosts=frozenset()
        )
        asyncio.run(service.poll_hub())
        with fleet_hub.init_db(db) as connection:
            other = fleet_hub_messages.add_principal(connection, {"name": "other", "label": "Fixture"})
        runtime.hub.message_token_file.write_text(other["token"])
        replacement_scope = message_scope(*message_settings(str(runtime.hub.message_token_file))[:2])
        with patch.object(fleet_client_messages, "_request", wraps=fleet_client_messages._request) as transport:
            asyncio.run(service.poll_hub())
        transport.assert_not_called()
        assert "hub_credential_changed" in caplog.text
        with tools.state.transaction() as connection:
            errors = connection.execute(
                "SELECT key,value FROM hub_state WHERE value='hub_credential_changed'"
            ).fetchall()
            assert errors
            assert all(not row["key"].startswith(replacement_scope + ":") for row in errors)
    finally:
        tools.state.close()


@requires_local_state
def test_duet_shutdown_waits_for_inflight_poll(authenticated_duet):
    import threading
    from brigade.dot.event_app import EventsApp

    target, _, _ = authenticated_duet
    _, tools, _ = m.build_listener_from_target(target, pack_id="duet")
    hub = tools.inbox.hub
    hub.require_identity()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_request = fleet_client_messages._request
    calls = []

    def blocked(base, token, body, **kwargs):
        calls.append(body["action"])
        if body["action"] == "list":
            started.set()
            assert release.wait(3)
        try:
            return real_request(base, token, body, **kwargs)
        finally:
            finished.set()

    async def scenario():
        service = EventService(
            tools.state, fleet_status=lambda rid: {}, hub=hub, hub_poll_seconds=30, callback_hosts=frozenset()
        )

        async def inner(scope, receive, send):
            await send({"type": "lifespan.startup.complete"})
            assert await asyncio.to_thread(started.wait, 2)
            shutdown = asyncio.create_task(send({"type": "lifespan.shutdown.complete"}))
            try:
                # A ready callback gives cancellation a turn without relying on a long sleep.
                await asyncio.sleep(0.01)
                assert not shutdown.done(), "shutdown returned while Hub work still owned the state"
            finally:
                release.set()
                await shutdown
            assert finished.is_set()
            assert calls == ["list"]  # No flush or later maintenance after cancellation.
            tools.state.close()

        await EventsApp(inner, service, enabled=False)({"type": "lifespan"}, AsyncMock(), AsyncMock())

    try:
        with patch.object(fleet_client_messages, "_request", side_effect=blocked):
            asyncio.run(scenario())
    finally:
        release.set()
        tools.state.close()


@requires_local_state
@pytest.mark.parametrize(
    "directory,metadata_id,denied",
    [("other", "private-notes", True), ("private-notes", "other", True), ("allowed", "allowed-id", False)],
)
@pytest.mark.parametrize("resource", ["SKILL.md", "references/detail.md"])
def test_duet_skill_read_denies_registry_identity(duet_configured, directory, metadata_id, denied, resource):
    _, runtime, _ = duet_configured
    entry = runtime.paths.workspace / ".brigade" / "skills" / "registry" / directory
    entry.mkdir(parents=True)
    (entry / "skill.json").write_text(json.dumps({"id": metadata_id, "title": "private metadata"}))
    path = entry / resource
    path.parent.mkdir(exist_ok=True)
    path.write_text("fixture skill content")
    memory = MemoryTools(runtime, commands=SimpleNamespace(), profile=DUET)
    result = memory.skill_get(directory, resource)
    if denied:
        assert result == {"error": "skill_not_found"}
    else:
        assert result["content"] == "fixture skill content"
