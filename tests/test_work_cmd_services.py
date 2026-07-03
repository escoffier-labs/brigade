import json
import os
import socket
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from brigade import cli
from brigade import dogfood_cmd
from brigade import localio
from brigade import repos_cmd
from brigade import roadmap_cmd
from brigade import security_cmd
from brigade import tools_cmd
from brigade import work_cmd

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
    _write_script_tool_config,
    _write_runtime_config,
    _write_policy_config,
    _queue_and_approve_runner,
    _checkpoint_script,
    _create_waiting_checkpoint,
    _write_mcp_tool_config,
    _fake_mcp_server_script,
    _queue_and_approve_mcp,
)


def test_tools_init_list_show_search_doctor_and_json(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")

    assert tools_cmd.init(target=tmp_path) == 0
    out = capsys.readouterr().out
    config = tmp_path / ".brigade" / "tools.toml"
    assert f"tools_config: {config}" in out
    assert "tools: 4" in out
    assert ".brigade/tools.toml" in (tmp_path / ".gitignore").read_text()

    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "simplify.md").write_text("Simplify command\n")
    (tmp_path / "tools" / "superpowers.md").write_text("Superpowers\n")
    (tmp_path / "tools" / "frontend.md").write_text("Frontend\n")
    (tmp_path / "tools" / "antislop.md").write_text("Anti-Slop\n")
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "simplify.md").write_text("Simplify command\n")
    (tmp_path / ".claude" / "commands" / "superpowers.md").write_text("Superpowers\n")
    (tmp_path / ".codex" / "skills" / "simplify").mkdir(parents=True)
    (tmp_path / ".codex" / "skills" / "simplify" / "SKILL.md").write_text("Simplify skill\n")
    (tmp_path / ".codex" / "skills" / "superpowers").mkdir(parents=True)
    (tmp_path / ".codex" / "skills" / "superpowers" / "SKILL.md").write_text("Superpowers skill\n")
    (tmp_path / ".opencode" / "superpowers").mkdir(parents=True)
    (tmp_path / ".opencode" / "superpowers" / "superpowers.md").write_text("Superpowers projection\n")
    assert tools_cmd.apply(target=tmp_path, all_tools=True, force=True) == 0

    assert tools_cmd.list_tools(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools:" in out
    assert "- simplify [slash-command]" in out

    assert tools_cmd.list_tools(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["tool_count"] == 4

    assert tools_cmd.show(target=tmp_path, tool_id="simplify") == 0
    out = capsys.readouterr().out
    assert "tool: simplify" in out
    assert "claude: current" in out
    assert "codex: current" in out

    assert tools_cmd.search(target=tmp_path, query="superpower", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["match_count"] == 1
    assert payload["matches"][0]["id"] == "superpowers"

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] tool_config:" in out
    assert "[ok] tool_catalog: no issues" in out


def test_tools_defaults_merges_builtins_and_preserves_custom_tools(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    for name in ("simplify", "superpowers", "frontend", "antislop", "custom"):
        (tools_dir / f"{name}.md").write_text(f"# {name}\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "old"
source_path = "tools/simplify.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/simplify.md" }

[[tool]]
id = "custom"
name = "Custom"
family = "skill"
enabled = true
description = "custom tool"
source_path = "tools/custom.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/custom.md" }
"""
    )

    assert tools_cmd.defaults(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["updated"] == ["simplify"]
    assert set(payload["added"]) == {"superpowers", "frontend", "antislop"}
    assert payload["conflicts"] == []

    assert tools_cmd.list_tools(target=tmp_path, json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {tool["id"] for tool in listed["tools"]} == {"simplify", "superpowers", "frontend", "antislop", "custom"}
    by_id = {tool["id"]: tool for tool in listed["tools"]}
    assert by_id["simplify"]["projection_coverage"]["codex"] == "missing"
    assert by_id["custom"]["projection_coverage"]["claude"] == "missing"

    text = config.read_text()
    assert 'id = "custom"' in text
    assert 'id = "frontend"' in text


def test_tools_defaults_creates_missing_builtin_source_files(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert tools_cmd.defaults(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_created_count"] == 4
    assert (tmp_path / "tools" / "simplify.md").is_file()
    assert (tmp_path / "tools" / "superpowers.md").is_file()
    assert (tmp_path / "tools" / "frontend.md").is_file()
    assert (tmp_path / "tools" / "antislop.md").is_file()

    assert tools_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["issue_count"] == 84
    assert {issue["issue_type"] for issue in doctor["issues"]} == {"missing_projection"}


def test_tools_defaults_reports_conflicting_builtin_id(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / ".brigade").mkdir()
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        """
[[tool]]
id = "frontend"
name = "Other Frontend"
family = "skill"
enabled = true
description = "custom"
source_path = "tools/private-frontend.md"
supported_harnesses = []
projections = {}
"""
    )

    assert tools_cmd.defaults(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["conflicts"][0]["tool_id"] == "frontend"
    assert "private-frontend" in config.read_text()


def test_tools_catalog_covers_portable_families_and_mcp_discovery(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / ".brigade").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "skill.md").write_text("Skill source\n")
    (tmp_path / "tools" / "command.md").write_text("Slash command\n")
    (tmp_path / "tools" / "super.md").write_text("Superpower\n")
    (tmp_path / "tools" / "script.sh").write_text("#!/bin/sh\n")
    (tmp_path / "tools" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "good": {"command": "brigade", "timeout": 10},
                    "bad": {},
                    "risky": {"command": "bash -c echo hi"},
                }
            }
        )
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        """
[[tool]]
id = "memory-skill"
name = "Memory Skill"
family = "skill"
enabled = true
description = "Portable memory skill."
source_path = "tools/skill.md"
supported_harnesses = []

[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "Portable simplify command."
source_path = "tools/command.md"
supported_harnesses = []

[[tool]]
id = "superpowers"
name = "Superpowers"
family = "superpower"
enabled = true
description = "Portable superpower."
source_path = "tools/super.md"
supported_harnesses = []

[[tool]]
id = "script-tool"
name = "Script Tool"
family = "script"
enabled = true
description = "Portable script."
source_path = "tools/script.sh"
command = "brigade status"
supported_harnesses = []

[[tool]]
id = "mcp-local"
name = "MCP Local"
family = "mcp"
enabled = true
description = "Local MCP config."
manifest_path = "tools/mcp.json"
supported_harnesses = []
"""
    )

    assert tools_cmd.list_tools(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {tool["family"] for tool in payload["tools"]} == {
        "skill",
        "slash-command",
        "superpower",
        "script",
        "mcp",
    }

    assert tools_cmd.show(target=tmp_path, tool_id="mcp-local", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"]["mcp"]["server_count"] == 3
    assert payload["tool"]["mcp"]["server_ids"] == ["bad", "good", "risky"]

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_missing_command: MCP server bad is missing command" in out
    assert "[warn] tool_missing_timeout: MCP server bad is missing timeout metadata" in out
    assert "[warn] tool_high_risk_command: MCP server risky command shape is high risk" in out


def test_tools_doctor_reports_parity_stale_schema_command_health_and_unsafe_fields(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / ".brigade").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "source.md").write_text("Tool source\n")
    projection = tmp_path / ".claude" / "commands" / "tool.md"
    projection.parent.mkdir(parents=True)
    projection.write_text("projected\n")
    schema = tmp_path / "tools" / "schema.json"
    schema.write_text("{not json")
    health = tmp_path / "tools" / "health.json"
    health.write_text("{}\n")
    old = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(health, (old, old))
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        """
[[tool]]
id = "portable"
name = "Portable Tool"
family = "script"
enabled = true
description = "Portable script with several repairable issues."
source_path = "tools/source.md"
schema_path = "tools/schema.json"
health_path = "tools/health.json"
command = "missing-command --flag"
auth_label = "local"
password = "do-not-print"
supported_harnesses = ["claude", "codex"]
projections = { claude = ".claude/commands/tool.md" }
"""
    )
    assert tools_cmd.apply(target=tmp_path, tool_id="portable", force=True) == 0
    (tmp_path / "tools" / "source.md").write_text("Tool source changed\n")
    capsys.readouterr()

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_unsafe_auth_fields: unsafe field names: password" in out
    assert "do-not-print" not in out
    assert "[warn] tool_invalid_schema:" in out
    assert "[warn] tool_stale_health:" in out
    assert "[warn] tool_missing_command: command is not resolvable: missing-command --flag" in out
    assert "[warn] tool_stale_projection:" in out
    assert "[warn] tool_parity_gap: missing projection for codex" in out

    assert tools_cmd.doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in payload["issues"]}
    assert {
        "unsafe_auth_fields",
        "invalid_schema",
        "stale_health",
        "missing_command",
        "stale_projection",
        "parity_gap",
    } <= issue_types
    rendered = json.dumps(payload, sort_keys=True)
    assert "do-not-print" not in rendered


def test_work_brief_and_doctor_include_tool_catalog_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    source = tmp_path / "tools" / "portable.md"
    source.parent.mkdir()
    source.write_text("Portable source.\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        """
[[tool]]
id = "portable"
name = "Portable Tool"
family = "skill"
enabled = true
description = "Portable missing source."
source_path = "tools/portable.md"
supported_harnesses = ["codex"]
projections = { codex = ".codex/skills/portable/SKILL.md" }
"""
    )

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_config:" in out
    assert "tool_catalog:" in out
    assert "tool_top_issue: portable/missing_projection" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_missing_projection:" in out
    assert "[ok] tools_config_ignored: yes" in out


def test_work_brief_and_doctor_include_roadmap_and_repo_fleet_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(
        roadmap_cmd,
        "health",
        lambda target: {
            "issue_count": 1,
            "top_issue": {"status": "warn", "name": "roadmap_example", "detail": "needs review"},
            "audit": {
                "issue_count": 1,
                "top_issue": {"status": "warn", "name": "roadmap_example", "detail": "needs review"},
            },
            "patterns": {"issue_count": 0, "top_issue": None},
            "checks": [{"status": "warn", "name": "roadmap_example", "detail": "needs review"}],
        },
    )
    monkeypatch.setattr(
        repos_cmd,
        "health",
        lambda target: {
            "config_path": str(tmp_path / ".brigade" / "repos.toml"),
            "repo_count": 1,
            "issue_count": 1,
            "top_issue": {"status": "warn", "name": "repo_example", "detail": "missing setup"},
            "checks": [{"status": "warn", "name": "repo_example", "detail": "missing setup"}],
        },
    )

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "roadmap_completion: 1 issue(s)" in out
    assert "roadmap_top_issue: roadmap_example needs review" in out
    assert "repo_fleet: 1 issue(s)" in out
    assert "repo_fleet_top_issue: repo_example missing setup" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] roadmap_example: needs review" in out
    assert "[warn] repo_example: missing setup" in out


def test_tools_plan_and_apply_projection_lifecycle(tmp_path, capsys):
    _init_git_repo(tmp_path)
    source = tmp_path / "tools" / "simplify.md"
    source.parent.mkdir()
    source.write_text("Simplify the current task.\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "Portable simplify command."
source_path = "tools/simplify.md"
supported_harnesses = ["claude", "codex"]
projections = { claude = ".claude/commands/simplify.md", codex = ".codex/skills/simplify/SKILL.md" }
"""
    )

    assert tools_cmd.plan(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools projection plan:" in out
    assert "simplify claude missing action=create" in out

    assert tools_cmd.plan(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["missing"] == 2
    assert payload["projections"][0]["expected_fingerprint"]

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", dry_run=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 2
    assert not (tmp_path / ".claude" / "commands" / "simplify.md").exists()

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 2
    projection = tmp_path / ".claude" / "commands" / "simplify.md"
    text = projection.read_text()
    assert "brigade-tool-projection:" in text
    assert "Simplify the current task." in text
    metadata, body = tools_cmd._read_projection(projection)
    assert metadata["tool_id"] == "simplify"
    assert metadata["family"] == "slash-command"
    assert metadata["harness"] == "claude"
    assert metadata["source_fingerprint"]
    assert metadata["projection_fingerprint"] == tools_cmd._text_hash(body)
    codex_projection = tmp_path / ".codex" / "skills" / "simplify" / "SKILL.md"
    codex_text = codex_projection.read_text()
    assert codex_text.startswith("---\n")
    assert '# brigade-tool-projection: {"family":"slash-command"' in codex_text
    assert 'name: "simplify"' in codex_text
    assert 'description: "Portable simplify command."' in codex_text
    codex_metadata, codex_body = tools_cmd._read_projection(codex_projection)
    assert codex_metadata["tool_id"] == "simplify"
    assert codex_metadata["harness"] == "codex"
    assert codex_body.startswith("---\n")
    assert "# brigade-tool-projection:" not in codex_body
    assert "Simplify the current task." in codex_body
    assert codex_metadata["projection_fingerprint"] == tools_cmd._text_hash(codex_body)

    assert tools_cmd.plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["current"] == 2

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 0
    assert payload["skipped_count"] == 2

    source.write_text("Simplify the current task and remove duplication.\n")
    assert tools_cmd.plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["stale"] == 2

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 2
    assert "remove duplication" in projection.read_text()

    projection.write_text(projection.read_text() + "\nlocal edit\n")
    assert tools_cmd.plan(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["conflicted"] == 1

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["conflict_count"] == 1
    assert "local edit" in projection.read_text()

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", force=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 1
    assert "local edit" not in projection.read_text()


def test_tools_apply_creates_harness_script_and_mcp_projections(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "super.md").write_text("Use shared superpowers.\n")
    (tools_dir / "script.sh").write_text("#!/bin/sh\necho ok\n")
    (tools_dir / "mcp.json").write_text('{"mcpServers":{"local":{"command":"brigade","timeout":10}}}\n')
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "superpowers"
name = "Superpowers"
family = "superpower"
enabled = true
description = "Shared superpowers."
source_path = "tools/super.md"
supported_harnesses = ["claude", "codex", "opencode", "hermes", "openclaw", "mcp", "scripts"]
projections = { claude = ".claude/commands/superpowers.md", codex = ".codex/skills/superpowers/SKILL.md", opencode = ".opencode/superpowers/superpowers.md", hermes = ".hermes/superpowers/superpowers.md", openclaw = ".openclaw/superpowers/superpowers.md", mcp = ".mcp/superpowers.md", scripts = "scripts/superpowers.md" }

[[tool]]
id = "script-tool"
name = "Script Tool"
family = "script"
enabled = true
description = "Script projection."
source_path = "tools/script.sh"
command = "brigade status"
supported_harnesses = ["scripts"]
projections = { scripts = "scripts/script-tool.md" }

[[tool]]
id = "mcp-local"
name = "MCP Local"
family = "mcp"
enabled = true
description = "MCP projection."
source_path = "tools/mcp.json"
supported_harnesses = ["mcp"]
projections = { mcp = ".mcp/mcp-local.md" }
"""
    )

    assert tools_cmd.apply(target=tmp_path, all_tools=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 9
    for rel_path in (
        ".claude/commands/superpowers.md",
        ".codex/skills/superpowers/SKILL.md",
        ".opencode/superpowers/superpowers.md",
        ".hermes/superpowers/superpowers.md",
        ".openclaw/superpowers/superpowers.md",
        ".mcp/superpowers.md",
        "scripts/superpowers.md",
    ):
        assert (tmp_path / rel_path).is_file()
        assert "brigade-tool-projection:" in (tmp_path / rel_path).read_text()
    script_projection = (tmp_path / "scripts" / "script-tool.md").read_text()
    assert "Managed Brigade script projection." in script_projection
    assert "command: `brigade status`" in script_projection
    mcp_projection = (tmp_path / ".mcp" / "mcp-local.md").read_text()
    assert "Managed Brigade MCP projection stub." in mcp_projection
    assert "does not start MCP servers" in mcp_projection


def test_tools_apply_refuses_unmanaged_projection_unless_forced(tmp_path, capsys):
    _init_git_repo(tmp_path)
    source = tmp_path / "tools" / "simplify.md"
    source.parent.mkdir()
    source.write_text("Simplify source.\n")
    projection = tmp_path / ".claude" / "commands" / "simplify.md"
    projection.parent.mkdir(parents=True)
    projection.write_text("user managed projection\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "Portable simplify command."
source_path = "tools/simplify.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/simplify.md" }
"""
    )

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["conflict_count"] == 1
    assert payload["conflicts"][0]["status"] == "unmanaged"
    assert projection.read_text() == "user managed projection\n"

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", force=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 1
    assert "brigade-tool-projection:" in projection.read_text()
    assert "Simplify source." in projection.read_text()


def test_tools_parity_closeout_quiets_projection_issues_and_resurfaces_changed_state(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    for name in ("current", "stale", "missing", "unmanaged", "conflicted", "gap"):
        (tools_dir / f"{name}.md").write_text(f"{name} source\n")
    unmanaged = tmp_path / ".claude" / "commands" / "unmanaged.md"
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_text("user projection\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[tool]]
id = "current"
name = "Current"
family = "slash-command"
enabled = true
description = "Current projection."
source_path = "tools/current.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/current.md" }

[[tool]]
id = "stale"
name = "Stale"
family = "slash-command"
enabled = true
description = "Stale projection."
source_path = "tools/stale.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/stale.md" }

[[tool]]
id = "missing"
name = "Missing"
family = "slash-command"
enabled = true
description = "Missing projection."
source_path = "tools/missing.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/missing.md" }

[[tool]]
id = "unmanaged"
name = "Unmanaged"
family = "slash-command"
enabled = true
description = "Unmanaged projection."
source_path = "tools/unmanaged.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/unmanaged.md" }

[[tool]]
id = "conflicted"
name = "Conflicted"
family = "slash-command"
enabled = true
description = "Conflicted projection."
source_path = "tools/conflicted.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/conflicted.md" }

[[tool]]
id = "gap"
name = "Gap"
family = "slash-command"
enabled = true
description = "Parity gap."
source_path = "tools/gap.md"
supported_harnesses = ["claude", "codex"]
projections = { claude = ".claude/commands/gap.md" }
"""
    )
    for tool_id in ("current", "stale", "conflicted", "gap"):
        assert tools_cmd.apply(target=tmp_path, tool_id=tool_id, json_output=True) == 0
        capsys.readouterr()
    (tools_dir / "stale.md").write_text("stale source changed\n")
    conflicted_path = tmp_path / ".claude" / "commands" / "conflicted.md"
    conflicted_path.write_text(conflicted_path.read_text() + "\nlocal edit\n")

    assert tools_cmd.parity_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in payload["issues"]}
    assert {
        "stale_projection",
        "missing_projection",
        "unmanaged_projection",
        "conflicted_projection",
        "parity_gap",
    } <= issue_types
    assert payload["quieted_issue_count"] == 0

    assert tools_cmd.parity_closeout(target=tmp_path, reason="reviewed", json_output=True) == 0
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["status"] == "reviewed"
    assert closeout["issue_count"] == 5
    assert len(closeout["source_fingerprints"]) == 5

    assert tools_cmd.parity_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["projection_issue_count"] == 0
    assert payload["quieted_issue_count"] == 5
    assert payload["changed_issue_count"] == 0

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_issues: 0" in out
    assert "tool_stale_projection" not in out

    (tools_dir / "stale.md").write_text("stale source changed again\n")
    assert tools_cmd.parity_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["projection_issue_count"] == 1
    assert payload["changed_issue_count"] == 1
    assert payload["issues"][0]["issue_type"] == "stale_projection"

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)
    assert imports["created"] == 1
    assert imports["imports"][0]["metadata"]["tool_issue_type"] == "stale_projection"

    assert tools_cmd.parity_closeout(target=tmp_path, reason="defer", defer=True, json_output=True) == 0
    deferred = json.loads(capsys.readouterr().out)
    assert deferred["status"] == "deferred"
    assert deferred["issue_count"] == 5


def test_tools_describe_and_contracts_report_schema_contracts(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "mode": {"type": "string", "enum": ["fast", "safe"]},
                },
                "additionalProperties": False,
            }
        )
    )
    (tools_dir / "output.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"ok": {"type": "boolean"}}})
    )
    (tools_dir / "examples.json").write_text("{}\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "script-tool"
name = "Script Tool"
family = "script"
enabled = true
description = "Contracted script."
command = "brigade status"
input_schema_path = "tools/input.schema.json"
output_schema_path = "tools/output.schema.json"
examples_path = "tools/examples.json"
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "on-request"
cwd = "."
env_labels = ["SAFE_ENV"]
argument_template = { path = "{path}", mode = "--mode={mode}" }
supported_harnesses = []
"""
    )

    assert tools_cmd.describe(target=tmp_path, tool_id="script-tool") == 0
    out = capsys.readouterr().out
    assert "tool: script-tool" in out
    assert "command: brigade status" in out
    assert "approval_mode: on-request" in out
    assert "permissions: read-files" in out

    assert tools_cmd.describe(target=tmp_path, tool_id="script-tool", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"]["contract"]["has_contract"] is True
    assert payload["tool"]["contract"]["permissions"] == ["read-files"]
    assert payload["issue_count"] == 0

    assert tools_cmd.contracts(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools contracts:" in out
    assert "- script-tool [script] ready issues=0" in out

    assert tools_cmd.contracts(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract_count"] == 1
    assert payload["issue_count"] == 0


def test_tools_contracts_report_malformed_and_unsupported_schemas(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "bad.schema.json").write_text("{not json")
    (tools_dir / "unsupported.schema.json").write_text(json.dumps({"type": "string"}))
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "bad-contract"
name = "Bad Contract"
family = "script"
enabled = true
description = "Bad schema."
command = "brigade status"
input_schema_path = "tools/bad.schema.json"
output_schema_path = "tools/unsupported.schema.json"
examples_path = "tools/missing-examples.json"
argument_template = { "bad-key!" = "{path" }
supported_harnesses = []
"""
    )

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_invalid_input_schema:" in out
    assert "[warn] tool_unsupported_output_schema:" in out
    assert "[warn] tool_missing_examples:" in out
    assert "[warn] tool_bad_argument_template:" in out

    assert tools_cmd.contracts(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in payload["issues"]}
    assert {
        "invalid_input_schema",
        "unsupported_output_schema",
        "missing_examples",
        "bad_argument_template",
    } <= issue_types


def test_tools_call_plan_validates_args_and_renders_template(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["path", "count", "tags", "mode"],
                "properties": {
                    "path": {"type": "string"},
                    "count": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "mode": {"type": "string", "enum": ["fast", "safe"]},
                },
                "additionalProperties": False,
            }
        )
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Call planner."
command = "brigade status"
input_schema_path = "tools/input.schema.json"
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "never"
auth_label = "local-safe"
env_labels = ["SAFE_ENV"]
argument_template = { target = "{path}", count = "--count={count}", mode = "--mode={mode}", tags = "{tags}" }
supported_harnesses = []
"""
    )
    args_path = tmp_path / "args.json"
    args_path.write_text(json.dumps({"path": "README.md", "count": 2, "tags": ["a", "b"], "mode": "safe"}))

    assert tools_cmd.call_plan(target=tmp_path, tool_id="runner", args_json=args_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["plan"]["command"] == "brigade status"
    assert payload["plan"]["arguments"]["target"] == "README.md"
    assert payload["plan"]["arguments"]["count"] == "--count=2"
    assert payload["plan"]["arguments"]["mode"] == "--mode=safe"
    assert payload["plan"]["approval_required"] is False

    assert (
        tools_cmd.call_plan(
            target=tmp_path,
            tool_id="runner",
            args='{"path":"README.md","count":"two","tags":["a", 1],"mode":"slow","extra":true}',
            json_output=True,
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    blockers = "\n".join(payload["blockers"])
    assert "$.count: expected integer" in blockers
    assert "$.tags[1]: expected string" in blockers
    assert "$.mode: expected one of 'fast', 'safe'" in blockers
    assert "$.extra: additional property not allowed" in blockers


def test_tools_call_plan_redacts_and_reports_blockers(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "blocked.md").write_text("Blocked source.\n")
    (tools_dir / "input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"token": {"type": "string"}}})
    )
    projection = tmp_path / ".claude" / "commands" / "blocked.md"
    projection.parent.mkdir(parents=True)
    projection.write_text("unmanaged\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "blocked"
name = "Blocked"
family = "script"
enabled = true
description = "Blocked plan."
source_path = "tools/blocked.md"
input_schema_path = "tools/input.schema.json"
auth_label = "api_token"
env_labels = ["SECRET_TOKEN"]
argument_template = { token = "{token}" }
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/blocked.md" }
"""
    )

    assert tools_cmd.call_plan(target=tmp_path, tool_id="blocked", args='{"token":"abc123"}', json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    blockers = "\n".join(payload["blockers"])
    assert "command is required for call planning" in blockers
    assert "auth_label appears unsafe" in blockers
    assert "env label appears unsafe: SECRET_TOKEN" in blockers
    assert "one or more projections are conflicted or unmanaged" in blockers
    assert payload["plan"]["auth_label"] == "[redacted]"
    assert payload["plan"]["env_labels"] == ["[redacted]"]
    assert payload["plan"]["args"]["token"] == "[redacted]"
    rendered = json.dumps(payload, sort_keys=True)
    assert "abc123" not in rendered


def test_tools_call_queue_list_show_and_review_transitions(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"path": {"type": "string"}}})
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Queue runner."
command = "brigade status"
input_schema_path = "tools/input.schema.json"
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "on-request"
argument_template = { path = "{path}" }
supported_harnesses = []
"""
    )
    args_file = tmp_path / "args.json"
    args_file.write_text('{"path":"README.md"}\n')

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args_json=args_file, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    call_id = payload["call"]["id"]
    assert payload["call"]["status"] == "pending"
    assert payload["call"]["contract"]["approval_mode"] == "on-request"

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert "already pending" in payload["reason"]

    assert tools_cmd.call_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools call list:" in out
    assert "pending: 1" in out

    assert tools_cmd.call_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["pending"] == 1

    assert tools_cmd.call_show(target=tmp_path, call_id=call_id[:12]) == 0
    out = capsys.readouterr().out
    assert f"call: {call_id}" in out
    assert "status: pending" in out

    assert tools_cmd.call_hold(target=tmp_path, call_id=call_id, reason="needs review", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["call"]["status"] == "held"
    assert payload["call"]["review_reason"] == "needs review"

    assert tools_cmd.call_reject(target=tmp_path, call_id=call_id, reason="not needed", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["call"]["status"] == "rejected"
    assert payload["call"]["review_reason"] == "not needed"


def test_tools_call_queue_blocked_requires_include_blocked_and_cannot_approve(tmp_path, capsys):
    _init_git_repo(tmp_path)
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "blocked"
name = "Blocked"
family = "script"
enabled = true
description = "Blocked call."
command = "brigade status"
supported_harnesses = []
"""
    )

    assert tools_cmd.call_queue(target=tmp_path, tool_id="blocked", args="{}", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocked"] == 1
    assert not (tmp_path / ".brigade" / "tools" / "calls.jsonl").exists()

    assert (
        tools_cmd.call_queue(target=tmp_path, tool_id="blocked", args="{}", include_blocked=True, json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    call_id = payload["call"]["id"]
    assert payload["call"]["blockers"]

    assert tools_cmd.call_approve(target=tmp_path, call_id=call_id, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "blocked calls cannot be approved"


def test_tools_call_queue_dedupes_and_requeues_after_change(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    schema = tools_dir / "input.schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"path": {"type": "string"}}}))
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Queue runner."
command = "brigade status"
input_schema_path = "tools/input.schema.json"
argument_template = { path = "{path}" }
supported_harnesses = []
"""
    )

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 0
    first = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_approve(target=tmp_path, call_id=first["id"], json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert "already approved" in payload["reason"]

    assert (
        tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"CHANGELOG.md"}', json_output=True) == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["created"] == 1
    assert second["call"]["id"] != first["id"]
    assert (
        tools_cmd.call_reject(target=tmp_path, call_id=second["call"]["id"], reason="bad timing", json_output=True) == 0
    )
    capsys.readouterr()
    assert (
        tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"CHANGELOG.md"}', json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert "rejected" in payload["reason"]

    schema.write_text(
        json.dumps({"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}})
    )
    assert (
        tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"CHANGELOG.md"}', json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1


def test_tools_call_run_approved_script_writes_receipt_and_redacts_output(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(
        tmp_path,
        script='import sys\nprint("path=" + sys.argv[1])\nprint("api_token=secret-value")\n',
    )
    call = _queue_and_approve_runner(tmp_path, capsys)

    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["call"]["status"] == "completed"
    assert payload["call"]["exit_code"] == 0
    receipt = payload["receipt"]
    assert receipt["call_id"] == call["id"]
    assert receipt["status"] == "completed"
    assert receipt["exit_code"] == 0
    assert receipt["permissions"] == ["read-files"]
    assert receipt["effects"] == ["local-read"]
    assert receipt["stdout_summary"].startswith("path=README.md")
    assert "secret-value" not in json.dumps(payload)
    assert "api_token=[redacted]" in receipt["stdout_summary"]
    assert (tmp_path / ".brigade" / "tools" / "runs").is_dir()
    assert os.path.isfile(receipt["receipt_path"])
    assert os.path.isfile(receipt["stdout_log_path"])
    assert os.path.isfile(receipt["stderr_log_path"])

    assert tools_cmd.call_show(target=tmp_path, call_id=call["id"]) == 0
    out = capsys.readouterr().out
    assert "status: completed" in out


def test_tools_call_run_refuses_non_runnable_statuses_and_stale_records(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n')

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"pending"}', json_output=True) == 0
    pending = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_run(target=tmp_path, call_id=pending["id"], json_output=True) == 1
    assert "must be approved" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    rejected = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"rejected"}')
    assert tools_cmd.call_reject(target=tmp_path, call_id=rejected["id"], reason="no", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=rejected["id"], json_output=True) == 1
    assert "must be approved" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    held = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"held"}')
    assert tools_cmd.call_hold(target=tmp_path, call_id=held["id"], reason="wait", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=held["id"], json_output=True) == 1
    assert "must be approved" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    blocked_config = tmp_path / ".brigade" / "tools.toml"
    blocked_config.write_text(
        f"""
[[tool]]
id = "blocked"
name = "Blocked"
family = "script"
enabled = true
description = "Blocked."
command = "{sys.executable} tools/runner.py"
supported_harnesses = []
"""
    )
    assert (
        tools_cmd.call_queue(target=tmp_path, tool_id="blocked", args="{}", include_blocked=True, json_output=True) == 0
    )
    blocked = json.loads(capsys.readouterr().out)["call"]
    calls = tools_cmd._read_calls(tmp_path)
    for item in calls:
        if item["id"] == blocked["id"]:
            item["status"] = "approved"
            item["reviewed_at"] = "2026-05-27T12:00:00+00:00"
            item["approval_fingerprint"] = tools_cmd._approval_fingerprint(item)
    tools_cmd._write_calls(tmp_path, calls)
    assert tools_cmd.call_run(target=tmp_path, call_id=blocked["id"], json_output=True) == 1
    assert "blocked calls cannot be run" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_script_tool_config(tmp_path, script='print("ok")\n')
    stale = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"stale"}')
    (tmp_path / "tools" / "input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}})
    )
    assert tools_cmd.call_run(target=tmp_path, call_id=stale["id"], json_output=True) == 1
    assert "contract fingerprint is stale" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_script_tool_config(tmp_path, script='print("ok")\n')
    completed = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"completed"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=completed["id"], json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=completed["id"], json_output=True) == 1
    assert "completed calls cannot be run again" in " ".join(json.loads(capsys.readouterr().out)["blockers"])


def test_tools_run_history_list_show_latest_and_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='import sys\nprint("ran=" + sys.argv[1])\n')
    call = _queue_and_approve_runner(tmp_path, capsys)

    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    run_payload = json.loads(capsys.readouterr().out)
    run_id = run_payload["receipt"]["id"]

    assert tools_cmd.run_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools run list:" in out
    assert f"- {run_id} [completed] runner exit_code=0" in out

    assert tools_cmd.run_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_count"] == 1
    assert payload["runs"][0]["id"] == run_id
    assert payload["runs"][0]["stdout_summary"] == "ran=README.md"

    assert tools_cmd.run_show(target=tmp_path, run_id=run_id[:12]) == 0
    out = capsys.readouterr().out
    assert f"run: {run_id}" in out
    assert "status: completed" in out
    assert "stdout_log_path:" in out

    assert tools_cmd.run_show(target=tmp_path, run_id=run_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["id"] == run_id
    assert payload["run"]["call_id"] == call["id"]

    assert tools_cmd.run_latest(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["id"] == run_id


def test_tools_run_history_malformed_receipt_and_missing_log_warnings(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n')
    call = _queue_and_approve_runner(tmp_path, capsys)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    Path(payload["receipt"]["stdout_log_path"]).unlink()
    runs_dir = tmp_path / ".brigade" / "tools" / "runs"
    (runs_dir / "bad.json").write_text("{not json")

    assert tools_cmd.run_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_count"] == 1

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_run_receipt_invalid:" in out
    assert "[warn] tool_run_missing_log:" in out


def test_tools_run_replay_creates_pending_call_without_execution(tmp_path, capsys):
    _init_git_repo(tmp_path)
    marker = tmp_path / "marker.txt"
    _write_script_tool_config(
        tmp_path,
        script='from pathlib import Path\nPath("marker.txt").write_text(Path("marker.txt").read_text() + "x" if Path("marker.txt").exists() else "x")\n',
    )
    call = _queue_and_approve_runner(tmp_path, capsys)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    run_id = json.loads(capsys.readouterr().out)["receipt"]["id"]
    assert marker.read_text() == "x"

    assert tools_cmd.run_replay(target=tmp_path, run_id=run_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    assert payload["executed"] == 0
    assert payload["call"]["status"] == "pending"
    assert payload["call"]["replay_of_run_id"] == run_id
    assert marker.read_text() == "x"

    calls = tools_cmd._read_calls(tmp_path)
    replay_calls = [item for item in calls if item.get("replay_of_run_id") == run_id]
    assert len(replay_calls) == 1
    assert replay_calls[0]["id"] != call["id"]


def test_tools_run_replay_blocks_stale_policy_state(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n')
    _write_policy_config(tmp_path)
    call = _queue_and_approve_runner(tmp_path, capsys)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    run_id = json.loads(capsys.readouterr().out)["receipt"]["id"]

    _write_policy_config(tmp_path, denied_effects=["local-read"])
    assert tools_cmd.run_replay(target=tmp_path, run_id=run_id, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert "effect is denied by policy: local-read" in "\n".join(payload["blockers"])


def test_tools_run_replay_does_not_recover_secret_env_values(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    secret_value = "super-secret-value"
    monkeypatch.setenv("BRIGADE_TEST_SECRET", secret_value)
    _write_script_tool_config(
        tmp_path,
        script='import os\nprint("secret=" + os.environ.get("SAFE_LABEL", ""))\n',
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(config.read_text() + 'env_labels = ["SAFE_LABEL"]\n')
    _write_policy_config(tmp_path, env_bindings={"SAFE_LABEL": "BRIGADE_TEST_SECRET"})
    call = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"README.md","api_token":"argument-secret"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    run_id = payload["receipt"]["id"]
    assert secret_value not in json.dumps(payload)
    assert payload["receipt"]["args"]["api_token"] == "[redacted]"

    assert tools_cmd.run_replay(target=tmp_path, run_id=run_id, json_output=True) == 0
    replay_payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(replay_payload)
    assert secret_value not in rendered
    assert replay_payload["call"]["args"]["api_token"] == "[redacted]"
    assert "argument-secret" not in rendered
    assert secret_value not in (tmp_path / ".brigade" / "tools" / "calls.jsonl").read_text()
    assert secret_value not in Path(payload["receipt"]["receipt_path"]).read_text()


def test_tools_checkpoint_creation_list_show_and_redaction(tmp_path, capsys):
    _init_git_repo(tmp_path)
    call, checkpoint_id, receipt = _create_waiting_checkpoint(tmp_path, capsys)
    assert call["status"] == "waiting"
    assert receipt["status"] == "waiting"
    assert receipt["checkpoint"]["id"] == checkpoint_id
    assert receipt["checkpoint"]["context"]["api_token"] == "[redacted]"
    assert "prompt-secret" not in json.dumps(receipt)
    assert "argument-secret" not in json.dumps(receipt)
    assert "private-value" not in json.dumps(receipt)

    assert tools_cmd.checkpoint_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools checkpoint list:" in out
    assert f"- {checkpoint_id} [pending] runner choose next step" in out

    assert tools_cmd.checkpoint_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint_count"] == 1
    assert payload["checkpoints"][0]["id"] == checkpoint_id

    assert tools_cmd.checkpoint_show(target=tmp_path, checkpoint_id=checkpoint_id[:12]) == 0
    out = capsys.readouterr().out
    assert f"checkpoint: {checkpoint_id}" in out
    assert "choices: continue, abort" in out

    assert tools_cmd.checkpoint_show(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint"]["context"]["note"] == "secret=[redacted]"


def test_tools_checkpoint_approve_reject_and_successful_resume(tmp_path, capsys):
    _init_git_repo(tmp_path)
    call, checkpoint_id, receipt = _create_waiting_checkpoint(tmp_path, capsys)

    assert (
        tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=checkpoint_id, choice="continue", json_output=True)
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint"]["status"] == "approved"
    assert payload["checkpoint"]["selected_choice"] == "continue"
    assert payload["call"]["status"] == "resume-pending"

    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint"]["status"] == "resumed"
    assert payload["call"]["status"] == "resumed"
    assert payload["receipt"]["status"] == "resumed"
    assert payload["receipt"]["original_call_id"] == call["id"]
    assert payload["receipt"]["original_run_id"] == receipt["id"]
    assert payload["receipt"]["checkpoint_id"] == checkpoint_id
    assert payload["receipt"]["resume_run_id"] == payload["receipt"]["id"]
    assert (tmp_path / "resumed.txt").read_text() == "continue"

    _write_script_tool_config(tmp_path, script=_checkpoint_script())
    second = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"other"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=second["id"], json_output=True) == 0
    second_checkpoint = json.loads(capsys.readouterr().out)["receipt"]["checkpoint_id"]
    assert (
        tools_cmd.checkpoint_reject(
            target=tmp_path, checkpoint_id=second_checkpoint, reason="not now", json_output=True
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint"]["status"] == "rejected"
    assert payload["checkpoint"]["review_reason"] == "not now"


def test_tools_checkpoint_resume_refuses_unapproved_expired_stale_blocked_and_policy_denied(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _, checkpoint_id, _ = _create_waiting_checkpoint(tmp_path, capsys)
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "checkpoint must be approved before resume" in "\n".join(payload["blockers"])

    assert (
        tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=checkpoint_id, choice="continue", json_output=True)
        == 0
    )
    capsys.readouterr()
    checkpoint, _ = tools_cmd._resolve_checkpoint(tmp_path, checkpoint_id)
    assert checkpoint is not None
    checkpoint["expires_at"] = "2026-05-01T00:00:00+00:00"
    tools_cmd._write_checkpoint(tmp_path, checkpoint)
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 1
    assert "checkpoint is expired" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_script_tool_config(tmp_path, script=_checkpoint_script())
    _, stale_checkpoint, _ = _create_waiting_checkpoint(tmp_path, capsys, args='{"path":"stale"}')
    assert (
        tools_cmd.checkpoint_approve(
            target=tmp_path, checkpoint_id=stale_checkpoint, choice="continue", json_output=True
        )
        == 0
    )
    capsys.readouterr()
    (tmp_path / "tools" / "input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}})
    )
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=stale_checkpoint, json_output=True) == 1
    blockers = "\n".join(json.loads(capsys.readouterr().out)["blockers"])
    assert "contract fingerprint is stale" in blockers

    _write_script_tool_config(tmp_path, script=_checkpoint_script())
    _, blocked_checkpoint, _ = _create_waiting_checkpoint(tmp_path, capsys, args='{"path":"blocked"}')
    assert (
        tools_cmd.checkpoint_approve(
            target=tmp_path, checkpoint_id=blocked_checkpoint, choice="continue", json_output=True
        )
        == 0
    )
    capsys.readouterr()
    calls = tools_cmd._read_calls(tmp_path)
    for item in calls:
        if item.get("checkpoint_id") == blocked_checkpoint:
            item["blockers"] = ["manual blocker"]
            item["approval_fingerprint"] = tools_cmd._approval_fingerprint(item)
    tools_cmd._write_calls(tmp_path, calls)
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=blocked_checkpoint, json_output=True) == 1
    assert "blocked calls cannot be run" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_script_tool_config(tmp_path, script=_checkpoint_script())
    _write_policy_config(tmp_path)
    _, policy_checkpoint, _ = _create_waiting_checkpoint(tmp_path, capsys, args='{"path":"policy"}')
    _write_policy_config(tmp_path, denied_effects=["local-read"])
    assert (
        tools_cmd.checkpoint_approve(
            target=tmp_path, checkpoint_id=policy_checkpoint, choice="continue", json_output=True
        )
        == 0
    )
    capsys.readouterr()
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=policy_checkpoint, json_output=True) == 1
    assert "effect is denied by policy: local-read" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])


def test_tools_checkpoint_does_not_store_secret_env_values(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    secret_value = "super-secret-value"
    monkeypatch.setenv("BRIGADE_TEST_SECRET", secret_value)
    _write_script_tool_config(
        tmp_path,
        script="""
import json
import os
from pathlib import Path

checkpoint_dir = Path(os.environ["BRIGADE_TOOL_CHECKPOINT_DIR"])
checkpoint_dir.mkdir(parents=True, exist_ok=True)
if os.environ.get("BRIGADE_TOOL_RESUME_CHECKPOINT_ID"):
    print("secret=" + os.environ.get("SAFE_LABEL", ""))
else:
    (checkpoint_dir / "request.json").write_text(json.dumps({
        "reason": "needs secret-safe review",
        "requested_action": "continue",
        "prompt": "secret=" + os.environ.get("SAFE_LABEL", ""),
        "context": {"secret": os.environ.get("SAFE_LABEL", ""), "api_token": "argument-secret"},
        "choices": ["continue"]
    }))
""",
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(config.read_text() + 'env_labels = ["SAFE_LABEL"]\n')
    _write_policy_config(tmp_path, env_bindings={"SAFE_LABEL": "BRIGADE_TEST_SECRET"})
    call = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"README.md","api_token":"argument-secret"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    checkpoint_id = payload["receipt"]["checkpoint_id"]
    rendered = json.dumps(payload)
    assert secret_value not in rendered
    assert "argument-secret" not in rendered

    assert (
        tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=checkpoint_id, choice="continue", json_output=True)
        == 0
    )
    capsys.readouterr()
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(payload)
    assert secret_value not in rendered
    assert "argument-secret" not in rendered
    assert secret_value not in Path(payload["receipt"]["receipt_path"]).read_text()
    assert secret_value not in Path(payload["receipt"]["stdout_log_path"]).read_text()


def test_tools_call_run_approved_mcp_stdio_writes_receipt_and_message_flow(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script())
    _write_runtime_config(tmp_path)
    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"])
    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    capsys.readouterr()
    try:
        call = _queue_and_approve_mcp(tmp_path, capsys)
        assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)
        receipt = payload["receipt"]
        assert payload["call"]["status"] == "completed"
        assert receipt["family"] == "mcp"
        assert receipt["mcp_server_id"] == "helper"
        assert receipt["mcp_tool_name"] == "echo"
        assert receipt["mcp_request_id"] == 3
        assert receipt["mcp_request_payload"]["method"] == "tools/call"
        assert receipt["mcp_request_payload"]["params"]["name"] == "echo"
        assert receipt["mcp_response_summary"]["result"]["content"][0]["text"].startswith("echo README.md")
        assert json.loads((tmp_path / "mcp-methods.json").read_text()) == ["initialize", "tools/list", "tools/call"]
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True)
        capsys.readouterr()


def test_tools_call_run_refuses_bad_mcp_status_policy_and_runtime(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script())
    _write_runtime_config(tmp_path)
    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"])

    assert tools_cmd.call_queue(target=tmp_path, tool_id="mcp-runner", args='{"path":"pending"}', json_output=True) == 0
    pending = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_run(target=tmp_path, call_id=pending["id"], json_output=True) == 1
    assert "must be approved" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    rejected = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"rejected"}')
    assert tools_cmd.call_reject(target=tmp_path, call_id=rejected["id"], reason="no", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=rejected["id"], json_output=True) == 1
    assert "must be approved" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    held = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"held"}')
    assert tools_cmd.call_hold(target=tmp_path, call_id=held["id"], reason="wait", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=held["id"], json_output=True) == 1
    assert "must be approved" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    blocked = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"blocked"}')
    calls = tools_cmd._read_calls(tmp_path)
    for item in calls:
        if item["id"] == blocked["id"]:
            item["blockers"] = ["manual blocker"]
            item["approval_fingerprint"] = tools_cmd._approval_fingerprint(item)
    tools_cmd._write_calls(tmp_path, calls)
    assert tools_cmd.call_run(target=tmp_path, call_id=blocked["id"], json_output=True) == 1
    assert "blocked calls cannot be run" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    stale = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"stale"}')
    (tmp_path / "tools" / "mcp-input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}})
    )
    assert tools_cmd.call_run(target=tmp_path, call_id=stale["id"], json_output=True) == 1
    assert "contract fingerprint is stale" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script())
    missing_runtime = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"missing-runtime"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=missing_runtime["id"], json_output=True) == 1
    assert "required runtime is not running: helper" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"])
    policy_denied = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"policy"}')
    _write_policy_config(tmp_path, allowed_families=["script"], allowed_runtimes=["helper"])
    assert tools_cmd.call_run(target=tmp_path, call_id=policy_denied["id"], json_output=True) == 1
    assert "family is not allowed by policy: mcp" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])


