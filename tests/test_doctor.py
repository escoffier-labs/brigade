"""Tests for solo-mise doctor."""
from __future__ import annotations

from pathlib import Path

import pytest

from solo_mise import doctor as doctor_mod
from solo_mise import init as init_mod


def test_doctor_passes_against_workspace_profile(tmp_target: Path, capsys):
    init_mod.run(target=tmp_target, profile_id="workspace")
    rc = doctor_mod.run(target=tmp_target, harness="generic")
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ok]" in out
    assert "[fail]" not in out


def test_doctor_reports_failures_on_empty_dir(tmp_target: Path, capsys):
    tmp_target.mkdir()
    rc = doctor_mod.run(target=tmp_target, harness="generic")
    assert rc == 1
    out = capsys.readouterr().out
    assert "[fail]" in out


def test_doctor_openclaw_reports_manual_when_config_missing(tmp_target: Path, monkeypatch, capsys):
    init_mod.run(target=tmp_target, profile_id="workspace")
    monkeypatch.setenv("HOME", str(tmp_target))  # so ~/.openclaw resolves into the temp dir
    monkeypatch.setattr(Path, "home", lambda: tmp_target)
    rc = doctor_mod.run(target=tmp_target, harness="openclaw")
    out = capsys.readouterr().out
    assert "openclaw: config" in out
    # missing config is MANUAL, not FAIL → exit 0
    assert rc == 0
    assert "[todo]" in out


def test_doctor_hermes_flags_experimental(tmp_target: Path, capsys):
    init_mod.run(target=tmp_target, profile_id="hermes")
    rc = doctor_mod.run(target=tmp_target, harness="hermes")
    out = capsys.readouterr().out
    assert "hermes:" in out
    assert "experimental" in out or "Hermes adapter" in out
    assert rc == 0
