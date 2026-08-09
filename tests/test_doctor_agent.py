"""Agent-facing doctor output mode (issue #734)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brigade import doctor as doctor_mod
from brigade import doctor_agent
from brigade import outcome_cmd
from brigade.install import install_selection
from brigade.selection import Selection


def _wire(tmp_path: Path, *, harnesses: list[str] | None = None) -> Path:
    target = tmp_path / "ws"
    assert (
        install_selection(
            target,
            Selection(
                depth="workspace",
                harnesses=harnesses or ["claude"],
                owner=(harnesses or ["claude"])[0],
                includes=[],
            ),
        )
        == 0
    )
    return target


def test_doctor_agent_healthy_target_prints_summary_only(tmp_path: Path, capsys, monkeypatch):
    target = tmp_path / "healthy"
    target.mkdir()
    checks = [(doctor_mod.OK, f"check-{index}", "ready") for index in range(14)]
    monkeypatch.setattr(doctor_mod, "_gather_checks", lambda _ctx: checks)

    assert doctor_mod.run(target=target, agent=True) == 0
    out = capsys.readouterr().out.strip()
    assert out == "14 checks passed, 0 findings"
    assert "severity:" not in out
    assert "brigade doctor:" not in out


def test_doctor_agent_findings_carry_structured_fields_text_and_json(tmp_path: Path, capsys, monkeypatch):
    target = tmp_path / "findings"
    target.mkdir()
    checks = [
        (doctor_mod.OK, "bootstrap: AGENTS.md", str(target / "AGENTS.md")),
        (
            doctor_mod.WARN,
            "security: config",
            f"missing at {target / '.brigade' / 'security.toml'}; run `brigade security init --target .`",
        ),
        (
            doctor_mod.FAIL,
            "bootstrap: AGENTS.md missing",
            f"missing at {target / 'AGENTS.md'}",
        ),
    ]
    monkeypatch.setattr(doctor_mod, "_gather_checks", lambda _ctx: checks)

    assert doctor_mod.run(target=target, agent=True) == 1
    text = capsys.readouterr().out
    assert "1 check passed, 2 findings" in text
    assert "severity: warn" in text
    assert "severity: error" in text
    assert "explanation:" in text
    assert "observed:" in text
    assert "expected:" in text
    assert "commands:" in text
    assert "brigade security init --target ." in text
    assert "check-0" not in text
    assert "bootstrap: AGENTS.md\n" not in text or "AGENTS.md missing" in text

    assert doctor_mod.run(target=target, agent=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "agent"
    assert payload["summary"]["passed"] == 1
    assert payload["summary"]["findings"] == 2
    assert "checks" not in payload
    finding = next(item for item in payload["findings"] if item["name"] == "security: config")
    assert finding["severity"] == "warn"
    assert finding["explanation"]
    assert "missing" in finding["observed"]
    assert "present" in finding["expected"]
    assert finding["commands"] == ["brigade security init --target ."]


def test_doctor_agent_dormant_loop_clears_after_verify(tmp_path: Path, capsys):
    target = _wire(tmp_path)
    capsys.readouterr()

    assert doctor_mod.run(target=target, agent=True, json_output=True) == 0
    before = json.loads(capsys.readouterr().out)
    dormant = [item for item in before["findings"] if item["name"] == "outcome loop: dormant"]
    assert dormant
    assert any("work verify run" in command for command in dormant[0]["commands"])

    # A verify run feeds the loop and clears the dormant finding.
    from brigade.work_cmd import verification as verify_mod

    assert verify_mod.verify_run(target=target, commands=["python3 -c \"print('ok')\""]) == 0
    capsys.readouterr()

    assert doctor_mod.run(target=target, agent=True, json_output=True) == 0
    after = json.loads(capsys.readouterr().out)
    assert not any(item["name"] == "outcome loop: dormant" for item in after["findings"])


def test_doctor_agent_silences_absent_harness_checks(tmp_path: Path, capsys, monkeypatch):
    target = _wire(tmp_path, harnesses=["claude", "openclaw"])
    # Ensure OpenClaw host artifacts are absent inside the temp HOME.
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    capsys.readouterr()

    assert doctor_mod.run(target=target, agent=True, json_output=True, operator=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert not any(str(item["name"]).startswith("openclaw:") for item in payload["findings"])


def test_doctor_agent_corrupt_hooks_is_error_severity(tmp_path: Path, capsys):
    target = _wire(tmp_path)
    (target / ".claude" / "settings.json").write_text("{not-json\n")
    capsys.readouterr()

    assert doctor_mod.run(target=target, agent=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    finding = next(item for item in payload["findings"] if item["name"] == "claude work loop")
    assert finding["severity"] == "error"
    assert "JSONDecodeError" in finding["observed"]
    assert "parse failure" in finding["observed"]


def test_doctor_agent_ledger_stale_finding(tmp_path: Path, monkeypatch):
    target = _wire(tmp_path)
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=doctor_mod.OUTCOME_LEDGER_STALE_DAYS + 2)).isoformat()

    class _Record:
        ts = stale_ts

    monkeypatch.setattr(
        outcome_cmd,
        "health",
        lambda _target: {
            "verify_run_count": 1,
            "record_count": 1,
            "issues": [],
        },
    )
    monkeypatch.setattr(outcome_cmd, "load_records", lambda _target: [_Record()])

    checks = doctor_mod._check_outcome_loop(doctor_mod.build_context(target))
    names = [name for _status, name, _detail in checks]
    assert "outcome loop: ledger stale" in names


def test_doctor_agent_extract_commands_and_harness_detection(tmp_path: Path):
    target = tmp_path / "detect"
    target.mkdir()
    (target / ".claude").mkdir()
    assert doctor_agent.harness_artifacts_present(target, "claude")
    assert not doctor_agent.harness_artifacts_present(target, "openclaw")
    assert doctor_agent.extract_commands("run `brigade security init --target .` now") == [
        "brigade security init --target ."
    ]
