"""Quarantine unreadable coverage sidecars before pytest-cov combines them.

pytest-cov ends a measured session by calling ``coverage.combine()`` on every
``<data file>.*`` sidecar next to the data file. ``combine_parallel_data`` skips a
file that raises ``CoverageException``, but a sidecar with no ``coverage_schema``
table (a process killed between creating the file and writing its schema) makes
``SqliteDb._init_db`` try to initialize it, and the raw
``sqlite3.OperationalError`` it raises is not wrapped. One such file aborts the
whole combine with an ``INTERNALERROR`` after every test has passed (#2219,
#2220, and the ``test shard (3.12, 3)`` job on #2232).

``quarantine_unreadable_sidecars`` moves each unreadable sidecar aside and prints
one line naming its size and mtime. The file name carries only a pid, so the guard
also attributes it. Each process that runs tests (every xdist worker, or the plain
session) appends to a small log in a directory the controller creates and hands to
the workers through ``workerinput``: its own pid, the id of each test it starts,
and the pid of every ``subprocess.Popen`` child with the test that started it. The
guard matches the sidecar's pid against that log and names the test, says the file
came from a worker itself, or lists the test each worker had most recently started
when the file was last written (a grandchild or ``multiprocessing`` child has no
recorded spawn). The cost is one line-buffered write per test and per spawn.
``pytest_runtestloop`` runs it on the controller (or a plain session) after
the test loop and before pytest-cov's own wrapper combines, and does nothing when
coverage is not active.

Hook ordering. pluggy calls wrappers outermost first, and a later-registered
wrapper is outer by default. Both this module (loaded from a conftest, after the
``pytest_cov`` entry point) and pytest-cov's ``CovPlugin`` are ``wrapper=True``
implementations of ``pytest_runtestloop``, so a plain hook here would be the OUTER
wrapper, and its post-yield code would run after pytest-cov combined. ``trylast``
puts it innermost, so its post-yield code runs first. ``tests/test_coverage_sidecar_guard.py``
asserts that order by running a real inner session.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

import pytest

QUARANTINE_PREFIX = "unreadable-"
ATTRIBUTION_KEY = "brigade_sidecar_attribution_dir"
_LOG_PREFIX = "attribution-"
_PID_IN_NAME = re.compile(r"\.pid(\d+)\.")


def _is_readable_coverage_db(path: Path) -> bool:
    """True when ``path`` is a sqlite file with the table coverage's reader needs."""
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        connection.execute("select version from coverage_schema").fetchone()
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return True


