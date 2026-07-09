"""Tests for brigade stations discover."""

from __future__ import annotations

import json

from brigade import stations_cmd


def test_discover_finds_station_json(tmp_path):
    side = tmp_path / "usage-tracker"
    side.mkdir()
    (side / "station.json").write_text(
        json.dumps(
            {
                "schema": "brigade.station.v1",
                "name": "usage-tracker",
                "station": "tokens",
                "summary": "usage export",
                "tools": [
                    {
                        "name": "usage-tracker",
                        "command": "usage-tracker",
                        "summary": "export usage",
                        "install": ["pipx", "install", "git+https://example.invalid/usage-tracker"],
                        "surfaces": [{"kind": "verify-exit", "command": ["usage-tracker", "--help"]}],
                    }
                ],
            }
        )
    )

    payload = stations_cmd.discover_payload(roots=[tmp_path], max_depth=2)

    assert payload["count"] == 1
    row = payload["manifests"][0]
    assert row["name"] == "usage-tracker"
    assert row["station"] == "tokens"
    assert "brigade add" in row["add_command"]


def test_discover_reports_invalid_manifest(tmp_path):
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "station.json").write_text(json.dumps({"schema": "nope"}))

    payload = stations_cmd.discover_payload(roots=[tmp_path], max_depth=2)

    assert payload["count"] == 0
    assert any("schema" in err["error"] for err in payload["errors"])
