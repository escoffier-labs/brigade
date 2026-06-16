"""Tests for `brigade operator checkup`."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import cli
from brigade.operator_cmd import lifecycle


def _stub(rc: int, payload: dict):
    def _doctor(**kwargs):
        print(json.dumps(payload))
        return rc

    return _doctor


def _patch_all_doctors(monkeypatch, *, skills_rc: int = 0):
    monkeypatch.setattr(lifecycle.core_doctor, "run", _stub(0, {"ready": True, "summary": {"failed": 0}}))
    monkeypatch.setattr(lifecycle, "operator_doctor", _stub(0, {"ready": True, "blocking_issue_count": 0}))
    monkeypatch.setattr(lifecycle.handoff_cmd, "doctor", _stub(0, {"issue_count": 0}))
    monkeypatch.setattr(lifecycle.tools_cmd, "doctor", _stub(0, {"issue_count": 0}))
    monkeypatch.setattr(lifecycle.skills_cmd, "doctor", _stub(skills_rc, {"issue_count": 3 if skills_rc else 0}))
    monkeypatch.setattr(lifecycle.security_cmd, "doctor", _stub(0, {"issue_count": 0}))


def test_operator_checkup_rolls_up_each_first_run_doctor(monkeypatch, capsys):
    _patch_all_doctors(monkeypatch, skills_rc=1)  # one failing surface
    rc = lifecycle.checkup(target=Path("."), json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["ready"] is False
    assert payload["blocking_surface_count"] == 1
    assert [s["name"] for s in payload["surfaces"]] == [
        "doctor",
        "operator",
        "handoff",
        "tools",
        "skills",
        "security",
    ]
    skills = next(s for s in payload["surfaces"] if s["name"] == "skills")
    assert skills["ready"] is False
    assert skills["issue_count"] == 3
    assert payload["next_command"] == "brigade skills doctor --target ."


def test_operator_checkup_is_ready_when_all_surfaces_pass(monkeypatch, capsys):
    _patch_all_doctors(monkeypatch, skills_rc=0)
    rc = lifecycle.checkup(target=Path("."), json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ready"] is True
    assert payload["blocking_surface_count"] == 0
    assert payload["next_command"] is None


def test_operator_checkup_cli_dispatch(monkeypatch, tmp_path):
    seen = {}

    def fake_checkup(**kwargs):
        seen.update(kwargs)
        return 0

    from brigade import operator_cmd

    monkeypatch.setattr(operator_cmd, "checkup", fake_checkup)
    assert cli.main(["operator", "checkup", "--target", str(tmp_path), "--json"]) == 0
    assert seen == {"target": tmp_path, "profile": "internal-dogfood", "json_output": True}
