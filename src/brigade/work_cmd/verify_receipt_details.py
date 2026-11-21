"""Additive details and terminal-state rules for verify receipts."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from .. import localio

_OUTCOMES = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed", "deselected")
# Outcomes that mean a test was actually run or reported, as opposed to left out.
_RAN = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
_ITEM = r"\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)"
_DURATION = r" in \d+(?:\.\d+)?s(?: \([^)]*\))?"
# pytest's terminal summary line, with or without the ``===`` rule that -q drops.
_SUMMARY = re.compile(rf"^(?:=+ )?(?P<body>{_ITEM}(?:, {_ITEM})*){_DURATION}(?: =+)?$")
_NO_TESTS = re.compile(rf"^(?:=+ )?no tests ran{_DURATION}(?: =+)?$")
# The full gate prints one combined line for all shards (scripts/verify_parallel.py).
_AGGREGATE = re.compile(rf"^Aggregate: (?P<body>{_ITEM}(?:, {_ITEM})*)$")
_COUNT = re.compile(r"(?P<count>\d+) (?P<name>[a-z]+)")

BUSY_EXIT_CODE = 75
# The full-gate slot lock prints this line when every slot is taken. Exit 75
# alone is not enough: any tool may use that code for an ordinary failure.
BUSY_MARKER = "brigade-verify: busy"


def is_busy_exit(exit_code: int | None, stderr: str) -> bool:
    """Return whether a command reported a busy verification slot."""
    return exit_code == BUSY_EXIT_CODE and any(line.strip() == BUSY_MARKER for line in stderr.splitlines())


def inherited_test_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return the bounded inherited test environment recorded by verify."""
    source = os.environ if environ is None else environ
    result: dict[str, Any] = {"pythonpath_set": "PYTHONPATH" in source}
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        if name in source:
            result[name] = source[name]
    return result


def pytest_outcomes(stdout: str) -> dict[str, int | bool] | None:
    """Parse pytest's terminal summary line from complete captured stdout.

    Only a line shaped like the summary counts, read from the end of the
    output. Other tools print similar phrases ("Found 3 errors in 2 files"),
    and a test may print "no tests ran" itself.
    """
    for raw in reversed(stdout.splitlines()):
        line = raw.strip()
        if _NO_TESTS.match(line):
            return {**dict.fromkeys(_OUTCOMES, 0), "no_tests_ran": True}
        match = _SUMMARY.match(line) or _AGGREGATE.match(line)
        if match is None:
            continue
        counts = dict.fromkeys(_OUTCOMES, 0)
        for item in _COUNT.finditer(match.group("body")):
            name = "errors" if item.group("name") == "error" else item.group("name")
            if name in counts:
                counts[name] = int(item.group("count"))
        return {**counts, "no_tests_ran": sum(counts[name] for name in _RAN) == 0}
    return None


def _passed(record: object) -> bool:
    return isinstance(record, dict) and record.get("status") == "completed" and record.get("exit_code") == 0


def terminal_status(receipt: Mapping[str, Any], rc: int) -> tuple[str, int]:
    """Return a fail-closed receipt status and process code."""
    commands = receipt.get("commands")
    planned = receipt.get("planned_commands")
    records = commands if isinstance(commands, list) else []
    expected = planned if isinstance(planned, list) else records
    # Every planned command has a record and nothing went wrong while recording.
    recorded = bool(records) and len(records) == len(expected) and not receipt.get("bookkeeping_error")
    nonpassing = [record for record in records if not _passed(record)]
    if recorded and not nonpassing and rc == 0:
        return "completed", 0
    if recorded and all(isinstance(record, dict) and record.get("status") == "busy" for record in nonpassing):
        return "busy", BUSY_EXIT_CODE
    if rc in (0, BUSY_EXIT_CODE):
        # A real failure must not exit 0, and must not read as a busy slot.
        codes = [
            record.get("exit_code")
            for record in nonpassing
            if isinstance(record, dict) and record.get("status") != "busy"
        ]
        rc = next((code for code in codes if type(code) is int and code not in (0, BUSY_EXIT_CODE)), 1)
    statuses = {record.get("status") for record in records if isinstance(record, dict)}
    return ("failed" if statuses & {"failed", "timed_out"} or receipt.get("bookkeeping_error") else "rejected"), rc


def fail_closed_fallback(receipt: dict[str, Any], rc: int) -> int:
    """Mark a receipt whose finalization raised as failed, whatever it said before."""
    receipt["bookkeeping_error"] = True
    if receipt.get("status") in ("running", "completed", "busy"):
        receipt["status"] = "failed"
    return 1 if rc in (0, BUSY_EXIT_CODE) else rc


def stamp_reused_tree(receipt: dict[str, Any], identity: Mapping[str, Any]) -> None:
    """Record the tree for a reused receipt. No command ran, so stability was not observed."""
    receipt["tree_fingerprint_after"] = identity.get("tree_fingerprint")
    receipt["tree_stable"] = None


def stamp_tree_after(target: Any, receipt: dict[str, Any]) -> None:
    """Capture post-run tree identity without making stability a gate."""
    try:
        after = localio.tree_fingerprint(target)
    except (OSError, RuntimeError):
        after = None
    receipt["tree_fingerprint_after"] = after
    before = receipt.get("tree_fingerprint")
    receipt["tree_stable"] = before == after if isinstance(before, str) and isinstance(after, str) else None
