"""Retired profile argv must stop before imports, credentials or target access."""

import socket
import subprocess
import sys

import pytest

from brigade import cli
from brigade.cli.removed_commands import REMOVED_IN


@pytest.mark.parametrize("profile", ["grokbot-memory", "grokbot-inbox"])
@pytest.mark.parametrize("command", ["setup", "doctor", "canary", "update", "install-service", "remove", "show"])
def test_retired_pack_flags_reach_tombstone(tmp_path, monkeypatch, capsys, profile, command):
    target = tmp_path / "target"
    target.mkdir()
    data = target / "retained.json"
    data.write_text('{"retained": true}')
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("network"))
    code = cli.main(
        [
            "run",
            "cloud",
            "grokbot",
            "pack",
            command,
            "--id",
            profile,
            "--target",
            str(target),
            "--historical-extra",
            "unused",
            "--apply",
        ]
    )
    assert code == 2
    output = capsys.readouterr()
    assert f"this was removed in {REMOVED_IN}." in output.err and "hub connector" in output.err
    assert "invalid choice" not in output.err
    assert {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "profile", ["grokbot-memory", "grokbot-inbox", "fleet-steward", "obsidian-operator", "t3-dispatch"]
)
def test_retired_serve_never_imports_implementation(profile):
    code = """
import sys
from brigade import cli
class Block:
    def find_spec(self, fullname, *args):
        if fullname.startswith(("brigade.dot", "brigade.grokbot", "mcp")):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
raise SystemExit(cli.main(sys.argv[1:]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            "run",
            "cloud",
            "grokbot",
            "serve",
            "--pack=" + profile,
            "--bearer-file",
            "missing",
            "--extra",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert f"this was removed in {REMOVED_IN}." in result.stderr and "hub connector" in result.stderr


@pytest.mark.parametrize("profile", ["grokbot-memory", "grokbot-inbox"])
def test_retired_runtime_and_profile_are_unavailable(profile):
    from brigade.dot.contracts import DotError
    from brigade.dot.profile import profile_for
    from brigade.dot.runtime_config import parse_runtime

    with pytest.raises(DotError, match="invalid_config"):
        profile_for(profile)
    with pytest.raises(DotError, match="invalid_config"):
        parse_runtime({}, schema=f"brigade.{profile}.runtime.v1")


@pytest.mark.parametrize("profile", ["grokbot-memory", "grokbot-inbox"])
def test_retired_module_flags_reach_tombstone(profile, capsys):
    from brigade.dot.__main__ import main

    assert main(["watch", "--pack", profile, "--old-flag"]) == 2
    assert f"this was removed in {REMOVED_IN}." in capsys.readouterr().err


@pytest.mark.parametrize("abbreviated", [False, True])
@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("profile", ["fleet-steward", "obsidian-operator", "t3-dispatch"])
@pytest.mark.parametrize("command", ["setup", "doctor", "canary", "update", "install-service", "remove", "show"])
def test_retired_steward_commands_preserve_user_files(
    tmp_path, monkeypatch, capsys, profile, command, abbreviated, attached
):
    from tests._home import set_home

    set_home(monkeypatch, tmp_path / "home")
    target = tmp_path / "target"
    target.mkdir()
    retained = target / "state.json"
    retained.write_text('{"retained": true}')
    before = retained.read_bytes()
    for prefix, flag in (
        (["run", "cloud", "grokbot", "pack", command], "--id"),
        (["run", "cloud", "grokbot", "serve"], "--pack"),
        (["hub", "connector", command], "--id"),
        (["dot", "watch"], "--pack"),
    ):
        if abbreviated:
            flag = "--pac" if flag == "--pack" else "--i"
        selection = [f"{flag}={profile}"] if attached else [flag, profile]
        assert cli.main([*prefix, *selection, "--target", str(target), "--obsolete", "--apply"]) == 2
        error = capsys.readouterr().err
        assert f"this was removed in {REMOVED_IN}." in error
        assert "hub connector setup --id cloud-agent" in error
        assert "systemctl --user disable --now" in error
        assert "usage:" not in error
        assert retained.read_bytes() == before
        assert sorted(p.name for p in target.iterdir()) == ["state.json"]


@pytest.mark.parametrize("command", ["watch", "intake", "install-watch", "install-intake"])
def test_retired_t3_module_commands(command):
    result = subprocess.run(
        [sys.executable, "-m", "brigade.grokbot_t3", command, "--old-flag"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert f"this was removed in {REMOVED_IN}." in result.stderr
    assert "brigade-grokbot-t3-watch.timer" in result.stderr
    assert "brigade-grokbot-t3-intake.timer" in result.stderr
