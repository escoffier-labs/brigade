"""The coverage sidecar guard must run before pytest-cov combines, and only quarantine bad files."""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from tests._coverage_env import isolated_coverage_env
from tests._coverage_sidecar_guard import QUARANTINE_PREFIX, attribute_sidecar, quarantine_unreadable_sidecars

# The crash needs a file the owner cannot write: root ignores the mode, and
# Windows has no os.geteuid, so evaluate it only on POSIX.
requires_unprivileged_posix = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs a non-root POSIX user so SQLite honors the read-only mode that reproduces the crash",
)

ROOT = Path(__file__).resolve().parents[1]
INNER_TIMEOUT_SECONDS = 120


def _coverage_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("create table coverage_schema (version integer)")
    connection.execute("insert into coverage_schema values (7)")
    connection.commit()
    connection.close()


def test_quarantine_moves_empty_and_schemaless_sidecars_and_keeps_valid_ones(tmp_path, capsys):
    data_file = tmp_path / ".coverage.3.12.3"
    valid = tmp_path / ".coverage.3.12.3.host.pid1.aaaa"
    empty = tmp_path / ".coverage.3.12.3.host.pid2.bbbb"
    schemaless = tmp_path / ".coverage.3.12.3.host.pid3.cccc"
    garbage = tmp_path / ".coverage.3.12.3.host.pid4.dddd"
    unrelated = tmp_path / ".coverage.other.host.pid5.eeee"
    _coverage_db(valid)
    empty.write_bytes(b"")
    connection = sqlite3.connect(schemaless)
    connection.execute("create table other (x)")
    connection.commit()
    connection.close()
    garbage.write_bytes(b"not sqlite" * 50)
    unrelated.write_bytes(b"")

    moved = quarantine_unreadable_sidecars(data_file)

    assert sorted(moved) == sorted([empty, schemaless, garbage])
    assert valid.exists()
    assert unrelated.exists()
    for path in (empty, schemaless, garbage):
        assert not path.exists()
        assert (tmp_path / (QUARANTINE_PREFIX + path.name)).exists()
    output = capsys.readouterr().out
    assert output.count("coverage sidecar guard:") == 3
    assert f"{empty} (size=0 bytes, mtime=" in output


def test_quarantined_names_no_longer_match_the_combine_glob(tmp_path):
    data_file = tmp_path / ".coverage"
    (tmp_path / ".coverage.x").write_bytes(b"")
    quarantine_unreadable_sidecars(data_file)
    assert [path.name for path in tmp_path.glob(".coverage.*")] == []


def test_quarantine_does_nothing_without_sidecars(tmp_path, capsys):
    assert quarantine_unreadable_sidecars(tmp_path / ".coverage") == []
    assert capsys.readouterr().out == ""


def test_attribution_names_the_spawning_test_the_worker_or_neither(tmp_path):
    worker_log = [
        "W\t50\tgw1",
        "T\t100.0\ttest_a.py::test_one",
        "S\t101.0\t901\ttest_a.py::test_one",
        "T\t200.0\ttest_a.py::test_two",
        "S\t201.0\t902\t",
        "S\t202.0",  # torn final line from a killed worker
    ]
    (tmp_path / "attribution-gw1-pid50.log").write_text("\n".join(worker_log), encoding="utf-8")
    (tmp_path / "attribution-main-pid40.log").write_text("W\t40\tmain\nS\t90.0\t50\t\n", encoding="utf-8")

    def source(pid: str, mtime: float) -> str:
        return attribute_sidecar(Path(f".coverage.3.12.3.host.pid{pid}.Xh2"), tmp_path, mtime)

    assert source("901", 150.0) == "pid 901 was spawned by subprocess.Popen in test test_a.py::test_one on gw1"
    assert source("902", 250.0) == "pid 902 was spawned by subprocess.Popen outside any test on gw1"
    assert (
        source("50", 150.0)
        == "pid 50 is the test process gw1 itself; latest test started on each worker by then: gw1: test_a.py::test_one"
    )
    unknown = source("7", 250.0)
    assert unknown.startswith("pid 7 is not a test process or a recorded Popen child")
    assert unknown.endswith("gw1: test_a.py::test_two")
    assert attribute_sidecar(Path(".coverage.nopid"), tmp_path, 1.0) == "no pid in the file name"


INNER_TEST = textwrap.dedent(
    """
    import os
    import sqlite3
    from pathlib import Path


    def test_leaves_a_schemaless_read_only_sidecar():
        sidecar = Path(os.environ["COVERAGE_FILE"] + ".bogus")
        connection = sqlite3.connect(sidecar)
        connection.execute("create table other (x)")
        connection.commit()
        connection.close()
        sidecar.chmod(0o444)
    """
)


