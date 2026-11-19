"""Operator visibility, clearing, and bounded failure markers for the read recorder."""

from datetime import date, timedelta
import hashlib
import json
import os

import pytest

from brigade import cli, doctor as doctor_mod, memory_card_reads as reads, memory_cmd
from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority
from tests.memory_read_fixtures import signing_key

TODAY = date(2026, 11, 15)


def _sentinel(tmp_path, text="recorder-error\n"):
    path = tmp_path / reads.DISABLED_REL
    path.write_text(text)
    return path


def _deny_coverage(*args, **kwargs):
    raise PermissionError("coverage unavailable")


@pytest.mark.parametrize("surface", ["memory-care-status", "memory-care-doctor", "doctor"])
def test_surfaces_report_sentinel_and_cause(tmp_path, capsys, surface):
    signing_key(tmp_path)
    _sentinel(tmp_path)
    if surface == "memory-care-status":
        memory_cmd.status(target=tmp_path)
    elif surface == "memory-care-doctor":
        memory_cmd.doctor(target=tmp_path)
    else:
        doctor_mod.run(target=tmp_path)
    out = capsys.readouterr().out
    assert reads.DISABLED_REL in out
    assert "recorder-error" in out
    assert "clear-read-recorder" in out


def test_surfaces_encode_a_hostile_sentinel_cause(tmp_path, capsys):
    signing_key(tmp_path)
    _sentinel(tmp_path, "recorder-error\x1b[31m\x0aforged\r\n")
    memory_cmd.status(target=tmp_path)
    out = capsys.readouterr().out
    line = next(row for row in out.splitlines() if "read_recorder" in row)
    assert "\x1b" not in line
    assert "\\x1b" in line
    assert "\\x0a" in line
    assert "forged" in line


def test_sentinel_cause_read_is_bounded(tmp_path, capsys):
    signing_key(tmp_path)
    _sentinel(tmp_path, "x" * 4096)
    assert reads.disabled_sentinel(tmp_path).cause == "x" * reads.DISABLED_CAUSE_BYTES
    memory_cmd.status(target=tmp_path)
    line = next(row for row in capsys.readouterr().out.splitlines() if "read_recorder" in row)
    assert "[truncated]" in line
    assert len(line) < 300


def test_clear_read_recorder_removes_sentinel_and_records_it(tmp_path, capsys):
    signing_key(tmp_path)
    sentinel = _sentinel(tmp_path)
    assert cli.main(["memory", "care", "clear-read-recorder", "--target", str(tmp_path), "--confirm"]) == 0
    assert not sentinel.exists()
    record = json.loads((tmp_path / reads.CLEARED_REL).read_text())
    assert record["sentinel"] == reads.DISABLED_REL
    assert record["cause_sha256"] == hashlib.sha256(b"recorder-error\n").hexdigest()
    assert date.fromisoformat(record["day"])
    assert "cleared" in capsys.readouterr().out


def test_clear_read_recorder_requires_confirm(tmp_path, capsys):
    signing_key(tmp_path)
    sentinel = _sentinel(tmp_path)
    assert cli.main(["memory", "care", "clear-read-recorder", "--target", str(tmp_path)]) == 2
    assert sentinel.exists()
    assert "--confirm" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")
def test_clear_read_recorder_refuses_a_symlink(tmp_path, capsys):
    signing_key(tmp_path)
    cause = tmp_path / "cause.txt"
    cause.write_text("recorder-error\n")
    sentinel = tmp_path / reads.DISABLED_REL
    sentinel.symlink_to(cause)
    assert cli.main(["memory", "care", "clear-read-recorder", "--target", str(tmp_path), "--confirm"]) == 2
    assert sentinel.is_symlink()
    assert cause.read_text() == "recorder-error\n"
    assert not (tmp_path / reads.CLEARED_REL).exists()
    assert "not a regular file" in capsys.readouterr().err


def test_repeated_failing_reads_keep_one_marker_per_interval(tmp_path, monkeypatch):
    signing_key(tmp_path)
    monkeypatch.setattr(reads, "_write_json", _deny_coverage)
    for _ in range(1000):
        assert reads.record(tmp_path, ["notes"], today=TODAY) is False
    markers = sorted(path for path in (tmp_path / reads.FAILURES_REL).iterdir() if path.is_file())
    assert len(markers) == 1
    assert json.loads(markers[0].read_text()) == {"day": TODAY.isoformat(), "count": 1000}
    ledger = reads.load(tmp_path)
    assert ledger.recorder_error
    assert not reads.covered(ledger, today=TODAY, days=30)


def test_failure_markers_start_one_per_day_interval(tmp_path, monkeypatch):
    signing_key(tmp_path)
    monkeypatch.setattr(reads, "_write_json", _deny_coverage)
    assert reads.record(tmp_path, ["notes"], today=TODAY) is False
    assert reads.record(tmp_path, ["notes"], today=TODAY + timedelta(days=1)) is False
    markers = sorted(path for path in (tmp_path / reads.FAILURES_REL).iterdir() if path.is_file())
    assert [json.loads(path.read_text())["count"] for path in markers] == [1, 1]
