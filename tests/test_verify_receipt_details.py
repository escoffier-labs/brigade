import json
import subprocess
from pathlib import Path

import pytest

from brigade import code_graph_delta, control_crosswalk_cosign, evidence_derivative, outcome, verify_trial
from brigade.work_cmd import helpers, verification, verify_argv_files, verify_receipt_details, verify_runtime


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
        verify_runtime,
        "_run_verify_child_process",
        lambda *args, **kwargs: ("failed", 75, "", f"slot taken\n{verify_receipt_details.BUSY_MARKER}\n"),
    )
    receipt, rc = verification._run_verify_commands(tmp_path, [["python", "-V"]], 30, code_graph_timeout=1)
    assert (receipt["status"], receipt["commands"][0]["status"], rc) == ("busy", "busy", 75)


def _quiet_graph(monkeypatch):
    monkeypatch.setattr(code_graph_delta, "capture_before", lambda *args, **kwargs: None)
    monkeypatch.setattr(code_graph_delta, "capture_after_and_diff", lambda *args, **kwargs: {})


def _children(monkeypatch, *results):
    pending = list(results)
    monkeypatch.setattr(verify_runtime, "_run_verify_child_process", lambda *args, **kwargs: pending.pop(0))


BUSY = ("failed", 75, "", f"{verify_receipt_details.BUSY_MARKER}\n")


@pytest.mark.parametrize(
    "stdout",
    [
        "Found 3 errors in 2 files (checked 412 source files)\n",
        "Found 2 errors.\n",
        "collected 10 items / 3 deselected / 7 selected\nINTERNALERROR> boom\n",
        "test result: ok. 12 passed; 0 failed\n",
        "7 xfailed\n",
    ],
)
def test_pytest_outcomes_ignore_lines_that_are_not_a_pytest_summary(stdout):
    assert verify_receipt_details.pytest_outcomes(stdout) is None


def test_pytest_outcomes_read_the_summary_line_not_stray_text():
    printed_by_a_test = "assert 'no tests ran in 0.01s' in output\n=== 1 failed, 40 passed in 2.10s ===\n"
    result = verify_receipt_details.pytest_outcomes(printed_by_a_test)
    assert (result["failed"], result["passed"], result["no_tests_ran"]) == (1, 40, False)
    with_warnings = verify_receipt_details.pytest_outcomes("12 passed, 2 warnings in 0.50s (0:00:01)\n")
    assert (with_warnings["passed"], with_warnings["no_tests_ran"]) == (12, False)
    error_only = verify_receipt_details.pytest_outcomes("1 error in 0.10s\n")
    assert (error_only["errors"], error_only["no_tests_ran"]) == (1, False)


def test_pytest_outcomes_count_a_run_that_only_deselected_as_no_tests():
    result = verify_receipt_details.pytest_outcomes("==== 10 deselected in 0.05s ====\n")
    assert (result["deselected"], result["no_tests_ran"]) == (10, True)


def test_pytest_outcomes_read_the_full_gate_aggregate_line():
    stdout = "5 passed in 1.00s\nAggregate: 4000 passed, 0 failed, 12 skipped, 0 errors\n7 xfailed\nSelection: ok\n"
    result = verify_receipt_details.pytest_outcomes(stdout)
    assert (result["passed"], result["skipped"], result["xfailed"]) == (4000, 12, 0)


def test_exit_75_without_the_marker_is_an_ordinary_failure(tmp_path, monkeypatch):
    _quiet_graph(monkeypatch)
    _children(monkeypatch, ("failed", 75, "", "some tool exited 75\n"))
    receipt, rc = verification._run_verify_commands(tmp_path, [["python", "-V"]], 30, code_graph_timeout=1)
    # The command record keeps the real exit code. The process does not return
    # 75, because callers read that code as a busy slot.
    assert (receipt["status"], receipt["commands"][0]["status"], rc) == ("failed", "failed", 1)
    assert receipt["commands"][0]["exit_code"] == 75
    assert outcome.SIGNAL_RULES[("verify", receipt["status"])] < 0


def test_a_real_failure_after_a_busy_command_does_not_exit_75(tmp_path, monkeypatch):
    _quiet_graph(monkeypatch)
    _children(monkeypatch, BUSY, ("failed", 3, "", "boom\n"))
    receipt, rc = verification._run_verify_commands(
        tmp_path, [["python", "-V"], ["python", "-V"]], 30, code_graph_timeout=1
    )
    assert (receipt["status"], [c["status"] for c in receipt["commands"]], rc) == ("failed", ["busy", "failed"], 3)


def test_busy_does_not_hide_a_bookkeeping_failure(tmp_path, monkeypatch):
    _quiet_graph(monkeypatch)
    _children(monkeypatch, BUSY)
    original = Path.write_text

    def fail_log(path, data, *args, **kwargs):
        if path.name.endswith("stdout.log"):
            raise OSError("simulated log failure")
        return original(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_log)
    receipt, rc = verification._run_verify_commands(tmp_path, [["python", "-V"]], 30, code_graph_timeout=1)
    assert (receipt["status"], receipt["failure_kind"], rc) == ("failed", "receipt_incomplete", 1)


