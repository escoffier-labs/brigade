"""Tests for `brigade security diff`."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import security_cmd


def _write_report(bundle: Path, fingerprints: list[str]) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    findings = [
        {
            "fingerprint": fp,
            "severity": "high",
            "category": "secret",
            "path": "app.py",
            "line": 1,
            "title": f"finding {fp}",
        }
        for fp in fingerprints
    ]
    (bundle / "security-report.json").write_text(
        json.dumps({"generated_at": "2026-06-16T00:00:00Z", "policy": "personal", "findings": findings})
    )


def test_security_diff_buckets_findings_and_flags_regressions(tmp_path: Path, capsys):
    base = tmp_path / "base"
    against = tmp_path / "against"
    _write_report(base, ["aaa", "bbb"])
    _write_report(against, ["bbb", "ccc"])  # bbb persists, aaa resolved, ccc new

    rc = security_cmd.diff(target=tmp_path, base_dir=base, against_dir=against, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert {f["fingerprint"] for f in payload["new"]} == {"ccc"}
    assert {f["fingerprint"] for f in payload["resolved"]} == {"aaa"}
    assert {f["fingerprint"] for f in payload["persisting"]} == {"bbb"}
    assert (payload["new_count"], payload["resolved_count"], payload["persisting_count"]) == (1, 1, 1)
    assert rc == 1  # nonzero: a new finding appeared


def test_security_diff_is_clean_when_nothing_new(tmp_path: Path, capsys):
    base = tmp_path / "base"
    against = tmp_path / "against"
    _write_report(base, ["aaa", "bbb"])
    _write_report(against, ["aaa"])  # only a resolution, no new findings

    rc = security_cmd.diff(target=tmp_path, base_dir=base, against_dir=against, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["new_count"] == 0
    assert payload["resolved_count"] == 1
    assert rc == 0


def test_security_diff_errors_on_missing_report(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()  # exists but has no security-report.json
    rc = security_cmd.diff(target=tmp_path, base_dir=base, against_dir=tmp_path / "missing", json_output=True)
    assert rc == 2
