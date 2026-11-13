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


@pytest.mark.parametrize(
    "suffix",
    [
        [name]
        for name in (
            "enqueue",
            "feed",
            "scout-feed",
            "build-feed",
            "reconcile-reports",
            "reconcile-findings",
            "convert-findings",
            "relay-findings",
            "claim",
            "renew",
            "start",
            "fail",
            "ack-cancel",
            "complete",
            "cancel",
            "expire",
            "status",
            "setup",
            "doctor",
            "canary",
            "install-service",
        )
    ]
    + [["pack", name] for name in ("relay-setup", "relay-doctor", "relay", "install-relay-service")],
)
def test_removed_queue_commands_preserve_populated_store(tmp_path, monkeypatch, capsys, suffix):
    import sqlite3
    from tests._home import set_home

    set_home(monkeypatch, tmp_path / "home")
    target = tmp_path / "target"
    target.mkdir()
    database = target / "retained.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE retained (body TEXT)")
        conn.execute("INSERT INTO retained VALUES ('keep this row')")
    (target / "config.json").write_text('{"retained": true}', encoding="utf-8")
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    assert cli.main(["run", "cloud", "grokbot", *suffix, "--target", str(target), "--obsolete", "--apply"]) == 2
    error = capsys.readouterr().err
    assert f"this was removed in {REMOVED_IN}." in error
    assert "usage:" not in error and "hub connector" in error
    assert "brigade-grokbot-findings-relay.timer" in error
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


@pytest.mark.parametrize("flag", ["--pack", "--pac"])
@pytest.mark.parametrize("attached", [True, False])
def test_operations_relay_removal_accepts_old_flags(flag, attached, capsys):
    selected = [flag + "=operations-relay"] if attached else [flag, "operations-relay"]
    assert cli.main(["run", "cloud", "grokbot", "serve", *selected, "--obsolete"]) == 2
    error = capsys.readouterr().err
    assert "brigade-grokbot-operations-relay.service" in error
    assert "usage:" not in error


@pytest.mark.parametrize("profile", ["operations-relay", "operator", "repository-scout", "implementation-worker"])
@pytest.mark.parametrize("command", ["setup", "show", "doctor", "canary", "update", "remove", "install-service"])
@pytest.mark.parametrize("flag", ["--id", "--i"])
def test_retired_queue_pack_paths(profile, command, flag, capsys):
    assert cli.main(["run", "cloud", "grokbot", "pack", command, flag + "=" + profile, "--obsolete"]) == 2
    error = capsys.readouterr().err
    assert f"this was removed in {REMOVED_IN}." in error
    assert "usage:" not in error
    assert "brigade-grokbot-" + profile + ".service" in error


def test_retired_enrollment_cli(capsys):
    assert cli.main(["fleet", "grokbot", "enroll-actor", "--obsolete"]) == 2
    assert f"this was removed in {REMOVED_IN}." in capsys.readouterr().err


@pytest.mark.parametrize("role", ["operator", "repository-scout", "implementation-worker"])
@pytest.mark.parametrize("flag", ["--instance", "--inst"])
def test_retired_listener_roles(role, flag, capsys):
    assert cli.main(["run", "cloud", "grokbot", "serve", flag + "=" + role, "--obsolete"]) == 2
    error = capsys.readouterr().err
    assert f"this was removed in {REMOVED_IN}." in error
    assert "brigade-grokbot-" + role + ".service" in error
    assert "usage:" not in error


@pytest.mark.parametrize("suffix", [[], ["serve"]])
def test_surviving_legacy_family_help(suffix, capsys):
    from pathlib import Path

    with pytest.raises(SystemExit) as exit:
        cli.main(["run", "cloud", "grokbot", *suffix, "--help"])
    assert exit.value.code == 0
    output = capsys.readouterr()
    assert "this was removed" not in output.err
    if suffix:
        expected = (Path(__file__).parent / "fixtures/grokbot_serve_help.txt").read_text(encoding="utf-8")
        assert output.out == expected
    else:
        assert "pack" in output.out and "serve" in output.out


@pytest.mark.parametrize("suffix", [["enqueue"], ["serve"], ["pack", "show", "--id", "operator"]])
def test_stop_instructions_cover_client_scoped_units(suffix, capsys):
    import fnmatch
    import shlex
    from brigade.dot.service_render import unit_name

    assert cli.main(["run", "cloud", "grokbot", *suffix]) == 2
    message = capsys.readouterr().err
    commands = [shlex.split(line) for line in message.splitlines() if line.startswith("systemctl ")]
    arguments = shlex.split(message.split("systemctl ", 1)[1].splitlines()[0])
    assert [command[:3] for command in commands] == [
        ["systemctl", "--user", "stop"],
        ["systemctl", "--user", "list-unit-files"],
    ]
    assert arguments[:3] == ["--user", "disable", "--now"]
    roles = ["operator"] if suffix[0] == "pack" else ["operator", "repository-scout", "implementation-worker"]
    for role in roles:
        assert unit_name(role) in arguments
        pattern = f"brigade-grokbot-{role}-*.service"
        assert pattern not in arguments
        assert all(pattern in command[3:] for command in commands)
        assert fnmatch.fnmatchcase(unit_name(role, "client-a"), pattern)


def test_listener_install_hint_names_connector_extra(tmp_path, monkeypatch, capsys):
    from brigade import dot
    from brigade.dot.transport import OptionalDependencyError
    from tests._home import set_home

    set_home(monkeypatch, tmp_path / "home")

    def missing_dependency(*args, **kwargs):
        raise OptionalDependencyError("fixture missing SDK")

    monkeypatch.setattr(dot, "build_listener_from_target", missing_dependency)
    assert cli.main(["run", "cloud", "grokbot", "serve", "--pack", "dot", "--target", str(tmp_path)]) == 2
    message = capsys.readouterr().err
    assert "pip install brigade-cli[connector]" in message
    assert "grokbot extra remains an alias during the removal window" in message
