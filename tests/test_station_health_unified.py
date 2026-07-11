"""Tests for the unified advisory station health collector."""

from __future__ import annotations

import json

from brigade import cli, station_health, work_cmd
from brigade.operator_cmd import lifecycle


def test_station_health_collector_rolls_up_managed_tool_doctors(monkeypatch, tmp_path):
    class FakeTool:
        name = "fake-tool"
        station = "tokens"
        command = "fake-tool"
        summary = "fake managed tool"
        surfaces = ()

        def detect(self):
            return True

        def doctor(self, ctx):
            assert ctx.target == tmp_path.resolve()
            return [("WARN", "fake-tool", "configured with advisory warning")]

    monkeypatch.setattr(station_health.managed, "all_tools", lambda: (FakeTool(),))

    payload = station_health.collect(tmp_path)

    assert payload["schema"] == "brigade.station.health.v1"
    assert payload["advisory"] is True
    assert payload["status"] == "warn"
    assert payload["issue_count"] == 1
    assert payload["stations"][0]["station"] == "tokens"
    assert payload["stations"][0]["health"] == "warn"
    assert payload["stations"][0]["tools"][0]["name"] == "fake-tool"
    assert payload["top_issue"]["tool"] == "fake-tool"


def test_operator_checkup_includes_station_health_without_blocking(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(lifecycle.core_doctor, "run", lambda **kwargs: print("{}") or 0)
    monkeypatch.setattr(lifecycle, "operator_doctor", lambda **kwargs: print("{}") or 0)
    monkeypatch.setattr(lifecycle.handoff_cmd, "doctor", lambda **kwargs: print("{}") or 0)
    monkeypatch.setattr(lifecycle.tools_cmd, "doctor", lambda **kwargs: print("{}") or 0)
    monkeypatch.setattr(lifecycle.skills_cmd, "doctor", lambda **kwargs: print("{}") or 0)
    monkeypatch.setattr(lifecycle.security_cmd, "doctor", lambda **kwargs: print("{}") or 0)
    monkeypatch.setattr(
        lifecycle.station_health,
        "collect",
        lambda target: {
            "schema": "brigade.station.health.v1",
            "advisory": True,
            "status": "warn",
            "issue_count": 1,
            "top_issue": {"station": "tokens", "tool": "fake-tool", "detail": "advisory only"},
            "stations": [],
        },
    )

    assert lifecycle.checkup(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is True
    assert payload["station_health"]["status"] == "warn"
    assert payload["station_health"]["issue_count"] == 1


def test_work_brief_includes_station_health_json_and_text(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        work_cmd.station_health,
        "collect",
        lambda target: {
            "schema": "brigade.station.health.v1",
            "advisory": True,
            "status": "warn",
            "issue_count": 1,
            "top_issue": {"station": "tokens", "tool": "fake-tool", "detail": "advisory only"},
            "stations": [],
        },
    )

    payload = work_cmd._brief_payload(tmp_path)
    assert payload["station_health"]["status"] == "warn"

    assert cli.main(["work", "brief", "--target", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "station_health: 1 issue" in out
    assert "station_top_issue: tokens/fake-tool advisory only" in out