def test_busy_does_not_hide_a_command_that_never_ran():
    receipt = {"planned_commands": ["gate", "tests"], "commands": [{"status": "busy", "exit_code": 75}]}
    assert verify_receipt_details.terminal_status(receipt, 75) == ("rejected", 1)


def test_an_empty_plan_is_never_completed():
    assert verify_receipt_details.terminal_status({"planned_commands": [], "commands": []}, 0) == ("rejected", 1)


def test_a_failed_receipt_write_is_not_reported_as_completed(tmp_path, monkeypatch):
    _quiet_graph(monkeypatch)
    _children(monkeypatch, ("completed", 0, "ok\n", ""))
    original = helpers._write_json
    failed_once = []

    def fail_first_receipt_write(path, payload, *args, **kwargs):
        if Path(path).name == "receipt.json" and not failed_once:
            failed_once.append(path)
            raise OSError("simulated full disk")
        return original(path, payload, *args, **kwargs)

    monkeypatch.setattr(helpers, "_write_json", fail_first_receipt_write)
    receipt, rc = verification._run_verify_commands(tmp_path, [["python", "-V"]], 30, code_graph_timeout=1)
    assert failed_once
    assert (receipt["status"], rc) == ("failed", 1)
    assert json.loads((Path(receipt["path"]) / "receipt.json").read_text())["status"] == "failed"


def test_tree_fields_follow_what_the_command_did(tmp_path, monkeypatch):
    from tests.work_cmd_test_helpers import _init_git_repo

    _init_git_repo(tmp_path)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=tmp_path,
        check=True,
    )
    _quiet_graph(monkeypatch)
    _children(monkeypatch, ("completed", 0, "", ""))
    stable, _ = verification._run_verify_commands(tmp_path, [["python", "-V"]], 30, code_graph_timeout=1)
    assert stable["tree_stable"] is True
    assert stable["tree_fingerprint_after"] == stable["tree_fingerprint"]

    def child_that_edits(*args, **kwargs):
        (tmp_path / "written-during-run.txt").write_text("changed")
        return ("completed", 0, "", "")

    monkeypatch.setattr(verify_runtime, "_run_verify_child_process", child_that_edits)
    moved, _ = verification._run_verify_commands(tmp_path, [["python", "-V"]], 30, code_graph_timeout=1)
    assert moved["tree_stable"] is False
    assert moved["tree_fingerprint_after"] != moved["tree_fingerprint"]


def test_reused_receipt_does_not_claim_an_observed_stable_tree():
    receipt: dict = {}
    verify_receipt_details.stamp_reused_tree(receipt, {"tree_fingerprint": "abc"})
    assert receipt == {"tree_fingerprint_after": "abc", "tree_stable": None}


@pytest.mark.parametrize("commands", [None, [], [{"status": "busy", "exit_code": 75}]])
def test_reuse_refuses_missing_empty_or_busy_command_records(tmp_path, monkeypatch, commands):
    monkeypatch.setattr(verify_argv_files, "argv_file_hashes", lambda target, planned: {})
    latest = {
        "status": "completed",
        "tree_fingerprint": "tree",
        "planned_commands": ["one"],
        "commands": commands,
        "argv_file_hashes": {},
    }
    assert not verify_argv_files.reusable_receipt(tmp_path, [["one"]], ["one"], "tree", latest)


def test_other_readers_know_the_busy_status():
    assert "busy" in evidence_derivative._RECEIPT_STATUSES
    assert control_crosswalk_cosign is not None


def test_a_receipt_without_a_planned_list_is_never_completed():
    receipt = {"commands": [{"status": "completed", "exit_code": 0}]}
    assert verify_receipt_details.terminal_status(receipt, 0) == ("rejected", 1)


def test_inherited_test_env_values_are_bounded():
    env = verify_receipt_details.inherited_test_env({"PYTEST_ADDOPTS": "x" * 5000})
    assert len(env["PYTEST_ADDOPTS"]) == 2048


def test_a_busy_retry_does_not_clear_an_older_uncaptured_failure(tmp_path, monkeypatch):
    failed = {"run_id": "one", "status": "failed", "planned_commands": ["gate"]}
    busy = {"run_id": "two", "status": "busy", "planned_commands": ["gate"]}
    monkeypatch.setattr(verification, "_verify_receipts", lambda target: [busy, failed])
    monkeypatch.setattr(verification, "_verify_receipt_has_outcome_capture", lambda target, receipt: False)
    identity = verification._receipt_planned_commands_identity(failed)
    assert verification._find_uncaptured_failed_verify_receipt(tmp_path, identity) is failed
