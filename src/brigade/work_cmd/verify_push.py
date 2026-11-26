"""Report local verification evidence for the immutable tips in a Git push."""

from __future__ import annotations

import heapq
import io
import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache, cached_property, partial
from itertools import groupby
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic
from typing import Any

from .. import localio, verify_manifest_git
from . import verify_gate
from .verify_push_input import INPUT_BYTES, branch_updates

_BUDGET_SECONDS = 2.0
_RECEIPT_LIMIT = 128
_RECEIPT_BYTES = 4 * 1024 * 1024
_TIMEOUT = "The push check reached its two-second time limit."
_SCAN_LIMIT = "The local receipt scan reached its limit. Run verification for the pushed tree."
_EARLIEST = datetime.min.replace(tzinfo=UTC)
_MASKED_PASS = "newer_required_failure"


@dataclass(frozen=True)
class _Verdict:
    status: str
    reason: str | None = None
    remedy: str | None = None
    reason_code: str | None = None
    tree: str | None = None

    def public(self, ref: str | None) -> dict[str, Any]:
        return {"ref": ref, "status": self.status, "reason": self.reason, "remedy": self.remedy}


class _Indeterminate(Exception):
    def __init__(self, message: str, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _Indeterminate(_TIMEOUT, "check_timeout")
    return remaining


def _receipt_started_at(receipt: dict[str, Any]) -> datetime:
    try:
        return localio.parse_iso_datetime(receipt.get("started_at")) or _EARLIEST
    except OverflowError:
        return _EARLIEST


def _read_stdin(deadline: float) -> str:
    try:
        fd = sys.stdin.fileno()
    except io.UnsupportedOperation:
        return sys.stdin.read(INPUT_BYTES + 1)

    result: Queue[bytes | OSError] = Queue(maxsize=1)

    def read_fd() -> None:
        # Use the descriptor directly so a stalled pipe cannot hold Python's
        # buffered stdin lock during interpreter shutdown.
        data = bytearray()
        try:
            while len(data) <= INPUT_BYTES:
                chunk = os.read(fd, min(65536, INPUT_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            result.put(bytes(data))
        except OSError as exc:
            result.put(exc)

    Thread(target=read_fd, daemon=True).start()
    try:
        data = result.get(timeout=_remaining(deadline))
    except Empty:
        raise _Indeterminate(_TIMEOUT, "check_timeout") from None
    if isinstance(data, OSError):
        raise data
    return os.fsdecode(data)


class _Receipts:
    def __init__(self, target: Path, deadline: float) -> None:
        self.root = target / ".brigade" / "work" / "verify-runs"
        self.deadline = deadline
        self.loaded: dict[str, dict[str, Any] | None] = {}

    def _names(self) -> Iterator[str]:
        try:
            with os.scandir(self.root) as dirs:
                for entry in dirs:
                    _remaining(self.deadline)
                    if entry.is_dir(follow_symlinks=False):
                        yield entry.name
        except FileNotFoundError:
            return

    @cached_property
    def names(self) -> list[str]:
        # Run IDs begin with the UTC start time. Keep only the newest names
        # in memory, plus one to detect an incomplete scan.
        return heapq.nlargest(_RECEIPT_LIMIT + 1, self._names())

    def _load(self, name: str) -> dict[str, Any] | None:
        _remaining(self.deadline)
        path = self.root / name / "receipt.json"
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                return None
            with path.open("rb") as stream:
                raw = stream.read(_RECEIPT_BYTES + 1)
            if len(raw) > _RECEIPT_BYTES:
                raise _Indeterminate("A local verification receipt exceeds the size limit.")
            data = json.loads(raw)
        except (FileNotFoundError, ValueError, RecursionError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("commands"), list) or not data["commands"]:
            return None
        return {
            **{
                key: data[key]
                for key in ("status", "tree_stable", "tree_fingerprint", "started_at", "gating", "bookkeeping_error")
                if key in data
            },
            "commands": [
                {key: record.get(key) for key in ("argv", "status", "test_outcomes")}
                if isinstance(record, dict)
                else {}
                for record in data["commands"]
            ],
        }

    def _matching(self, name: str, tree: str) -> dict[str, Any] | None:
        if name not in self.loaded:
            self.loaded[name] = self._load(name)
        receipt = self.loaded[name]
        if receipt is None or receipt.get("status") in ("running", "busy", "canceled"):
            return None
        stamp = receipt.get("gating")
        plan = stamp.get("plan") if isinstance(stamp, dict) else None
        # Closeout's comparison allows changes under .brigade/ and uses a
        # separate Git timeout. Push receipts must identify the exact committed tree.
        if receipt.get("tree_fingerprint") == tree or (isinstance(plan, dict) and plan.get("snapshot_tree") == tree):
            return receipt
        return None

    def matching(self, tree: str, *, required_order: bool) -> Iterator[tuple[dict[str, Any], datetime]]:
        overflow = len(self.names) > _RECEIPT_LIMIT
        for second, names in groupby(self.names[:_RECEIPT_LIMIT], key=lambda name: name[:15]):
            # Random run-ID suffixes do not order runs started in the same second.
            # An omitted run in that second could supersede an observed pass or failure.
            if required_order and overflow and self.names[-1][:15] == second:
                raise _Indeterminate(_SCAN_LIMIT)
            matches = (
                (receipt, _receipt_started_at(receipt))
                for name in names
                if (receipt := self._matching(name, tree)) is not None
            )
            yield from sorted(matches, key=lambda match: match[1], reverse=True) if required_order else matches
        if overflow:
            raise _Indeterminate(_SCAN_LIMIT)


def _remedy(target: Path, policy: verify_gate._GatePolicy, receipt: dict[str, Any], *, fresh: bool = False) -> str:
    stamp = receipt.get("gating")
    plan = stamp.get("plan") if isinstance(stamp, dict) else None
    argv = plan.get("run_argv") if isinstance(plan, dict) else None
    prefix = list(policy.argv_prefix)
    if (
        isinstance(argv, list)
        and argv[: len(prefix)] == prefix
        and len(argv) > len(prefix)
        and all(isinstance(arg, str) and arg for arg in argv)
    ):
        return verify_gate._render_verify_command(target, argv)
    command = verify_gate._render_verify_command(target, verify_gate._suggested_argv(policy))
    if fresh:
        command += " --no-reuse"
    if policy.needs_arguments:
        command += "\nFind the required test files with brigade work verify required --worktree."
    return command


def _tree_verdict(
    target: Path, policy: verify_gate._GatePolicy, tree: str, receipts: _Receipts, deadline: float
) -> _Verdict:
    prefix = list(policy.argv_prefix)
    rejection: tuple[tuple[bool, datetime], dict[str, Any], str] | None = None
    failure: _Verdict | None = None
    try:
        for receipt, started_at in receipts.matching(tree, required_order=bool(prefix)):
            eligible, reason = verify_gate.evaluate_receipt(policy, receipt, tree, deadline=deadline)
            if reason == "policy_timeout":
                raise _Indeterminate(_TIMEOUT, "check_timeout")
            required = [
                command
                for command in receipt["commands"]
                if prefix and isinstance(command["argv"], list) and command["argv"][: len(prefix)] == prefix
            ]
            checked = [
                command for command in required if not policy.needs_arguments or len(command["argv"]) > len(prefix)
            ]
            failed = _failed_required_check(receipt, checked)
            if eligible and (required or not prefix) and not failed:
                if failure is None:
                    return _Verdict("ok")
                return replace(
                    failure,
                    reason=f"A newer run of the required check failed on this tree. An earlier run had passed. {failure.reason}",
                    reason_code=_MASKED_PASS,
                )
            if failure is not None:
                continue  # The remaining scan can only confirm whether an earlier pass existed.
            if eligible:
                reason = "no_tests_passed" if failed else "not_the_required_check"
            if failed:
                failure = _rejected(target, policy, receipt, reason, "The selected run of the required check")
            rank = (bool(required), started_at)
            if rejection is None or rank > rejection[0]:
                rejection = rank, receipt, reason
    except _Indeterminate:
        if failure is None:
            raise

    if failure is not None:
        return failure
    _remaining(deadline)
    if rejection is None:
        return _Verdict(
            "missing_receipt", "No local verification receipt matches this tree.", _remedy(target, policy, {})
        )
    (ran_required, _), receipt, reason = rejection
    run_label = "The most recent run of the required check" if ran_required else "The last run"
    return _rejected(target, policy, receipt, reason, run_label)


def _failed_required_check(receipt: dict[str, Any], commands: list[dict[str, Any]]) -> bool:
    """Recognize required command failure, timeout, or recorded test failures and errors."""
    if receipt.get("tree_stable") is False:
        return False
    for command in commands:
        outcomes = command.get("test_outcomes")
        status = command.get("status")
        if status is None and len(receipt["commands"]) == 1 and not receipt.get("bookkeeping_error"):
            status = receipt.get("status")  # Older receipts may only have the overall status.
        if isinstance(outcomes, dict):
            for count in (outcomes.get("failed"), outcomes.get("errors")):
                if type(count) is int and count > 0:
                    return True
            if outcomes.get("no_tests_ran") is True and status == "failed":
                continue
        if status in ("failed", "timed_out"):
            return True
    return False


def _rejected(
    target: Path,
    policy: verify_gate._GatePolicy,
    receipt: dict[str, Any],
    reason: str,
    run_label: str,
) -> _Verdict:
    explanation, _ = verify_gate._plan_guidance(receipt, reason)
    explanation = explanation or f"The last run has an ineligible verdict ({reason.replace('_', ' ')})."
    if explanation.startswith("The last run"):
        explanation = run_label + explanation.removeprefix("The last run")
    return _Verdict(
        "rejected",
        explanation,
        _remedy(target, policy, receipt),
    )


def _check(
    target: Path, updates: list[tuple[str, str]], ref: str | None, deadline: float, *, details: bool = False
) -> list[dict[str, Any]]:
    # Object reads in partial clones must stay offline even on Git versions
    # that do not recognize GIT_NO_LAZY_FETCH.
    git_env = {
        **verify_manifest_git.trusted_git_env(),
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    git = partial(verify_manifest_git._git, deadline=deadline, git_env=git_env)
    try:
        root = git(target, "rev-parse", "--show-toplevel")
        if root.returncode != 0:
            raise _Indeterminate("Git could not locate the worktree.")
        target = Path(os.fsdecode(root.stdout.rstrip(b"\r\n")))
        if ref is not None:
            resolved = git(target, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
            if resolved.returncode:
                raise ValueError("The supplied ref does not resolve to a local commit.")
            updates = [(ref, resolved.stdout.decode("ascii").strip())]
    except (subprocess.TimeoutExpired, _Indeterminate) as exc:
        verdict = (
            _Verdict("indeterminate", _TIMEOUT, reason_code="check_timeout")
            if isinstance(exc, subprocess.TimeoutExpired)
            else _Verdict("indeterminate", str(exc), reason_code=exc.reason_code)
        )
        if details:
            return [
                {**verdict.public(name), "tip": commit, "tree": None, "reason_code": verdict.reason_code}
                for name, commit in updates
            ]
        names: list[str | None] = [name for name, _ in updates] if updates else [ref]
        return [verdict.public(name) for name in names]

    receipts = _Receipts(target, deadline)
    by_commit: dict[str, _Verdict] = {}
    by_tree: dict[tuple[verify_gate._GatePolicy, str], _Verdict] = {}

    bases = verify_manifest_git._BaseResolver(target, deadline=deadline, git_env=git_env)

    @cache
    def committed_policy(commit: str) -> verify_gate._GatePolicy | None:
        return verify_gate._committed_policy(target, commit, deadline, git_env=git_env)

    @cache
    def read_tree(commit: str) -> str:
        resolved = git(target, "rev-parse", "--verify", f"{commit}^{{commit}}^{{tree}}")
        if resolved.returncode:
            raise _Indeterminate("Git could not read the pushed commit's tree.")
        return resolved.stdout.decode("ascii").strip()

    observed_trees: dict[str, str] = {}

    def check_tip(commit: str) -> _Verdict:
        _remaining(deadline)
        tree = None
        if details:
            tree = observed_trees[commit] = read_tree(commit)
        base = bases.resolve(commit)
        if base.timed_out:
            raise _Indeterminate(base.message or _TIMEOUT, "check_timeout")
        if base.status != "resolved" or base.commit is None:
            raise _Indeterminate(base.message or "The trusted Git base is unresolved.")
        if base.default_branch_tip is None:
            raise _Indeterminate("Git could not find a fetched default branch tip.")
        policy = committed_policy(base.commit)
        if policy is None:
            tip = base.default_branch_tip
            tip_policy = committed_policy(tip) if tip != base.commit else None
            if tip_policy is None:
                return _Verdict("not_applicable", tree=tree)
            if tip_policy.mode == "unresolved":
                raise _Indeterminate(verify_gate._policy_error(tip_policy))
            return _Verdict(
                "base_predates_policy",
                f"Update the branch from {base.reference} to pick up the required check.",
                tree=tree,
            )
        if policy.mode == "unresolved":
            raise _Indeterminate(verify_gate._policy_error(policy))
        try:
            tree = tree if tree is not None else read_tree(commit)
            key = (policy, tree)
            if key not in by_tree:
                by_tree[key] = _tree_verdict(target, policy, tree, receipts, deadline)
        except (subprocess.TimeoutExpired, _Indeterminate) as exc:
            reason = _TIMEOUT if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
            code = "check_timeout" if isinstance(exc, subprocess.TimeoutExpired) else exc.reason_code
            return _Verdict("indeterminate", reason, _remedy(target, policy, {}, fresh=True), code, tree)
        return replace(by_tree[key], tree=tree)

    results = []
    for name, commit in updates:
        if commit not in by_commit:
            try:
                by_commit[commit] = check_tip(commit)
            except subprocess.TimeoutExpired:
                by_commit[commit] = _Verdict("indeterminate", _TIMEOUT, reason_code="check_timeout")
            except _Indeterminate as exc:
                by_commit[commit] = _Verdict("indeterminate", str(exc), reason_code=exc.reason_code)
            except OSError as exc:
                by_commit[commit] = _Verdict("indeterminate", str(exc), reason_code="check_failed")
        verdict = by_commit[commit]
        result = verdict.public(name)
        if details:
            result.update(tip=commit, tree=verdict.tree or observed_trees.get(commit), reason_code=verdict.reason_code)
        results.append(result)
    return results


def push_check(*, target: Path, ref: str | None, json_output: bool) -> int:
    """Read Git pre-push updates or check a single local commit ref."""
    deadline = monotonic() + _BUDGET_SECONDS
    try:
        updates = branch_updates(_read_stdin(deadline)) if ref is None else []
        results = _check(target, updates, ref, deadline) if updates or ref is not None else []
    except ValueError as exc:
        print(f"push-check: {exc}", file=sys.stderr)
        return 2
    except (OSError, _Indeterminate) as exc:
        results = [_Verdict("indeterminate", str(exc)).public(ref)]

    if json_output:
        print(json.dumps({"refs": results}))
    else:
        for result in results:
            if result["status"] == "not_applicable":
                continue
            name = result["ref"] or "push-check"
            print(f"{name}: {result['status']}")
            for key in ("reason", "remedy"):
                if result[key]:
                    print(result[key])

    statuses = {result["status"] for result in results}
    if statuses & {"missing_receipt", "rejected"}:
        return 1
    return 3 if statuses & {"base_predates_policy", "indeterminate"} else 0
