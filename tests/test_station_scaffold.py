"""Tests for station manifest scaffold helpers."""

from __future__ import annotations

import json
import os

from brigade import station_manifest, station_scaffold, stations_cmd


def test_scaffold_payload_defaults_to_bounded_read_only_version_surface(tmp_path):
    output = tmp_path / "station"

    payload = station_scaffold.scaffold_payload(
        output,
        station="evidence",
        name="example-ledger",
        summary="Example evidence station sidecar",
        command="example-ledger",
        install=["python3", "-m", "example_ledger"],
    )

    assert payload["schema"] == "brigade.station_scaffold.v1"
    assert payload["ok"] is True
    assert payload["status"] == "ready"
    assert payload["output"] == str(output)
    assert payload["files"] == ["README.md", "station.json"]
    assert payload["manifest"]["station"] == "evidence"
    assert payload["manifest"]["tools"][0]["surfaces"] == [
        {
            "kind": "verify-exit",
            "command": ["example-ledger", "--version"],
            "read_only": True,
            "timeout_seconds": 2,
        }
    ]
    assert payload["safety"] == {
        "install_executed": False,
        "probe_executed": False,
        "writes_outside_output": False,
    }
    assert payload["next_commands"] == [
        "brigade stations verify .",
        "brigade add . --install",
    ]
    assert not output.exists()


def test_scaffold_payload_accepts_repeatable_surfaces_and_validates_with_manifest_loader(tmp_path):
    payload = station_scaffold.scaffold_payload(
        tmp_path / "station",
        station="search",
        name="example-search",
        summary="Example search station sidecar",
        command="example-search",
        install=["python3", "-m", "example_search"],
        surfaces=[
            {
                "kind": "verify-exit",
                "command": ["example-search", "--version"],
                "read_only": True,
                "timeout_seconds": 2,
            },
            {
                "kind": "brief-markdown",
                "command": ["example-search", "brief", "<task>", "--markdown"],
                "read_only": False,
                "probe": ["example-search", "brief", "--help"],
                "probe_contains": ["--markdown"],
                "timeout_seconds": 3,
                "max_chars": 4000,
            },
        ],
    )

    assert payload["ok"] is True
    assert payload["manifest"]["tools"][0]["surfaces"][1]["command"][2] == "<task>"

    manifest_path = tmp_path / "station.json"
    manifest_path.write_text(json.dumps(payload["manifest"]))
    manifest = station_manifest.load(str(manifest_path))
    assert manifest.station == "search"
    assert manifest.tools[0].surfaces[1].placeholders == ("task",)


def test_scaffold_payload_rejects_surface_that_fails_static_verify_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(stations_cmd, "_run_bounded", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
    payload = station_scaffold.scaffold_payload(
        tmp_path / "station",
        station="evidence",
        name="example-tool",
        summary="Example sidecar",
        command="example-tool",
        install=["python3", "-m", "example_tool"],
        surfaces=[
            {
                "kind": "verify-exit",
                "command": ["example-tool", "--version"],
                "read_only": True,
            }
        ],
    )

    assert payload["ok"] is False
    assert "timeout_seconds" in payload["detail"]


def test_scaffold_payload_returns_json_ready_errors_for_invalid_inputs(tmp_path):
    invalid_station = station_scaffold.scaffold_payload(
        tmp_path / "station",
        station="not-a-station",
        name="example-tool",
        summary="Example sidecar",
        command="example-tool",
        install=["python3", "-m", "example_tool"],
    )
    assert invalid_station["ok"] is False
    assert invalid_station["status"] == "error"
    assert "unknown station" in invalid_station["detail"]

    invalid_surface = station_scaffold.scaffold_payload(
        tmp_path / "station",
        station="evidence",
        name="example-tool",
        summary="Example sidecar",
        command="example-tool",
        install=["python3", "-m", "example_tool"],
        surfaces=[{"kind": "verify-exit", "command": ["example-tool", "query", "<unsafe>"]}],
    )
    assert invalid_surface["ok"] is False
    assert invalid_surface["status"] == "error"
    assert "placeholder" in invalid_surface["detail"]


def test_write_scaffold_writes_manifest_and_readme_without_running_install_or_probe(tmp_path, monkeypatch):
    marker = tmp_path / "install-ran"
    output = tmp_path / "station"
    monkeypatch.setenv("PATH", "")

    payload = station_scaffold.write_scaffold(
        output,
        station="tokens",
        name="example-tokens",
        summary="Example tokens station sidecar",
        command="example-tokens",
        install=["python3", "-c", f"open({str(marker)!r}, 'w').write('bad')"],
    )

    assert payload["ok"] is True
    assert payload["status"] == "written"
    assert payload["wrote"] is True
    assert {entry["path"] for entry in payload["written"]} == {"README.md", "station.json"}
    assert not marker.exists()
    assert (output / "README.md").is_file()
    manifest = station_manifest.load(str(output))
    assert manifest.name == "example-tokens"
    assert manifest.tools[0].install[0] == "python3"


def test_write_scaffold_refuses_overwrite_even_with_partial_existing_files(tmp_path):
    output = tmp_path / "station"
    output.mkdir()
    (output / "station.json").write_text("{}")

    payload = station_scaffold.write_scaffold(
        output,
        station="tokens",
        name="example-tokens",
        summary="Example tokens station sidecar",
        command="example-tokens",
        install=["python3", "-m", "example_tokens"],
    )

    assert payload["ok"] is False
    assert payload["status"] == "refused"
    assert "already exists" in payload["detail"]
    assert json.loads((output / "station.json").read_text()) == {}


def test_write_scaffold_refuses_symlinked_output_root_and_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "station"
    output.symlink_to(outside, target_is_directory=True)

    root_refused = station_scaffold.write_scaffold(
        output,
        station="tokens",
        name="example-tokens",
        summary="Example tokens station sidecar",
        command="example-tokens",
        install=["python3", "-m", "example_tokens"],
    )

    assert root_refused["ok"] is False
    assert not (outside / "station.json").exists()

    output.unlink()
    output.mkdir()
    (output / "README.md").symlink_to(outside / "README.md")
    destination_refused = station_scaffold.write_scaffold(
        output,
        station="tokens",
        name="example-tokens",
        summary="Example tokens station sidecar",
        command="example-tokens",
        install=["python3", "-m", "example_tokens"],
    )

    assert destination_refused["ok"] is False
    assert not (outside / "README.md").exists()


def test_scaffold_payload_rejects_empty_install_argv(tmp_path):
    payload = station_scaffold.scaffold_payload(
        tmp_path / "station",
        station="tokens",
        name="example-tokens",
        summary="Example tokens station sidecar",
        command="example-tokens",
        install=[],
    )

    assert payload["ok"] is False
    assert "install" in payload["detail"]
    assert os.environ.get("PATH") is not None
