"""The selected-file driver retains full-suite validation and result policy."""

import json

import pytest

from scripts import windows_pytest as driver


@pytest.mark.parametrize(
    "status,allowlisted,expected",
    [("passed", False, 0), ("failed", True, 0), ("failed", False, 1), ("timeout", False, 1)],
)
def test_files_from_limits_execution_and_preserves_policy(tmp_path, monkeypatch, status, allowlisted, expected):
    names = ["test_selected.py", "test_other.py", "test_serial.py"]
    monkeypatch.setattr(driver, "is_windows", lambda: True)
    monkeypatch.setattr(driver, "install_console_handler", lambda: None)
    monkeypatch.setattr(driver, "discover_files", lambda repo: names)
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(driver, "make_temp_root", lambda repo: root)
    captured = {}

    def run(**kwargs):
        captured.update(kwargs)
        return [driver.FileResult("test_selected.py", status, 0, 0, "logs/selected.log")]

    monkeypatch.setattr(driver, "run_files", run)
    allowlist = tmp_path / "allowlist.json"
    # Unselected allowlist entries must still validate against full discovery.
    allowlist.write_text(
        json.dumps(
            {"known_failures": ["test_other.py"] + (["test_selected.py"] if allowlisted else []), "known_timeouts": []}
        )
    )
    selected = tmp_path / "files.txt"
    selected.write_text("test_selected.py\ntest_selected.py\n")
    record = tmp_path / "summary.json"
    assert (
        driver.main(
            [
                "--repo",
                str(tmp_path),
                "--allowlist",
                str(allowlist),
                "--record",
                str(record),
                "--files-from",
                str(selected),
                "--serial",
                "test_serial.py",
            ]
        )
        == expected
    )
    assert captured["files"] == ["test_selected.py"]
    assert captured["serial"] == set()
    assert json.loads(record.read_text())["expected_files"] == ["test_selected.py"]


@pytest.mark.parametrize("value", ["../test_escape.py", "test_missing.py", "tests/test_selected.py", ""])
def test_files_from_rejects_invalid_missing_or_empty_selection(tmp_path, value):
    selected = tmp_path / "files.txt"
    selected.write_text(value)
    with pytest.raises(ValueError):
        driver.load_selected_files(selected, ["test_selected.py"])
