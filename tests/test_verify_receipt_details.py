from pathlib import Path

from brigade import code_graph_delta, outcome, verify_trial
from brigade.work_cmd import verification, verify_argv_files, verify_receipt_details, verify_runtime


def test_pytest_outcomes_parse_quiet_summary_and_no_tests():
    assert verify_receipt_details.pytest_outcomes(". [100%]\n1 passed, 2 skipped, 3 deselected in 0.02s\n") == {
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 2,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 3,
        "no_tests_ran": False,
    }
    empty = verify_receipt_details.pytest_outcomes("no tests ran in 0.01s\n")
    assert empty is not None and empty["no_tests_ran"] is True
    assert verify_receipt_details.pytest_outcomes("ordinary command output\n") is None


def test_terminal_status_requires_every_planned_command_to_pass():
    receipt = {"planned_commands": ["one", "two"], "commands": [{"status": "completed", "exit_code": 0}]}
    assert verify_receipt_details.terminal_status(receipt, 0) == ("rejected", 1)


def test_terminal_status_preserves_busy_as_neutral_exit():
    receipt = {"planned_commands": ["gate"], "commands": [{"status": "busy", "exit_code": 75}]}
    assert verify_receipt_details.terminal_status(receipt, 75) == ("busy", 75)
    assert outcome.SIGNAL_RULES[("verify", "busy")] == 0
    receipt["status"] = "busy"
    verify_trial.stamp_verify_receipt_failure_taxonomy(receipt)
    assert (receipt["failure_class"], receipt["failure_kind"]) == ("infrastructure", "busy")


def test_inherited_test_env_is_bounded():
    assert verify_receipt_details.inherited_test_env(
        {"PYTEST_ADDOPTS": "-q", "PYTEST_PLUGINS": "sample", "PYTHONPATH": "secret/path"}
    ) == {"PYTEST_ADDOPTS": "-q", "PYTEST_PLUGINS": "sample", "pythonpath_set": True}


def test_reuse_rejects_incomplete_completed_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_argv_files, "argv_file_hashes", lambda target, commands: {})
    latest = {
        "status": "completed",
        "tree_fingerprint": "tree",
        "planned_commands": ["one", "two"],
        "commands": [{"status": "completed", "exit_code": 0}],
        "argv_file_hashes": {},
    }
    assert not verify_argv_files.reusable_receipt(tmp_path, [["one"], ["two"]], ["one", "two"], "tree", latest)


def test_log_write_failure_is_nonpassing(tmp_path, monkeypatch):
    monkeypatch.setattr(code_graph_delta, "capture_before", lambda *args, **kwargs: None)
    monkeypatch.setattr(code_graph_delta, "capture_after_and_diff", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        verify_runtime, "_run_verify_child_process", lambda *args, **kwargs: ("completed", 0, "ok\n", "")
    )
    original = Path.write_text

    def fail_log(path, data, *args, **kwargs):
        if path.name.endswith("stdout.log"):
            raise OSError("simulated log failure")
        return original(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_log)
    receipt, rc = verification._run_verify_commands(tmp_path, [["python", "-V"]], 30, code_graph_timeout=1)
    assert rc != 0
    assert receipt["status"] != "completed"
    assert receipt["failure_class"] == "infrastructure"


def test_busy_command_and_receipt_return_75(tmp_path, monkeypatch):
    monkeypatch.setattr(code_graph_delta, "capture_before", lambda *args, **kwargs: None)
    monkeypatch.setattr(code_graph_delta, "capture_after_and_diff", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        verify_runtime, "_run_verify_child_process", lambda *args, **kwargs: ("failed", 75, "", "slot busy\n")
    )
    receipt, rc = verification._run_verify_commands(tmp_path, [["python", "-V"]], 30, code_graph_timeout=1)
    assert (receipt["status"], receipt["commands"][0]["status"], rc) == ("busy", "busy", 75)