def test_tools_call_run_mcp_timeout_and_malformed_receipts(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_runtime_config(tmp_path)
    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"])
    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    capsys.readouterr()
    try:
        _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script(malformed=True))
        malformed = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"malformed"}')
        assert tools_cmd.call_run(target=tmp_path, call_id=malformed["id"], json_output=True) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["receipt"]["status"] == "failed"
        assert "invalid JSON-RPC response" in payload["receipt"]["stderr_summary"]

        _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script(sleep_seconds=1.0), timeout=0.1)
        timed = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"timeout"}')
        assert tools_cmd.call_run(target=tmp_path, call_id=timed["id"], json_output=True) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["receipt"]["status"] == "failed"
        assert payload["receipt"]["timed_out"] is True
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True)
        capsys.readouterr()


def test_tools_call_run_mcp_redacts_payloads_and_env_values(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    secret_value = "super-secret-value"
    monkeypatch.setenv("BRIGADE_TEST_SECRET", secret_value)
    _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script(copy_env=True))
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(config.read_text() + 'env_labels = ["SAFE_LABEL"]\n')
    _write_runtime_config(tmp_path)
    _write_policy_config(
        tmp_path,
        allowed_families=["mcp"],
        allowed_runtimes=["helper"],
        env_bindings={"SAFE_LABEL": "BRIGADE_TEST_SECRET"},
    )
    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    capsys.readouterr()
    try:
        call = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"README.md","api_token":"argument-secret"}')
        assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)
        rendered = json.dumps(payload)
        assert secret_value not in rendered
        assert "argument-secret" not in rendered
        assert payload["receipt"]["mcp_request_payload"]["params"]["arguments"]["api_token"] == "[redacted]"
        assert secret_value not in Path(payload["receipt"]["receipt_path"]).read_text()
        assert secret_value not in Path(payload["receipt"]["stdout_log_path"]).read_text()
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True)
        capsys.readouterr()


