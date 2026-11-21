"""Additive details and terminal-state rules for verify receipts."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from .. import localio

_OUTCOMES = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed", "deselected")
_PYTEST_COUNT = re.compile(r"(?P<count>\d+)\s+(?P<name>passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")


def inherited_test_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return the bounded inherited test environment recorded by verify."""
    source = os.environ if environ is None else environ
    result: dict[str, Any] = {"pythonpath_set": "PYTHONPATH" in source}
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        if name in source:
            result[name] = source[name]
    return result


def pytest_outcomes(stdout: str) -> dict[str, int | bool] | None:
    """Parse pytest's terminal summary from complete captured stdout."""
    lines = stdout.splitlines()
    if any("no tests ran" in line.lower() for line in lines):
        return {**dict.fromkeys(_OUTCOMES, 0), "no_tests_ran": True}
    counts = dict.fromkeys(_OUTCOMES, 0)
    found = False
    for line in reversed(lines):
        matches = list(_PYTEST_COUNT.finditer(line.lower()))
        if not matches:
            continue
        found = True
        for match in matches:
            name = match.group("name")
            if name == "error":
                name = "errors"
            counts[name] = int(match.group("count"))
        break
    if not found:
        return None
    return {**counts, "no_tests_ran": sum(counts.values()) == 0}


def terminal_status(receipt: Mapping[str, Any], rc: int) -> tuple[str, int]:
    """Return a fail-closed receipt status and process code."""
    commands = receipt.get("commands")
    planned = receipt.get("planned_commands")
    records = commands if isinstance(commands, list) else []
    expected = planned if isinstance(planned, list) else records
    complete = len(records) == len(expected) and all(
        isinstance(record, dict) and record.get("status") == "completed" and record.get("exit_code") == 0
        for record in records
    )
    if complete and rc == 0 and not receipt.get("bookkeeping_error"):
        return "completed", 0
    nonpassing = [
        record
        for record in records
        if not (isinstance(record, dict) and record.get("status") == "completed" and record.get("exit_code") == 0)
    ]
    if nonpassing and all(isinstance(record, dict) and record.get("status") == "busy" for record in nonpassing):
        return "busy", 75
    if rc == 0:
        rc = 1
    statuses = {record.get("status") for record in records if isinstance(record, dict)}
    return ("failed" if statuses & {"failed", "timed_out"} or receipt.get("bookkeeping_error") else "rejected"), rc


def stamp_tree_after(target: Any, receipt: dict[str, Any]) -> None:
    """Capture post-run tree identity without making stability a gate."""
    try:
        after = localio.tree_fingerprint(target)
    except (OSError, RuntimeError):
        after = None
    receipt["tree_fingerprint_after"] = after
    before = receipt.get("tree_fingerprint")
    receipt["tree_stable"] = before == after if isinstance(before, str) and isinstance(after, str) else None
