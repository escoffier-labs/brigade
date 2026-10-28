import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failing", [False, True])
def test_shard_runner_aggregates_real_results_and_preserves_failure_output(tmp_path, failing):
    suite = tmp_path / "test_sample.py"
    suite.write_text(
        "import pytest\n"
        "def test_pass():\n    assert True\n"
        "@pytest.mark.skip(reason='sample skip')\n"
        "def test_skip():\n    pass\n"
        f"def test_other():\n    assert {not failing!r}, 'shard failure sentinel'\n"
    )
    env = dict(os.environ, BRIGADE_VERIFY_SHARDS="2")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_parallel.py"),
            str(suite),
            f"--confcutdir={tmp_path}",
            "--cov-reset",
            f"--cov={tmp_path}",
            "--cov-fail-under=0",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=45,
    )
    assert result.returncode == (1 if failing else 0), result.stdout + result.stderr
    assert (
        f"Aggregate: {1 if failing else 2} passed, {1 if failing else 0} failed, 1 skipped, 0 errors" in result.stdout
    )
    if failing:
        assert "FAILED shard" in result.stdout
        assert "shard failure sentinel" in result.stdout
    else:
        assert "Combined coverage" in result.stdout


@pytest.mark.skipif(pytest.version_tuple < (9,), reason="builtin subtest reporting requires pytest 9")
def test_shard_runner_keeps_subtest_counts_separate(tmp_path):
    suite = tmp_path / "test_subtests.py"
    suite.write_text(
        "import unittest\n"
        "class TestExample(unittest.TestCase):\n"
        "    def test_values(self):\n"
        "        for value in [1, 2]:\n"
        "            with self.subTest(value=value):\n"
        "                self.assertGreater(value, 0)\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_parallel.py"),
            str(suite),
            f"--confcutdir={tmp_path}",
            "--cov-reset",
            f"--cov={tmp_path}",
        ],
        env=dict(os.environ, BRIGADE_VERIFY_SHARDS="2"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Aggregate: 1 passed, 0 failed, 0 skipped, 0 errors" in result.stdout
    assert "2 subtests passed" in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="process group cancellation is POSIX")
@pytest.mark.parametrize("repeat", [False, True])
def test_shard_cancellation_stops_test_grandchildren(tmp_path, repeat):
    import signal
    import time

    heartbeat = tmp_path / "heartbeat"
    pid_file = tmp_path / "child.pid"
    leader_file = tmp_path / "leader.pid"
    child_code = (
        "import os,time,signal; from pathlib import Path; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if repeat else "")
        + f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        f"p=Path({str(heartbeat)!r}); "
        "\nfor i in range(400):\n p.write_text(str(i)); time.sleep(0.05)\n"
    )
    suite = tmp_path / "test_child.py"
    suite.write_text(
        "import subprocess,sys,time,signal,os\nfrom pathlib import Path\n"
        "def test_child():\n"
        f"    Path({str(leader_file)!r}).write_text(str(os.getpid()))\n"
        + ("    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if repeat else "")
        + f"    subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "    time.sleep(30)\n"
    )
    runner = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts/verify_parallel.py"),
            str(suite),
            f"--confcutdir={tmp_path}",
        ],
        env=dict(os.environ, BRIGADE_VERIFY_SHARDS="1"),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not heartbeat.exists():
            assert runner.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        runner.terminate()
        if repeat:
            time.sleep(0.7)
            runner.terminate()
        runner.communicate(timeout=15)
        assert runner.returncode == 130
        time.sleep(0.2)
        stopped = heartbeat.read_text()
        time.sleep(0.2)
        assert heartbeat.read_text() == stopped
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.communicate()
        for path in (pid_file, leader_file):
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_combined_coverage_floor_fails_after_successful_shards(tmp_path):
    suite = tmp_path / "test_low_coverage.py"
    suite.write_text(
        "def test_pass():\n    assert True\n"
        + "\n".join(f"def unused_{index}():\n    return {index}\n" for index in range(20))
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_parallel.py"),
            str(suite),
            f"--confcutdir={tmp_path}",
            "--cov-reset",
            f"--cov={tmp_path}",
        ],
        env=dict(os.environ, BRIGADE_VERIFY_SHARDS="2"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode != 0
    assert "Aggregate: 1 passed, 0 failed, 0 skipped, 0 errors" in result.stdout
    assert "Coverage failure" in result.stdout
