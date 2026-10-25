"""Builtin repo sweeps must not import the inspected repository's Python."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade.repos_cmd import constants, sweeps
from tests.test_repos_sweep_health_cmd import _init_repo


@pytest.mark.parametrize("shadow", ["module", "package", "pythonpath"])
def test_builtin_repo_sweeps_use_trusted_package_and_explicit_target(tmp_path, monkeypatch, shadow):
    repo = tmp_path / "inspected workspace"
    _init_repo(repo)
    marker = tmp_path / "shadow-executed"
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    root = hostile if shadow == "pythonpath" else repo
    planted = root / ("brigade/__init__.py" if shadow == "package" else "brigade.py")
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(0)\n")
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    entry = constants.RepoEntry("example", "Example", repo)
    run = subprocess.run
    launches = []

    def observe(argv, **kwargs):
        launches.append((argv, kwargs))
        return run(argv, **kwargs)

    observed_subprocess = SimpleNamespace(**vars(subprocess))
    observed_subprocess.run = observe
    monkeypatch.setattr(sweeps, "subprocess", observed_subprocess)
    for command in sweeps._sweep_commands():
        result = sweeps._run_sweep_command(entry, command, tmp_path / "sweep")
        assert not marker.exists(), f"{command.label} executed repository-controlled Python"
        assert result["exit_code"] == 0, result
        output = json.loads((tmp_path / "sweep/logs/example" / command.label / "stdout.log").read_text())
        assert output["target"] == str(repo), "builtin lost the inspected target"
    assert len(launches) == 3
    for argv, kwargs in launches:
        assert argv[1:3] == ["-I", "-c"]
        assert argv[argv.index("--target") + 1] == str(repo.resolve())
        assert Path(kwargs["cwd"]).resolve() != repo.resolve()


def test_custom_health_command_keeps_repo_cwd_and_environment(tmp_path, monkeypatch):
    repo = tmp_path / "workspace"
    repo.mkdir()
    monkeypatch.setenv("CUSTOM_HEALTH_VALUE", "kept")
    command = constants.SweepCommand(
        "custom", [sys.executable, "-c", "import os;print(os.getcwd());print(os.environ['CUSTOM_HEALTH_VALUE'])"]
    )
    result = sweeps._run_sweep_command(constants.RepoEntry("example", "Example", repo), command, tmp_path / "sweep")
    assert result["exit_code"] == 0
    assert (tmp_path / "sweep/logs/example/custom/stdout.log").read_text().splitlines() == [str(repo), "kept"]
