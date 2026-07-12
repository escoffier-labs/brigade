import json
from pathlib import Path

import pytest

from brigade import doctor
from brigade import registry
from brigade import status as status_mod
from brigade.install import install_selection
from brigade.selection import Selection
from brigade.station import Station


@pytest.mark.parametrize(
    ("raw", "installed", "expected"),
    [
        ("ok", True, "ok"),
        ("warn", True, "degraded"),
        ("fail", True, "failed"),
        ("timeout", True, "degraded"),
        ("incomplete", True, "degraded"),
        ("unwired", True, "not-configured"),
        ("missing", False, "not-installed"),
        ("missing", True, "unchecked"),
        ("manual", False, "not-installed"),
        ("unknown", True, "unchecked"),
    ],
)
def test_normalize_payload_health(raw, installed, expected):
    assert status_mod._normalize_payload_health(raw, installed=installed) == expected


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([], "unchecked"),
        ([(doctor.INFO, "x", "x")], "not-configured"),
        ([(doctor.OK, "x", "x")], "ok"),
        ([(doctor.OK, "x", "x"), (doctor.WARN, "y", "y")], "degraded"),
        ([(doctor.FAIL, "x", "x"), (doctor.WARN, "y", "y")], "failed"),
    ],
)
def test_health_from_doctor_checks(checks, expected):
    assert status_mod._health_from_checks(checks) == expected


def test_status_uses_optional_station_payload(monkeypatch, tmp_target, capsys):
    monkeypatch.setattr(status_mod, "all_stations", lambda: (registry.SEARCH,))
    monkeypatch.setattr(
        status_mod,
        "_optional_station_payload",
        lambda station, target: {"installed": True, "health": "ok", "summary": "graph ok"},
    )

    assert status_mod.run(tmp_target, json_output=True) == 0
    row = json.loads(capsys.readouterr().out)["stations"][0]
    assert row["health"] == "ok"
    assert row["summary"] == "graph ok"


def test_status_warning_is_degraded(monkeypatch, tmp_target, capsys):
    station = Station(
        name="warning-test",
        summary="warning fixture",
        doctor=lambda ctx: [(doctor.WARN, "warning", "degraded")],
    )
    monkeypatch.setattr(status_mod, "all_stations", lambda: (station,))
    monkeypatch.setattr(status_mod, "_optional_station_payload", lambda station, target: None)

    assert status_mod.run(tmp_target, json_output=True) == 0
    row = json.loads(capsys.readouterr().out)["stations"][0]
    assert row["health"] == "degraded"
    assert row["warn"] == 1


def test_status_lists_stations_for_installed_workspace(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    rc = status_mod.run(target=tmp_target)
    out = capsys.readouterr().out
    assert rc == 0
    assert "core" in out
    assert "memory" in out
    assert "guard" in out
    assert "security" in out


def test_status_runs_on_empty_dir(tmp_target: Path, capsys):
    tmp_target.mkdir()
    rc = status_mod.run(target=tmp_target)
    out = capsys.readouterr().out
    # status never fails; it reports health, it does not gate
    assert rc == 0
    assert "core" in out


def test_status_json_output_is_structured(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()  # drain install output so only the status JSON remains
    rc = status_mod.run(target=tmp_target, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["target"].endswith("ws")
    names = {row["station"] for row in payload["stations"]}
    assert {"core", "memory", "guard", "security"} <= names
    first = payload["stations"][0]
    assert {"station", "health", "ok", "warn", "fail", "summary"} <= set(first)