def test_tools_runtime_init_list_show_status_and_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert tools_cmd.runtime_init(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "runtime_config:" in out
    assert (tmp_path / ".brigade" / "tools" / "runtimes.toml").is_file()

    assert tools_cmd.runtime_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools runtime list:" in out
    assert "local-helper" in out

    assert tools_cmd.runtime_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime_count"] == 1

    assert tools_cmd.runtime_show(target=tmp_path, runtime_id="local-helper") == 0
    out = capsys.readouterr().out
    assert "runtime: local-helper" in out

    assert tools_cmd.runtime_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["stopped"] == 1


def test_tools_runtime_start_stop_restart_with_pid_logs_and_unmanaged_refusal(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_runtime_config(tmp_path)

    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    pid = payload["pid"]
    assert payload["runtime"]["state"] == "running"
    assert os.path.isfile(payload["runtime"]["pid_path"])
    assert os.path.isfile(payload["runtime"]["stdout_log_path"])
    assert os.path.isfile(payload["runtime"]["stderr_log_path"])

    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert payload["runtime"]["pid"] == pid

    assert tools_cmd.runtime_restart(target=tmp_path, runtime_id="helper", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"]["state"] == "running"
    assert payload["runtime"]["pid"] != pid

    assert tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped"] == 1
    assert payload["runtime"]["state"] == "stopped"

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid_path = tmp_path / ".brigade" / "tools" / "runtime" / "helper.pid"
        pid_path.write_text(f"{process.pid}\n")
        assert tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True) == 1
        payload = json.loads(capsys.readouterr().out)
        assert "unmanaged" in payload["error"]
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_tools_runtime_doctor_safety_warnings(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    _write_runtime_config(
        tmp_path,
        runtime_id="bad",
        command="bash -c echo hi",
        cwd="missing",
        health_command=f'{sys.executable} -c "import sys; sys.exit(2)"',
    )

    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="bad", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "runtime command shape is high risk" in payload["blockers"]
    assert any("runtime cwd missing" in blocker for blocker in payload["blockers"])

    _write_runtime_config(tmp_path, runtime_id="stale")
    stale_pid = tmp_path / ".brigade" / "tools" / "runtime" / "stale.pid"
    stale_pid.parent.mkdir(parents=True, exist_ok=True)
    stale_pid.write_text("999999\n")
    # A live process can legitimately hold pid 999999 on the test host; pin the
    # sentinel dead so the stale-pid branch is deterministic.
    real_process_alive = tools_cmd.runtimes._process_alive
    monkeypatch.setattr(
        tools_cmd.runtimes,
        "_process_alive",
        lambda pid: False if pid == 999999 else real_process_alive(pid),
    )
    assert tools_cmd.runtime_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_runtime_stale_pid:" in out

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((".".join(("127", "0", "0", "1")), 0))
        sock.listen()
        port = sock.getsockname()[1]
        _write_runtime_config(tmp_path, runtime_id="porty", port=port)
        assert tools_cmd.runtime_doctor(target=tmp_path) == 0
        out = capsys.readouterr().out
        assert "[warn] tool_runtime_port_conflict:" in out

    _write_runtime_config(
        tmp_path,
        runtime_id="health",
        health_command=f'{sys.executable} -c "import sys; sys.exit(3)"',
    )
    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="health", json_output=True) == 0
    capsys.readouterr()
    try:
        assert tools_cmd.runtime_doctor(target=tmp_path) == 0
        out = capsys.readouterr().out
        assert "[warn] tool_runtime_health_failed:" in out
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="health", json_output=True)
        capsys.readouterr()


def test_tools_call_run_requires_healthy_runtime_and_receipt_snapshot(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n')
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        config.read_text()
        + """
runtime_id = "helper"
requires_runtime = true
"""
    )
    _write_runtime_config(tmp_path)
    call = _queue_and_approve_runner(tmp_path, capsys)

    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "required runtime is not running: helper" in payload["blockers"]

    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    capsys.readouterr()
    try:
        assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["receipt"]["runtime_id"] == "helper"
        assert payload["receipt"]["runtime"]["state"] == "running"
        assert payload["receipt"]["runtime"]["managed"] is True
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True)
        capsys.readouterr()


