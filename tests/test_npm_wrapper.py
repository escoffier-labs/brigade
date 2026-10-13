"""Tests for the zero-dependency npm launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_launcher(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    assert node is not None
    env = os.environ.copy()
    env["PATH"] = str(tmp_path)
    return subprocess.run(
        [node, str(ROOT / "bin/brigade.js"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def test_package_exposes_brigade_bin_at_python_version():
    package = json.loads((ROOT / "package.json").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert package["name"] == "brigade-cli"
    assert package["bin"] == {"brigade": "bin/brigade.js"}
    assert f'version = "{package["version"]}"' in pyproject
    assert package["dependencies"] == {}

    dependabot = (ROOT / ".github/dependabot.yml").read_text()
    assert "- package-ecosystem: npm\n    directory: /" in dependabot


def test_launcher_prefers_existing_brigade_and_forwards_arguments(tmp_path):
    _executable(tmp_path / "brigade", 'printf "existing:%s\\n" "$*"\nexit 7\n')

    result = _run_launcher(tmp_path, "doctor", "--json")

    assert result.returncode == 7
    assert result.stdout == "existing:doctor --json\n"


def test_launcher_falls_back_to_pipx_run(tmp_path):
    _executable(tmp_path / "pipx", 'printf "pipx:%s\\n" "$*"\n')

    result = _run_launcher(tmp_path, "--version")

    assert result.returncode == 0
    assert result.stdout == "pipx:run brigade-cli --version\n"


def test_launcher_reports_install_commands_when_no_backend_exists(tmp_path):
    result = _run_launcher(tmp_path, "--version")

    assert result.returncode == 1
    assert "pipx install brigade-cli" in result.stderr
    assert "python -m pip install brigade-cli" in result.stderr
