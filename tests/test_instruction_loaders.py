"""Tests for the OpenClaw and Codex instruction loader caps."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from brigade.instruction_loaders import (
    codex_project_cap,
    openclaw_config_path,
    openclaw_install_state_path,
    openclaw_loaders,
    user_md_caps,
)
from tests._instruction_fixtures import HAS_FIFO, deadline, openclaw_home, sized

NO_NONBLOCK = not hasattr(os, "O_NONBLOCK")  # Windows

FIELD = {
    "agents": {
        "defaults": {"bootstrapMaxChars": 40000, "bootstrapTotalMaxChars": 120000},
        "list": [
            {"id": "main"},
            {"id": "lean", "workspace": "~/lean", "bootstrapMaxChars": 3000, "bootstrapTotalMaxChars": 6000},
        ],
    }
}


def test_defaults_workspace_matches_an_agent_without_its_own_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(
        tmp_path,
        {**FIELD, "agents": {**FIELD["agents"], "defaults": {**FIELD["agents"]["defaults"], "workspace": str(ws)}}},
    )
    found = openclaw_loaders(ws, home=home, environ=env)
    assert [(c.loader, c.label, c.cap) for c in found.per_file] == [
        ("openclaw:main", "loader:openclaw:defaults-max-chars", 40000)
    ]
    assert [(c.loader, c.cap) for c in found.totals] == [("openclaw:main", 120000)]
    assert [c.cap for c in user_md_caps(found)] == [4000]


def test_every_agent_without_a_workspace_matches_the_defaults_workspace(tmp_path):
    # Never looser: with no workspace of their own, both agents may load the defaults workspace.
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(
        tmp_path,
        {"agents": {"defaults": {"workspace": str(ws)}, "list": [{"id": "main"}, {"id": "ops"}]}},
    )
    found = openclaw_loaders(ws, home=home, environ=env)
    assert [c.loader for c in found.per_file] == ["openclaw:main", "openclaw:ops"]
    assert [c.loader for c in found.totals] == ["openclaw:main", "openclaw:ops"]


def test_defaults_workspace_with_the_agent_id_appended_matches_that_agent_only(tmp_path):
    base = tmp_path / "ws"
    (base / "ops").mkdir(parents=True)
    home, env = openclaw_home(
        tmp_path,
        {"agents": {"defaults": {"workspace": str(base)}, "list": [{"id": "main"}, {"id": "ops"}]}},
    )
    assert [c.loader for c in openclaw_loaders(base / "ops", home=home, environ=env).per_file] == ["openclaw:ops"]


def test_no_agents_means_implicit_main_with_harness_defaults(tmp_path):
    home, env = openclaw_home(tmp_path, {"agents": {}})
    ws = home / ".openclaw" / "workspace-main"
    ws.mkdir(parents=True)
    found = openclaw_loaders(ws, home=home, environ=env)
    assert [(c.loader, c.label, c.cap) for c in found.per_file] == [
        ("openclaw:main", "loader:openclaw:default-max-chars", 20000)
    ]
    assert [c.cap for c in found.totals] == [60000]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_agent_workspace_matches_by_real_path(tmp_path):  # Review Focus 2
    home, env = openclaw_home(tmp_path, FIELD)
    (home / "lean").mkdir()
    (home / "alias").symlink_to(home / "lean")
    assert [c.loader for c in openclaw_loaders(home / "alias", home=home, environ=env).per_file] == ["openclaw:lean"]
    assert openclaw_loaders(home / "elsewhere", home=home, environ=env).per_file == ()
    no_default, env2 = openclaw_home(tmp_path / "b", {"agents": {"list": [{"id": "x"}]}})
    shared = no_default / ".openclaw" / "workspace"
    shared.mkdir(parents=True)
    assert [c.loader for c in openclaw_loaders(shared, home=no_default, environ=env2).per_file] == ["openclaw:x"]


def test_config_path_and_state_dir_follow_the_environment(tmp_path):
    home = tmp_path / "h"
    assert openclaw_config_path(home, {}) == home / ".openclaw" / "openclaw.json"
    assert openclaw_config_path(home, {"OPENCLAW_STATE_DIR": str(tmp_path / "s")}) == tmp_path / "s" / "openclaw.json"
    assert (
        openclaw_config_path(
            home, {"OPENCLAW_CONFIG_PATH": str(tmp_path / "c.json"), "OPENCLAW_STATE_DIR": str(tmp_path / "s")}
        )
        == tmp_path / "c.json"
    )
    assert openclaw_install_state_path(home) == home / ".openclaw" / "brigade" / "install-state.json"


def test_the_state_dir_moves_the_configuration_and_the_per_agent_workspaces(tmp_path):
    home = tmp_path / "h"
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "openclaw.json").write_text(json.dumps({"agents": {"list": [{"id": "main"}]}}), encoding="utf-8")
    moved = state / "workspace-main"
    moved.mkdir()
    default_place = home / ".openclaw" / "workspace-main"
    default_place.mkdir(parents=True)
    env = {"OPENCLAW_STATE_DIR": str(state)}
    assert [c.loader for c in openclaw_loaders(moved, home=home, environ=env).per_file] == ["openclaw:main"]
    assert openclaw_loaders(default_place, home=home, environ=env).per_file == ()


def test_entries_and_list_merge_and_the_list_wins_key_by_key(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    config = {
        "agents": {
            "entries": {"main": {"workspace": str(ws), "bootstrapMaxChars": 1000, "bootstrapTotalMaxChars": 5000}},
            "list": [{"id": "main", "bootstrapMaxChars": 2000}, {"id": "extra", "workspace": str(ws)}],
        }
    }
    home, env = openclaw_home(tmp_path, config)
    found = openclaw_loaders(ws, home=home, environ=env)
    assert [(c.loader, c.cap) for c in found.per_file] == [("openclaw:extra", 20000), ("openclaw:main", 2000)]
    # The total came from `entries`, and the workspace too: the merge keeps keys the list leaves out.
    assert [(c.loader, c.cap) for c in found.totals] == [("openclaw:extra", 60000), ("openclaw:main", 5000)]


def test_float_caps_are_floored_and_a_float_below_one_is_invalid(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    for value, expected in (("3000.9", (3000, None)), ("0.5", (None, "config-key-invalid"))):
        text = '{"agents": {"list": [{"id": "main", "workspace": %s, "bootstrapMaxChars": %s}]}}' % (
            json.dumps(str(ws)),
            value,
        )
        home, env = openclaw_home(tmp_path / value, text)
        (cap,) = openclaw_loaders(ws, home=home, environ=env).per_file
        assert (cap.cap, cap.reason) == expected


@pytest.mark.parametrize(
    "agents",
    [{"list": [{"name": "no id"}, 3, {"id": 7}]}, {"list": [{"id": ""}]}, {"entries": {"a": 1}}],
)
def test_agents_without_a_usable_id_are_an_invalid_key_not_the_implicit_main(tmp_path, agents):
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(tmp_path, {"agents": agents}, install_workspace=ws)
    found = openclaw_loaders(ws, home=home, environ=env)
    assert found.reason == "config-key-invalid"
    assert [(c.loader, c.reason) for c in found.per_file] == [("openclaw:unknown-agent", "config-key-invalid")]


def test_a_config_that_matches_no_agent_keeps_the_user_md_cap_with_a_reason(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    config = {"agents": {"list": [{"id": "other", "workspace": str(tmp_path / "elsewhere")}]}}
    home, env = openclaw_home(tmp_path, config, install_workspace=ws)
    found = openclaw_loaders(ws, home=home, environ=env)
    assert found.reason == "config-workspace-unmatched"
    assert [(c.loader, c.cap, c.reason) for c in found.per_file] == [
        ("openclaw:unknown-agent", None, "config-workspace-unmatched")
    ]
    assert [c.cap for c in user_md_caps(found)] == [4000]
    # Without an install state nothing says OpenClaw loads this workspace.
    other, env2 = openclaw_home(tmp_path / "o", config)
    quiet = openclaw_loaders(ws, home=other, environ=env2)
    assert (quiet.per_file, quiet.reason) == ((), None)


@pytest.mark.parametrize("workspace", ['"relative/dir"', "7", '""', "null"])
def test_an_unusable_workspace_value_has_its_own_reason(tmp_path, workspace):
    ws = tmp_path / "ws"
    ws.mkdir()
    text = '{"agents": {"list": [{"id": "main", "workspace": %s}]}}' % workspace
    home, env = openclaw_home(tmp_path, text, install_workspace=ws)
    found = openclaw_loaders(ws, home=home, environ=env)
    assert [(c.loader, c.reason) for c in found.per_file] == [("openclaw:unknown-agent", "config-workspace-invalid")]
    assert [c.cap for c in user_md_caps(found)] == [4000]
    other, env2 = openclaw_home(tmp_path / "o", text)
    assert openclaw_loaders(ws, home=other, environ=env2).reason == "config-workspace-invalid"


def test_json5_config_falls_back_but_keeps_user_md_cap(tmp_path):  # Review Focus 1
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(tmp_path, '{ // comment\n "agents": {},\n}', install_workspace=ws)
    found = openclaw_loaders(ws, home=home, environ=env)
    assert found.reason == "config-not-strict-json"
    assert [(c.loader, c.cap, c.reason) for c in found.per_file] == [
        ("openclaw:unknown-agent", None, "config-not-strict-json")
    ]
    assert [c.cap for c in user_md_caps(found)] == [4000]
    other, env3 = openclaw_home(tmp_path / "o", "{ ,}")  # no install state: OpenClaw not a known loader
    assert openclaw_loaders(ws, home=other, environ=env3).per_file == ()


def test_install_state_is_read_under_home_even_with_a_state_dir(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(tmp_path, FIELD, install_workspace=ws)
    found = openclaw_loaders(ws, home=home, environ={"OPENCLAW_STATE_DIR": str(tmp_path / "state")})
    assert found.reason == "config-missing"  # the configuration moved with the state dir
    assert [c.loader for c in found.per_file] == ["openclaw:unknown-agent"]


@pytest.mark.parametrize("value", ["true", '"3000"', "0", "-1", "1e999", "[3000]"])
def test_wrong_type_caps_are_ignored_with_a_reason(tmp_path, value):  # Review Focus 5
    ws = tmp_path / "ws"
    ws.mkdir()
    text = '{"agents": {"list": [{"id": "main", "workspace": %s, "bootstrapMaxChars": %s}]}}' % (
        json.dumps(str(ws)),
        value,
    )  # literal text: 1e999 is strict JSON and parses to inf
    home, env = openclaw_home(tmp_path, text)
    (cap,) = openclaw_loaders(ws, home=home, environ=env).per_file
    assert (cap.cap, cap.reason) == (None, "config-key-invalid")


def test_a_wrong_type_agents_table_is_an_invalid_key(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    bad, env = openclaw_home(tmp_path / "b", {"agents": {"entries": [1]}})
    assert openclaw_loaders(ws, home=bad, environ=env).reason == "config-key-invalid"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_literals_are_not_strict_json(tmp_path, literal):
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(tmp_path, '{"agents": {"defaults": {"bootstrapMaxChars": %s}}}' % literal)
    assert openclaw_loaders(ws, home=home, environ=env).reason == "config-not-strict-json"


def test_oversized_and_missing_configs_are_named(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(tmp_path, None)
    assert openclaw_loaders(ws, home=home, environ=env).reason == "config-missing"
    big, env5 = openclaw_home(tmp_path / "big", '{"x": "' + "a" * (1024 * 1024) + '"}')
    assert openclaw_loaders(ws, home=big, environ=env5).reason == "config-too-large"


def _expect_codex_cap(cap, value):
    """Windows cannot read the Codex config without O_NONBLOCK, so the cap is unavailable there."""
    if NO_NONBLOCK:
        assert (cap.cap, cap.reason) == (None, "config-nonblocking-unavailable")
    else:
        assert (cap.cap, cap.reason) == (value, None)


def test_codex_cap_reads_the_given_home_and_environment(tmp_path):
    home = tmp_path / "h"
    sized(home / ".codex" / "config.toml", 0).write_text("project_doc_max_bytes = 9000\n")
    cap = codex_project_cap(home=home, environ={})
    assert (cap.loader, cap.label) == ("codex:project", "loader:codex:project-doc-max-bytes")
    _expect_codex_cap(cap, 9000)
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "config.toml").write_text("project_doc_max_bytes = 0\n")
    zero = codex_project_cap(home=home, environ={"CODEX_HOME": str(custom)})
    if NO_NONBLOCK:
        _expect_codex_cap(zero, None)
    else:
        assert (zero.cap, zero.reason) == (None, "config-key-invalid")
    # No config file: the default needs no read, on every platform.
    assert codex_project_cap(home=tmp_path / "empty", environ={}).cap == 32768


def test_codex_cap_without_o_nonblock_is_unavailable_with_a_hyphenated_reason(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)  # the Windows case, on any platform
    home = tmp_path / "h"
    sized(home / ".codex" / "config.toml", 0).write_text("project_doc_max_bytes = 9000\n")
    cap = codex_project_cap(home=home, environ={})
    assert (cap.cap, cap.reason) == (None, "config-nonblocking-unavailable")


def test_codex_reasons_use_the_same_hyphenated_style(tmp_path):
    home = tmp_path / "h"
    config = sized(home / ".codex" / "config.toml", 0)
    for text, reason in (
        ("project_doc_max_bytes = [", "config-invalid-toml"),
        ('project_doc_max_bytes = "big"', "config-invalid-project-doc-max-bytes"),
    ):
        config.write_text(text + "\n")
        cap = codex_project_cap(home=home, environ={})
        if NO_NONBLOCK:
            reason = "config-nonblocking-unavailable"
        assert (cap.cap, cap.reason) == (None, reason)


@pytest.mark.skipif(not HAS_FIFO, reason="POSIX FIFOs")
def test_a_fifo_install_state_does_not_block(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(tmp_path, "{ ,}")
    state = openclaw_install_state_path(home)
    state.parent.mkdir(parents=True)
    os.mkfifo(state)
    with deadline():
        found = openclaw_loaders(ws, home=home, environ=env)
    assert (found.per_file, found.reason) == ((), "config-not-strict-json")


@pytest.mark.skipif(not HAS_FIFO, reason="POSIX FIFOs")
def test_a_config_swapped_for_a_fifo_after_the_stat_does_not_block(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(tmp_path, None)
    config = home / ".openclaw" / "openclaw.json"
    config.parent.mkdir(parents=True)
    os.mkfifo(config)
    regular = os.stat_result((stat.S_IFREG | 0o644, 1, 1, 1, 0, 0, 10, 0, 0, 0))
    original = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, **kw: regular if self == config else original(self, **kw))
    with deadline():
        found = openclaw_loaders(ws, home=home, environ=env)
    assert found.reason == "config-unreadable"


def test_an_oversized_install_state_is_not_read_whole(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home, env = openclaw_home(tmp_path, "{ ,}")
    state = openclaw_install_state_path(home)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"workspace": str(ws), "pad": "x" * (1024 * 1024)}))
    assert openclaw_loaders(ws, home=home, environ=env).per_file == ()
