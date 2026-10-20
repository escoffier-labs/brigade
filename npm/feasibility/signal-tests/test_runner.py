"""Owner checks for fail-closed signal-test selection, using harmless pytest cases."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HELPER = Path(__file__).with_name("supervisor.py")


@pytest.mark.parametrize("outcome", ["pass", "skip", "fail", "empty", "collection-error", "wrong-identity"])
def test_core_gate_requires_all_selected_cases_pass(tmp_path, outcome):
    """Exit zero alone cannot bless skipped, missing, or failed signal coverage."""
    tests = tmp_path / "tests"
    tests.mkdir()
    body = {"pass": "pass", "skip": "pytest.skip('deliberate')", "fail": "assert False"}.get(outcome, "pass")
    source = (
        "import pytest\n@pytest.fixture\ndef verify_signal_probe(): return None\n"  # content-guard: allow email
        "@pytest.mark.parametrize('case', [1, 2])\n"
        f"def test_selected(verify_signal_probe, case):\n    {body}\n"
        "def test_unrelated(): raise AssertionError('must not run')\n"
    )
    if outcome == "empty":
        source = "def test_unrelated(): pass\n"
    elif outcome == "collection-error":
        source += "\nraise RuntimeError('deliberate collection error')\n"
    if outcome == "wrong-identity":
        (tests / "conftest.py").write_text(
            "import pytest\n@pytest.hookimpl(hookwrapper=True)\n"  # content-guard: allow email
            "def pytest_runtest_makereport(item, call):\n"
            "    result = yield\n"
            "    result.get_result().nodeid = 'tests/test_other.py::test_substitute'\n"
        )
    (tests / "test_sample.py").write_text(source)
    code = (
        "import importlib.util,sys;from pathlib import Path;"
        "s=importlib.util.spec_from_file_location('supervisor',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "sys.exit(m.run_core(Path(sys.argv[2])))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(HELPER), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert "CORE_RESULT " in result.stdout, result.stdout + result.stderr
    evidence = json.loads(result.stdout.split("CORE_RESULT ")[-1].splitlines()[0])
    assert result.returncode == (0 if outcome == "pass" else 1), result.stdout + result.stderr
    assert evidence["status"] == ("passed" if outcome == "pass" else "failed")
    if outcome == "pass":
        assert evidence["selected_functions"] == ["tests/test_sample.py::test_selected"]
        assert evidence["collected"] == evidence["passed"] == 2
        assert evidence["skipped"] == evidence["failed"] == 0


def test_supervisor_refuses_host_namespace_even_with_approval():
    result = subprocess.run(
        [sys.executable, str(HELPER), "core", f"{sys.version_info.major}.{sys.version_info.minor}"],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "BRIGADE_PARENT_SIGNAL_APPROVAL": "approved",
            "BRIGADE_HOST_PID_NAMESPACE": os.readlink("/proc/self/ns/pid"),
        },
    )
    assert result.returncode != 0
    assert "separate PID namespace required" in result.stderr


def test_source_metadata_survives_bounded_log_tail(tmp_path):
    """Verbose test output must not erase the independent source identity proof."""
    import importlib.util
    import io

    spec = importlib.util.spec_from_file_location("signal_runner", HELPER.with_name("run.py"))
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    source = {"src/example.py": "a" * 64}
    stream = io.BytesIO(("SOURCE_SHA256 " + json.dumps(source) + "\n").encode() + b"ordinary test output\n" * 120000)
    log = tmp_path / "runtime.log"
    metadata = runner.capture_output(stream, log)
    assert metadata["SOURCE_SHA256"] == source
    assert log.stat().st_size <= 2 * 1024**2
    assert b"ordinary test output" in log.read_bytes()


def test_namespace_observation_waits_for_running_container(monkeypatch):
    """docker start --attach returns before the daemon marks the container running."""
    import importlib.util
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("signal_runner", HELPER.with_name("run.py"))
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    observations = iter([False, True])
    running = False

    def docker(*args, **kwargs):
        nonlocal running
        if args[0] == "inspect":
            running = next(observations)
            return SimpleNamespace(
                stdout=json.dumps([{"State": {"Running": running, "Status": "running" if running else "created"}}])
            )
        assert running, "namespace exec raced asynchronous container start"
        return SimpleNamespace(stdout="pid:[123456]\n")

    monkeypatch.setattr(runner, "docker", docker)
    assert runner.container_namespace("owned-container") == "pid:[123456]"


def test_exact_removal_proof_distinguishes_absence_from_inspection_errors():
    """Docker versions use both object and container in missing-resource errors."""
    import importlib.util
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("signal_runner", HELPER.with_name("run.py"))
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    for message in ["Error: No such container: owned", "error: no such object: owned"]:
        assert runner.container_absent(SimpleNamespace(returncode=1, stderr=message))
    for rc, message in [(0, ""), (1, "permission denied"), (1, "cannot connect to daemon")]:
        assert not runner.container_absent(SimpleNamespace(returncode=rc, stderr=message))