def test_tools_policy_init_show_doctor_and_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert tools_cmd.policy_init(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "policy_config:" in out
    assert (tmp_path / ".brigade" / "tools" / "policy.toml").is_file()

    assert tools_cmd.policy_show(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools policy:" in out
    assert "allowed_families: script" in out

    assert tools_cmd.policy_show(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy"]["allowed_families"] == ["script"]
    assert "SAFE_ENV" in payload["policy"]["env_bindings"]

    assert tools_cmd.policy_doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is True
    assert payload["issue_count"] == 0


def test_tools_policy_blocks_plan_and_run_for_effect_timeout_runtime_approval_and_env(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n', timeout=30)
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        f"""
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Run local script."
command = "{sys.executable} tools/runner.py"
input_schema_path = "tools/input.schema.json"
timeout = 30
permissions = ["read-files"]
effects = ["remote-mutation"]
approval_mode = "never"
argument_template = {{ path = "{{path}}" }}
supported_harnesses = []
runtime_id = "helper"
requires_runtime = false
env_labels = ["SAFE_LABEL"]
"""
    )
    _write_policy_config(
        tmp_path,
        allowed_effects=["local-read"],
        denied_effects=["remote-mutation"],
        required_approval_modes=["on-request"],
        max_timeout=5,
        allowed_runtimes=["other"],
        env_bindings={},
    )

    assert tools_cmd.call_plan(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    blockers = "\n".join(payload["blockers"])
    assert "effect is denied by policy: remote-mutation" in blockers
    assert "effect is not allowed by policy: remote-mutation" in blockers
    assert "approval mode is not allowed by policy: never" in blockers
    assert "timeout exceeds policy max: 30.0 > 5.0" in blockers
    assert "runtime is not allowed by policy: helper" in blockers
    assert "missing env binding for label: SAFE_LABEL" in blockers

    assert (
        tools_cmd.call_queue(
            target=tmp_path, tool_id="runner", args='{"path":"README.md"}', include_blocked=True, json_output=True
        )
        == 0
    )
    call = json.loads(capsys.readouterr().out)["call"]
    calls = tools_cmd._read_calls(tmp_path)
    calls[0]["status"] = "approved"
    calls[0]["reviewed_at"] = "2026-05-27T12:00:00+00:00"
    calls[0]["approval_fingerprint"] = tools_cmd._approval_fingerprint(calls[0])
    tools_cmd._write_calls(tmp_path, calls)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "effect is denied by policy: remote-mutation" in "\n".join(payload["blockers"])


def test_tools_policy_env_binding_passes_values_without_storing_them(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    secret_value = "super-secret-value"
    monkeypatch.setenv("BRIGADE_TEST_SECRET", secret_value)
    _write_script_tool_config(
        tmp_path,
        script='import os\nprint("label=" + os.environ.get("SAFE_LABEL", ""))\n',
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        config.read_text()
        + """
env_labels = ["SAFE_TOKEN"]
"""
    )
    config.write_text(config.read_text().replace('env_labels = ["SAFE_TOKEN"]', 'env_labels = ["SAFE_LABEL"]'))
    _write_policy_config(tmp_path, env_bindings={"SAFE_LABEL": "BRIGADE_TEST_SECRET"})

    assert tools_cmd.call_plan(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy"]["env_labels_used"] == ["SAFE_LABEL"]
    assert secret_value not in json.dumps(payload)

    call = _queue_and_approve_runner(tmp_path, capsys)
    assert secret_value not in (tmp_path / ".brigade" / "tools" / "calls.jsonl").read_text()

    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["receipt"]["env_labels_used"] == ["SAFE_LABEL"]
    assert payload["receipt"]["policy"]["env_labels_used"] == ["SAFE_LABEL"]
    assert secret_value not in json.dumps(payload)
    assert (
        secret_value
        not in (tmp_path / ".brigade" / "tools" / "runs" / f"{payload['receipt']['id']}.stdout.log").read_text()
    )
    assert (
        "[redacted]"
        in (tmp_path / ".brigade" / "tools" / "runs" / f"{payload['receipt']['id']}.stdout.log").read_text()
    )
    assert secret_value not in Path(payload["receipt"]["receipt_path"]).read_text()


def test_work_inbox_doctor_reports_hygiene_issues_and_daily_loop(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "python3 scanner.py"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/repo-scan.jsonl"
import_path = ".brigade/repo-scan.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )
    missing = work_cmd._make_import("Missing provenance", kind="task", source="repo-scan")
    missing["created_at"] = "2026-05-30T11:00:00+00:00"
    stale = work_cmd._make_import("Stale pending", kind="task", source="manual")
    stale["created_at"] = "2026-05-20T12:00:00+00:00"
    promoted = work_cmd._make_import("Broken promoted", kind="task", source="repo-scan")
    promoted.update({"status": "promoted", "task_id": "missing-task", "updated_at": "2026-05-29T12:00:00+00:00"})
    dismissed_changed = work_cmd._make_import(
        "Dismissed old",
        kind="task",
        source="repo-scan",
        metadata={"source_item_key": "same-item", "source_fingerprint": "old"},
    )
    dismissed_changed.update({"status": "dismissed", "dismissed_at": "2026-05-29T12:00:00+00:00"})
    changed_pending = work_cmd._make_import(
        "Dismissed new",
        kind="task",
        source="repo-scan",
        metadata={
            "source_item_key": "same-item",
            "source_fingerprint": "new",
            "scanner_id": "repo-scan",
            "scanner_source": "repo-scan",
        },
    )
    noisy = []
    for index in range(work_cmd.DISMISSED_SOURCE_WARN_THRESHOLD):
        item = work_cmd._make_import(f"Noisy {index}", kind="task", source="noisy-source")
        item.update({"status": "dismissed", "dismissed_at": "2026-05-29T12:00:00+00:00"})
        noisy.append(item)
    work_cmd._write_imports(tmp_path, [missing, stale, promoted, dismissed_changed, changed_pending, *noisy])
    run_dir = tmp_path / ".brigade" / "scanners" / "runs" / "no-import-run"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "no-import-run",
            "scanner_id": "repo-scan",
            "source": "repo-scan",
            "status": "completed",
            "started_at": "2026-05-30T10:00:00+00:00",
            "completed_at": "2026-05-30T10:00:01+00:00",
            "exit_code": 0,
            "timed_out": False,
            "import_path": str(tmp_path / ".brigade" / "repo-scan.jsonl"),
        },
    )

    assert work_cmd.inbox_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] inbox_missing_provenance:" in out
    assert "[warn] inbox_stale_pending:" in out
    assert "[warn] inbox_promoted_task_missing:" in out
    assert "[warn] inbox_dismissed_changed:" in out
    assert "[warn] inbox_noisy_sources:" in out
    assert "[warn] inbox_provenance_contract:" in out
    assert "[warn] inbox_scanner_run_no_imports:" in out

    assert work_cmd.inbox_doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] == 7
    assert payload["top_issue"]["name"] == "inbox_missing_provenance"

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "inbox_hygiene: 7 issue(s)" in out
    assert "inbox_top_issue: inbox_missing_provenance" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] inbox_missing_provenance:" in out
    assert "[warn] inbox_scanner_run_no_imports:" in out


def test_work_inbox_archive_preserves_pending_and_archives_closed(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    pending = work_cmd._make_import("Keep pending", kind="task", source="repo-scan")
    pending.update({"status": "pending", "updated_at": "2026-05-20T12:00:00+00:00"})
    promoted = work_cmd._make_import("Archive promoted", kind="task", source="repo-scan")
    promoted.update({"status": "promoted", "updated_at": "2026-05-20T12:00:00+00:00"})
    dismissed = work_cmd._make_import("Archive dismissed", kind="task", source="repo-scan")
    dismissed.update({"status": "dismissed", "updated_at": "2026-05-20T12:00:00+00:00"})
    superseded = work_cmd._make_import("Archive superseded", kind="task", source="repo-scan")
    superseded.update({"status": "superseded", "updated_at": "2026-05-20T12:00:00+00:00"})
    fresh = work_cmd._make_import("Keep fresh dismissed", kind="task", source="repo-scan")
    fresh.update({"status": "dismissed", "updated_at": "2026-05-30T11:00:00+00:00"})
    work_cmd._write_imports(tmp_path, [pending, promoted, dismissed, superseded, fresh])

    assert work_cmd.inbox_archive(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["archived"] == 3
    assert payload["kept"] == 2

    remaining = [item["text"] for item in work_cmd._read_imports(tmp_path)]
    assert remaining == ["Keep pending", "Keep fresh dismissed"]
    archived = [
        json.loads(line)
        for line in (tmp_path / ".brigade" / "work" / "imports" / "archive.jsonl").read_text().splitlines()
    ]
    assert [item["text"] for item in archived] == ["Archive promoted", "Archive dismissed", "Archive superseded"]
    assert all(item["archived_at"] == "2026-05-30T12:00:00+00:00" for item in archived)


def test_work_brief_and_doctor_include_security_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    assert security_cmd.init(target=tmp_path) == 0
    (tmp_path / ".env").write_text("SERVICE_TOKEN=abcd1234abcd1234abcd1234\n")
    assert security_cmd.scan(target=tmp_path, fail_on="none") == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "security_config:" in out
    assert "security_health:" in out
    assert "security_top_finding:" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] security_open_findings:" in out


def test_work_brief_and_doctor_include_memory_care_health(tmp_path, monkeypatch, capsys):
    from brigade import memory_cmd

    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    assert memory_cmd.init(target=tmp_path, update_gitignore=False) == 0
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "stale.md").write_text(
        "\n".join(
            [
                "---",
                "topic: stale",
                "last_reviewed: 2026-01-01",
                "confidence: high",
                'evidence: ["README.md"]',
                "---",
                "",
                "Body.",
            ]
        )
    )
    (tmp_path / "MEMORY.md").write_text("- [stale](memory/cards/stale.md)\n")
    assert memory_cmd.scan(target=tmp_path) == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "memory_care_config:" in out
    assert "memory_care_health:" in out
    assert "memory_care_top_issue:" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] memory_care_open_issues:" in out


def test_work_brief_includes_handoff_ingest_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    log = tmp_path / ".brigade" / "handoff-ingest" / "latest.log"
    log.parent.mkdir(parents=True)
    log.write_text("SKIP bad.md: no recognizable markdown sections found\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": ".brigade/handoff-ingest/latest.log"},
            }
        )
    )

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["handoff_issues"]["count"] == 1
    assert payload["handoff_issues"]["known_count"] == 0
    assert payload["handoff_issues"]["total_count"] == 1
    assert payload["handoff_issues"]["by_category"] == {"skip": 1}

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "handoff_ingest_issues_new: 1" in out
    assert "handoff_ingest_issues_by_category:" in out
    assert "  skip: 1" in out


