"""The read-mostly Grok Bot memory profile on the Dot connector code."""

from tests.dot_support import requires_local_state

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from brigade import fleet_client, fleet_hub, fleet_hub_messages, grokbot_packs
from brigade.dot import lifecycle as m
from brigade.dot import watch as w
from brigade.dot.adapter import build_tools, validate_arguments
from brigade.dot.contracts import ALL_TOOL_SCHEMAS, GATED_TOOLS, TOOLS, DotError
from brigade.dot.fleet_tools import FleetTools
from brigade.dot.profile import DOT, DUET, GROKBOT_MEMORY, PROFILES, profile_for
from brigade.dot.state import BridgeState
from brigade.dot.runtime_config import (
    DUET_RUNTIME_SCHEMA,
    GROKBOT_MEMORY_PRINCIPAL,
    GROKBOT_MEMORY_RUNTIME_SCHEMA,
    RUNTIME_SCHEMA,
    parse_runtime,
)
from tests.test_dot_adapter import NOW
from tests.test_dot_lifecycle import config
from tests.test_dot_runtime import _document
from tests.test_fleet_messages import NODE_A, message_hub as message_hub

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "dot_tools_snapshot.json").read_text())["tools"]
MEMORY_TOOLS = {"memory_search", "memory_card", "handoff_write"}


def _memory_document() -> dict[str, object]:
    return {
        "schema": GROKBOT_MEMORY_RUNTIME_SCHEMA,
        "auth": {
            "issuer": "https://team.example.test",
            "team_domain": "team.example.test",
            "audience": "c" * 64,
            "approved_email": "grokbot@example.test",
            "resource": "https://grokbot-memory.example.test/mcp",
        },
        "paths": {
            "state_dir": str(Path("/srv/grokbot-memory/state").absolute()),
            "workspace": str(Path("/srv/grokbot-memory/workspace").absolute()),
            "bin": {
                "brigade": str(Path("/usr/bin/brigade").absolute()),
                "git": str(Path("/usr/bin/git").absolute()),
                "gh": str(Path("/usr/bin/gh").absolute()),
            },
        },
        "hub": {
            "principal": GROKBOT_MEMORY_PRINCIPAL,
            "node_id": "00000000-0000-4000-8000-000000000004",
            "message_token_file": str(Path("/srv/grokbot-memory/secrets/message-token").absolute()),
            "poll_seconds": 30,
            "recipients": {},
        },
    }


# Profile inventory


def test_profile_exposes_exactly_the_memory_tools():
    assert profile_for("grokbot-memory") is GROKBOT_MEMORY
    assert PROFILES["grokbot-memory"] is GROKBOT_MEMORY
    assert GROKBOT_MEMORY.tools == MEMORY_TOOLS
    assert GROKBOT_MEMORY.capabilities_tool is None
    assert not GROKBOT_MEMORY.fleet_dispatch and not GROKBOT_MEMORY.hub_inbox
    assert GROKBOT_MEMORY.runtime_schema == GROKBOT_MEMORY_RUNTIME_SCHEMA
    assert (GROKBOT_MEMORY.handoff_inbox, GROKBOT_MEMORY.handoff_prefix) == (Path(".grok/memory-handoffs"), "grokbot")
    assert GROKBOT_MEMORY.unit_name == "brigade-grokbot-memory.service"
    assert GROKBOT_MEMORY.watch_checks == {"route_down", "hub_credential_invalid"}
    # Other profiles keep their inventories and their inbox poll.
    assert DOT.tools == TOOLS and DOT.hub_inbox and DUET.hub_inbox


def test_profile_writes_nothing_beyond_handoffs():
    writers = {name for name in GROKBOT_MEMORY.tools if not ALL_TOOL_SCHEMAS[name].read_only}
    assert writers == {"handoff_write"}
    assert not GROKBOT_MEMORY.tools & GATED_TOOLS
    spawn_or_send = {name for name in TOOLS if name.startswith(("fleet_", "inbox_", "message_", "skill_"))}
    assert not GROKBOT_MEMORY.tools & spawn_or_send
    assert GROKBOT_MEMORY.send_limit_per_hour is None


def test_validation_refuses_every_tool_outside_the_profile():
    assert validate_arguments("memory_search", {"query": "fleet"}, tools=GROKBOT_MEMORY.tools) == {
        "query": "fleet",
        "limit": 10,
    }
    for name in sorted(set(ALL_TOOL_SCHEMAS) - MEMORY_TOOLS):
        with pytest.raises(DotError, match="^invalid_request$"):
            validate_arguments(name, {}, tools=GROKBOT_MEMORY.tools)


