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
one line naming its size and mtime, so the next occurrence says which process left
it. ``pytest_runtestloop`` runs it on the controller (or a plain session) after
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
import sqlite3

import pytest

QUARANTINE_PREFIX = "unreadable-"


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


def quarantine_unreadable_sidecars(data_file: Path | str) -> list[Path]:
    """Move every unreadable ``<data_file>.*`` sidecar aside and return the moved paths.

    The new name starts with ``QUARANTINE_PREFIX``, so it no longer matches the
    ``<data_file>.*`` glob that ``combine`` uses. A valid sidecar is never touched.
    """
    base = Path(data_file)
    moved: list[Path] = []
    for sidecar in sorted(base.parent.glob(f"{base.name}.*")):
        if not sidecar.is_file():
            continue
        if _is_readable_coverage_db(sidecar):
            continue
        description = _describe(sidecar)
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


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtestloop(session: pytest.Session):
    result = yield
    config = session.config
    # An xdist worker saves its own data and never combines; only the controller does.
    if not hasattr(config, "workerinput"):
        data_file = _active_data_file(config)
        if data_file is not None:
            quarantine_unreadable_sidecars(data_file)
    return result
