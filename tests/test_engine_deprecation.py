"""Deprecation diagnostics leave stdout and protocol output unchanged."""

from __future__ import annotations

import argparse
import json

import pytest

from brigade import cli, component_bins, component_manifest, config, engine_deprecation, engine_paths
from brigade.selection import Selection


@pytest.fixture(autouse=True)
def reset_notices(monkeypatch):
    monkeypatch.setattr(engine_deprecation, "_seen", set())


@pytest.mark.parametrize("legacy,canonical", component_manifest.COMPONENT_FAMILIES.items())
def test_binary_environment_notice_once_per_spelling(tmp_path, capsys, legacy, canonical):
    executable = tmp_path / canonical
    executable.write_text("fixture")
    executable.chmod(0o755)
    variable = component_bins.ENV_OVERRIDES[legacy]
    replacement = component_bins.ENV_OVERRIDES[canonical]
    env = {variable: str(executable), "HOME": str(tmp_path), "PATH": ""}
    assert component_bins.resolve(canonical, env=env) == str(executable)
    assert component_bins.resolve(canonical, env=env) == str(executable)
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == f"{variable} is deprecated; use {replacement}; removed in v0.33.0\n"


def test_canonical_environment_is_silent(tmp_path, capsys):
    component_bins.resolve("brigade-code-graph", env={"HOME": str(tmp_path), "PATH": ""})
    engine_paths.code_graph_db(tmp_path, env={engine_paths.CODE_GRAPH_DB_ENV: "explicit.db"})
    assert capsys.readouterr().err == ""


def test_database_environment_notice_is_deduplicated(tmp_path, capsys):
    env = {engine_paths.LEGACY_CODE_GRAPH_DB_ENV: "legacy.db"}
    for _ in range(2):
        assert engine_paths.code_graph_db(tmp_path, env=env).path == tmp_path / "legacy.db"
    assert capsys.readouterr().err.count("deprecated") == 1


def test_legacy_config_notice_even_when_canonical_value_wins(tmp_path, capsys):
    config.write_config(
        tmp_path,
        config.Config(version=1, selection=Selection(depth="workspace", harnesses=["codex"], owner="this-repo")),
    )
    path = tmp_path / ".brigade" / "config.json"
    payload = json.loads(path.read_text())
    payload[config.LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY] = 9
    payload[config.CODE_GRAPH_DELTA_TIMEOUT_KEY] = 4
    path.write_text(json.dumps(payload))
    for _ in range(2):
        assert config.load_config(tmp_path).code_graph_delta_timeout_seconds == 4
    assert capsys.readouterr().err.count("deprecated") == 1


def test_legacy_option_notice_once_and_canonical_option_silent(capsys):
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", action=engine_deprecation.LegacyOption, replacement="--new")
    assert parser.parse_args(["--old", "value"]).old == "value"
    parser.parse_args(["--old=again"])
    assert capsys.readouterr().err == "--old is deprecated; use --new; removed in v0.33.0\n"


def test_receipts_alias_notice_keeps_json_clean(tmp_path, monkeypatch, capsys):
    legacy = next(key for key, value in component_manifest.COMPONENT_FAMILIES.items() if value == "brigade-evidence")

    def export(**kwargs):
        print('{"ok": true}')
        return 0

    from brigade import receipts_cmd

    monkeypatch.setattr(receipts_cmd, "export_" + legacy, export)
    for _ in range(2):
        assert cli.main(["receipts", "export", legacy, "--target", str(tmp_path), "--json"]) == 0
    out = capsys.readouterr()
    assert [json.loads(line) for line in out.out.splitlines()] == [{"ok": True}, {"ok": True}]
    assert out.err.count("deprecated") == 1


@pytest.mark.parametrize(
    "family,canonical,legacy_suffix,canonical_flag,argv",
    [
        (
            "brigade-code-graph",
            "--code-graph-timeout",
            "-timeout",
            "--code-graph-timeout",
            ["work", "verify", "run", "--command", "true"],
        ),
        ("brigade-evidence", "--from-evidence", "", "--from-evidence", ["work", "import", "context"]),
    ],
)
def test_actual_hidden_options_warn_once(capsys, family, canonical, legacy_suffix, canonical_flag, argv):
    legacy_name = next(key for key, value in component_manifest.COMPONENT_FAMILIES.items() if value == family)
    legacy = "--" + ("from-" if family == "brigade-evidence" else "") + legacy_name + legacy_suffix
    value = "7" if legacy_suffix else "query"
    for _ in range(2):
        cli._build_parser().parse_args([*argv, legacy, value])
    assert capsys.readouterr().err == f"{legacy} is deprecated; use {canonical}; removed in v0.33.0\n"
    cli._build_parser().parse_args([*argv, canonical_flag, value])
    assert capsys.readouterr().err == ""


def test_projection_refuses_conflicting_overrides(monkeypatch, capsys):
    legacy, canonical = next(iter(component_manifest.COMPONENT_FAMILIES.items()))
    monkeypatch.setenv(component_bins.ENV_OVERRIDES[legacy], "old-executable")
    monkeypatch.setenv(component_bins.ENV_OVERRIDES[canonical], "new-executable")
    with pytest.raises(ValueError, match="invalid or conflicting"):
        component_bins.canonical_command(legacy)
    assert capsys.readouterr().err.count("deprecated") == 1
