import json

import pytest

from brigade import managed
from brigade import managed_snapshot


EXPECTED_NAMES = {
    "agentpantry",
    "content-guard",
    "graphtrail",
    "miseledger",
    "skillet",
    "token-glace",
}


def test_bundled_snapshot_covers_first_class_sidecars():
    payload = managed_snapshot.load_snapshot()
    records = payload["records"]
    assert payload["schema"] == managed_snapshot.SCHEMA
    assert {record["manifest"]["name"] for record in records} == EXPECTED_NAMES

    by_name = {record["manifest"]["name"]: record["manifest"] for record in records}
    assert by_name["content-guard"]["lifecycle"] == "embedded"
    assert by_name["content-guard"]["owner"] == "brigade-cli"
    assert by_name["skillet"]["tools"][0]["kind"] == "skill-roster"
    assert managed.resolve("skillet") is None


def test_bundled_executable_contracts_drive_managed_catalog():
    contracts = managed_snapshot.executable_contracts()
    assert set(contracts) == {"agentpantry", "graphtrail", "miseledger", "token-glace"}

    for name, contract in contracts.items():
        tool = managed.resolve(name)
        assert tool is not None
        assert tool.station == contract["station"]
        assert tool.command == contract["command"]
        assert tool.summary == contract["summary"]
        assert tool.install_args == contract["install"]
        actual_surfaces = [
            {
                "kind": surface.kind,
                "command": list(surface.command),
                "read_only": surface.read_only,
                "timeout_seconds": surface.timeout_seconds,
                "max_chars": surface.max_chars,
                "probe": list(surface.probe),
                "probe_contains": list(surface.probe_contains),
            }
            for surface in tool.surfaces
        ]
        expected_surfaces = [
            {
                "kind": surface["kind"],
                "command": surface.get("command", []),
                "read_only": surface.get("read_only", True),
                "timeout_seconds": float(surface["timeout_seconds"])
                if surface.get("timeout_seconds") is not None
                else None,
                "max_chars": surface.get("max_chars"),
                "probe": surface.get("probe", []),
                "probe_contains": surface.get("probe_contains", []),
            }
            for surface in contract["surfaces"]
        ]
        assert actual_surfaces == expected_surfaces


def test_build_snapshot_sorts_and_digests_manifests(monkeypatch, tmp_path):
    monkeypatch.setattr(managed_snapshot, "_git_revision", lambda root: "abc123")
    paths = []
    for name in ("zeta", "alpha"):
        root = tmp_path / name
        root.mkdir()
        path = root / "station.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "brigade.station.v1",
                    "name": name,
                    "station": "search",
                    "summary": name,
                    "lifecycle": "embedded",
                    "owner": "brigade-cli",
                    "tools": [],
                }
            )
        )
        paths.append(path)

    payload = managed_snapshot.build_snapshot(paths)
    assert [record["manifest"]["name"] for record in payload["records"]] == ["alpha", "zeta"]
    assert {record["source"]["revision"] for record in payload["records"]} == {"abc123"}
    managed_snapshot.load_snapshot(_write_snapshot(tmp_path, payload))


def test_load_snapshot_rejects_digest_drift(tmp_path):
    payload = {
        "schema": managed_snapshot.SCHEMA,
        "records": [
            {
                "manifest": {
                    "schema": "brigade.station.v1",
                    "name": "x",
                    "station": "search",
                    "summary": "x",
                    "tools": [],
                },
                "source": {
                    "repository": "x",
                    "revision": "abc",
                    "manifest_sha256": "wrong",
                },
            }
        ],
    }
    with pytest.raises(ValueError, match="digest"):
        managed_snapshot.load_snapshot(_write_snapshot(tmp_path, payload))


def test_build_snapshot_rejects_incomplete_station_manifest(tmp_path):
    path = tmp_path / "station.json"
    path.write_text(json.dumps({"schema": "brigade.station.v1", "name": "missing-fields"}))

    with pytest.raises(ValueError, match="station"):
        managed_snapshot.build_snapshot([path])


def test_apply_snapshot_preserves_runtime_callables():
    original = managed.resolve("graphtrail")
    assert original is not None
    contract = {
        "station": "search",
        "command": "graphtrail-next",
        "summary": "snapshot summary",
        "install": ["cargo", "install", "graphtrail-next"],
        "surfaces": [],
    }

    updated = managed._apply_snapshot(original, contract)

    assert updated.command == "graphtrail-next"
    assert updated.summary == "snapshot summary"
    assert updated.wire is original.wire
    assert updated.doctor is original.doctor


def _write_snapshot(tmp_path, payload):
    path = tmp_path / "managed-snapshot.json"
    path.write_text(managed_snapshot.render_snapshot(payload))
    return path
