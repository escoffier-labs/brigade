"""Tests for the OpenClaw and Codex instruction loader caps."""

from __future__ import annotations

import json
import os

import pytest

from brigade.instruction_loaders import (
    codex_project_cap,
    openclaw_config_path,
    openclaw_install_state_path,
    openclaw_loaders,
    user_md_caps,
)
from tests._instruction_fixtures import openclaw_home, sized

FIELD = {
    "agents": {
        "defaults": {"bootstrapMaxChars": 40000, "bootstrapTotalMaxChars": 120000},
        "list": [
            {"id": "main"},
            {"id": "lean", "workspace": "~/lean", "bootstrapMaxChars": 3000, "bootstrapTotalMaxChars": 6000},
        ],
    }
}


def test_defaults_workspace_matches_main_only(tmp_path):
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
    bad, env4 = openclaw_home(tmp_path / "b", {"agents": {"entries": [1]}})
    assert openclaw_loaders(ws, home=bad, environ=env4).reason == "config-key-invalid"


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


def test_codex_cap_reads_the_given_home_and_environment(tmp_path):
    home = tmp_path / "h"
    sized(home / ".codex" / "config.toml", 0).write_text("project_doc_max_bytes = 9000\n")
    cap = codex_project_cap(home=home, environ={})
    assert (cap.loader, cap.label, cap.cap) == ("codex:project", "loader:codex:project-doc-max-bytes", 9000)
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "config.toml").write_text("project_doc_max_bytes = 0\n")
    zero = codex_project_cap(home=home, environ={"CODEX_HOME": str(custom)})
    assert (zero.cap, zero.reason) == (None, "config-key-invalid")
    assert codex_project_cap(home=tmp_path / "empty", environ={}).cap == 32768