def test_work_brief_and_doctor_include_handoff_draft_queue(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "reviewed.md").write_text(
        """# Memory Handoff

## Type
decision

## Title
Reviewed draft

## Summary
Reviewed draft.

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### Reviewed draft

- source: test
"""
    )
    (inbox / "invalid.md").write_text("# Memory Handoff\n")
    runs_root = tmp_path / ".brigade" / "handoffs" / "ingest-runs"
    runs_root.mkdir(parents=True)
    (runs_root / "run-brief.json").write_text(
        json.dumps(
            {
                "run_id": "run-brief",
                "started_at": "2026-05-28T10:00:00+00:00",
                "completed_at": "2026-05-28T10:01:00+00:00",
                "source_root": str(tmp_path),
                "inbox_paths": [str(inbox)],
                "processed_handoff_paths": [str(inbox / "reviewed.md")],
                "promoted_card_targets": [],
                "routed_document_targets": [],
                "skipped_handoff_paths": [],
                "failed_handoff_paths": [],
                "warning_count": 0,
                "safe_summary": "processed=1",
                "log_path": "latest.log",
            }
        )
    )

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["handoff_drafts"]["counts"]["total"] == 2
    assert payload["handoff_drafts"]["counts"]["reviewed"] == 1
    assert payload["handoff_drafts"]["latest_ingest_run"]["run_id"] == "run-brief"
    assert payload["handoff_drafts"]["issue_count"] >= 1

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "handoff_drafts_pending: 1" in out
    assert "handoff_drafts_reviewed: 1" in out
    assert "handoff_ingest_latest: run-brief completed=2026-05-28T10:01:00+00:00" in out
    assert "handoff_draft_next_command: brigade handoff show" in out

    assert work_cmd.doctor(target=tmp_path) == 1
    out = capsys.readouterr().out
    assert "handoff_draft_invalid" in out


