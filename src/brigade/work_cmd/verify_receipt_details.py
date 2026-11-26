"""Additive details and terminal-state rules for verify receipts."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import localio

_OUTCOMES = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed", "deselected")
# Outcomes that mean a test was actually run or reported, as opposed to left out.
_RAN = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
_LABEL = r"passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun|subtests (?:passed|failed|skipped|xfailed|xpassed)"
_ITEM = rf"\d+ (?:{_LABEL})"
_DURATION = r" in \d+(?:\.\d+)?s(?: \([^)]*\))?"
# pytest's terminal summary line, with or without the ``===`` rule that -q drops.
_SUMMARY = re.compile(rf"^(?:=+ )?(?P<body>{_ITEM}(?:, {_ITEM})*){_DURATION}(?: =+)?$")
_NO_TESTS = re.compile(rf"^(?:=+ )?no tests ran{_DURATION}(?: =+)?$")
# The full gate prints one combined line for all shards (scripts/verify_parallel.py).
_AGGREGATE = re.compile(rf"^Aggregate: (?P<body>{_ITEM}(?:, {_ITEM})*)$")
_COUNT = re.compile(rf"(?P<count>\d+) (?P<name>{_LABEL})")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_ENV_VALUE_LIMIT = 2048
BUSY_EXIT_CODE = 75
# The full-gate slot lock prints this line when every slot is taken. Exit 75
# alone is not enough: any tool may use that code for an ordinary failure.
BUSY_MARKER = "brigade-verify: busy"


def is_busy_exit(exit_code: int | None, *streams: str) -> bool:
    """Return whether a command reported a busy verification slot.

    Both output streams are checked, because a wrapper may merge them.
    """
    return exit_code == BUSY_EXIT_CODE and any(
        line.strip() == BUSY_MARKER for stream in streams for line in stream.splitlines()
    )


def inherited_test_env(environ: Mapping[str, str] | None = None, *, include_py: bool = False) -> dict[str, Any]:
    """Return the bounded inherited test environment recorded by verify."""
    source = os.environ if environ is None else environ
    result: dict[str, Any] = {"pythonpath_set": "PYTHONPATH" in source}
    if include_py and "PY" in source:
        result["py_set"] = True
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        if name in source:
            # Kept verbatim so a reader can see what changed pytest's behavior, but bounded.
            result[name] = source[name][:_ENV_VALUE_LIMIT]
    return result


def pytest_outcomes(stdout: str) -> dict[str, int | bool] | None:
    """Parse pytest's terminal summary line from complete captured stdout.

    Only a line shaped like the summary counts, read from the end of the
    output. Other tools print similar phrases ("Found 3 errors in 2 files"),
    and a test may print "no tests ran" itself.
    """
    for raw in reversed(stdout.splitlines()):
        # pytest colors the summary when asked to, even when its output is captured.
        line = _ANSI.sub("", raw).strip()
        if _NO_TESTS.match(line):
            return {**dict.fromkeys(_OUTCOMES, 0), "no_tests_ran": True}
        match = _SUMMARY.match(line) or _AGGREGATE.match(line)
        if match is None:
            continue
        counts = dict.fromkeys(_OUTCOMES, 0)
        failed_subtests = 0
        for item in _COUNT.finditer(match.group("body")):
            name = "errors" if item.group("name") == "error" else item.group("name")
            if name == "subtests failed":
                failed_subtests = int(item.group("count"))
            elif name in counts:
                counts[name] = int(item.group("count"))
        # A failed subtest disqualifies the run. Only top-level passes count as passed tests.
        counts["failed"] += failed_subtests
        return {**counts, "no_tests_ran": sum(counts[name] for name in _RAN) == 0}
    return None


def _passed(record: object) -> bool:
    return isinstance(record, dict) and record.get("status") == "completed" and record.get("exit_code") == 0


def terminal_status(receipt: Mapping[str, Any], rc: int) -> tuple[str, int]:
    """Return a fail-closed receipt status and process code."""
    commands = receipt.get("commands")
    planned = receipt.get("planned_commands")
    records = commands if isinstance(commands, list) else []
    # Every planned command has a record and nothing went wrong while recording.
    # A receipt with no planned list cannot show that, so it fails closed.
    recorded = (
        isinstance(planned, list)
        and bool(records)
        and len(records) == len(planned)
        and not receipt.get("bookkeeping_error")
    )
    nonpassing = [record for record in records if not _passed(record)]
    if recorded and not nonpassing and rc == 0:
        return "completed", 0
    if (
        recorded
        and nonpassing
        and all(isinstance(record, dict) and record.get("status") == "busy" for record in nonpassing)
    ):
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
    # A receipt written before the field existed has nothing to copy. Leave the key out.
    if receipt.get("inherited_test_env") is None:
        receipt.pop("inherited_test_env", None)


def same_verification_tree(
    target: Path, before: str, after: str, *, git_env: dict[str, str] | None = None
) -> bool | None:
    """Compare Git trees ignoring changes under .brigade/. None means comparison failed."""
    if before == after:
        return True
    # Receipts store full object IDs. Keep arbitrary revisions and options out of Git.
    if not all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree) for tree in (before, after)):
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "diff-tree",
                "-r",
                "--name-only",
                "-z",
                "--no-relative",
                "--no-renames",
                "--ignore-submodules=none",
                f"{before}^{{tree}}",
                f"{after}^{{tree}}",
                "--",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            env=git_env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return all(path.startswith(b".brigade/") for path in result.stdout.split(b"\0")[:-1])


def stamp_tree_after(target: Path, receipt: dict[str, Any], *, git_env: dict[str, str] | None = None) -> None:
    """Capture post-run tree identity without making stability a gate."""
    try:
        after = localio.tree_fingerprint(target) if git_env is None else _batch_fingerprint(target, git_env)[0]
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        after = None
    receipt["tree_fingerprint_after"] = after
    before = receipt.get("tree_fingerprint")
    receipt["tree_stable"] = (
        same_verification_tree(target, before, after, git_env=git_env) is True
        if isinstance(before, str) and isinstance(after, str)
        else None
    )


def _batch_fingerprint(target: Path, git_env: dict[str, str]) -> tuple[str, str]:
    from time import monotonic

    from .. import run_batch_verify_tree

    return run_batch_verify_tree.fingerprint(target, git_env, deadline=monotonic() + 60)


def capture_batch_identity(target: Path, run_dir: Path, git_env: dict[str, str]) -> dict[str, Any]:
    """Fingerprint the coordinator snapshot and retain its patch with the same Git environment."""
    from .. import receipt_schema

    tree, head = _batch_fingerprint(target, git_env)
    patch = run_dir / "changes.patch"
    with patch.open("wb") as output:
        subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                head,
                "--",
            ],
            cwd=target,
            env=git_env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
    return {
        "schema_version": receipt_schema.VERIFY_RECEIPT_SCHEMA_VERSION,
        "baseline_commit": head,
        "tree_fingerprint": tree,
        "tree_fingerprint_head": head,
        "changes_patch_sha256": localio.file_sha256(patch),
    }
