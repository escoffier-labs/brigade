"""The duet connector profile on the Dot connector code (#1943)."""

from tests.dot_support import requires_local_state

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from brigade import fleet_client, grokbot_packs
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
    assert DUET.watch_checks == {"stale_inbox", "idle", "route_down", "hub_credential_invalid"}
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
        found = {"skills": [{"id": "private-notes"}, {"id": "brigade-work"}], "count": 2}
        self.runner.handler = lambda argv, stdin: CommandResult(0, json.dumps(found).encode())
        self.assertEqual(self.call("skill_search", query="notes"), {"skills": [{"id": "brigade-work"}], "count": 1})


class DuetMessagingOff(DuetFixture):
    messaging = False

    @requires_local_state
    def test_messaging_gate_is_separate_from_dispatch(self):
        with self.assertRaisesRegex(DotError, "^messaging_disabled$"):
            self.call("message_send", to="operator", subject="subject", body="body")
        self.assertEqual(self.messages.calls, [])
        self.assertFalse(self.call("duet_capabilities")["messaging_enabled"])


class DotMessagingWithoutDispatch(Fixture):
    @requires_local_state
    def test_dot_can_message_with_dispatch_off(self):
        self.runtime = replace(self.runtime, messaging_enabled=True)
        self.adapter = self.make_adapter(False)
        self.assertIn("id", self.call("message_send", to="review", subject="subject", body="body"))
        with self.assertRaisesRegex(DotError, "^dispatch_disabled$"):
            self.call("fleet_submit", repository="brigade", title="t", prompt="p", host="alpha", seat="sol61")

    @requires_local_state
    def test_dot_messaging_off_with_dispatch_on(self):
        self.runtime = replace(self.runtime, messaging_enabled=False)
        self.adapter = self.make_adapter(True)
        with self.assertRaisesRegex(DotError, "^messaging_disabled$"):
            self.call("message_send", to="review", subject="subject", body="body")


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
    assert "-m brigade.dot watch --target" in service and service.rstrip().count("--pack duet") == 1
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
def test_duet_doctor_checks_isolation_instead_of_t3_fleet(duet_configured):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    target, _, _ = duet_configured
    with (
        patch("brigade.fleet_client_message_credentials.read_message_token_file", return_value="fixture"),
        patch.object(m, "_probe", return_value=(401, None)),
    ):
        checks = m.doctor(target, pack_id="duet")
    assert [c["check"] for c in checks] == [
        "dependency",
        "config",
        "state",
        "access-config",
        "hub-token",
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
    dot_data["hub"]["message_token_file"] = str(dot_private_dir.parent / "dot.token")
    dot_runtime.write_text(json.dumps(dot_data))
    assert m._isolated_from_dot(target, runtime) is True


@pytest.fixture
def duet_hub(duet_configured, monkeypatch):
    _, runtime, _ = duet_configured
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: dict(hub_url="https://hub.invalid", node_token="n")
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-a")
    return message_scope(*message_settings(str(runtime.hub.message_token_file))[:2])


@requires_local_state
def test_duet_watch_runs_only_its_checks(duet_configured, duet_hub, capsys):
    target, runtime, _ = duet_configured
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
    assert kinds == {"stale_inbox", "duet_idle", "route_down"}
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
