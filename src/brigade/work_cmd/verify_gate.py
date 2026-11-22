"""Verdicts for the repository's required local verification check."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from .. import localio, verify_manifest_git

_DECLARATION_PATH = "verify/required-check.json"
_DECLARATION_SCHEMA = "brigade.verify_required_check.v1"


@dataclass(frozen=True)
class _GatePolicy:
    mode: Literal["required_check", "audit_only", "unresolved"]
    reason: str
    argv_prefix: tuple[str, ...] = ()
    needs_arguments: bool = False
    needs_passed_tests: bool = False
    base_commit: str | None = None
    declaration_sha256: str | None = None


def _parse_declaration(raw: bytes, commit: str) -> _GatePolicy:
    invalid = _GatePolicy("unresolved", "declaration_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return invalid
    if not isinstance(payload, dict) or payload.get("schema") != _DECLARATION_SCHEMA:
        return invalid
    prefix = payload.get("argv_prefix")
    needs_arguments = payload.get("needs_arguments")
    needs_passed_tests = payload.get("needs_passed_tests")
    if (
        not isinstance(prefix, list)
        or not prefix
        or not all(isinstance(arg, str) and "\0" not in arg for arg in prefix)
        or not prefix[0].strip()
        or not isinstance(needs_arguments, bool)
        or not isinstance(needs_passed_tests, bool)
    ):
        return invalid
    return _GatePolicy(
        "required_check",
        "passed_required_check",
        tuple(prefix),
        needs_arguments,
        needs_passed_tests,
        base_commit=commit,
        declaration_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _load_policy(target: Path) -> _GatePolicy:
    git = verify_manifest_git._git
    repository = git(target, "rev-parse", "--git-dir")
    if repository.returncode:
        if repository.stderr.startswith(b"fatal: not a git repository"):
            return _GatePolicy("audit_only", "audit_only")
        return _GatePolicy("unresolved", "base_unresolved")
    base = verify_manifest_git.resolve_base(target)
    if base.status != "resolved" or not base.commit:
        return _GatePolicy("unresolved", "base_unresolved")

    unreadable = _GatePolicy("unresolved", "declaration_unreadable")
    entry = git(target, "ls-tree", "--full-tree", "-z", base.commit, "--", _DECLARATION_PATH)
    if entry.returncode:
        return unreadable
    if not entry.stdout:
        return _GatePolicy("audit_only", "audit_only")
    metadata = entry.stdout.split(b"\t", 1)[0].split()
    if len(metadata) != 3 or metadata[0] not in {b"100644", b"100755"} or metadata[1] != b"blob":
        return unreadable
    oid = metadata[2].decode("ascii")
    size = git(target, "cat-file", "-s", oid)
    if size.returncode or not size.stdout.strip().isdigit():
        return unreadable
    if int(size.stdout) > verify_manifest_git.MAX_MANIFEST_BYTES:
        return unreadable
    blob = git(target, "cat-file", "blob", oid)
    if blob.returncode:
        return unreadable
    return _parse_declaration(blob.stdout, base.commit)


def _tests_passed(command: Mapping[str, Any]) -> bool:
    outcomes = command.get("test_outcomes")
    if not isinstance(outcomes, dict):
        return False
    passed = outcomes.get("passed")
    failed = outcomes.get("failed")
    errors = outcomes.get("errors")
    return (
        type(passed) is int
        and passed > 0
        and type(failed) is int
        and failed == 0
        and type(errors) is int
        and errors == 0
        and outcomes.get("no_tests_ran") is False
    )


def _required_check_verdict(receipt: Mapping[str, Any], policy: _GatePolicy) -> tuple[bool, str]:
    commands = receipt.get("commands")
    records = commands if isinstance(commands, list) else []
    prefix = list(policy.argv_prefix)
    matches = [
        record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("argv"), list) and record["argv"][: len(prefix)] == prefix
    ]
    if not matches:
        return False, "not_the_required_check"
    selected = [record for record in matches if not policy.needs_arguments or len(record["argv"]) > len(prefix)]
    if not selected:
        return False, "no_selector"
    if policy.needs_passed_tests and not any(_tests_passed(record) for record in selected):
        return False, "no_tests_passed"
    return True, "passed_required_check"


def _blocked_reason(receipt: Mapping[str, Any]) -> str | None:
    if receipt.get("status") != "completed":
        return "not_completed"
    if receipt.get("tree_stable") is False:
        return "tree_changed_during_run"
    return None


def receipt_is_gating(receipt: Mapping[str, Any]) -> tuple[bool, str]:
    """Read a stored verdict. A missing verdict requires a repository policy decision."""
    if problem := _blocked_reason(receipt):
        return False, problem
    if "gating" not in receipt:
        return False, "missing_gating"
    stamp = receipt["gating"]
    if (
        not isinstance(stamp, dict)
        or type(stamp.get("eligible")) is not bool
        or not isinstance(stamp.get("reason"), str)
    ):
        return False, "invalid_gating"
    return stamp["eligible"], stamp["reason"]


def stamp_gating(target: Path, receipt: dict[str, Any], *, reused: Mapping[str, Any] | None = None) -> None:
    """Record a verdict before the verify receipt is digested."""
    problem = _blocked_reason(receipt)
    previous = reused if reused is not None else receipt
    previous_stamp = previous.get("gating")
    if isinstance(previous_stamp, dict):
        # Reuse carries the source verdict. A canceled finalization can still
        # invalidate a verdict it already stamped.
        stamp = dict(previous_stamp)
        if problem:
            stamp.update(eligible=False, reason=problem)
        receipt["gating"] = stamp
        return

    policy = _load_policy(target)
    if problem:
        eligible, reason = False, problem
    elif policy.mode == "unresolved":
        eligible, reason = False, policy.reason
    elif reused is not None:
        eligible, reason = receipt_is_gating(reused)
        if reason == "missing_gating" and policy.mode == "audit_only":
            eligible, reason = True, "audit_only"
    elif receipt.get("verify_manifest_id"):
        eligible, reason = True, "manifest_run"
    elif policy.mode == "audit_only":
        eligible, reason = True, "audit_only"
    else:
        eligible, reason = _required_check_verdict(receipt, policy)

    stamp = {"mode": policy.mode, "eligible": eligible, "reason": reason, "plan_coverage": "not_checked"}
    if policy.mode == "required_check":
        stamp.update(base_commit=policy.base_commit, declaration_sha256=policy.declaration_sha256)
    receipt["gating"] = stamp


class StopGate:
    """Share one lazy policy lookup across receipt checks and Stop guidance."""

    def __init__(self, target: Path) -> None:
        self.target = target

    @cached_property
    def _policy(self) -> _GatePolicy:
        return _load_policy(self.target)

    def completion_in_window(
        self, receipt: Mapping[str, Any], started: datetime, fingerprint: str | None
    ) -> datetime | None:
        if fingerprint is not None:
            session = receipt.get("harness_session")
            if (
                not isinstance(session, dict)
                or session.get("harness") != "claude"
                or session.get("fingerprint") != fingerprint
            ):
                return None
        receipt_started = localio.parse_iso_datetime(receipt.get("started_at"))
        completed = localio.parse_iso_datetime(receipt.get("completed_at"))
        if receipt_started is None or completed is None or receipt_started < started or completed < receipt_started:
            return None
        eligible, reason = receipt_is_gating(receipt)
        if eligible or (reason == "missing_gating" and self._policy.mode == "audit_only"):
            return completed
        return None

    def command(self) -> str:
        policy = self._policy
        if policy.mode != "required_check":
            return "<test>"
        return shlex.join(policy.argv_prefix) + (" <selector>" if policy.needs_arguments else "")

    def block_guidance(self, replacement: str) -> str:
        policy = self._policy
        if policy.mode == "unresolved":
            return f"{self.target}: the required check could not be resolved ({policy.reason})."
        message = f"{self.target}: run `{replacement}`"
        if policy.mode == "audit_only":
            message += "\n  This repository declares no required check."
        return message
