"""CLI integration tests for station generator helpers."""

from __future__ import annotations

import json

import pytest

from brigade import cli


def test_stations_help_lists_generator_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["stations", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "conformance-kit" in out
    assert "scaffold" in out


def test_stations_conformance_kit_writes_human_output(tmp_path, capsys):
    output = tmp_path / "kit"

    rc = cli.main(["stations", "conformance-kit", str(output)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "station conformance kit written" in out
    assert "files:" in out
    assert "README.md" in out
    assert "next:" in out
    assert 'PATH="$PWD/fixtures:$PATH" brigade stations verify .' in out
    assert (output / "station.json").is_file()
    assert (output / "tests" / "test_station_contract.py").is_file()


def test_stations_conformance_kit_json_refused_exits_two(tmp_path, capsys):
    output = tmp_path / "kit"
    output.mkdir()
    (output / "keep.txt").write_text("user content\n")

    rc = cli.main(["stations", "conformance-kit", str(output), "--json"])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.station_conformance.v1"
    assert payload["ok"] is False
    assert payload["status"] == "refused"
    assert payload["wrote"] is False
    assert (output / "keep.txt").read_text() == "user content\n"


def test_stations_scaffold_writes_json_with_repeatable_inputs(tmp_path, capsys):
    output = tmp_path / "station"
    surface = {
        "kind": "verify-exit",
        "command": ["example-tokens", "--version"],
        "read_only": True,
        "timeout_seconds": 2,
    }

    rc = cli.main(
        [
            "stations",
            "scaffold",
            str(output),
            "--station",
            "tokens",
            "--name",
            "example-tokens",
            "--summary",
            "Example tokens station sidecar",
            "--command",
            "example-tokens",
            "--install-arg",
            "python3",
            "--install-arg=-m",
            "--install-arg",
            "example_tokens",
            "--surface-json",
            json.dumps(surface),
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.station_scaffold.v1"
    assert payload["status"] == "written"
    tool = payload["manifest"]["tools"][0]
    assert tool["command"] == "example-tokens"
    assert tool["install"] == ["python3", "-m", "example_tokens"]
    assert tool["surfaces"] == [surface]
    assert (output / "README.md").is_file()
    manifest = json.loads((output / "station.json").read_text())
    assert manifest["tools"][0]["command"] == "example-tokens"


def test_stations_scaffold_rejects_non_object_surface_json(tmp_path, capsys):
    output = tmp_path / "station"

    rc = cli.main(
        [
            "stations",
            "scaffold",
            str(output),
            "--station",
            "tokens",
            "--name",
            "example-tokens",
            "--summary",
            "Example tokens station sidecar",
            "--command",
            "example-tokens",
            "--install-arg",
            "python3",
            "--surface-json",
            "[]",
            "--json",
        ]
    )

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.station_scaffold.v1"
    assert payload["ok"] is False
    assert "surface-json 1 must be a JSON object" in payload["detail"]
    assert not output.exists()
