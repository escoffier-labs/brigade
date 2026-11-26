"""Keep the fleet capability tables tied to the connector, CLI, and hook contracts."""

from itertools import product
from pathlib import Path

from brigade.cli import _build_parser_for
from brigade.dot.cloud_runtime import CLOUD_AGENT_PRINCIPAL
from brigade.dot.contracts import ALL_TOOL_SCHEMAS, ToolClass
from brigade.dot.profile import PROFILES, runtime_profile
from brigade.dot.runtime_config import DUET_PRINCIPAL
from brigade.fleet_agent_capabilities import AGENT_CAPABILITIES, render_markdown
from brigade.fleet_inbox_delivery import PROMPT_EVENTS, TOOL_EVENTS
from brigade.fleet_mcp import tool_specs


def test_connector_rows_cover_all_profiles():
    profile_names = [row.connector_profile for row in AGENT_CAPABILITIES if row.connector_profile is not None]
    assert sorted(profile_names) == sorted(PROFILES)
    assert len({row.kind for row in AGENT_CAPABILITIES}) == len(AGENT_CAPABILITIES)


def test_connector_messaging_tools_match_profiles():
    for row in AGENT_CAPABILITIES:
        if row.connector_profile is None:
            continue
        profile = PROFILES[row.connector_profile]
        full_inventory = {
            name
            for dispatch, messaging in product((False, True), repeat=2)
            for name in runtime_profile(profile, dispatch, messaging_enabled=messaging).tools
        }
        messaging_tools = {
            name
            for name in full_inventory
            if name.startswith(("inbox_", "message_")) or ALL_TOOL_SCHEMAS[name].tool_class == ToolClass.MESSAGING
        }
        documented_tools = {row.send, row.read, row.ack, row.reply, row.status} - {None}
        assert documented_tools == messaging_tools, row.kind
        assert row.live_delivery is None, row.kind


def test_connector_memory_and_work_roles_match_profiles():
    for row in AGENT_CAPABILITIES:
        if row.connector_profile is None:
            continue
        profile = runtime_profile(PROFILES[row.connector_profile], dispatch_enabled=True, messaging_enabled=True)
        assert row.memory_read == tuple(tool for tool in ("memory_search", "memory_card") if tool in profile.tools)
        assert row.handoff_write == ("handoff_write" if "handoff_write" in profile.tools else None)
        assert row.work_view == tuple(
            tool for tool in ("fleet_work_list", "fleet_sessions", "fleet_overview") if tool in profile.tools
        )


def test_memory_principals_match_agent_kinds():
    assert {row.kind: row.memory_principal for row in AGENT_CAPABILITIES} == {
        "local-claude-code": "node",
        "local-codex": "node",
        "dot": "host [memory_hub] read credential",
        "duet": "host [memory_hub] read credential",
        "cloud-agent": CLOUD_AGENT_PRINCIPAL,
    }


def test_cloud_agent_records_deployed_memory_and_work_limits():
    cloud = next(row for row in AGENT_CAPABILITIES if row.kind == "cloud-agent")
    assert cloud.memory_status.startswith("not yet")
    assert cloud.work_status.startswith("not yet")
    assert "The deployed `tool_grants` deny `handoff_write`." in cloud.limits


def test_local_memory_and_work_roles_use_registered_cli_paths():
    for row in AGENT_CAPABILITIES:
        if row.connector_profile is not None:
            continue
        assert row.memory_read == ("brigade memory search",)
        assert row.handoff_write == "brigade handoff draft"
        assert row.work_view == ("brigade fleet work list", "brigade fleet sessions", "brigade fleet claims")
        for command, args in (
            (row.memory_read[0], ["example"]),
            (row.handoff_write, ["--title", "Example", "--summary", "Example"]),
            *((command, []) for command in row.work_view),
        ):
            argv = [*command.split()[1:], *args]
            _build_parser_for(argv).parse_args(argv)


def test_local_rows_match_fleet_station_and_delivery_hooks():
    local_rows = [row for row in AGENT_CAPABILITIES if row.connector_profile is None]
    assert {row.kind for row in local_rows} == {"local-claude-code", "local-codex"}
    station_tools = {tool["name"] for tool in tool_specs()}
    for row in local_rows:
        roles = (row.send, row.read, row.ack, row.reply, row.status)
        assert roles == ("fleet_send", "fleet_inbox", "fleet_ack", "fleet_reply", "fleet_sent"), row.kind
        assert set(roles) <= station_tools, row.kind
        assert row.live_delivery == PROMPT_EVENTS | TOOL_EVENTS, row.kind
        assert row.wake == "next-event", row.kind


def test_documented_capability_table_matches_renderer():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "fleet-inbox-delivery.md").read_text(encoding="utf-8")
    _, heading, remainder = doc.partition("## Capability table\n")
    section, _, _ = remainder.partition("\n## ")
    assert heading + section == render_markdown()


CONNECTOR_ROLE_TOOLS = {
    "send": "message_send",
    "read": "inbox_list",
    "ack": "inbox_ack",
    "reply": "inbox_reply",
    "status": "message_status",
}


def test_connector_rows_map_each_role_to_its_tool():
    for row in AGENT_CAPABILITIES:
        if row.connector_profile is None:
            continue
        for role, tool in CONNECTOR_ROLE_TOOLS.items():
            assert getattr(row, role) in (None, tool), (row.kind, role)
        assert None not in (row.send, row.read, row.ack, row.reply), row.kind


def test_connector_principals_match_runtime_enforcement():
    principals = {row.kind: row.principal_source for row in AGENT_CAPABILITIES}
    assert principals["duet"] == DUET_PRINCIPAL
    assert principals["cloud-agent"] == CLOUD_AGENT_PRINCIPAL
