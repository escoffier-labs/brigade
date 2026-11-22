"""Verdicts for the repository's required local verification check."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from .. import localio, verify_manifest_git

_DECLARATION_PATH = "verify/required-check.json"
_DECLARATION_SCHEMA = "brigade.verify_required_check.v1"
STOP_POLICY_BUDGET_SECONDS = 2.0
_HEAD_FALLBACK_BUDGET_SECONDS = 1.0


@dataclass(frozen=True)
class _GatePolicy:
    mode: Literal["required_check", "audit_only", "unresolved"]
    reason: str
    argv_prefix: tuple[str, ...] = ()
    needs_arguments: bool = False
    needs_passed_tests: bool = False
    base_commit: str | None = None
    declaration_sha256: str | None = None
    fallback_source: Literal["head", "worktree", "none"] | None = None
    base_message: str | None = None
    error: str | None = None

    def matches(self, stamp: Mapping[str, Any]) -> bool:
        return (
            stamp.get("mode") == self.mode
            and stamp.get("declaration_sha256") == self.declaration_sha256
            and stamp.get("fallback_source") == self.fallback_source
        )

    def stamp(self, eligible: bool, reason: str) -> dict[str, Any]:
        stamp = {"mode": self.mode, "eligible": eligible, "reason": reason, "plan_coverage": "not_checked"}
        if self.declaration_sha256 is not None:
            stamp["declaration_sha256"] = self.declaration_sha256
        if self.base_commit is not None:
            stamp["base_commit"] = self.base_commit
        if self.fallback_source is not None:
            stamp["fallback_source"] = self.fallback_source
        return stamp


def _parse_declaration(raw: bytes, commit: str | None = None) -> _GatePolicy:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        return _GatePolicy("unresolved", "declaration_invalid", error=str(exc))
    if not isinstance(payload, dict) or payload.get("schema") != _DECLARATION_SCHEMA:
        return _GatePolicy("unresolved", "declaration_invalid", error=f"expected schema {_DECLARATION_SCHEMA}")
    prefix = payload.get("argv_prefix")
    needs_arguments = payload.get("needs_arguments")
    needs_passed_tests = payload.get("needs_passed_tests")
    if (
        not isinstance(prefix, list)
        or not prefix
        or not all(isinstance(arg, str) and "\0" not in arg for arg in prefix)
        or not prefix[0].strip()
    ):
        return _GatePolicy("unresolved", "declaration_invalid", error="argv_prefix must be a nonempty command array")
    if not isinstance(needs_arguments, bool) or not isinstance(needs_passed_tests, bool):
        return _GatePolicy(
            "unresolved", "declaration_invalid", error="needs_arguments and needs_passed_tests must be booleans"
        )
    return _GatePolicy(
        "required_check",
        "passed_required_check",
        tuple(prefix),
        needs_arguments,
        needs_passed_tests,
        base_commit=commit,
        declaration_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _unreadable(detail: str) -> _GatePolicy:
    return _GatePolicy("unresolved", "declaration_unreadable", error=detail)


def _committed_policy(target: Path, commit: str, deadline: float | None) -> _GatePolicy | None:
    git = verify_manifest_git._git
    entry = git(target, "ls-tree", "--full-tree", "-l", "-z", commit, "--", _DECLARATION_PATH, deadline=deadline)
    if entry.returncode:
        return _unreadable("Git could not read the declaration's tree entry")
    if not entry.stdout:
        return None
    metadata = entry.stdout.split(b"\t", 1)[0].split()
    if len(metadata) != 4 or metadata[0] not in {b"100644", b"100755"} or metadata[1] != b"blob":
        return _unreadable("the declaration must be a regular file")
    oid, size = metadata[2:]
    if not size.isdigit() or int(size) > verify_manifest_git.MAX_MANIFEST_BYTES:
        return _unreadable("the declaration exceeds the size limit or Git could not determine its size")
    blob = git(target, "cat-file", "blob", oid.decode("ascii"), deadline=deadline)
    if blob.returncode:
        return _unreadable("Git could not read the declaration's contents")
    return _parse_declaration(blob.stdout, commit)


def _worktree_policy(target: Path) -> _GatePolicy:
    path = target / _DECLARATION_PATH
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return replace(_unreadable("the declaration must be a regular file"), fallback_source="worktree")
        with path.open("rb") as stream:
            raw = stream.read(verify_manifest_git.MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        return _GatePolicy("audit_only", "audit_only", fallback_source="none")
    except OSError as exc:
        return replace(_unreadable(exc.strerror or "the declaration could not be read"), fallback_source="worktree")
    policy = (
        _unreadable("the declaration exceeds the size limit")
        if len(raw) > verify_manifest_git.MAX_MANIFEST_BYTES
        else _parse_declaration(raw)
    )
    return replace(policy, fallback_source="worktree")


def _fallback_policy(target: Path, deadline: float | None, base_message: str | None) -> _GatePolicy:
    try:
        head = verify_manifest_git._git(target, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", deadline=deadline)
        if head.returncode == 0 and head.stdout.strip():
            policy = _committed_policy(target, head.stdout.decode("ascii").strip(), deadline)
            if policy is not None:
                return replace(policy, fallback_source="head", base_message=base_message)
        return replace(_worktree_policy(target), base_message=base_message)
    except subprocess.TimeoutExpired:
        return _GatePolicy("unresolved", "policy_timeout", base_message=base_message)


def _load_policy(target: Path, *, deadline: float | None = None) -> _GatePolicy:
    base_deadline = None if deadline is None else deadline - _HEAD_FALLBACK_BUDGET_SECONDS
    try:
        base = verify_manifest_git.resolve_base(target, deadline=base_deadline)
    except subprocess.TimeoutExpired:
        return _fallback_policy(target, deadline, "Git timed out while resolving the base.")
    if base.status != "resolved" or not base.commit:
        return _fallback_policy(target, deadline, base.message)
    try:
        return _committed_policy(target, base.commit, deadline) or _GatePolicy("audit_only", "audit_only")
    except subprocess.TimeoutExpired:
        return _GatePolicy("unresolved", "policy_timeout")


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


def reuse_gating(target: Path, receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the stamp to copy if the source receipt still belongs to the current policy."""
    if _blocked_reason(receipt):
        return None
    policy = _load_policy(target)
    if policy.mode == "unresolved":
        return None
    _, reason = receipt_is_gating(receipt)
    if reason == "missing_gating" and policy.mode == "audit_only":
        return policy.stamp(True, "audit_only")
    stamp = receipt.get("gating")
    if isinstance(stamp, dict) and reason != "invalid_gating" and policy.matches(stamp):
        return dict(stamp)
    return None


