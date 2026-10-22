"""Exercise diagnostics through a real pytest subprocess, including a killed test."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("hang", [False, True], ids=["completed", "file-timeout"])
def test_test_phase_diagnostics_survive_file_timeout(tmp_path, hang):
    test_file = tmp_path / "test_example.py"
    test_file.write_text(
        "import time\ndef test_example():\n    " + ("time.sleep(60)" if hang else "assert True") + "\n",
        encoding="utf-8",
    )
    progress = tmp_path / "progress.log"
    env = dict(os.environ, BRIGADE_WINDOWS_TEST_PROGRESS=str(progress), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
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
        str(test_file),
    ]
    if hang:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            subprocess.run(argv, env=env, capture_output=True, timeout=5, check=False)
        assert b"Timeout (0:00:01)" in caught.value.stderr
    else:
        result = subprocess.run(argv, env=env, capture_output=True, timeout=15, check=False)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    output = progress.read_text(encoding="utf-8")
    assert "test_example.py::test_example phase=setup" in output
    assert "phase=setup outcome=passed seconds=" in output
    assert "test_example.py::test_example phase=call" in output
    if hang:
        assert output.splitlines()[-1].endswith("phase=call started")
    else:
        assert "phase=call outcome=passed seconds=" in output
        assert "phase=teardown outcome=passed seconds=" in output
