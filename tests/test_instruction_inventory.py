"""Tests for the instruction-file inventory."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brigade.install import install_selection
from brigade.instruction_inventory import (
    FileOverride,
    InstructionsConfig,
    discover,
    find,
    load_instructions_config,
)
from brigade.selection import Selection
from tests._instruction_fixtures import (
    HAS_FIFO,
    deadline,
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


def test_owned_files_are_left_out(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    sized(home / ".cursor" / "plugins/local/brigade-loop/rules/brigade-loop.mdc", 50000)
    inv = discover(ws, InstructionsConfig(), home=home, environ=env)
    assert not any("brigade-loop.mdc" in f.display for f in inv.files)


def test_an_excluded_file_stays_as_a_marked_row_and_still_counts(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    inv = discover(ws, InstructionsConfig(exclude=("AGENTS.md",)), home=home, environ=env)
    row = find(inv, "AGENTS.md")
    assert row is not None
    assert (row.excluded, row.exclusion_source) == (True, "config:instructions.exclude")
    assert (row.file_class, row.class_source) == ("report", "excluded")
    assert row.size == 12200 and row.limit == 12000  # still measured against its limit
    # The harness loads it either way, so the loader total keeps its bytes.
    assert [(t.loader, t.total) for t in inv.totals] == [("openclaw:main", 20441)]
    assert find(inv, "USER.md").excluded is False


def test_exclusion_by_absolute_path_marks_the_same_row(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    inv = discover(ws, InstructionsConfig(exclude=(str(ws / "USER.md"),)), home=home, environ=env)
    assert find(inv, "USER.md").excluded is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_excluding_one_alias_of_a_symlinked_file_marks_the_row(tmp_path):
    home, env = openclaw_home(tmp_path, None)
    finding_2_tree(home)
    ws = tmp_path / "ws"
    ws.mkdir()
    inv = discover(ws, InstructionsConfig(exclude=("~/.grok/AGENTS.md",)), home=home, environ=env)
    (row,) = [f for f in inv.files if f.level == "user"]
    assert (row.excluded, row.exclusion_source) == (True, "config:instructions.exclude")
    assert row.paths == ("~/.codex/AGENTS.md", "~/.config/opencode/AGENTS.md", "~/.grok/AGENTS.md")


def test_an_override_records_what_was_requested_beside_what_applies(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    config = InstructionsConfig(files=(("AGENTS.md", FileOverride("report", 9000)),))
    row = find(discover(ws, config, home=home, environ=env), "AGENTS.md")
    assert (row.requested_class, row.requested_limit_bytes) == ("report", 9000)
    assert (row.requested_source, row.class_source, row.override_reason) == (
        "config:instructions.files",
        "config",
        None,
    )
    assert (row.target_budget, row.target_source, row.limit) == (9000, "target:config", 9000)
    plain = find(discover(ws, InstructionsConfig(), home=home, environ=env), "AGENTS.md")
    assert (plain.requested_class, plain.requested_limit_bytes, plain.requested_source) == (None, None, None)
    assert plain.class_source == "default"


def test_a_configured_limit_can_only_lower_never_raise_above_a_loader_cap(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    files = (
        ("USER.md", FileOverride(None, 50000)),
        ("AGENTS.md", FileOverride(None, 50000)),
        ("SOUL.md", FileOverride(None, 1000)),
    )
    sized(ws / "SOUL.md", 10)
    inv = discover(ws, InstructionsConfig(files=files), home=home, environ=env)
    user, agents, soul = find(inv, "USER.md"), find(inv, "AGENTS.md"), find(inv, "SOUL.md")
    assert (user.limit, user.limit_source, user.requested_limit_bytes) == (
        4000,
        "loader:openclaw:user-md-fixed-cap",
        50000,
    )
    assert (agents.limit, agents.limit_source) == (32768, "loader:codex:project-doc-max-bytes")
    assert (soul.limit, soul.limit_source) == (1000, "target:config")


def test_an_override_outside_the_target_and_home_is_rejected_and_listed(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    outside = sized(tmp_path / "outside" / "notes.md", 300)
    sized(tmp_path / "outside" / "other.md", 300)
    files = (
        (str(outside), FileOverride("append", 100)),  # absolute and not a discovered file
        ("../outside/other.md", FileOverride("policy", 50)),  # relative, but outside both
    )
    inv = discover(ws, InstructionsConfig(files=files), home=home, environ=env)
    (row,) = [f for f in inv.files if f.real_path == outside.resolve()]
    assert (row.file_class, row.class_source) == ("report", "rejected-override")
    assert row.override_reason == "absolute-path-not-discovered"
    assert (row.requested_class, row.requested_limit_bytes) == ("append", 100)  # asked, not applied
    assert (row.target_budget, row.target_source) == (20000, "target:default")
    assert row.display == "<external>/notes.md" and str(tmp_path) not in row.display
    assert {(n.reason, n.action) for n in inv.override_notes} == {
        ("absolute-path-not-discovered", "forced-report"),
        ("outside-target-and-home", "forced-report"),
    }
    assert all(str(tmp_path) not in n.name for n in inv.override_notes)


def test_a_relative_override_outside_both_is_rejected_on_its_own(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    sized(tmp_path / "outside" / "notes.md", 300)
    config = InstructionsConfig(files=(("../outside/notes.md", FileOverride("append", None)),))
    inv = discover(ws, config, home=home, environ=env)
    (row,) = [f for f in inv.files if f.override_reason]
    assert (row.override_reason, row.file_class) == ("outside-target-and-home", "report")
    assert [(n.reason, n.action) for n in inv.override_notes] == [("outside-target-and-home", "forced-report")]


def test_an_absolute_path_to_a_discovered_file_and_inside_paths_are_accepted(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    sized(ws / "notes" / "extra.md", 100)
    sized(home / "extra-home.md", 100)
    files = (
        (str(ws / "AGENTS.md"), FileOverride("report", None)),  # already discovered
        ("notes/extra.md", FileOverride("append", 500)),  # inside the target
        ("~/extra-home.md", FileOverride("append", 600)),  # inside home
    )
    inv = discover(ws, InstructionsConfig(files=files), home=home, environ=env)
    assert inv.override_notes == ()
    assert find(inv, "AGENTS.md").file_class == "report"
    extra = find(inv, "notes/extra.md")
    assert (extra.level, extra.file_class, extra.limit, extra.override_reason) == ("config", "append", 500, None)
    assert find(inv, "~/extra-home.md").limit == 600


def test_an_override_for_no_file_is_listed_as_ignored(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    config = InstructionsConfig(files=(("notes/missing.md", FileOverride("append", 10)),))
    inv = discover(ws, config, home=home, environ=env)
    assert [(n.name, n.reason, n.action) for n in inv.override_notes] == [
        ("notes/missing.md", "matches-no-file", "ignored")
    ]


def test_bootstrap_md_is_discovered_capped_and_counted(tmp_path):  # a file OpenClaw loads
    ws = tmp_path / "ws"
    sized(ws / "BOOTSTRAP.md", 200)
    config = {"agents": {"defaults": {"workspace": str(ws), "bootstrapTotalMaxChars": 100}}}
    home, env = openclaw_home(tmp_path, config)
    inv = discover(ws, InstructionsConfig(), home=home, environ=env)
    row = find(inv, "BOOTSTRAP.md")
    assert (row.loaders, row.size, row.level) == (("openclaw:main",), 200, "workspace")
    assert (row.limit, row.limit_source) == (20000, "loader:openclaw:default-max-chars")  # a tie goes to the loader
    assert [(t.loader, t.total, t.cap) for t in inv.totals] == [("openclaw:main", 200, 100)]
    explicit = InstructionsConfig(files=(("BOOTSTRAP.md", FileOverride(None, 150)),))
    again = find(discover(ws, explicit, home=home, environ=env), "BOOTSTRAP.md")
    assert (again.loaders, again.limit) == (("openclaw:main",), 150)


def test_the_user_md_cap_is_for_the_workspace_user_md_only(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    sized(ws / "notes" / "USER.md", 6000)
    config = InstructionsConfig(files=(("notes/USER.md", FileOverride(None, None)),))
    inv = discover(ws, config, home=home, environ=env)
    stray = find(inv, "notes/USER.md")
    assert (stray.loaders, stray.caps) == ((), ())
    assert (stray.limit, stray.limit_source) == (20000, "target:default")
    assert find(inv, "USER.md").limit == 4000


def test_user_md_keeps_its_cap_when_the_config_matches_no_agent(tmp_path):
    other = {"agents": {"list": [{"id": "other", "workspace": "/nowhere/else"}]}}
    ws, home, env = field_workspace(tmp_path, config=other, install=True)
    user = find(discover(ws, InstructionsConfig(), home=home, environ=env), "USER.md")
    assert (user.limit, user.limit_source, user.limit_reason) == (
        4000,
        "loader:openclaw:user-md-fixed-cap",
        "config-workspace-unmatched",
    )


def test_codex_cap_unavailable_falls_back_to_the_target_budget_with_a_reason(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    sized(home / ".codex" / "config.toml", 0).write_text("project_doc_max_bytes = 5000\n")
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)  # as on Windows
    agents = find(discover(ws, InstructionsConfig(), home=home, environ=env), "AGENTS.md")
    assert (agents.limit, agents.limit_source) == (12000, "target:table")
    assert agents.limit_reason == "config-nonblocking-unavailable"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_root_file_linked_to_a_user_level_file_is_one_workspace_row(tmp_path):
    home, env = openclaw_home(tmp_path, {"agents": {}})
    real = sized(home / ".codex" / "AGENTS.md", 500)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").symlink_to(real)
    rows = discover(ws, InstructionsConfig(), home=home, environ=env).files
    (row,) = rows
    assert (row.level, row.key, row.display) == ("workspace", "agents-md", "~/.codex/AGENTS.md")
    assert row.paths == ("AGENTS.md", "~/.codex/AGENTS.md")
    assert row.loaders == ("codex:project", "codex:user")
    # From another workspace the same file is only reached at user level.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (user_row,) = discover(elsewhere, InstructionsConfig(), home=home, environ=env).files
    assert (user_row.level, user_row.key, user_row.loaders) == ("user", "codex-agents-md", ("codex:user",))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_symlink_target_outside_target_and_home_shows_a_marker_and_the_name_only(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    real = sized(tmp_path / "private-dir" / "real-agents.md", 700)
    (ws / "AGENTS.md").unlink()
    (ws / "AGENTS.md").symlink_to(real)
    row = find(discover(ws, InstructionsConfig(), home=home, environ=env), "AGENTS.md")
    assert (row.display, row.paths) == ("<external>/real-agents.md", ("AGENTS.md",))
    assert row.real_path == real.resolve()  # code that needs the path still has it
    assert "private-dir" not in row.display


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_symlinked_target_directory_displays_relative_paths(tmp_path):
    real_ws = tmp_path / "real-ws"
    sized(real_ws / "AGENTS.md", 300)
    link = tmp_path / "link-ws"
    link.symlink_to(real_ws)
    home, env = openclaw_home(tmp_path, {"agents": {}})
    inv = discover(link, InstructionsConfig(), home=home, environ=env)
    assert [(f.display, f.level, f.size) for f in inv.files] == [("AGENTS.md", "workspace", 300)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_symlink_loop_is_dropped_without_raising(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    (ws / "SOUL.md").symlink_to(ws / "SOUL.md")
    for include_missing in (False, True):
        inv = discover(ws, InstructionsConfig(), home=home, environ=env, include_missing=include_missing)
        assert find(inv, "SOUL.md") is None


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
def test_hard_links_to_one_file_are_one_row(tmp_path):
    home, env = openclaw_home(tmp_path, {"agents": {}})
    real = sized(home / ".codex" / "AGENTS.md", 500)
    ws = tmp_path / "ws"
    ws.mkdir()
    os.link(real, ws / "AGENTS.md")
    (row,) = discover(ws, InstructionsConfig(), home=home, environ=env).files
    assert row.size == 500 and len(row.paths) == 2
    assert "codex:project" in row.loaders and "codex:user" in row.loaders


@pytest.mark.skipif(not HAS_FIFO, reason="POSIX FIFOs")
def test_a_file_swapped_for_a_fifo_after_discovery_does_not_block(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    os.mkfifo(ws / "SOUL.md")
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: True if self.name == "SOUL.md" else original(self))
    with deadline():
        inv = discover(ws, InstructionsConfig(), home=home, environ=env)
    soul = find(inv, "SOUL.md")
    assert (soul.skip_reason, soul.size) == ("not-regular", 0)


@pytest.mark.skipif(not HAS_FIFO, reason="POSIX FIFOs")
def test_a_fifo_at_a_candidate_path_is_not_a_row(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    os.mkfifo(ws / "SOUL.md")
    with deadline():
        assert find(discover(ws, InstructionsConfig(), home=home, environ=env), "SOUL.md") is None


def _care_config(ws: Path) -> Path:
    path = ws / ".brigade" / "memory-care.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_an_oversized_instructions_config_is_the_documented_error(tmp_path):
    _care_config(tmp_path).write_text("# " + "x" * (1024 * 1024) + "\n[instructions]\nenabled = true\n")
    with pytest.raises(ValueError, match="invalid instructions: .*over 1 MiB"):
        load_instructions_config(tmp_path)


def test_a_non_utf8_instructions_config_is_the_documented_error_without_its_bytes(tmp_path):
    _care_config(tmp_path).write_bytes(b"[instructions]\nexclude = ['\xff\xfe']\n")
    with pytest.raises(ValueError, match="invalid instructions: .*not UTF-8") as info:
        load_instructions_config(tmp_path)
    assert "0xff" not in str(info.value)


def test_a_directory_at_the_config_path_is_the_documented_error(tmp_path):
    _care_config(tmp_path).mkdir()
    with pytest.raises(ValueError, match="invalid instructions: .*not a regular file"):
        load_instructions_config(tmp_path)


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs a non-root POSIX user"
)
def test_an_unreadable_instructions_config_is_the_documented_error(tmp_path):
    path = _care_config(tmp_path)
    path.write_text("[instructions]\nenabled = true\n")
    path.chmod(0)
    try:
        with pytest.raises(ValueError, match="invalid instructions: .*could not be read"):
            load_instructions_config(tmp_path)
    finally:
        path.chmod(0o600)


@pytest.mark.skipif(not HAS_FIFO, reason="POSIX FIFOs")
def test_a_fifo_or_swapped_instructions_config_does_not_block(tmp_path, monkeypatch):
    path = _care_config(tmp_path)
    os.mkfifo(path)
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: True if self.name == "memory-care.toml" else original(self))
    with deadline(), pytest.raises(ValueError, match="invalid instructions: .*not a regular file"):
        load_instructions_config(tmp_path)


def test_discover_uses_the_defaults_when_the_care_config_cannot_be_read(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    path = _care_config(ws)
    path.write_text("# " + "x" * (1024 * 1024) + "\n")
    inv = discover(ws, InstructionsConfig(), home=home, environ=env)  # must not raise
    assert find(inv, "USER.md").limit == 4000
    path.unlink()
    path.mkdir()
    assert find(discover(ws, InstructionsConfig(), home=home, environ=env), "USER.md").limit == 4000


def test_odd_files_are_reported_not_dropped(tmp_path):  # Review Focus 4
    ws, home, env = field_workspace(tmp_path)
    (ws / "TOOLS.md").write_bytes(b"\xff\xfe" * 10)
    (ws / "SOUL.md").write_bytes(b"x" * (1024 * 1024 + 1))
    (ws / "IDENTITY.md").write_text("é" * 2500, encoding="utf-8")
    inv = discover(ws, InstructionsConfig(), home=home, environ=env)
    assert find(inv, "TOOLS.md").skip_reason == "not-utf8"
    assert (find(inv, "SOUL.md").skip_reason, find(inv, "SOUL.md").size) == ("over-1-mib", 1024 * 1024 + 1)
    identity = find(inv, "IDENTITY.md")
    assert (identity.size, identity.target_budget, identity.limit) == (5000, 4000, 4000)  # bytes, over its 4000 target


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
