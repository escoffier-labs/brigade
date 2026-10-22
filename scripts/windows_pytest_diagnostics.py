"""Flushed per-phase evidence that survives termination of a pytest file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _record(message: str) -> None:
    destination = os.environ.get("BRIGADE_WINDOWS_TEST_PROGRESS")
    if destination:
        # A separate file stays visible even while pytest captures stdout/stderr.
        with Path(destination).open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    _record(f"{item.nodeid} phase=setup started")
    return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    _record(f"{item.nodeid} phase=call started")
    return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    _record(f"{item.nodeid} phase=teardown started")
    return (yield)


def pytest_runtest_logreport(report):
    _record(f"{report.nodeid} phase={report.when} outcome={report.outcome} seconds={report.duration:.3f}")
