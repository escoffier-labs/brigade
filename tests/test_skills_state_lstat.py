"""Installed-copy inspection under the skills state root stays anchored (#2677).

The Windows acceptance tests in ``test_skills_state_windows`` forbid any
pathname metadata lookup under ``.brigade``. These tests hold the same line on
every platform, plus the one string normalization link targets need.
"""

from __future__ import annotations

import json

import pytest

from brigade import skills_cmd
from brigade.skills_cmd import registry
from tests.test_skills_cmd import _install_module
from tests.test_skills_state_windows import _fleet_state_path_probes


def _install_bundled_mcp(workspace, capsys):
    assert skills_cmd.install(workspace=workspace, skill="brigade-work", harness="mcp", json_output=True) == 0
    capsys.readouterr()


def test_installed_path_state_classifies_state_root_copy_through_the_anchor(tmp_path, monkeypatch, capsys):
    _install_bundled_mcp(tmp_path, capsys)
    state = tmp_path / ".brigade"
    dest = state / "skills" / "mcp-resources" / "brigade-work"
    install_mod = _install_module()

    with registry._held_state_root(tmp_path, writable=False) as anchor:
        with _fleet_state_path_probes(monkeypatch, state) as probes:
            assert install_mod._installed_path_state(tmp_path, dest, anchor=anchor) == ("dir", None)
            assert install_mod._installed_path_state(tmp_path, dest.with_name("absent"), anchor=anchor) == (
                "missing",
                None,
            )
    assert not probes, f"state-root classification must use the held anchor: {probes}"


def test_fleet_status_inspects_state_backed_copy_without_pathname_probes(tmp_path, monkeypatch, capsys):
    _install_bundled_mcp(tmp_path, capsys)
    state = tmp_path / ".brigade"

    with _fleet_state_path_probes(monkeypatch, state) as probes:
        assert skills_cmd.fleet_status(target=tmp_path, json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)

    assert [(row["skill_id"], row["harness"], row["status"]) for row in payload["copies"]] == [
        ("brigade-work", "mcp", "current")
    ]
    assert not probes, f"fleet inspection must use held descriptors: {probes}"


def test_compatibility_reads_state_backed_copy_without_pathname_probes(tmp_path, monkeypatch, capsys):
    _install_bundled_mcp(tmp_path, capsys)
    state = tmp_path / ".brigade"

    with _fleet_state_path_probes(monkeypatch, state) as probes:
        assert skills_cmd.compatibility(target=tmp_path, skill="brigade-work", json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)

    mcp = next(row for row in payload["adapters"] if row["id"] == "mcp")
    assert mcp["installed"] is True
    assert mcp["installed_state"] == "dir"
    assert not probes, f"compatibility must read state through held descriptors: {probes}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"\\?\D:\a\ws\shared\linked-copy", r"D:\a\ws\shared\linked-copy"),
        (r"\\?\d:\ws\x", r"d:\ws\x"),
        (r"\\?\UNC\srv\share\copy", r"\\srv\share\copy"),
        (r"D:\a\ws\shared\linked-copy", r"D:\a\ws\shared\linked-copy"),
        (r"\\srv\share\copy", r"\\srv\share\copy"),
        ("../shared/linked-copy", "../shared/linked-copy"),
        ("/tmp/outside", "/tmp/outside"),
    ],
)
def test_link_target_drops_the_extended_length_prefix(tmp_path, monkeypatch, raw, expected):
    install_mod = _install_module()
    assert install_mod._plain_link_target(raw) == expected

    monkeypatch.setattr(install_mod.os, "readlink", lambda _path: raw)
    assert install_mod._link_target(tmp_path / "linked") == expected