CHILD_SIDECAR = (
    "import os,pathlib;"
    "pathlib.Path(os.environ['COVERAGE_FILE'] + f'.{tag}.host.pid{os.getpid()}.rand').write_bytes(b'')"
)
INNER_ATTRIBUTION_TEST = textwrap.dedent(
    """
    import os
    from pathlib import Path
    import subprocess
    import sys

    CHILD = {child!r}


    def _child(code):
        return subprocess.run([sys.executable, "-c", code], check=True)


    def test_child_left_the_sidecar():
        _child("tag = 'direct'; " + CHILD)


    def test_grandchild_left_the_sidecar():
        inner = "tag = 'grand'; " + CHILD
        _child("import subprocess, sys; subprocess.run([sys.executable, '-c', " + repr(inner) + "], check=True)")


    def test_worker_left_the_sidecar():
        Path(os.environ["COVERAGE_FILE"] + f".self.host.pid{{os.getpid()}}.rand").write_bytes(b"")
    """
).format(child=CHILD_SIDECAR)


# Load the hook the way tests/conftest.py does. A `-p` plugin registers before the
# pytest-cov entry point and would be the inner wrapper regardless of `trylast`, so
# only a conftest import exercises the ordering this guard depends on.
INNER_CONFTEST = "from tests._coverage_sidecar_guard import pytest_configure, pytest_runtestloop  # noqa: F401\n"


def _inner_session(tmp_path: Path, *, guard: bool, test_source: str = INNER_TEST) -> subprocess.CompletedProcess[str]:
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "test_sidecar.py").write_text(test_source)
    if guard:
        (inner / "conftest.py").write_text(INNER_CONFTEST)
    environment = isolated_coverage_env(
        tmp_path / ".coverage",
        PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH", "")])),
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-n",
            "2",
            f"--cov={inner}",
            "--cov-report=",
            "-q",
            "test_sidecar.py",
        ],
        cwd=inner,
        env=environment,
        capture_output=True,
        text=True,
        timeout=INNER_TIMEOUT_SECONDS,
    )


@requires_unprivileged_posix
def test_unreadable_sidecar_without_the_guard_aborts_or_is_skipped_by_coverage(tmp_path):
    # Older coverage aborts the combine on an unreadable sidecar, which is why the
    # guard exists. coverage 7.17.2 (2026-11-04) skips such a file with a warning
    # instead. Accept either upstream behavior; the guard test below covers ours.
    result = _inner_session(tmp_path, guard=False)
    output = result.stdout + result.stderr
    if result.returncode == 3:
        assert "attempt to write a readonly database" in output
    else:
        assert result.returncode == 0, output
        assert "isn't a coverage data file" in output


@requires_unprivileged_posix
def test_guard_keeps_the_session_alive_and_names_the_file(tmp_path):
    result = _inner_session(tmp_path, guard=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"coverage sidecar guard: set aside unreadable data file {tmp_path / '.coverage.bogus'}" in output
    assert (tmp_path / (QUARANTINE_PREFIX + ".coverage.bogus")).exists()
    assert "INTERNALERROR" not in output


def test_guard_names_the_test_that_started_the_child_that_left_a_sidecar(tmp_path):
    result = _inner_session(tmp_path, guard=True, test_source=INNER_ATTRIBUTION_TEST)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    lines = {
        tag: [line for line in output.splitlines() if f".coverage.{tag}." in line]
        for tag in ("direct", "grand", "self")
    }
    assert all(len(found) == 1 for found in lines.values()), output
    assert (
        "was spawned by subprocess.Popen in test test_sidecar.py::test_child_left_the_sidecar on gw"
        in lines["direct"][0]
    )
    assert "is not a test process or a recorded Popen child" in lines["grand"][0]
    assert "test_sidecar.py::test_grandchild_left_the_sidecar" in lines["grand"][0]
    assert "is the test process gw" in lines["self"][0]
    assert "itself; latest test started on each worker by then:" in lines["self"][0]
    assert "test_sidecar.py::test_worker_left_the_sidecar" in lines["self"][0]
    # The xdist controller runs no tests, so it must not show up as a worker.
    assert "main:" not in lines["self"][0]


def test_repo_conftest_registers_the_guard_hooks():
    from tests import _coverage_sidecar_guard, conftest

    assert conftest.pytest_runtestloop is _coverage_sidecar_guard.pytest_runtestloop
    assert conftest.pytest_configure is _coverage_sidecar_guard.pytest_configure
