from pathlib import Path

import pytest

from brigade import graphtrail_delta


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_capture_before_propagates_process_signals(monkeypatch, tmp_path, signal_type):
    def raise_signal():
        raise signal_type()

    monkeypatch.setattr(graphtrail_delta, "_graphtrail_bin", raise_signal)

    with pytest.raises(signal_type):
        graphtrail_delta.capture_before(tmp_path, tmp_path / "run")


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_capture_after_and_diff_propagates_process_signals(monkeypatch, tmp_path, signal_type):
    def raise_signal(*args, **kwargs):
        raise signal_type()

    before = {
        "ok": True,
        "binary": "graphtrail",
        "db_path": str(tmp_path / ".graphtrail" / "graphtrail.db"),
        "before_snapshot_path": str(tmp_path / "run" / graphtrail_delta.SNAPSHOT_NAME),
    }
    monkeypatch.setattr(graphtrail_delta, "_run_graphtrail", raise_signal)

    with pytest.raises(signal_type):
        graphtrail_delta.capture_after_and_diff(tmp_path, tmp_path / "run", before)


@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_compact_summary_propagates_process_signals(signal_type):
    class SignalMapping(dict):
        def get(self, key, default=None):
            raise signal_type()

    with pytest.raises(signal_type):
        graphtrail_delta._compact_summary(SignalMapping())


def test_capture_before_still_fails_open_for_regular_exceptions(monkeypatch, tmp_path):
    def fail():
        raise OSError("graph unavailable")

    monkeypatch.setattr(graphtrail_delta, "_graphtrail_bin", fail)

    result = graphtrail_delta.capture_before(Path(tmp_path), tmp_path / "run")

    assert result["status"] == "capture_failed"
    assert "OSError: graph unavailable" in result["summary"]
