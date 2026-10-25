"""Exercise diagnostics through a real pytest subprocess, including a killed test."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import windows_job, windows_pytest


@pytest.mark.parametrize("hang", [None, "call", "session-exit", "collection"])
def test_test_phase_diagnostics_survive_file_timeout(tmp_path, hang):
    test_file = tmp_path / "test_example.py"
    test_file.write_text(
        "import time, threading\ndef test_example():\n    "
        + {
            None: "assert True",
            "call": "time.sleep(60)",
            "session-exit": "threading.Thread(target=time.sleep, args=(60,)).start()",
            "collection": "assert True",
        }[hang]
        + "\n",
        encoding="utf-8",
    )
    progress = tmp_path / "progress.log"
    env = dict(os.environ, BRIGADE_WINDOWS_TEST_PROGRESS=str(progress), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    if hang == "collection":
        (tmp_path / "conftest.py").write_text(
            "import time\ndef pytest_collection_modifyitems(items):\n    time.sleep(60)\n", encoding="utf-8"
        )
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "scripts.windows_pytest_diagnostics",
        "-o",
        "addopts=",
        "-o",
        "faulthandler_timeout=1",
        f"--rootdir={tmp_path}",
        str(test_file),
    ]
    last_phase = {
        "call": "test_example.py::test_example phase=call started",
        "session-exit": "session phase=unconfigure completed",
        "collection": "session phase=collection started",
    }.get(hang)
    log = tmp_path / "child.log"
    with log.open("wb") as stream:
        process = subprocess.Popen(argv, env=env, cwd=Path(__file__).resolve().parents[1], stdout=stream, stderr=stream)
        try:
            if hang:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    evidence = progress.read_text(encoding="utf-8") if progress.exists() else ""
                    stack_ready = hang != "call" or b"Timeout (0:00:01)" in log.read_bytes()
                    if last_phase in evidence and stack_ready:
                        break
                    assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")
                    time.sleep(0.02)
                else:
                    pytest.fail(f"child did not reach {last_phase}: {evidence}")
            else:
                assert process.wait(timeout=30) == 0, log.read_text(encoding="utf-8", errors="replace")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
    output = progress.read_text(encoding="utf-8")
    if hang == "collection":
        assert output.splitlines()[-1] == last_phase
        return
    assert "test_example.py::test_example phase=setup" in output
    assert "phase=setup outcome=passed seconds=" in output
    assert "test_example.py::test_example phase=call" in output
    if hang == "call":
        assert output.splitlines()[-1].endswith("phase=call started")
    else:
        assert "phase=call outcome=passed seconds=" in output
        assert "phase=teardown outcome=passed seconds=" in output
        assert output.splitlines()[-1] == "session phase=unconfigure completed"


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_driver_names_last_phase_even_when_cleanup_is_unconfirmed(tmp_path, monkeypatch, cleanup_error):
    output = tmp_path / "output"
    progress = output / "logs" / "test_example.py.progress.log"
    progress.parent.mkdir(parents=True)
    progress.write_text("stale previous run\n", encoding="utf-8")

    class Process:
        cause = windows_job.FILE_TIMEOUT
        token = SimpleNamespace(leaked_descendants=0)

        def wait(self, timeout):
            if cleanup_error:
                raise windows_pytest.CleanupUnconfirmed("root process wait expired")
            return self.cause

        def finish(self, **kwargs):
            pass

        def close(self):
            pass

    def launch(**kwargs):
        assert not progress.exists()
        assert kwargs["env"]["BRIGADE_WINDOWS_TEST_PROGRESS"] == str(progress.resolve())
        progress.write_text("test_example.py::test_hang phase=teardown started\n", encoding="utf-8")
        return Process()

    monkeypatch.setattr(windows_pytest, "launch_process", launch)
    result = windows_pytest.run_file(
        "test_example.py",
        repo=tmp_path,
        python=Path(sys.executable),
        output_dir=output,
        temp_root=tmp_path,
        timeout_seconds=10,
    )
    assert result.status == ("cleanup-unconfirmed" if cleanup_error else "timeout")
    assert "test_example.py::test_hang phase=teardown started" in result.diagnostic


@pytest.mark.parametrize("unstarted", [False, True])
def test_deadline_attributes_progress_only_to_started_files(tmp_path, unstarted):
    progress = tmp_path / "logs" / "test_example.py.progress.log"
    progress.parent.mkdir()
    progress.write_text("test_example.py::previous_test phase=call started\n", encoding="utf-8")
    result = windows_pytest._deadline_failure("test_example.py", tmp_path, unstarted=unstarted)
    assert ("previous_test" in result.diagnostic) is not unstarted


@pytest.mark.parametrize(
    "patched_open,target_plugin,patched_environment_get",
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
    ids=["plain", "patched-path-open", "target-plugin", "patched-environment-get"],
)
def test_driver_loads_diagnostics_outside_its_own_checkout(
    tmp_path, monkeypatch, patched_open, target_plugin, patched_environment_get
):
    repo = tmp_path / "checkout"
    (repo / "tests").mkdir(parents=True)
    (repo / "checkout_support.py").write_text("VALUE = 42\n", encoding="utf-8")
    if target_plugin:
        (repo / "scripts").mkdir()
        (repo / "scripts" / "__init__.py").touch()
        (repo / "scripts" / "windows_pytest_diagnostics.py").write_text(
            "raise RuntimeError('target diagnostics plugin loaded')\n", encoding="utf-8"
        )
    source = "from checkout_support import VALUE\ndef test_example(monkeypatch):\n    assert VALUE == 42\n"
    if patched_open:
        source += (
            "    from pathlib import Path\n    monkeypatch.setattr(Path, 'open', lambda *args, **kwargs: object())\n"
        )
    if patched_environment_get:
        source += (
            "    import os\n    def forbid_environment_get(*args, **kwargs):\n"
            "        raise AssertionError('environment lookup after pytest starts')\n"
            "    monkeypatch.setattr(os.environ, 'get', forbid_environment_get, raising=False)\n"
        )
    (repo / "tests" / "test_example.py").write_text(source, encoding="utf-8")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    def launch(**kwargs):
        process = subprocess.Popen(
            [str(kwargs["python"]), *kwargs["args"]],
            cwd=kwargs["cwd"],
            env=kwargs["env"],
            stdout=kwargs["log"],
            stderr=subprocess.STDOUT,
        )
        return SimpleNamespace(
            wait=process.wait,
            cause=None,
            token=SimpleNamespace(leaked_descendants=0),
            terminate=lambda cause: process.kill(),
            finish=lambda **ignored: None,
            close=lambda: None,
        )

    monkeypatch.setattr(windows_pytest, "launch_process", launch)
    output = tmp_path / "output"
    result = windows_pytest.run_file(
        "test_example.py",
        repo=repo,
        python=Path(sys.executable),
        output_dir=output,
        temp_root=tmp_path / "temps",
        timeout_seconds=30,
    )
    assert result.status == "passed", (output / result.log).read_text(encoding="utf-8", errors="replace")
    progress = output / "logs" / "test_example.py.progress.log"
    assert "phase=call outcome=passed seconds=" in progress.read_text(encoding="utf-8")
