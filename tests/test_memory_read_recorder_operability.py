"""Operator visibility, clearing, and bounded failure markers for the read recorder."""

from datetime import date, timedelta
import hashlib
import io
import json
import os

import pytest

from brigade import (
    cli,
    doctor as doctor_mod,
    memory_card_reads as reads,
    memory_cmd,
    memory_read_recorder_cmd,
)
from tests.memory_read_fixtures import coverage
from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority
from tests.memory_read_fixtures import signing_key

TODAY = date(2026, 11, 15)


def _sentinel(tmp_path, text="recorder-error\n"):
    path = tmp_path / reads.DISABLED_REL
    path.write_text(text)
    return path


def _deny_coverage(*args, **kwargs):
    raise PermissionError("coverage unavailable")


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


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


def test_clear_starts_a_fresh_window_over_an_outage(tmp_path):
    start, outage, clear_day = TODAY - timedelta(days=100), TODAY - timedelta(days=6), TODAY - timedelta(days=4)
    coverage(tmp_path, start, outage)
    _sentinel(tmp_path)
    assert reads.load(tmp_path).recorder_error
    result = reads.clear_disabled(tmp_path, today=clear_day)
    assert result == {"cleared": True, "recorded": True}
    assert reads.record(tmp_path, ["notes"], today=clear_day)
    ledger = reads.load(tmp_path)
    assert ledger.healthy_since == clear_day
    assert not reads.covered(ledger, today=clear_day, days=30)


def test_clear_keeps_the_sentinel_when_the_window_reset_fails(tmp_path, monkeypatch):
    signing_key(tmp_path)
    sentinel = _sentinel(tmp_path)
    monkeypatch.setattr(reads, "_write_json", _deny_coverage)
    result = reads.clear_disabled(tmp_path, today=TODAY)
    assert result == {"cleared": False, "reason": "coverage reset failed"}
    assert sentinel.exists()
    assert not (tmp_path / reads.CLEARED_REL).exists()


def test_load_honors_a_recorded_clear_after_a_ledger_rollback(tmp_path):
    start, outage, clear_day = TODAY - timedelta(days=100), TODAY - timedelta(days=6), TODAY - timedelta(days=4)
    coverage(tmp_path, start, outage)
    reads._write_json(
        tmp_path,
        reads.CLEARED_REL,
        {"sentinel": reads.DISABLED_REL, "day": clear_day.isoformat(), "cause_sha256": "0" * 64},
    )
    ledger = reads.load(tmp_path)
    assert ledger.healthy_since == clear_day
    assert not reads.covered(ledger, today=clear_day, days=30)
    assert reads.record(tmp_path, ["notes"], today=clear_day)
    assert reads.load(tmp_path).healthy_since == clear_day


def test_rewritten_failure_marker_survives_cleanup(tmp_path, monkeypatch):
    signing_key(tmp_path)
    reads._record_failure(tmp_path, today=TODAY)
    marker = tmp_path / reads.FAILURES_REL / f"{TODAY.isoformat()}.json"
    real = reads._failures

    def rewritten(target):
        entries = real(target)
        marker.write_text(json.dumps({"day": TODAY.isoformat(), "count": 99}, sort_keys=True) + "\n")
        return entries

    monkeypatch.setattr(reads, "_failures", rewritten)
    assert reads.record(tmp_path, ["notes"], today=TODAY)
    assert marker.exists()
    assert json.loads(marker.read_text())["count"] == 99


def test_failed_marker_write_falls_back_to_the_sentinel(tmp_path, monkeypatch):
    signing_key(tmp_path)

    def denied(*args, **kwargs):
        raise OSError("marker storage unavailable")

    monkeypatch.setattr(reads, "_failure_count", denied)
    reads._record_failure(tmp_path, today=TODAY)
    assert (tmp_path / reads.DISABLED_REL).read_text() == "recorder-error\n"
    assert not (tmp_path / reads.FAILURES_REL).exists()
    assert reads.load(tmp_path).recorder_error


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(b"", "empty"), (b"\xff\xferecorder-error\n", "\ufffd\ufffdrecorder-error")],
)
def test_empty_and_non_utf8_sentinel_causes(tmp_path, capsys, payload, expected):
    signing_key(tmp_path)
    (tmp_path / reads.DISABLED_REL).write_bytes(payload)
    state = reads.disabled_sentinel(tmp_path)
    assert state.present
    assert state.cause == expected
    memory_cmd.status(target=tmp_path)
    assert "read_recorder" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard links")
def test_clear_refuses_a_hardlink(tmp_path, capsys):
    signing_key(tmp_path)
    cause = tmp_path / "cause.txt"
    cause.write_text("recorder-error\n")
    os.link(cause, tmp_path / reads.DISABLED_REL)
    assert cli.main(["memory", "care", "clear-read-recorder", "--target", str(tmp_path), "--confirm"]) == 2
    assert (tmp_path / reads.DISABLED_REL).exists()
    assert "multiple hard links" in capsys.readouterr().err


def test_clear_refuses_a_directory(tmp_path, capsys):
    signing_key(tmp_path)
    sentinel = tmp_path / reads.DISABLED_REL
    sentinel.mkdir()
    assert cli.main(["memory", "care", "clear-read-recorder", "--target", str(tmp_path), "--confirm"]) == 2
    assert sentinel.is_dir()
    assert "not a regular file" in capsys.readouterr().err


def test_tty_prompt_yes_clears_the_sentinel(tmp_path):
    signing_key(tmp_path)
    sentinel = _sentinel(tmp_path)
    assert memory_read_recorder_cmd.clear(target=tmp_path, confirm=True, stdin=_Tty("y\n")) == 0
    assert not sentinel.exists()


def test_tty_prompt_no_keeps_the_sentinel(tmp_path, capsys):
    signing_key(tmp_path)
    sentinel = _sentinel(tmp_path)
    assert memory_read_recorder_cmd.clear(target=tmp_path, confirm=True, stdin=_Tty("n\n")) == 2
    assert sentinel.exists()
    assert "not cleared" in capsys.readouterr().err
