"""Flushed per-phase evidence that survives termination of a pytest file."""

from __future__ import annotations

import os
from io import open as _open

import pytest

_DESTINATION = os.environ.get("BRIGADE_WINDOWS_TEST_PROGRESS")


def _record(message: str) -> None:
    if _DESTINATION:
        # A separate file stays visible even while pytest captures stdout/stderr.
        with _open(_DESTINATION, "a", encoding="utf-8") as stream:
            stream.write(message + "\n")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection():
    _record("session phase=collection started")
    yield
    _record("session phase=collection completed")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish():
    _record("session phase=sessionfinish started")
    yield
    _record("session phase=sessionfinish completed")


@pytest.hookimpl(trylast=True)
def pytest_unconfigure():
    _record("session phase=unconfigure completed")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _record(f"{item.nodeid} phase=setup started")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _record(f"{item.nodeid} phase=call started")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    _record(f"{item.nodeid} phase=teardown started")


def pytest_runtest_logreport(report):
    _record(f"{report.nodeid} phase={report.when} outcome={report.outcome} seconds={report.duration:.3f}")