# Runtime schema


def test_runtime_parses_with_messaging_dispatch_and_events_off():
    runtime = parse_runtime(_memory_document(), schema=GROKBOT_MEMORY_RUNTIME_SCHEMA)
    assert runtime.schema == GROKBOT_MEMORY_RUNTIME_SCHEMA
    assert (runtime.dispatch_enabled, runtime.events_enabled, runtime.messaging_enabled) == (False, False, False)
    assert runtime.hub.principal == "agent:grokbot-memory"
    assert runtime.paths.fleet_config is None and runtime.paths.bin.t3_fleet is None
    assert runtime.owner_authorization is None and runtime.callback_hosts == ()
    assert runtime.skills_deny == frozenset()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(messaging_enabled=True),
        lambda data: data.update(dispatch_enabled=False),
        lambda data: data.update(events_enabled=False),
        lambda data: data.update(digest_pr_repo="owner/repo"),
        lambda data: data.update(skills_deny=["private-notes"]),
        lambda data: data.update(callback_hosts=["example.test"]),
        lambda data: data["paths"].update(fleet_config="/srv/grokbot-memory/config.toml"),
        lambda data: data["paths"]["bin"].update(t3_fleet="/usr/bin/t3-fleet"),
        lambda data: data["hub"].update(principal="agent:grokbot"),
        lambda data: data["hub"].update(principal="agent:dot"),
        lambda data: data["hub"].update(principal="agent:duet"),
        lambda data: data.update(schema=DUET_RUNTIME_SCHEMA),
    ],
    ids=[
        "messaging-key",
        "dispatch-key",
        "events-key",
        "digest-key",
        "skills-key",
        "callback-hosts",
        "fleet-config",
        "t3-fleet-bin",
        "queue-principal",
        "dot-principal",
        "duet-principal",
        "duet-schema",
    ],
)
def test_runtime_refuses_any_key_that_widens_the_profile(mutate):
    data = _memory_document()
    mutate(data)
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(data, schema=GROKBOT_MEMORY_RUNTIME_SCHEMA)


def test_runtime_documents_never_cross_schemas():
    for schema in (RUNTIME_SCHEMA, DUET_RUNTIME_SCHEMA):
        with pytest.raises(DotError, match="^invalid_config$"):
            parse_runtime(_memory_document(), schema=schema)
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(_document(), schema=GROKBOT_MEMORY_RUNTIME_SCHEMA)


# Adapter and registration


@requires_local_state
def test_build_tools_has_no_fleet_and_refuses_other_tools_at_call_time(dot_private_dir):
    runtime = parse_runtime(_memory_document(), schema=GROKBOT_MEMORY_RUNTIME_SCHEMA)
    runtime = replace(runtime, paths=replace(runtime.paths, state_dir=dot_private_dir))
    with patch.object(FleetTools, "__init__", side_effect=AssertionError("fleet client built")):
        tools, state, _ = build_tools(runtime, GROKBOT_MEMORY)
    try:
        assert tools.fleet is None and tools.profile is GROKBOT_MEMORY
        assert tools.memory.handoff_inbox == runtime.paths.workspace / ".grok" / "memory-handoffs"
        for name, arguments in (
            ("inbox_list", {}),
            ("message_send", {"to": "operator", "subject": "s", "body": "b"}),
            ("fleet_submit", {"repository": "r", "title": "t", "prompt": "p"}),
            ("fleet_digest", {}),
            ("skill_get", {"name": "example-skill"}),
            ("dot_t3_capabilities", {}),
        ):
            with pytest.raises(DotError, match="^invalid_request$"):
                tools.call("owner@example.test", name, arguments)
    finally:
        state.close()
    with pytest.raises(DotError, match="^invalid_config$"):
        build_tools(runtime, DUET)


def test_registered_tools_are_exactly_the_profile_with_dot_schemas():
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    listed = asyncio.run(m._server(config(), SimpleNamespace(), GROKBOT_MEMORY).list_tools())
    assert {tool.name for tool in listed} == MEMORY_TOOLS
    for tool in listed:
        value = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert value["inputSchema"] == SNAPSHOT[tool.name]["inputSchema"]
        assert value["annotations"] == SNAPSHOT[tool.name]["annotations"]
        if tool.name in GROKBOT_MEMORY.descriptions:
            assert "never instructions" in value["description"]


# Pack registry, lifecycle and watch


