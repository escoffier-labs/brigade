"""Tests for scripts/measure_run_journal.py journal-authority measurement harness."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import run_events, runguard

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "measure_run_journal.py"


def _load_measure_run_journal_module():
    spec = importlib.util.spec_from_file_location("measure_run_journal_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_measure_runs_returns_report_shape(tmp_path):
    module = _load_measure_run_journal_module()

    report = module.measure_runs(runs=3, root=tmp_path)

    assert {"runs", "per_run", "aggregate", "environment"} <= set(report)
    assert report["runs"] == 3
    per_run = report["per_run"]
    assert isinstance(per_run, list)
    assert len(per_run) == 3
    for entry in per_run:
        assert isinstance(entry["journal_bytes"], int)
        assert entry["journal_bytes"] > 0
        assert isinstance(entry["event_count"], int)
        assert entry["event_count"] > 0
        latencies = entry["append_latencies_ns"]
        assert isinstance(latencies, list)
        assert latencies
        assert all(isinstance(value, int) and value >= 0 for value in latencies)


def test_measure_runs_aggregate_has_integer_percentiles(tmp_path):
    module = _load_measure_run_journal_module()

    report = module.measure_runs(runs=3, root=tmp_path)

    aggregate = report["aggregate"]
    for key in ("p50_ns", "p95_ns", "p99_ns", "max_ns"):
        value = aggregate[key]
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value >= 0


def test_measure_runs_environment_metadata(tmp_path):
    module = _load_measure_run_journal_module()

    report = module.measure_runs(runs=1, root=tmp_path)

    environment = report["environment"]
    assert environment["python_version"] == sys.version
    assert isinstance(environment["platform"], str) and environment["platform"]
    assert isinstance(environment["filesystem_type"], str) and environment["filesystem_type"]
    assert isinstance(environment["directory_fsync_supported"], bool)


def test_measure_runs_report_has_no_threshold_or_pass(tmp_path):
    module = _load_measure_run_journal_module()

    report = module.measure_runs(runs=1, root=tmp_path)

    serialized = json.dumps(report)
    assert "threshold" not in serialized
    assert "pass" not in serialized


def test_measure_runs_rejects_nonpositive_runs(tmp_path):
    module = _load_measure_run_journal_module()

    with pytest.raises(ValueError):
        module.measure_runs(runs=0, root=tmp_path)


def test_measure_runs_preserves_preexisting_root_and_leaves_no_measurement_content(tmp_path):
    module = _load_measure_run_journal_module()

    sentinel_file = tmp_path / "sentinel.txt"
    sentinel_dir = tmp_path / "sentinel-dir"
    sentinel_file.write_text("keep me")
    sentinel_dir.mkdir()
    (sentinel_dir / "inner.txt").write_text("inner")

    module.measure_runs(runs=2, root=tmp_path)

    assert sentinel_file.read_text() == "keep me"
    assert (sentinel_dir / "inner.txt").read_text() == "inner"
    assert not (tmp_path / "workspace").exists()
    assert not (tmp_path / "runs").exists()
    measurement_leftovers = [
        path.name for path in tmp_path.iterdir() if path.name.startswith("brigade-run-journal-measure-")
    ]
    assert measurement_leftovers == []


def test_measure_runs_twice_against_same_root_is_independent(tmp_path):
    module = _load_measure_run_journal_module()

    first = module.measure_runs(runs=2, root=tmp_path)
    second = module.measure_runs(runs=2, root=tmp_path)

    for report in (first, second):
        assert report["runs"] == 2
        assert isinstance(report["per_run"], list)
        assert len(report["per_run"]) == 2
        for entry in report["per_run"]:
            assert len(entry["append_latencies_ns"]) == 2

    first_bytes = [entry["journal_bytes"] for entry in first["per_run"]]
    second_bytes = [entry["journal_bytes"] for entry in second["per_run"]]
    assert first_bytes == second_bytes, "stale journal reused across invocations"

    assert not (tmp_path / "workspace").exists()
    assert not (tmp_path / "runs").exists()
    measurement_leftovers = [
        path.name for path in tmp_path.iterdir() if path.name.startswith("brigade-run-journal-measure-")
    ]
    assert measurement_leftovers == []


def test_write_report_writes_canonical_json_creating_parents(tmp_path):
    module = _load_measure_run_journal_module()
    report = {"b": 1, "a": {"z": [1, 2], "y": True}}
    output = tmp_path / "nested" / "dir" / "report.json"

    module.write_report(report, output)

    expected = json.dumps(report, indent=2, sort_keys=True) + "\n"
    assert output.is_file()
    assert output.read_text() == expected


def test_cli_writes_exact_output_path(tmp_path):
    output = tmp_path / "out" / "report.json"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--runs", "1", "--output", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert output.is_file()
    raw = output.read_text()
    report = json.loads(raw)
    assert report["runs"] == 1
    assert "threshold" not in raw


def test_measure_runs_nested_under_outer_run_lock(tmp_path):
    """A measurement run inside an outer runguard.run_lock must not collide.

    Regression for the nested-lock bug: the harness staged its lock workspace
    under the requested root, and runguard lock discovery treats every
    descendant of a git worktree as the same worktree, so an inner
    measure_runs call resolved to the outer repo's run.lock and raised
    RunLockError. The lock workspace must live outside the detected git
    worktree so the inner lock is independent of the outer one.
    """
    module = _load_measure_run_journal_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    sentinel = repo / "sentinel.txt"
    sentinel.write_text("keep me")

    outer_lock_dir = repo / ".brigade" / "run.lock"
    with runguard.run_lock(repo):
        assert outer_lock_dir.is_dir(), "outer run lock was not acquired"
        report = module.measure_runs(runs=1, root=repo)
        # The outer lock must remain owned by this process throughout the call.
        assert outer_lock_dir.is_dir(), "outer run lock was released during measurement"
        owner = json.loads((outer_lock_dir / "owner.json").read_text())
        assert owner["pid"] == os.getpid(), "outer run lock owner changed during measurement"

    assert isinstance(report["per_run"], list)
    assert len(report["per_run"]) == 1
    assert sentinel.read_text() == "keep me", "unrelated repo file was modified"

    repo_leftovers = [path.name for path in repo.iterdir() if path.name.startswith("brigade-run-journal-")]
    assert repo_leftovers == [], f"measurement temp dirs left under repo: {repo_leftovers}"
    parent_leftovers = [path.name for path in tmp_path.iterdir() if path.name.startswith("brigade-run-journal-")]
    assert parent_leftovers == [], f"lock workspace temp dirs left under repo parent: {parent_leftovers}"


def test_measure_volume_report_shape(tmp_path):
    module = _load_measure_run_journal_module()

    report = module.measure_volume(
        tmp_path,
        roster_seats=2,
        roster_attempts_per_seat=1,
        roster_pause_cycles=0,
        worst_seats=2,
        worst_attempts_per_seat=2,
        worst_pause_cycles=1,
    )

    assert report["issue"] == 635
    assert report["kind"] == "journal-volume"
    assert report["bounds"]["max_journal_events"] > 0
    assert report["bounds"]["max_journal_bytes"] > 0
    assert {"python_version", "platform", "filesystem_type", "directory_fsync_supported"} <= set(report["environment"])
    for key in ("representative_synthetic", "roster_sized_synthetic", "worst_case_synthetic"):
        entry = report[key]
        assert isinstance(entry["event_count"], int) and entry["event_count"] > 0
        assert isinstance(entry["journal_bytes"], int) and entry["journal_bytes"] > 0
        assert "headroom_events" in entry
        assert "pct_events" in entry
        assert "event_type_counts" in entry
    assert report["representative_synthetic"]["event_count"] < report["worst_case_synthetic"]["event_count"]
    assert report["scanned_real_runs"]["aggregate"]["runs"] == 0


def test_scan_lifecycle_journals_reads_existing_files(tmp_path):
    module = _load_measure_run_journal_module()
    journal = tmp_path / "runs" / "run-a" / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True)
    # Non-canonical lines still contribute to the volume survey via line count.
    journal.write_text('{"event_type":"run.created"}\n{"event_type":"run.completed"}\n')

    scanned = module.scan_lifecycle_journals([tmp_path])

    assert scanned["aggregate"]["runs"] == 1
    assert scanned["aggregate"]["event_count_max"] == 2
    assert scanned["per_run"][0]["journal_bytes"] == journal.stat().st_size


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        (lambda root: None, "missing"),
        (lambda root: root.mkdir(), "stale"),
    ],
)
def test_scan_lifecycle_journals_fails_closed_for_missing_or_stale_input(tmp_path, prepare, message):
    module = _load_measure_run_journal_module()
    scan_root = tmp_path / "scan-input"
    prepare(scan_root)

    with pytest.raises(module.MeasurementInputError, match=message):
        module.scan_lifecycle_journals([scan_root])


def test_scan_lifecycle_journals_fails_closed_for_unreadable_input(tmp_path, monkeypatch):
    module = _load_measure_run_journal_module()
    scan_root = tmp_path / "scan-input"
    scan_root.mkdir()

    def _raise_unreadable(_self, _pattern):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "rglob", _raise_unreadable)

    with pytest.raises(module.MeasurementInputError, match="unreadable"):
        module.scan_lifecycle_journals([scan_root])


def test_scan_lifecycle_journals_uses_complete_raw_count_after_valid_prefix_corruption(tmp_path):
    module = _load_measure_run_journal_module()
    journal = tmp_path / "runs" / "run-a" / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True)
    first = run_events.build_event(
        run_id="20260727-153045-a1b2c3d4",
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="first",
        recorded_at="2026-07-27T15:30:45.000000Z",
        previous_digest=None,
    )
    second = run_events.build_event(
        run_id="20260727-153045-a1b2c3d4",
        sequence=2,
        event_type="run.planning.started",
        payload={"detail": "planning"},
        idempotency_key="second",
        recorded_at="2026-07-27T15:30:46.000000Z",
        previous_digest=first["event_digest"],
    )
    journal.write_bytes(
        run_events.canonical_bytes(first) + b"\n{not-json}\n" + run_events.canonical_bytes(second) + b"\n"
    )

    scanned = module.scan_lifecycle_journals([tmp_path])

    assert scanned["per_run"][0]["event_count"] == 3
    assert scanned["per_run"][0]["omitted"] is False
    assert scanned["per_run"][0]["integrity_error"]
    assert scanned["aggregate"]["runs"] == 1
    assert scanned["aggregate"]["event_count_max"] == 3


def test_scan_lifecycle_journals_omits_undecodable_journal_without_zeroing_it(tmp_path):
    module = _load_measure_run_journal_module()
    journal = tmp_path / "runs" / "run-a" / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"\x80abc\n\xff")

    scanned = module.scan_lifecycle_journals([tmp_path])

    entry = scanned["per_run"][0]
    assert entry["event_count"] == 1
    assert entry["omitted"] is True
    assert entry["error"] == "undecodable journal"
    assert scanned["aggregate"]["runs"] == 0
    assert scanned["aggregate"]["omitted_runs"] == 1


def test_scan_lifecycle_journals_omits_stat_failure(tmp_path, monkeypatch):
    module = _load_measure_run_journal_module()
    journal = tmp_path / "runs" / "run-a" / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text("record\n")
    real_stat = Path.stat

    def fail_journal_stat(self, *args, **kwargs):
        if self == journal:
            raise OSError("stat race")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_journal_stat)

    scanned = module.scan_lifecycle_journals([tmp_path])

    entry = scanned["per_run"][0]
    assert entry["event_count"] is None
    assert entry["journal_bytes"] is None
    assert entry["omitted"] is True
    assert entry["error"].startswith("journal stat failed:")
    assert scanned["aggregate"]["runs"] == 0


def test_scan_lifecycle_journals_omits_second_read_failure(tmp_path, monkeypatch):
    module = _load_measure_run_journal_module()
    journal = tmp_path / "runs" / "run-a" / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text("record\n")

    def fail_second_read(*_args, **_kwargs):
        raise OSError("second read race")

    monkeypatch.setattr(module.run_journal, "read_journal_bounded", fail_second_read)

    scanned = module.scan_lifecycle_journals([tmp_path])

    entry = scanned["per_run"][0]
    assert entry["event_count"] == 1
    assert entry["omitted"] is True
    assert entry["error"].startswith("journal read failed:")
    assert scanned["aggregate"]["runs"] == 0


def test_measure_volume_fails_closed_when_a_scenario_stops_early(tmp_path, monkeypatch):
    module = _load_measure_run_journal_module()

    def _stopped_scenario(*_args, **_kwargs):
        return {"stopped_reason": "dispatch failed", "event_count": 0, "journal_bytes": 0}

    monkeypatch.setattr(module, "_drive_volume_scenario", _stopped_scenario)

    with pytest.raises(module.MeasurementInputError, match="scenario did not complete"):
        module.measure_volume(tmp_path)


def test_cli_volume_mode_writes_report(tmp_path):
    output = tmp_path / "out" / "volume.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "volume",
            "--worst-seats",
            "1",
            "--worst-attempts-per-seat",
            "1",
            "--worst-pause-cycles",
            "0",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["kind"] == "journal-volume"
    assert "threshold" not in output.read_text()
    assert "pass" not in output.read_text()
