"""Tests for solo-mise doctor."""
from __future__ import annotations

from pathlib import Path
import json

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


def test_doctor_reports_memory_care_files(tmp_target: Path, capsys):
    init_mod.run(target=tmp_target, profile_id="workspace")
    decay = tmp_target / "memory" / "cards" / "decay"
    decay.mkdir()
    (decay / "scan-latest.json").write_text(
        json.dumps({"scan_date": "2026-05-13", "counts": {"stale": 2}})
    )
    (decay / "refresh-queue.json").write_text(json.dumps({"cards": [{"file": "x.md"}]}))

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory-care: scan-latest" in out
    assert "stale=2" in out
    assert "memory-care: refresh-queue" in out
    assert "1 queued" in out


def test_doctor_openclaw_reports_cron_memory_jobs(tmp_target: Path, monkeypatch, capsys):
    init_mod.run(target=tmp_target, profile_id="workspace")
    openclaw_dir = tmp_target / ".openclaw"
    cron_dir = openclaw_dir / "cron"
    cron_dir.mkdir(parents=True)
    (openclaw_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "plugins": {"entries": {"memory-core": {}}},
                "agents": {"defaults": {"model": {"primary": "openai-codex/gpt-5.5"}}},
            }
        )
    )
    (cron_dir / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "Claude Memory Handoff Ingest",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 1800000},
                    },
                    {
                        "name": "Card Decay Scanner (Daily)",
                        "enabled": True,
                        "schedule": {
                            "kind": "cron",
                            "expr": "30 5 * * *",
                            "tz": "America/New_York",
                        },
                    },
                    {
                        "name": "Card Decay Auto-Refresh (Safe)",
                        "enabled": True,
                        "schedule": {
                            "kind": "cron",
                            "expr": "40 5 * * *",
                            "tz": "America/New_York",
                        },
                    },
                    {
                        "name": "Card Decay Deep Report (Weekly)",
                        "enabled": True,
                        "schedule": {
                            "kind": "cron",
                            "expr": "30 5 * * 0",
                            "tz": "America/New_York",
                        },
                    },
                ]
            }
        )
    )
    monkeypatch.setenv("HOME", str(tmp_target))
    monkeypatch.setattr(Path, "home", lambda: tmp_target)

    rc = doctor_mod.run(target=tmp_target, harness="openclaw")
    out = capsys.readouterr().out
    assert rc == 0
    assert "openclaw: handoff ingest cron" in out
    assert "every 30 min" in out
    assert "openclaw: card decay scanner" in out
    assert "openclaw: card decay refresh" in out
    assert "openclaw: card decay weekly" in out
