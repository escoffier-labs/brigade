"""Tests for normalized station catalog and capability graph surfaces."""

from __future__ import annotations

import json

from brigade import cli, stations_cmd
from brigade.stations import catalog as station_catalog
from brigade.stations import graph as station_graph


def _write_station(root, name, *, tool_name=None, station="tokens", lifecycle="active", requires_brigade=None):
    side = root / name
    side.mkdir()
    payload = {
        "schema": "brigade.station.v1",
        "name": name,
        "station": station,
        "summary": f"{name} contract",
        "lifecycle": lifecycle,
        "owner": "external-maintainer",
        "tools": [
            {
                "name": tool_name or name,
                "command": tool_name or name,
                "summary": f"{name} tool",
                "produces": ["usage-json"],
                "consumes": ["workspace"],
                "dependencies": ["python"],
                "surfaces": [
                    {
                        "kind": "summary-json",
                        "command": [tool_name or name, "export", "--json"],
                        "read_only": True,
                        "timeout_seconds": 5,
                        "max_chars": 2000,
                    }
                ],
            }
        ],
    }
    if requires_brigade is not None:
        payload["requires_brigade"] = requires_brigade
    path = side / "station.json"
    path.write_text(json.dumps(payload))
    return path


def test_catalog_payload_keeps_managed_and_external_rows_separate(tmp_path):
    external = _write_station(tmp_path, "managed-name-collision", tool_name="token-glace")

    payload = station_catalog.catalog_payload(external_manifests=[external])

    collision_rows = [row for row in payload["rows"] if row["tool"]["name"] == "token-glace"]
    assert {row["source"] for row in collision_rows} == {"managed", "external"}
    assert len({row["id"] for row in collision_rows}) == len(collision_rows)
    external_row = next(row for row in collision_rows if row["source"] == "external")
    assert external_row["manifest"]["path"] == str(external)
    assert external_row["compatible"] is True
    assert external_row["compatibility"]["status"] == "compatible"
    assert external_row["tool"]["produces"] == ["usage-json"]
    assert external_row["tool"]["consumes"] == ["workspace"]
    assert external_row["tool"]["dependencies"] == ["python"]


def test_catalog_payload_surfaces_incompatible_external_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr("brigade.station_manifest._BRIGADE_VERSION", "1.2.3")
    external = _write_station(tmp_path, "future-sidecar", requires_brigade={"min_version": "9.0.0"})

    payload = station_catalog.catalog_payload(external_manifests=[external])

    row = next(row for row in payload["rows"] if row["source"] == "external")
    assert row["compatible"] is False
    assert row["compatibility"]["status"] == "incompatible"
    assert "requires Brigade >= 9.0.0" in row["compatibility"]["detail"]


def test_graph_payload_is_deterministic_and_uses_stable_ids(tmp_path):
    external = _write_station(tmp_path, "usage-sidecar")

    first = station_graph.graph_payload(external_manifests=[external])
    second = station_graph.graph_payload(external_manifests=[external])

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    node_ids = {node["id"] for node in first["nodes"]}
    edge_ids = {edge["id"] for edge in first["edges"]}
    assert "station:tokens" in node_ids
    assert "external:usage-sidecar:usage-sidecar" in node_ids
    assert "surface:external:usage-sidecar:usage-sidecar:summary-json:0" in node_ids
    assert "capability:usage-json" in node_ids
    assert "external:usage-sidecar:usage-sidecar->capability:usage-json:produces" in edge_ids
    assert all(node["id"] == node["id"].lower() for node in first["nodes"])


def test_stations_catalog_and_graph_cli_json(tmp_path, capsys):
    external = _write_station(tmp_path, "usage-sidecar")

    assert cli.main(["stations", "catalog", "--manifest", str(external), "--json"]) == 0
    catalog_payload = json.loads(capsys.readouterr().out)
    assert catalog_payload["schema"] == "brigade.stations.catalog.v1"
    assert any(row["source"] == "external" for row in catalog_payload["rows"])

    assert cli.main(["stations", "graph", "--manifest", str(external), "--json"]) == 0
    graph_payload = json.loads(capsys.readouterr().out)
    assert graph_payload["schema"] == "brigade.stations.graph.v1"
    assert graph_payload["node_count"] == len(graph_payload["nodes"])
    assert graph_payload["edge_count"] == len(graph_payload["edges"])


def test_discover_includes_structured_compatibility(tmp_path, monkeypatch):
    monkeypatch.setattr("brigade.station_manifest._BRIGADE_VERSION", "1.2.3")
    _write_station(tmp_path, "future-sidecar", requires_brigade={"min_version": "9.0.0"})

    payload = stations_cmd.discover_payload(roots=[tmp_path], max_depth=2)

    row = payload["manifests"][0]
    assert row["compatible"] is False
    assert row["compatibility"]["status"] == "incompatible"