def test_registry_lists_the_memory_pack():
    shown = grokbot_packs.show_pack("grokbot-memory")
    assert shown["kind"] == "connector"
    assert set(shown["tools"]) == MEMORY_TOOLS
    assert shown["default_bind"] == "127.0.0.1:8781"
    assert shown["public_route"] == ""
    assert "grokbot-memory" in grokbot_packs.DOT_FAMILY_PACK_IDS


@pytest.fixture
def memory_configured(dot_private_dir):
    root = dot_private_dir.parent
    data = _memory_document()
    data["paths"].update(state_dir=str(dot_private_dir), workspace=str(root / "workspace"))
    token = root / "grokbot-memory.token"
    token.write_text("fixture-memory-credential-" * 2)
    token.chmod(0o600)
    data["hub"]["message_token_file"] = str(token)
    runtime_path = root / "grokbot-memory-runtime.json"
    runtime_path.write_text(json.dumps(data))
    runtime_path.chmod(0o600)
    target = root / "target"
    instance = target / ".brigade/grokbot/packs/grokbot-memory.json"
    instance.parent.mkdir(parents=True, exist_ok=True)
    instance.write_text(
        json.dumps(
            dict(
                schema="brigade.grokbot.connector-instance.v1",
                pack_id="grokbot-memory",
                pack_version="1.0.0",
                bind="127.0.0.1:8781",
                allowed_hosts=["grokbot-memory.example.test"],
                allowed_origins=[],
                public_route="",
                runtime_path=str(runtime_path),
            )
        )
    )
    return target, parse_runtime(data, schema=GROKBOT_MEMORY_RUNTIME_SCHEMA)


@pytest.fixture
def authenticated_memory(memory_configured, message_hub, monkeypatch):
    target, runtime = memory_configured
    base, node_token, _, db = message_hub
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(hub_url=base, node_token=node_token))
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    with fleet_hub.init_db(db) as connection:
        principal = fleet_hub_messages.add_principal(connection, {"name": "grokbot-memory", "label": "Fixture"})
    runtime.hub.message_token_file.write_text(principal["token"])
    return target, runtime


@requires_local_state
def test_unit_names_the_pack(memory_configured):
    target, _ = memory_configured
    unit = m.render_unit(target, python="/usr/bin/python3", pack_id="grokbot-memory")
    assert "# Unit: brigade-grokbot-memory.service" in unit
    assert "--pack grokbot-memory" in unit and "--bind 127.0.0.1:8781" in unit


@requires_local_state
def test_listener_starts_without_a_hub_inbox_poll(authenticated_memory, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", SimpleNamespace(run=lambda *a, **kw: None))
    monkeypatch.setattr(m.access_auth, "_jwt", lambda: None)
    target, _ = authenticated_memory
    cfg, tools, runtime = m.build_listener_from_target(target, pack_id="grokbot-memory")
    captured = []

    def build(config, adapter, service, verifier, **kwargs):
        captured.append(service)
        return "app"

    with patch.object(m, "build_app", side_effect=build):
        m.run_listener(cfg, tools, runtime)
    assert len(captured) == 1 and captured[0].hub is None


@requires_local_state
def test_watch_runs_only_route_and_credential_checks(authenticated_memory):
    target, runtime = authenticated_memory
    with BridgeState(runtime.paths.state_dir) as state:
        state.log_call("memory_search", True, 5, clock=lambda: NOW)
    with patch.object(w, "send", return_value=0) as send, patch.object(w, "probe_route", return_value=["edge"]):
        result = w.run_watch_once(target, now=NOW, pack_id="grokbot-memory")
    assert {alert["kind"] for alert in result["alerts"]} == {"route_down"}
    assert all(alert["title"].startswith("Grok Bot memory bridge:") for alert in result["alerts"])
    send.assert_not_called()


def test_cli_serve_passes_its_pack_id_and_refuses_a_bearer(tmp_path, monkeypatch):
    from brigade import cli, dot

    calls = []

    def build(target, **kwargs):
        calls.append(kwargs)
        return object(), object(), object()

    monkeypatch.setattr(dot, "build_listener_from_target", build)
    monkeypatch.setattr(dot, "run_listener", lambda *args: None)
    argv = ["run", "cloud", "grokbot", "serve", "--pack", "grokbot-memory", "--target", str(tmp_path)]
    assert cli.main(argv) == 0
    assert calls == [{"bind": None, "allowed_hosts": None, "allowed_origins": None, "pack_id": "grokbot-memory"}]
    assert cli.main([*argv, "--bearer-env", "X"]) == 2