def test_work_brief_suppresses_known_handoff_ingest_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    log = tmp_path / ".brigade" / "handoff-ingest" / "latest.log"
    log.parent.mkdir(parents=True)
    log.write_text("SKIP bad.md: no recognizable markdown sections found\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": ".brigade/handoff-ingest/latest.log"},
            }
        )
    )
    from brigade import handoff_cmd

    issue = handoff_cmd.collect_issues(tmp_path)[0]
    dismissed = work_cmd._make_import(
        issue.text,
        kind=issue.kind,
        source="handoff-ingest",
        metadata=issue.as_import_record()["metadata"],
    )
    dismissed["status"] = "dismissed"
    work_cmd._write_imports(tmp_path, [dismissed])

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["handoff_issues"]["count"] == 0
    assert payload["handoff_issues"]["known_count"] == 1
    assert payload["handoff_issues"]["known_by_category"] == {"skip": 1}

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "handoff_ingest_issues_new" not in out
    assert "handoff_ingest_issues_known: 1" in out


def test_work_inbox_cli(tmp_path, monkeypatch):
    seen = []

    def fake_inbox(**kwargs):
        seen.append(("inbox", kwargs))
        return 0

    def fake_inbox_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_inbox_archive(**kwargs):
        seen.append(("archive", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "inbox", fake_inbox)
    monkeypatch.setattr(work_cmd, "inbox_doctor", fake_inbox_doctor)
    monkeypatch.setattr(work_cmd, "inbox_archive", fake_inbox_archive)

    assert cli.main(["work", "inbox", "--target", str(tmp_path), "--limit", "7"]) == 0
    assert cli.main(["work", "inbox", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "inbox", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "inbox", "archive", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("inbox", {"target": tmp_path, "json_output": False, "limit": 7}),
        ("inbox", {"target": tmp_path, "json_output": True, "limit": 20}),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("archive", {"target": tmp_path, "json_output": True}),
    ]


def test_tools_cli(tmp_path, monkeypatch):
    seen = []

    def fake_init(**kwargs):
        seen.append(("init", kwargs))
        return 0

    def fake_list(**kwargs):
        seen.append(("list", kwargs))
        return 0

    def fake_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_search(**kwargs):
        seen.append(("search", kwargs))
        return 0

    def fake_describe(**kwargs):
        seen.append(("describe", kwargs))
        return 0

    def fake_contracts(**kwargs):
        seen.append(("contracts", kwargs))
        return 0

    def fake_call_plan(**kwargs):
        seen.append(("call-plan", kwargs))
        return 0

    def fake_call_queue(**kwargs):
        seen.append(("call-queue", kwargs))
        return 0

    def fake_call_list(**kwargs):
        seen.append(("call-list", kwargs))
        return 0

    def fake_call_show(**kwargs):
        seen.append(("call-show", kwargs))
        return 0

    def fake_call_approve(**kwargs):
        seen.append(("call-approve", kwargs))
        return 0

    def fake_call_reject(**kwargs):
        seen.append(("call-reject", kwargs))
        return 0

    def fake_call_hold(**kwargs):
        seen.append(("call-hold", kwargs))
        return 0

    def fake_call_run(**kwargs):
        seen.append(("call-run", kwargs))
        return 0

    def fake_run_list(**kwargs):
        seen.append(("run-list", kwargs))
        return 0

    def fake_run_show(**kwargs):
        seen.append(("run-show", kwargs))
        return 0

    def fake_run_latest(**kwargs):
        seen.append(("run-latest", kwargs))
        return 0

    def fake_run_replay(**kwargs):
        seen.append(("run-replay", kwargs))
        return 0

    def fake_checkpoint_list(**kwargs):
        seen.append(("checkpoint-list", kwargs))
        return 0

    def fake_checkpoint_show(**kwargs):
        seen.append(("checkpoint-show", kwargs))
        return 0

    def fake_checkpoint_approve(**kwargs):
        seen.append(("checkpoint-approve", kwargs))
        return 0

    def fake_checkpoint_reject(**kwargs):
        seen.append(("checkpoint-reject", kwargs))
        return 0

    def fake_checkpoint_resume(**kwargs):
        seen.append(("checkpoint-resume", kwargs))
        return 0

    def fake_runtime_init(**kwargs):
        seen.append(("runtime-init", kwargs))
        return 0

    def fake_runtime_list(**kwargs):
        seen.append(("runtime-list", kwargs))
        return 0

    def fake_runtime_show(**kwargs):
        seen.append(("runtime-show", kwargs))
        return 0

    def fake_runtime_status(**kwargs):
        seen.append(("runtime-status", kwargs))
        return 0

    def fake_runtime_start(**kwargs):
        seen.append(("runtime-start", kwargs))
        return 0

    def fake_runtime_stop(**kwargs):
        seen.append(("runtime-stop", kwargs))
        return 0

    def fake_runtime_restart(**kwargs):
        seen.append(("runtime-restart", kwargs))
        return 0

    def fake_runtime_doctor(**kwargs):
        seen.append(("runtime-doctor", kwargs))
        return 0

    def fake_defaults(**kwargs):
        seen.append(("defaults", kwargs))
        return 0

    def fake_policy_init(**kwargs):
        seen.append(("policy-init", kwargs))
        return 0

    def fake_policy_show(**kwargs):
        seen.append(("policy-show", kwargs))
        return 0

    def fake_policy_doctor(**kwargs):
        seen.append(("policy-doctor", kwargs))
        return 0

    def fake_plan(**kwargs):
        seen.append(("plan", kwargs))
        return 0

    def fake_apply(**kwargs):
        seen.append(("apply", kwargs))
        return 0

    def fake_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_import_issues(**kwargs):
        seen.append(("import-issues", kwargs))
        return 0

    def fake_parity_status(**kwargs):
        seen.append(("parity-status", kwargs))
        return 0

    def fake_parity_closeout(**kwargs):
        seen.append(("parity-closeout", kwargs))
        return 0

    monkeypatch.setattr(tools_cmd, "init", fake_init)
    monkeypatch.setattr(tools_cmd, "list_tools", fake_list)
    monkeypatch.setattr(tools_cmd, "show", fake_show)
    monkeypatch.setattr(tools_cmd, "search", fake_search)
    monkeypatch.setattr(tools_cmd, "describe", fake_describe)
    monkeypatch.setattr(tools_cmd, "contracts", fake_contracts)
    monkeypatch.setattr(tools_cmd, "call_plan", fake_call_plan)
    monkeypatch.setattr(tools_cmd, "call_queue", fake_call_queue)
    monkeypatch.setattr(tools_cmd, "call_list", fake_call_list)
    monkeypatch.setattr(tools_cmd, "call_show", fake_call_show)
    monkeypatch.setattr(tools_cmd, "call_approve", fake_call_approve)
    monkeypatch.setattr(tools_cmd, "call_reject", fake_call_reject)
    monkeypatch.setattr(tools_cmd, "call_hold", fake_call_hold)
    monkeypatch.setattr(tools_cmd, "call_run", fake_call_run)
    monkeypatch.setattr(tools_cmd, "run_list", fake_run_list)
    monkeypatch.setattr(tools_cmd, "run_show", fake_run_show)
    monkeypatch.setattr(tools_cmd, "run_latest", fake_run_latest)
    monkeypatch.setattr(tools_cmd, "run_replay", fake_run_replay)
    monkeypatch.setattr(tools_cmd, "checkpoint_list", fake_checkpoint_list)
    monkeypatch.setattr(tools_cmd, "checkpoint_show", fake_checkpoint_show)
    monkeypatch.setattr(tools_cmd, "checkpoint_approve", fake_checkpoint_approve)
    monkeypatch.setattr(tools_cmd, "checkpoint_reject", fake_checkpoint_reject)
    monkeypatch.setattr(tools_cmd, "checkpoint_resume", fake_checkpoint_resume)
    monkeypatch.setattr(tools_cmd, "runtime_init", fake_runtime_init)
    monkeypatch.setattr(tools_cmd, "runtime_list", fake_runtime_list)
    monkeypatch.setattr(tools_cmd, "runtime_show", fake_runtime_show)
    monkeypatch.setattr(tools_cmd, "runtime_status", fake_runtime_status)
    monkeypatch.setattr(tools_cmd, "runtime_start", fake_runtime_start)
    monkeypatch.setattr(tools_cmd, "runtime_stop", fake_runtime_stop)
    monkeypatch.setattr(tools_cmd, "runtime_restart", fake_runtime_restart)
    monkeypatch.setattr(tools_cmd, "runtime_doctor", fake_runtime_doctor)
    monkeypatch.setattr(tools_cmd, "defaults", fake_defaults)
    monkeypatch.setattr(tools_cmd, "policy_init", fake_policy_init)
    monkeypatch.setattr(tools_cmd, "policy_show", fake_policy_show)
    monkeypatch.setattr(tools_cmd, "policy_doctor", fake_policy_doctor)
    monkeypatch.setattr(tools_cmd, "plan", fake_plan)
    monkeypatch.setattr(tools_cmd, "apply", fake_apply)
    monkeypatch.setattr(tools_cmd, "doctor", fake_doctor)
    monkeypatch.setattr(tools_cmd, "import_issues", fake_import_issues)
    monkeypatch.setattr(tools_cmd, "parity_status", fake_parity_status)
    monkeypatch.setattr(tools_cmd, "parity_closeout", fake_parity_closeout)

    assert cli.main(["tools", "init", "--target", str(tmp_path), "--force", "--no-gitignore"]) == 0
    assert (
        cli.main(["tools", "defaults", "--target", str(tmp_path), "--dry-run", "--force", "--no-gitignore", "--json"])
        == 0
    )
    assert cli.main(["tools", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "show", "simplify", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "describe", "simplify", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "contracts", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "search", "simple", "--target", str(tmp_path), "--json"]) == 0
    assert (
        cli.main(["tools", "call", "plan", "simplify", "--target", str(tmp_path), "--args", '{"x":1}', "--json"]) == 0
    )
    assert (
        cli.main(
            [
                "tools",
                "call",
                "queue",
                "simplify",
                "--target",
                str(tmp_path),
                "--args",
                '{"x":1}',
                "--include-blocked",
                "--json",
            ]
        )
        == 0
    )
    assert cli.main(["tools", "call", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "call", "show", "call-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "call", "approve", "call-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "call", "reject", "call-123", "--target", str(tmp_path), "--reason", "no", "--json"]) == 0
    assert cli.main(["tools", "call", "hold", "call-123", "--target", str(tmp_path), "--reason", "wait", "--json"]) == 0
    assert cli.main(["tools", "call", "run", "call-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "call", "run", "--next", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "run", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "run", "show", "run-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "run", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "run", "replay", "run-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "checkpoint", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "checkpoint", "show", "checkpoint-123", "--target", str(tmp_path), "--json"]) == 0
    assert (
        cli.main(
            [
                "tools",
                "checkpoint",
                "approve",
                "checkpoint-123",
                "--choice",
                "continue",
                "--target",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            ["tools", "checkpoint", "reject", "checkpoint-123", "--reason", "no", "--target", str(tmp_path), "--json"]
        )
        == 0
    )
    assert cli.main(["tools", "checkpoint", "resume", "checkpoint-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "init", "--target", str(tmp_path), "--force"]) == 0
    assert cli.main(["tools", "runtime", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "show", "helper", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "status", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "start", "helper", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "stop", "helper", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "restart", "helper", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "policy", "init", "--target", str(tmp_path), "--force"]) == 0
    assert cli.main(["tools", "policy", "show", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "policy", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "parity", "status", "--target", str(tmp_path), "--json"]) == 0
    assert (
        cli.main(
            ["tools", "parity", "closeout", "--target", str(tmp_path), "--reason", "reviewed", "--defer", "--json"]
        )
        == 0
    )
    assert cli.main(["tools", "plan", "simplify", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "apply", "simplify", "--target", str(tmp_path), "--dry-run", "--force", "--json"]) == 0
    assert cli.main(["tools", "apply", "--all", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "import-issues", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False}),
        (
            "defaults",
            {
                "target": tmp_path,
                "dry_run": True,
                "force": True,
                "update_gitignore": False,
                "json_output": True,
            },
        ),
        ("list", {"target": tmp_path, "json_output": True}),
        ("show", {"target": tmp_path, "tool_id": "simplify", "json_output": True}),
        ("describe", {"target": tmp_path, "tool_id": "simplify", "json_output": True}),
        ("contracts", {"target": tmp_path, "json_output": True}),
        ("search", {"target": tmp_path, "query": "simple", "json_output": True}),
        (
            "call-plan",
            {
                "target": tmp_path,
                "tool_id": "simplify",
                "args": '{"x":1}',
                "args_json": None,
                "json_output": True,
            },
        ),
        (
            "call-queue",
            {
                "target": tmp_path,
                "tool_id": "simplify",
                "args": '{"x":1}',
                "args_json": None,
                "include_blocked": True,
                "json_output": True,
            },
        ),
        ("call-list", {"target": tmp_path, "json_output": True}),
        ("call-show", {"target": tmp_path, "call_id": "call-123", "json_output": True}),
        ("call-approve", {"target": tmp_path, "call_id": "call-123", "json_output": True}),
        ("call-reject", {"target": tmp_path, "call_id": "call-123", "reason": "no", "json_output": True}),
        ("call-hold", {"target": tmp_path, "call_id": "call-123", "reason": "wait", "json_output": True}),
        ("call-run", {"target": tmp_path, "call_id": "call-123", "next_call": False, "json_output": True}),
        ("call-run", {"target": tmp_path, "call_id": None, "next_call": True, "json_output": True}),
        ("run-list", {"target": tmp_path, "json_output": True}),
        ("run-show", {"target": tmp_path, "run_id": "run-123", "json_output": True}),
        ("run-latest", {"target": tmp_path, "json_output": True}),
        ("run-replay", {"target": tmp_path, "run_id": "run-123", "json_output": True}),
        ("checkpoint-list", {"target": tmp_path, "json_output": True}),
        ("checkpoint-show", {"target": tmp_path, "checkpoint_id": "checkpoint-123", "json_output": True}),
        (
            "checkpoint-approve",
            {"target": tmp_path, "checkpoint_id": "checkpoint-123", "choice": "continue", "json_output": True},
        ),
        (
            "checkpoint-reject",
            {"target": tmp_path, "checkpoint_id": "checkpoint-123", "reason": "no", "json_output": True},
        ),
        ("checkpoint-resume", {"target": tmp_path, "checkpoint_id": "checkpoint-123", "json_output": True}),
        ("runtime-init", {"target": tmp_path, "force": True}),
        ("runtime-list", {"target": tmp_path, "json_output": True}),
        ("runtime-show", {"target": tmp_path, "runtime_id": "helper", "json_output": True}),
        ("runtime-status", {"target": tmp_path, "json_output": True}),
        ("runtime-start", {"target": tmp_path, "runtime_id": "helper", "json_output": True}),
        ("runtime-stop", {"target": tmp_path, "runtime_id": "helper", "json_output": True}),
        ("runtime-restart", {"target": tmp_path, "runtime_id": "helper", "json_output": True}),
        ("runtime-doctor", {"target": tmp_path, "json_output": True}),
        ("policy-init", {"target": tmp_path, "force": True}),
        ("policy-show", {"target": tmp_path, "json_output": True}),
        ("policy-doctor", {"target": tmp_path, "json_output": True}),
        ("parity-status", {"target": tmp_path, "json_output": True}),
        ("parity-closeout", {"target": tmp_path, "reason": "reviewed", "defer": True, "json_output": True}),
        ("plan", {"target": tmp_path, "tool_id": "simplify", "json_output": True}),
        (
            "apply",
            {
                "target": tmp_path,
                "tool_id": "simplify",
                "all_tools": False,
                "dry_run": True,
                "force": True,
                "json_output": True,
            },
        ),
        (
            "apply",
            {
                "target": tmp_path,
                "tool_id": None,
                "all_tools": True,
                "dry_run": False,
                "force": False,
                "json_output": True,
            },
        ),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("import-issues", {"target": tmp_path, "json_output": True}),
    ]
