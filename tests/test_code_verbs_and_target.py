"""Tests for the expanded `brigade code` passthrough surface.

Covers the engine verbs beyond sync/context/impact, the standard `--target`
flag mapping onto the engine's working directory, and the rebranding of engine
usage text so the facade never prints `Usage: graphtrail` at users.
"""

from __future__ import annotations

import json

import pytest

from brigade import cli, completions, context_cmd, proc
from brigade.cli.code import ENGINE_VERBS

NEW_VERBS = sorted(set(ENGINE_VERBS) - {"sync", "context", "impact"})


def _patch_graphtrail(monkeypatch, *, run_calls: list | None = None, stderr: str = ""):
    def fake_run(args, timeout=30.0, **kwargs):
        if run_calls is not None:
            run_calls.append({"args": list(args), "timeout": timeout, "kwargs": kwargs})
        stdout = '{"ok": true}\n' if "--json" in args else "graphtrail-stdout\n"
        return proc.Result(0, stdout, stderr)

    monkeypatch.setattr(context_cmd, "_graphtrail_bin", lambda: "/fake/graphtrail")
    monkeypatch.setattr(proc, "run", fake_run)


# --- new passthrough verbs --------------------------------------------------


def test_engine_verb_table_covers_required_surface():
    assert {"callers", "callees", "affected", "search"} <= set(ENGINE_VERBS)


@pytest.mark.parametrize("verb", NEW_VERBS)
def test_new_verbs_forward_exact_argv(monkeypatch, verb):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(["code", verb, "some-symbol", "--json"])

    assert rc == 0
    assert calls[0]["args"] == ["/fake/graphtrail", verb, "some-symbol", "--json"]
    assert calls[0]["kwargs"] == {}


def test_query_verbs_use_short_timeout(monkeypatch):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    assert cli.main(["code", "callers", "brigade.cli.main"]) == 0
    assert calls[0]["timeout"] == 30.0


def test_evaluate_uses_long_timeout_with_env_override(monkeypatch, tmp_path):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    assert cli.main(["code", "evaluate", str(tmp_path)]) == 0
    assert calls[0]["timeout"] == 900.0

    monkeypatch.setenv("BRIGADE_CODE_EVALUATE_TIMEOUT_SECONDS", "42.5")
    assert cli.main(["code", "evaluate", str(tmp_path)]) == 0
    assert calls[1]["timeout"] == 42.5


@pytest.mark.parametrize("value", ("", "zero", "0", "-1", "nan", "inf"))
def test_evaluate_rejects_invalid_configured_timeout(monkeypatch, tmp_path, capsys, value):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)
    monkeypatch.setenv("BRIGADE_CODE_EVALUATE_TIMEOUT_SECONDS", value)

    assert cli.main(["code", "evaluate", str(tmp_path)]) == 2
    assert calls == []
    assert "BRIGADE_CODE_EVALUATE_TIMEOUT_SECONDS" in capsys.readouterr().err


def test_command_tree_exposes_new_code_verbs():
    tree = completions._command_tree()
    assert set(tree["brigade code"]) >= set(ENGINE_VERBS)
    # The compatibility alias group stays frozen: its own status/doctor plus
    # the original three executable verbs, nothing from the expanded set.
    assert set(tree["brigade search"]) == {"status", "doctor", "sync", "context", "impact"}


@pytest.mark.parametrize("verb", sorted(ENGINE_VERBS))
def test_verb_help_is_forwarded_to_engine(monkeypatch, verb):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    assert cli.main(["code", verb, "--help"]) == 0
    assert calls[0]["args"] == ["/fake/graphtrail", verb, "--help"]


def test_group_help_lists_new_verbs(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["code", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for verb in NEW_VERBS:
        assert verb in out


# --- --target mapping -------------------------------------------------------


def test_target_maps_to_engine_cwd_and_is_not_forwarded(monkeypatch, tmp_path):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(["code", "callers", "brigade.cli.main", "--target", str(tmp_path)])

    assert rc == 0
    assert calls[0]["args"] == ["/fake/graphtrail", "callers", "brigade.cli.main"]
    assert calls[0]["kwargs"] == {"cwd": tmp_path}


def test_target_equals_form_and_leading_position(monkeypatch, tmp_path):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(["code", "sync", f"--target={tmp_path}"])

    assert rc == 0
    assert calls[0]["args"] == ["/fake/graphtrail", "sync"]
    assert calls[0]["kwargs"] == {"cwd": tmp_path}


def test_target_missing_value_reports_branded_usage(monkeypatch, capsys):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(["code", "sync", "--target"])

    assert rc == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "brigade code sync" in err
    assert "graphtrail" not in err


def test_target_must_be_a_directory(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)
    missing = tmp_path / "nope"

    rc = cli.main(["code", "callers", "x", "--target", str(missing)])

    assert rc == 2
    assert calls == []
    assert "--target is not a directory" in capsys.readouterr().err


def test_search_alias_accepts_target_too(monkeypatch, tmp_path):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(["search", "impact", "brigade.cli.main", "--target", str(tmp_path)])

    assert rc == 0
    assert calls[0]["args"] == ["/fake/graphtrail", "impact", "brigade.cli.main"]
    assert calls[0]["kwargs"] == {"cwd": tmp_path}


# --- rebranded engine text --------------------------------------------------


def test_engine_usage_lines_are_rebranded_on_stderr(monkeypatch, capsys):
    stderr = "error: unexpected argument '--bogus' found\n\nUsage: graphtrail sync [OPTIONS] [ROOT]\n\nFor more information, try '--help'.\n"
    _patch_graphtrail(monkeypatch, stderr=stderr)

    cli.main(["code", "sync", "--bogus"])

    err = capsys.readouterr().err
    assert "Usage: brigade code sync [OPTIONS] [ROOT]" in err
    assert "Usage: graphtrail" not in err


def test_engine_usage_lines_are_rebranded_on_stdout(monkeypatch, capsys):
    def fake_run(args, timeout=30.0, **kwargs):
        return proc.Result(0, "Usage: graphtrail callers [OPTIONS] <SYMBOL>\n", "")

    monkeypatch.setattr(context_cmd, "_graphtrail_bin", lambda: "/fake/graphtrail")
    monkeypatch.setattr(proc, "run", fake_run)

    assert cli.main(["code", "callers", "x"]) == 0
    assert capsys.readouterr().out == "Usage: brigade code callers [OPTIONS] <SYMBOL>\n"


def test_non_usage_engine_output_passes_through_verbatim(monkeypatch, capsys):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(["code", "search", "receipts", "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