def _describe(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return f"{path} (stat failed)"
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    return f"{path} (size={stat.st_size} bytes, mtime={mtime})"


class SpawnLog:
    """Append-only record of one test-running process: its pid, test starts and spawned pids.

    Tab-separated lines, flushed per line so a killed worker keeps what it wrote:
    ``W pid label``, ``T time nodeid`` and ``S time childpid nodeid``.
    """

    def __init__(self, directory: Path, label: str) -> None:
        self.label = label
        self.current_test = ""
        self._file = (directory / f"{_LOG_PREFIX}{label}-pid{os.getpid()}.log").open("a", encoding="utf-8", buffering=1)
        self._write("W", os.getpid(), label)

    def _write(self, *fields: object) -> None:
        self._file.write("\t".join(str(field).replace("\t", " ").replace("\n", " ") for field in fields) + "\n")

    def test_started(self, nodeid: str) -> None:
        self.current_test = nodeid
        self._write("T", time.time(), nodeid)

    def spawned(self, pid: int) -> None:
        self._write("S", time.time(), pid, self.current_test)

    def close(self) -> None:
        self._file.close()


def _read_logs(directory: Path):
    """Return ``(workers, spawns, starts)`` parsed from every log in ``directory``.

    ``workers`` maps pid to label, ``spawns`` maps a child pid to ``(nodeid, label)``, and
    ``starts`` maps a label to its ``(time, nodeid)`` test starts in order.
    """
    workers: dict[int, str] = {}
    spawns: dict[int, tuple[str, str]] = {}
    starts: dict[str, list[tuple[float, str]]] = {}
    for log in sorted(directory.glob(f"{_LOG_PREFIX}*.log")):
        label = ""
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            kind, _, rest = line.partition("\t")
            fields = rest.split("\t")
            try:
                if kind == "W":
                    label = fields[1]
                    workers[int(fields[0])] = label
                elif kind == "T":
                    starts.setdefault(label, []).append((float(fields[0]), fields[1]))
                elif kind == "S":
                    spawns[int(fields[1])] = (fields[2], label)
            except (IndexError, ValueError):
                continue  # a torn final line from a killed process
    return workers, spawns, starts


def attribute_sidecar(sidecar: Path, directory: Path, mtime: float) -> str:
    """Say which test or worker likely produced ``sidecar``, from the logs in ``directory``."""
    match = _PID_IN_NAME.search(sidecar.name)
    if match is None:
        return "no pid in the file name"
    pid = int(match.group(1))
    workers, spawns, starts = _read_logs(directory)
    latest = []
    for label, tests in sorted(starts.items()):
        before = [entry for entry in tests if entry[0] <= mtime]
        if before:
            latest.append(f"{label}: {before[-1][1]}")
    detail = f"; latest test started on each worker by then: {'; '.join(latest)}" if latest else ""
    # Check workers first: the controller's Popen log also records the workers it spawned.
    if pid in workers:
        return f"pid {pid} is the test process {workers[pid]} itself{detail}"
    if pid in spawns:
        nodeid, label = spawns[pid]
        where = f"in test {nodeid}" if nodeid else "outside any test"
        return f"pid {pid} was spawned by subprocess.Popen {where} on {label}"
    return f"pid {pid} is not a test process or a recorded Popen child (grandchild or multiprocessing child?){detail}"


def quarantine_unreadable_sidecars(data_file: Path | str, attribution_dir: Path | None = None) -> list[Path]:
    """Move every unreadable ``<data_file>.*`` sidecar aside and return the moved paths.

    The new name starts with ``QUARANTINE_PREFIX``, so it no longer matches the
    ``<data_file>.*`` glob that ``combine`` uses. A valid sidecar is never touched.
    With ``attribution_dir``, the printed line also says which test likely made it.
    """
    base = Path(data_file)
    moved: list[Path] = []
    for sidecar in sorted(base.parent.glob(f"{base.name}.*")):
        if not sidecar.is_file():
            continue
        if _is_readable_coverage_db(sidecar):
            continue
        description = _describe(sidecar)
        if attribution_dir is not None:
            try:
                description += f"; source: {attribute_sidecar(sidecar, attribution_dir, sidecar.stat().st_mtime)}"
            except OSError:
                pass
        target = sidecar.with_name(QUARANTINE_PREFIX + sidecar.name)
        try:
            os.replace(sidecar, target)
        except OSError:
            sidecar.unlink(missing_ok=True)
        print(f"coverage sidecar guard: set aside unreadable data file {description}", flush=True)
        moved.append(sidecar)
    return moved


def _active_data_file(config: pytest.Config) -> Path | None:
    """The data file pytest-cov will combine into, or ``None`` when coverage is not active."""
    plugin = config.pluginmanager.getplugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    cov = getattr(controller, "cov", None)
    data_file = getattr(getattr(cov, "config", None), "data_file", None)
    if not data_file:
        return None
    return Path(os.path.abspath(data_file))


_LOG_DIR = pytest.StashKey[Path]()
_LOG = pytest.StashKey[SpawnLog]()
_OWNS_LOG_DIR = pytest.StashKey[bool]()


class _Attribution:
    """Registered by ``pytest_configure``; logs test starts and ``Popen`` children."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.original_init = subprocess.Popen.__init__

    def install(self, log: SpawnLog) -> None:
        original = self.original_init

        def init(popen, *args, **kwargs):
            original(popen, *args, **kwargs)
            log.spawned(popen.pid)

        subprocess.Popen.__init__ = init  # type: ignore[method-assign]

    def pytest_runtest_logstart(self, nodeid: str, location) -> None:
        self.config.stash[_LOG].test_started(nodeid)

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node) -> None:
        # xdist controller: hand each worker the directory so every process writes next to the others.
        node.workerinput[ATTRIBUTION_KEY] = str(self.config.stash[_LOG_DIR])

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        subprocess.Popen.__init__ = self.original_init  # type: ignore[method-assign]
        config.stash[_LOG].close()
        if config.stash.get(_OWNS_LOG_DIR, False):
            shutil.rmtree(config.stash[_LOG_DIR], ignore_errors=True)


def pytest_configure(config: pytest.Config) -> None:
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        # An xdist worker records only when its controller measures coverage and shared a directory.
        shared = workerinput.get(ATTRIBUTION_KEY)
        if not shared:
            return
        directory, label, owns = Path(shared), str(workerinput.get("workerid", "worker")), False
    elif config.pluginmanager.getplugin("_cov") is not None:
        # The controller, or a plain session that runs the tests itself.
        directory, label, owns = Path(tempfile.mkdtemp(prefix="brigade-sidecar-attribution-")), "main", True
    else:
        return
    config.stash[_LOG_DIR] = directory
    config.stash[_OWNS_LOG_DIR] = owns
    log = config.stash[_LOG] = SpawnLog(directory, label)
    plugin = _Attribution(config)
    plugin.install(log)
    config.pluginmanager.register(plugin, "brigade_sidecar_attribution")


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtestloop(session: pytest.Session):
    result = yield
    config = session.config
    # An xdist worker saves its own data and never combines; only the controller does.
    if not hasattr(config, "workerinput"):
        data_file = _active_data_file(config)
        if data_file is not None:
            quarantine_unreadable_sidecars(data_file, config.stash.get(_LOG_DIR, None))
    return result
