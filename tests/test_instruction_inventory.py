"""Tests for the instruction-file inventory."""

from __future__ import annotations

import os

import pytest

from brigade.install import install_selection
from brigade.instruction_inventory import (
    InstructionsConfig,
    discover,
    find,
    load_instructions_config,
)
from brigade.selection import Selection
from tests._instruction_fixtures import (
    field_workspace,
    finding_2_tree,
    openclaw_home,
    refuse_subprocess,
    sized,
)


def test_finding_1_user_md_takes_the_loader_cap(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    inv = discover(ws, InstructionsConfig(), home=home, environ=env)
    user = find(inv, "USER.md")
    assert (user.key, user.file_class, user.size) == ("user-md", "append", 8241)
    assert (user.limit, user.limit_source, user.target_budget) == (4000, "loader:openclaw:user-md-fixed-cap", 8000)
    assert (user.act_at, user.trim_to, user.loaders) == (3400, 2800, ("openclaw:main",))
    agents = find(inv, "AGENTS.md")
    assert (agents.limit, agents.limit_source, agents.file_class) == (12000, "target:table", "policy")
    assert agents.loaders == ("codex:project", "openclaw:main")
    assert [(t.loader, t.total, t.cap) for t in inv.totals] == [("openclaw:main", 20441, 120000)]


def test_finding_1_user_md_limit_survives_unreadable_config(tmp_path):  # Review Focus 1
    ws, home, env = field_workspace(tmp_path, config="{ // json5\n}", install=True)
    user = find(discover(ws, InstructionsConfig(), home=home, environ=env), "USER.md")
    assert (user.limit, user.limit_reason) == (4000, "config-not-strict-json")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_finding_2_one_row_per_real_file(tmp_path):
    home, env = openclaw_home(tmp_path, None)
    finding_2_tree(home)
    ws = tmp_path / "ws"
    ws.mkdir()
    rows = [f for f in discover(ws, InstructionsConfig(), home=home, environ=env).files if f.level == "user"]
    assert len(rows) == 1
    row = rows[0]
    assert (row.display, row.key, row.limit, row.limit_source) == (
        "~/.codex/AGENTS.md",
        "codex-agents-md",
        20000,
        "target:default",
    )
    assert row.paths == ("~/.codex/AGENTS.md", "~/.config/opencode/AGENTS.md", "~/.grok/AGENTS.md")
    assert row.loaders == ("codex:user", "grok:user", "opencode:user")


def test_openclaw_loaders_cover_only_the_files_openclaw_loads(tmp_path):  # decision 5
    ws, home, env = field_workspace(tmp_path)
    sized(ws / "CLAUDE.md", 5000)
    sized(ws / "TOOLS.md", 5000)
    inv = discover(ws, InstructionsConfig(), home=home, environ=env)
    assert find(inv, "CLAUDE.md").loaders == () and find(inv, "TOOLS.md").loaders == ()
    assert [(t.loader, t.total) for t in inv.totals] == [("openclaw:main", 20441)]


def test_codex_override_takes_the_project_cap(tmp_path):  # decision 7
    ws, home, env = field_workspace(tmp_path)
    sized(ws / "AGENTS.override.md", 500)
    inv = discover(ws, InstructionsConfig(), home=home, environ=env)
    override = find(inv, "AGENTS.override.md")
    assert (override.loaders, override.file_class, override.limit) == (("codex:project",), "policy", 20000)
    assert find(inv, "AGENTS.md").loaders == ("openclaw:main",)


def test_openclaw_profile_file_is_the_field_workspace_agents_md(tmp_path):  # decision 8
    home, env = openclaw_home(tmp_path, {"agents": {}})
    ws = home / ".openclaw" / "workspace"
    sized(ws / "AGENTS.md", 1000)
    agents = find(discover(ws, InstructionsConfig(), home=home, environ=env), "AGENTS.md")
    assert agents.level == "workspace"
    assert agents.loaders == ("codex:project", "openclaw:main", "openclaw:user")


def test_classes_follow_the_first_matching_rule(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    sized(ws / "MEMORY.md", 100)
    sized(ws / "SOUL.md", 100, head="intro\n<!-- brigade:instructions report -->\n")
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text(
        '[instructions.files."AGENTS.md"]\nclass = "report"\nlimit_bytes = 9000\n'
    )
    inv = discover(ws, load_instructions_config(ws), home=home, environ=env)
    assert {f.display: f.file_class for f in inv.files if f.level == "workspace"} == {
        "AGENTS.md": "report",
        "MEMORY.md": "report",
        "SOUL.md": "report",
        "USER.md": "append",
    }
    assert (find(inv, "AGENTS.md").target_budget, find(inv, "AGENTS.md").target_source) == (9000, "target:config")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_include_missing_lists_absent_per_name_files_but_not_dangling_links(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    (ws / "TOOLS.md").symlink_to(ws / "nowhere.md")
    inv = discover(ws, InstructionsConfig(), home=home, environ=env, include_missing=True)
    assert find(inv, "SOUL.md").exists is False and find(inv, "SOUL.md").size == 0
    assert find(inv, "TOOLS.md") is None


def test_excluded_and_owned_files_are_left_out(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    sized(home / ".cursor" / "plugins/local/brigade-loop/rules/brigade-loop.mdc", 50000)
    inv = discover(ws, InstructionsConfig(exclude=("AGENTS.md",)), home=home, environ=env)
    assert find(inv, "AGENTS.md") is None
    assert not any("brigade-loop.mdc" in f.display for f in inv.files)


def test_odd_files_are_reported_not_dropped(tmp_path):  # Review Focus 4
    ws, home, env = field_workspace(tmp_path)
    (ws / "TOOLS.md").write_bytes(b"\xff\xfe" * 10)
    (ws / "SOUL.md").write_bytes(b"x" * (1024 * 1024 + 1))
    (ws / "IDENTITY.md").write_text("é" * 2500, encoding="utf-8")
    inv = discover(ws, InstructionsConfig(), home=home, environ=env)
    assert find(inv, "TOOLS.md").skip_reason == "not-utf8"
    assert (find(inv, "SOUL.md").skip_reason, find(inv, "SOUL.md").size) == ("over-1-mib", 1024 * 1024 + 1)
    assert find(inv, "IDENTITY.md").size == 5000  # bytes, over its 4000 target


def test_repository_depth_reports_every_file(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    install_selection(ws, Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[]))
    inv = discover(ws, InstructionsConfig(), home=home, environ=env)
    assert inv.repository_depth and {f.file_class for f in inv.files if f.level == "workspace"} == {"report"}


def test_invalid_config_names_the_key(tmp_path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "memory-care.toml").write_text(
        "[instructions]\nact_at_percent = 60\ntrim_to_percent = 70\n"
    )
    with pytest.raises(ValueError, match="invalid instructions.trim_to_percent"):
        load_instructions_config(tmp_path)


def test_discovery_starts_no_subprocess(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    refuse_subprocess(monkeypatch)
    assert discover(ws, InstructionsConfig(), home=home, environ=env).files
