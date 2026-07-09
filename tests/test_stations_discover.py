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


def _write_lifecycle_manifest(root, name, lifecycle, *, tools=None):
    side = root / name
    side.mkdir()
    if tools is None:
        tools = []
    (side / "station.json").write_text(
        json.dumps(
            {
                "schema": "brigade.station.v1",
                "name": name,
                "station": "guard",
                "summary": f"{lifecycle} contract",
                "lifecycle": lifecycle,
                "owner": "maintained-package",
                "tools": tools,
            }
        )
    )


def test_discover_preserves_legacy_keys_and_adds_contract_fields(tmp_path):
    _write_lifecycle_manifest(
        tmp_path,
        "active-sidecar",
        "active",
        tools=[
            {
                "name": "active-sidecar",
                "command": "active-sidecar",
                "summary": "active tool",
                "install": ["pipx", "install", "active-sidecar"],
                "surfaces": [
                    {
                        "kind": "brief-markdown",
                        "command": ["active-sidecar", "show", "<query>"],
                        "probe": ["active-sidecar", "show", "--help"],
                        "probe_contains": ["--limit"],
                    }
                ],
            }
        ],
    )

    payload = stations_cmd.discover_payload(roots=[tmp_path], max_depth=2)

    assert set(["roots", "max_depth", "count", "manifests", "errors", "docs"]) <= set(payload)
    assert payload["active_count"] == 1
    assert payload["non_active_count"] == 0
    assert payload["lifecycle_counts"] == {
        "active": 1,
        "embedded": 0,
        "deprecated": 0,
        "historical": 0,
    }
    row = payload["manifests"][0]
    assert set(["path", "name", "station", "summary", "tools", "add_command"]) <= set(row)
    assert row["lifecycle"] == "active"
    assert row["owner"] == "maintained-package"
    assert row["tools"][0]["kind"] == "executable"
    assert row["tools"][0]["surfaces"][0]["probe"] == ["active-sidecar", "show", "--help"]
    assert row["tools"][0]["surfaces"][0]["probe_contains"] == ["--limit"]
    assert row["tools"][0]["surfaces"][0]["placeholders"] == ["query"]


def test_discover_counts_each_lifecycle_separately(tmp_path):
    executable = [{"name": "active", "command": "active"}]
    _write_lifecycle_manifest(tmp_path, "active", "active", tools=executable)
    _write_lifecycle_manifest(tmp_path, "embedded", "embedded")
    _write_lifecycle_manifest(tmp_path, "deprecated", "deprecated")
    _write_lifecycle_manifest(tmp_path, "historical", "historical")

    payload = stations_cmd.discover_payload(roots=[tmp_path], max_depth=2)

    assert payload["count"] == 4
    assert payload["active_count"] == 1
    assert payload["non_active_count"] == 3
    assert payload["lifecycle_counts"] == {
        "active": 1,
        "embedded": 1,
        "deprecated": 1,
        "historical": 1,
    }
