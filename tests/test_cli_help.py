import argparse

import pytest

from brigade import cli


def _subparsers_action(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subparsers action found")


def test_command_groups_cover_every_command_exactly_once():
    parser = cli._build_parser()
    sub = _subparsers_action(parser)
    grouped = [name for _, names in cli.COMMAND_GROUPS for name in names]
    assert len(grouped) == len(set(grouped)), "duplicate command in COMMAND_GROUPS"
    assert set(grouped) == set(sub.choices)


def test_top_level_help_lists_all_commands_and_group_titles():
    parser = cli._build_parser()
    sub = _subparsers_action(parser)
    help_text = parser.format_help()
    for title, _ in cli.COMMAND_GROUPS:
        assert title in help_text
    for name in sub.choices:
        assert name in help_text


def test_top_level_help_has_start_here_block():
    parser = cli._build_parser()
    help_text = parser.format_help()
    assert "Start here:" in help_text
    assert "operator quickstart" in help_text


def test_top_level_help_does_not_dump_flat_subcommand_list():
    parser = cli._build_parser()
    help_text = parser.format_help()
    # The grouped epilog owns the command listing; argparse's default
    # indented dump would repeat every command a second time.
    assert help_text.count("hermes-fragments") == 1
    assert help_text.count("reconfigure") == 1


def test_subcommand_help_still_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["work", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "brief" in out
    assert "tasks" in out


def test_guard_command_delegates_to_embedded_guard(monkeypatch):
    calls = []

    def fake_main(argv=None):
        calls.append(argv)
        return 17

    monkeypatch.setattr("brigade.guard.cli.main", fake_main)

    assert cli.main(["guard", "scan", "-"]) == 17
    assert calls == [["scan", "-"]]


def test_guard_help_prints_group_help_without_delegating(monkeypatch, capsys):
    calls = []

    def fake_main(argv=None):
        calls.append(argv)
        return 0

    monkeypatch.setattr("brigade.guard.cli.main", fake_main)

    assert cli.main(["guard", "--help"]) == 0
    assert calls == []
    out = capsys.readouterr().out
    for name in ("scan", "audit", "git", "commits", "publish-check", "pr", "n8n-validate"):
        assert name in out


def test_guard_no_args_prints_group_help(capsys):
    assert cli.main(["guard"]) == 0
    out = capsys.readouterr().out
    assert "usage: brigade guard" in out
    assert "git" in out
    assert "scan" in out


def test_guard_routes_git_to_git_scan(monkeypatch):
    calls = []

    def fake_main(argv=None):
        calls.append(argv)
        return 5

    monkeypatch.setattr("brigade.guard.git_scan.main", fake_main)

    assert cli.main(["guard", "git", "--all-tracked", "--policy", "p.json"]) == 5
    assert calls == [["--all-tracked", "--policy", "p.json"]]


def test_guard_routes_publish_check(monkeypatch):
    calls = []

    def fake_main(argv=None):
        calls.append(argv)
        return 0

    monkeypatch.setattr("brigade.guard.publish_check.main", fake_main)

    assert cli.main(["guard", "publish-check", "--target", "."]) == 0
    assert calls == [["--target", "."]]
