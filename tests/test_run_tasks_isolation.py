"""Reload coverage must preserve task class identity for later contract tests."""

from pathlib import Path
import subprocess
import sys


def test_task_reload_preserves_native_contract_in_same_process():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_run_tasks.py::test_acceptance_and_attempt_replay_survive_module_reload",
            "tests/test_native_t3_contract.py::test_dispatch_payload_batch_client_key_and_all_authority_fields_conflict[changes7]",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
