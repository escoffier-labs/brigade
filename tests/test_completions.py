"""Tests for `brigade completions` (issue #89)."""

from __future__ import annotations

from brigade import cli, completions


def test_command_tree_has_nested_paths():
    tree = completions._command_tree()
    assert "operator" in tree["brigade"]
    assert "completions" in tree["brigade"]
    # nested one level deep (operator subcommands, including the new checkup)
    assert "doctor" in tree["brigade operator"]
    assert "checkup" in tree["brigade operator"]


def test_bash_script_walks_the_tree():
    script = completions.bash_script()
    assert "complete -F _brigade_complete brigade" in script
    assert '["brigade operator"]=' in script


def test_zsh_script_enables_bash_compat():
    assert "bashcompinit" in completions.zsh_script()


def test_fish_script_uses_complete():
    script = completions.fish_script()
    assert "complete -c brigade" in script
    assert "__fish_use_subcommand" in script


def test_completions_cli_emits_script(capsys):
    assert cli.main(["completions", "bash"]) == 0
    out = capsys.readouterr().out
    assert "_brigade_complete" in out


def test_completions_cli_rejects_unknown_shell():
    # argparse choices reject an unknown shell with exit code 2
    try:
        cli.main(["completions", "powershell"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("expected SystemExit for unknown shell")
