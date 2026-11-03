"""The CLI builds only the selected command family; everything else must look unchanged."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys

import pytest

from brigade import cli
from brigade.cli import _index


def _sub(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def _top_level_rows(parser: argparse.ArgumentParser) -> list[tuple[str, str | None, str | None]]:
    return [(action.dest, action.metavar, action.help) for action in _sub(parser)._choices_actions]


def _parse_failure(parser: argparse.ArgumentParser, argv: list[str]) -> tuple[object, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)
    return exc.value.code, err.getvalue()


@pytest.fixture(params=["1", "0"], ids=["extras-on", "extras-off"])
def extras(request, monkeypatch):
    monkeypatch.setenv("BRIGADE_EXTRAS", request.param)
    return request.param == "1"


def test_index_matches_every_real_top_level_registration(extras):
    full = cli._build_parser()
    lazy = cli._build_parser_for([])
    assert _top_level_rows(lazy) == _top_level_rows(full)
    assert list(_sub(lazy).choices) == list(_sub(full).choices)
    assert [entry.name for entry in _index.COMMANDS] == [action.dest for action in _sub(full)._choices_actions]


def test_top_level_help_and_invalid_choice_are_unchanged(extras):
    full = cli._build_parser()
    lazy = cli._build_parser_for(["--help"])
    assert lazy.format_help() == full.format_help()
    assert lazy.format_usage() == full.format_usage()
    assert _parse_failure(lazy, ["nosuchcmd"]) == _parse_failure(full, ["nosuchcmd"])
    assert _parse_failure(cli._build_parser_for([]), []) == _parse_failure(full, [])


def test_each_family_builds_the_same_subtree_alone(extras):
    full_choices = _sub(cli._build_parser()).choices
    for entry in _index.COMMANDS:
        lazy_choices = _sub(cli._build_parser_for([entry.name, "--help"])).choices
        assert lazy_choices[entry.name].format_help() == full_choices[entry.name].format_help(), entry.name
        for alias in entry.aliases:
            assert lazy_choices[alias] is lazy_choices[entry.name]


def test_alias_selects_the_owning_family():
    parser = cli._build_parser_for(["stations", "list"])
    assert parser.parse_args(["stations", "list"]).command == "stations"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], None),
        (["--help"], None),
        (["--version"], None),
        (["memory", "search", "x"], "memory"),
        (["-h", "guard"], "guard"),
        (["--", "init"], "init"),
        (["stations"], "stations"),
        (["memroy", "search"], None),
        (["notify-probe"], None),
    ],
)
def test_selected_command(argv, expected):
    assert cli._selected_command(argv) == expected


def test_disabled_extras_family_still_gets_the_stub(monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "0")
    assert cli.main(["pantry", "--help"]) == 2
    assert "brigade extras on" in capsys.readouterr().err


def test_historical_package_attributes_resolve_lazily():
    from brigade.cli import fleet, init

    assert cli._init_group is init
    assert cli.fleet is fleet
    assert cli.run_cloud.__name__ == "brigade.cli.run_cloud"
    from brigade.prompt import prompt_for_selection

    assert cli.prompt_for_selection is prompt_for_selection
    with pytest.raises(AttributeError):
        cli._no_such_group  # noqa: B018


def test_completions_still_walk_the_full_tree(capsys):
    assert cli.main(["completions", "bash"]) == 0
    out = capsys.readouterr().out
    assert '["brigade fleet msg"]=' in out
    assert '["brigade memory"]=' in out
    assert '["brigade work verify"]=' in out


_PROBE = r"""
import contextlib, io, json, sys
from brigade import cli
argv = json.loads(sys.argv[1])
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    try:
        cli.main(argv)
    except SystemExit:
        pass
print(json.dumps(sorted(m for m in sys.modules if m.startswith("brigade"))))
"""


def _loaded_modules(argv: list[str], tmp_path) -> set[str]:
    env = {**os.environ, "HOME": str(tmp_path), "BRIGADE_NO_UPDATE_CHECK": "1", "BRIGADE_EXTRAS": "1"}
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(argv)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=tmp_path,
    )
    return set(json.loads(result.stdout))


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["nosuchcmd"]])
def test_top_level_paths_import_no_command_family(argv, tmp_path):
    loaded = _loaded_modules(argv, tmp_path)
    families = {f"brigade.cli.{entry.module}" for entry in _index.COMMANDS}
    assert not loaded & families
    assert "brigade.work_cmd" not in loaded


def test_selected_family_imports_only_itself(tmp_path):
    scan = tmp_path / "scan.txt"
    scan.write_text("hello\n")
    loaded = _loaded_modules(["guard", "scan", str(scan)], tmp_path)
    families = {f"brigade.cli.{entry.module}" for entry in _index.COMMANDS}
    assert loaded & families == {"brigade.cli.guard"}
    assert "brigade.work_cmd" not in loaded


def test_memory_search_skips_the_work_command_facade(tmp_path):
    loaded = _loaded_modules(["memory", "search", "x", "--target", str(tmp_path)], tmp_path)
    assert "brigade.cli.memory" in loaded
    assert "brigade.work_cmd" not in loaded
