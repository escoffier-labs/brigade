"""Tests for the station-verb parity backlog (#90)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from brigade import friction_cmd, projects_cmd, scrub, security_cmd, skills_cmd


def test_security_suppress_and_unsuppress_json(tmp_path: Path, capsys):
    fingerprint = "0123456789abcdef"
    assert security_cmd.suppress(target=tmp_path, fingerprint=fingerprint, reason="reviewed", json_output=True) == 0
    suppressed = json.loads(capsys.readouterr().out)
    assert suppressed["fingerprint"] == fingerprint
    assert suppressed["suppressed_count"] == 1

    assert security_cmd.unsuppress(target=tmp_path, fingerprint=fingerprint, json_output=True) == 0
    unsuppressed = json.loads(capsys.readouterr().out)
    assert unsuppressed["fingerprint"] == fingerprint
    assert unsuppressed["suppressed_count"] == 0


def test_projects_doctor_runs(tmp_path: Path, capsys):
    rc = projects_cmd.doctor(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc in (0, 1)
    assert "issue_count" in payload
    assert payload["target"].endswith(tmp_path.name)


def test_friction_show_reads_latest(tmp_path: Path, capsys):
    latest = tmp_path / ".brigade" / "friction" / "latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-16T00:00:00Z",
                "candidates": [
                    {"text": "auth retries", "severity": "high", "workflow": "login"},
                    {"text": "slow build", "severity": "low", "workflow": "ci"},
                ],
            }
        )
    )
    assert friction_cmd.show(target=tmp_path, severity="high", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["workflow"] == "login"


def test_friction_show_errors_without_scan(tmp_path: Path):
    assert friction_cmd.show(target=tmp_path, json_output=True) == 2


def test_skills_uninstall_removes_installed(tmp_path: Path):
    targets = skills_cmd._install_targets(tmp_path)
    assert targets
    harness = targets[0]
    dest = skills_cmd._install_dir(tmp_path, harness, "demo")
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("demo\n")
    assert skills_cmd.uninstall(workspace=tmp_path, skill="demo", harness=harness, json_output=True) == 0
    assert not dest.exists()


def test_skills_uninstall_missing_returns_error(tmp_path: Path):
    harness = skills_cmd._install_targets(tmp_path)[0]
    assert skills_cmd.uninstall(workspace=tmp_path, skill="nope", harness=harness, json_output=True) == 1


def test_scrub_writes_summary_only_receipt(tmp_path: Path, monkeypatch, capsys):
    policy_file = tmp_path / "policy.json"
    policy_file.write_text("{}\n")
    monkeypatch.setattr(scrub, "scanner_dir", lambda: tmp_path)
    monkeypatch.setattr(scrub, "_resolve_policy", lambda target, scanner, policy: policy_file)
    monkeypatch.setattr(scrub, "policy_path", lambda repo, policy: policy_file)

    def _clean(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="SECRET=abc leaked", stderr="")

    monkeypatch.setattr(scrub.subprocess, "run", _clean)
    rc = scrub.run(tmp_path, policy="public-repo", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    receipt = json.loads((tmp_path / ".brigade" / "scrub" / "latest.json").read_text())
    # The receipt records the verdict but never the matched snippet.
    assert receipt["status"] == "ok"
    assert "stdout" not in receipt
    assert "SECRET" not in json.dumps(receipt)
    assert payload["status"] == "ok"
