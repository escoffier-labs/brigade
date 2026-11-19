"""JSON nested past the parser limit reads as malformed on receipt and run.json reads (#2589)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brigade import causal_receipt, dogfood_cmd, evidence_cmd, outcome_cmd, scorecard
from brigade.work_cmd import verification

DEEP_JSON = "[" * 200000


def test_archived_receipt_integrity_raises_unreadable_for_deep_json(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(DEEP_JSON)

    with pytest.raises(OSError, match="is unreadable"):
        verification._assert_archived_receipt_integrity(receipt)


def test_scorecard_load_receipt_skips_deep_json(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(DEEP_JSON)

    assert scorecard._load_receipt(receipt) is None


def test_read_run_receipt_returns_none_for_deep_json(tmp_path: Path) -> None:
    (tmp_path / "run.json").write_text(DEEP_JSON)

    payload, run_json = outcome_cmd._read_run_receipt(tmp_path)

    assert payload is None
    assert run_json == tmp_path / "run.json"


def test_route_coverage_is_none_for_deep_plan_attempts(tmp_path: Path) -> None:
    (tmp_path / "run.json").write_text("{}")
    (tmp_path / "plan-attempts.json").write_text(DEEP_JSON)

    assert outcome_cmd._route_coverage(tmp_path / "run.json") is None


def test_parent_manifest_read_is_none_for_deep_json(tmp_path: Path) -> None:
    path = causal_receipt.parent_manifest_path(tmp_path, "m1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEEP_JSON)

    assert causal_receipt.read_parent_manifest(tmp_path, {"parent_manifest": {"id": "m1"}}) is None


def test_dogfood_skips_deep_run_json(tmp_path: Path) -> None:
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    (good / "run.json").write_text('{"started_at": "2026-01-01"}')
    (bad / "run.json").write_text(DEEP_JSON)
    os.utime(good / "run.json", (1_900_000_000, 1_900_000_000))

    latest = dogfood_cmd._latest_run(tmp_path)

    assert latest is not None and latest[0] == good
    assert dogfood_cmd._read_run_json(bad) is None


def test_evidence_last_run_is_none_for_deep_json(tmp_path: Path) -> None:
    path = evidence_cmd._last_run_path(tmp_path, "src")
    path.parent.mkdir(parents=True)
    path.write_text(DEEP_JSON)

    assert evidence_cmd._read_last_run(tmp_path, "src") is None
