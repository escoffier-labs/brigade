"""Launcher metadata is recognized without a Brigade-specific override."""

import json
import os
import shutil
import subprocess
import venv
from pathlib import Path

import pytest

from brigade import telemetry_usage


@pytest.mark.parametrize("channel", ["pip", "pipx", "uvx"])
def test_installed_channel_from_launcher_metadata(tmp_path, channel):
    # Custom homes deliberately contain no launcher name. These are the files
    # pipx and uv write into their environments, independent of their location.
    prefix = tmp_path / "custom-home" / "venvs" / "brigade-cli"
    builder = venv.EnvBuilder(with_pip=False)
    builder.create(prefix)
    context = builder.ensure_directories(prefix)
    python = context.env_exec_cmd
    probe = subprocess.run(
        [python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    package = Path(probe.stdout.strip()) / "brigade"
    package.mkdir()
    source = Path(telemetry_usage.__file__).parent
    for name in ("__init__.py", "distribution.py", "telemetry_usage.py"):
        shutil.copyfile(source / name, package / name)
    if channel == "pipx":
        (prefix / "pipx_metadata.json").write_text(json.dumps({"main_package": {"package": "brigade-cli"}}))
    elif channel == "uvx":
        with (prefix / "pyvenv.cfg").open("a") as config:
            config.write("uv = 0.12.23\n")
    env = {key: value for key, value in os.environ.items() if key != "BRIGADE_INSTALL_CHANNEL"}
    result = subprocess.run(
        [
            python,
            "-I",
            "-c",
            "import os; from brigade.telemetry_usage import install_channel; print(install_channel(os.environ))",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == channel
