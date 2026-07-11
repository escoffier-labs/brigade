"""Tests for the station conformance kit generator."""

from __future__ import annotations

import json
import os
import shutil
import stat

from brigade import station_conformance, station_manifest, stations_cmd


def test_conformance_payload_is_json_ready_without_writing(tmp_path):
    output = tmp_path / "kit"

    payload = station_conformance.conformance_payload(output)

    assert payload["schema"] == "brigade.station_conformance.v1"
    assert payload["ok"] is True
    assert payload["output"] == str(output)
    assert payload["would_write"] is True
    assert payload["files"] == [
        "README.md",
        "station.json",
        "fixtures/example-station",
        "tests/test_station_contract.py",
    ]
    assert payload["safety"] == {
        "install_executed": False,
        "probe_executed": False,
        "writes_outside_output": False,
        "runtime_dependencies": [],
    }
    assert payload["next_commands"] == [
        'PATH="$PWD/fixtures:$PATH" brigade stations verify .',
        'PATH="$PWD/fixtures:$PATH" pytest -q tests/test_station_contract.py',
    ]
    assert not output.exists()


def test_write_conformance_kit_creates_self_contained_verifiable_manifest(tmp_path, monkeypatch):
    output = tmp_path / "kit"
    payload = station_conformance.write_conformance_kit(output)

    assert payload["ok"] is True
    assert payload["wrote"] is True
    assert {entry["path"] for entry in payload["written"]} == set(payload["files"])
    assert (output / "README.md").is_file()
    assert (output / "tests" / "test_station_contract.py").is_file()
    fixture = output / "fixtures" / "example-station"
    assert fixture.is_file()
    assert fixture.stat().st_mode & stat.S_IXUSR

    manifest = station_manifest.load(str(output))
    assert manifest.name == "example-station"
    assert manifest.station == "evidence"
    assert manifest.tools[0].install
    assert manifest.tools[0].surfaces[0].command == ("example-station", "--version")

    monkeypatch.setenv("PATH", f"{fixture.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    verify_payload = stations_cmd.verify_payload(str(output))
    assert verify_payload["ok"] is True


def test_write_conformance_kit_refuses_nonempty_output_unless_forced(tmp_path):
    output = tmp_path / "kit"
    output.mkdir()
    (output / "keep.txt").write_text("user content\n")

    refused = station_conformance.write_conformance_kit(output)

    assert refused["ok"] is False
    assert refused["status"] == "refused"
    assert "non-empty" in refused["detail"]
    assert (output / "keep.txt").read_text() == "user content\n"
    assert not (output / "station.json").exists()

    forced = station_conformance.write_conformance_kit(output, force=True)

    assert forced["ok"] is True
    assert (output / "keep.txt").read_text() == "user content\n"
    assert (output / "station.json").is_file()


def test_conformance_template_manifest_has_no_personal_data_or_external_dependencies(tmp_path):
    output = tmp_path / "kit"
    station_conformance.write_conformance_kit(output)
    serialized = json.dumps(json.loads((output / "station.json").read_text()), sort_keys=True)
    all_text = "\n".join(path.read_text() for path in output.rglob("*") if path.is_file())

    assert "@example.com" not in all_text
    assert "localhost" not in all_text
    assert "pip install" not in all_text
    assert "npm install" not in all_text
    assert "pipx" not in all_text
    assert "python -m pytest" not in serialized
    assert shutil.which("example-station") is None
