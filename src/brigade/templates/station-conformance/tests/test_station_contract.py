"""Example pytest contract for a Brigade station manifest."""

from __future__ import annotations

import os
from pathlib import Path

from brigade import station_manifest, stations_cmd


ROOT = Path(__file__).resolve().parents[1]


def test_station_manifest_loads_and_verifies(monkeypatch):
    fixture_dir = ROOT / "fixtures"
    monkeypatch.setenv("PATH", f"{fixture_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    manifest = station_manifest.load(str(ROOT))
    assert manifest.name == "example-station"
    assert manifest.tools[0].install

    payload = stations_cmd.verify_payload(str(ROOT))
    assert payload["ok"] is True
    assert payload["tools"][0]["surfaces"][0]["execution"] == "command"
