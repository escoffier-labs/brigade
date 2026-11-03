"""The scrubbed child environment must not carry the outer coverage run's variables."""

import os
from pathlib import Path

import pytest

from tests._coverage_env import isolated_coverage_env, scrub_coverage_environ

ROOT = Path(__file__).resolve().parents[1]
OUTER = {
    "COV_CORE_SOURCE": "/work/src/brigade",
    "COV_CORE_CONFIG": "/work/pyproject.toml",
    "COV_CORE_DATAFILE": "/work/.coverage.3.12.3",
    "COVERAGE_PROCESS_START": "/work/pyproject.toml",
    "COVERAGE_PROCESS_CONFIG": ":data:{}",
    "COVERAGE_PROCESS_SLUG": "x",
    "COVERAGE_FILE": ".coverage.3.12.3",
}


def _outer_names(env):
    return sorted(name for name in env if name.startswith(("COV_CORE_", "COVERAGE_PROCESS_")))


def test_scrubbed_environment_drops_every_outer_coverage_variable():
    env = isolated_coverage_env(base={**OUTER, "COVERAGE_CORE": "sysmon", "PATH": "/bin"})
    assert _outer_names(env) == []
    assert "COVERAGE_FILE" not in env
    assert env["COVERAGE_CORE"] == "sysmon"
    assert env["PATH"] == "/bin"


def test_scrubbed_environment_sets_only_the_requested_data_file(tmp_path):
    env = isolated_coverage_env(tmp_path / ".coverage.3.12.0", base=OUTER, PYTHONPATH="/probe")
    assert _outer_names(env) == []
    assert env["COVERAGE_FILE"] == str(tmp_path / ".coverage.3.12.0")
    assert env["PYTHONPATH"] == "/probe"


def test_live_process_environment_is_scrubbed_even_inside_a_coverage_run(monkeypatch):
    for name, value in OUTER.items():
        monkeypatch.setenv(name, value)
    assert _outer_names(isolated_coverage_env()) == []
    assert "COVERAGE_FILE" not in isolated_coverage_env()
    scrub_coverage_environ(monkeypatch)
    assert _outer_names(os.environ) == []
    assert "COVERAGE_FILE" not in os.environ


@pytest.mark.parametrize("name", ["test_subprocess_coverage.py", "test_verify_parallel.py"])
def test_tests_that_spawn_pytest_or_coverage_do_not_copy_the_outer_environment(name):
    source = (ROOT / "tests" / name).read_text()
    assert "dict(os.environ" not in source
    assert "isolated_coverage_env(" in source