def stamp_gating(target: Path, receipt: dict[str, Any]) -> None:
    """Record a verdict before the verify receipt is digested."""
    problem = _blocked_reason(receipt)
    previous = receipt.get("gating")
    if isinstance(previous, dict):
        # A canceled finalization can invalidate a verdict it already stamped.
        if problem:
            previous.update(eligible=False, reason=problem)
        return

    policy = _load_policy(target)
    if problem:
        eligible, reason = False, problem
    elif policy.mode == "unresolved":
        eligible, reason = False, policy.reason
    elif receipt.get("verify_manifest_id"):
        eligible, reason = True, "manifest_run"
    elif policy.mode == "audit_only":
        eligible, reason = True, "audit_only"
    else:
        eligible, reason = _required_check_verdict(receipt, policy)
    receipt["gating"] = policy.stamp(eligible, reason)


_INELIGIBLE_NOTICE = {
    "not_completed": "The last run did not complete successfully.",
    "tree_changed_during_run": "The tree changed during the last run.",
    "not_the_required_check": "The last run did not run the required check.",
    "no_selector": "The last run had no test selector.",
    "no_tests_passed": "The last run passed no tests or reported test failures or errors.",
}


class StopGate:
    """Share one lazy policy lookup across receipt checks and Stop guidance."""

    def __init__(self, target: Path, *, deadline: float | None = None) -> None:
        self.target = target
        self.deadline = deadline
        self._last_ineligible: tuple[datetime, datetime, str] | None = None

    @cached_property
    def _policy(self) -> _GatePolicy:
        return _load_policy(self.target, deadline=self.deadline)

    @property
    def timed_out(self) -> bool:
        return self._policy.reason == "policy_timeout"

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
        eligible, recorded_reason = receipt_is_gating(receipt)
        reason = recorded_reason
        stamp = receipt.get("gating")
        if isinstance(stamp, dict) and not self._policy.matches(stamp) and not _blocked_reason(receipt):
            eligible, reason = False, "missing_gating"
        if self._policy.mode != "unresolved" and (
            eligible or (reason == "missing_gating" and self._policy.mode == "audit_only")
        ):
            return completed
        if isinstance(stamp, dict) and stamp.get("eligible") is False:
            if self._last_ineligible is None or completed > self._last_ineligible[0]:
                self._last_ineligible = completed, receipt_started, recorded_reason
        return None

    def command(self) -> list[str]:
        policy = self._policy
        if policy.mode != "required_check":
            return ["pytest", "tests/test_example.py"]
        return [*policy.argv_prefix, *(["tests/test_example.py"] if policy.needs_arguments else [])]

    def notice(self) -> str | None:
        policy = self._policy
        if self.timed_out and policy.base_message is None:
            return f"{self.target}: Git timed out reading the required check. Stop skipped this check."
        if policy.fallback_source is None and policy.base_message is None:
            return None
        source = {
            "head": "Using the declaration from local HEAD.",
            "worktree": "Using the declaration from the working tree.",
            "none": "There is no declaration in either local HEAD or the working tree.",
            None: "Git timed out before the local fallback could be read. Stop skipped this check.",
        }[policy.fallback_source]
        remedy = policy.base_message or ""
        return f"{self.target}: Git base was unresolved. {source} {remedy}".strip()

    def block_guidance(self, replacement: str, since: object) -> str:
        policy = self._policy
        if policy.mode == "unresolved":
            message = f"{self.target}: could not load {_DECLARATION_PATH}: {policy.error}."
        else:
            message = f"{self.target}: run `{replacement}` in a POSIX shell."
            if policy.mode == "audit_only":
                message += "\n  This repository declares no required check."
        threshold = localio.parse_iso_datetime(since)
        if self._last_ineligible and threshold is not None and self._last_ineligible[1] >= threshold:
            explanation = _INELIGIBLE_NOTICE.get(self._last_ineligible[2])
            if explanation:
                message += f"\n  {explanation}"
        return message
