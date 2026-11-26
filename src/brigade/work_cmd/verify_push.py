"""Report local verification evidence for the immutable tips in a Git push."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
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

from .. import config, localio, verify_manifest_git
from . import verify_gate
from .verify_push_input import INPUT_BYTES, branch_updates

_BUDGET_SECONDS = 2.0
_RECEIPT_LIMIT = 128
_RECEIPT_BYTES = 4 * 1024 * 1024
_TIMEOUT = "The push check reached its two-second time limit."
_RUN_NAME = re.compile(r"[0-9]{8}-[0-9]{6}-work-verify-[0-9a-f]{6}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_UNFINISHED = {"running", "busy", "canceled"}
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


@dataclass(frozen=True)
class _Candidate:
    receipt: dict[str, Any]
    started_at: datetime | None
    ran_required: bool
    eligible: bool
    failed: bool
    reason: str


class _Indeterminate(Exception):
    def __init__(self, message: str, reason_code: str | None = None, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.remedy = remedy


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _Indeterminate(_TIMEOUT, "check_timeout")
    return remaining


def _receipt_started_at(receipt: dict[str, Any]) -> datetime | None:
    try:
        return localio.parse_iso_datetime(receipt.get("started_at"))
    except OverflowError:
        return None


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


def _valid_oid(value: object) -> bool:
    return value is None or (isinstance(value, str) and _OBJECT_ID.fullmatch(value) is not None)


def _valid_command(command: object) -> bool:
    if not isinstance(command, dict):
        return False
    argv, status, outcomes = command.get("argv"), command.get("status"), command.get("test_outcomes")
    if argv is None and status != "rejected":
        return False
    if argv is not None and (not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv)):
        return False
    if status is not None and not isinstance(status, str):
        return False
    return outcomes is None or (
        isinstance(outcomes, dict)
        and all(
            type(value) is bool if key == "no_tests_ran" else type(value) is int and value >= 0
            for key, value in outcomes.items()
        )
    )


def _valid_receipt(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    status, commands = data.get("status"), data.get("commands")
    if not isinstance(status, str) or not isinstance(commands, list) or "tree_fingerprint" not in data:
        return False
    stamp = data.get("gating")
    if stamp is not None and not isinstance(stamp, dict):
        return False
    plan = stamp.get("plan") if stamp is not None else None
    if plan is not None and (not isinstance(plan, dict) or not _valid_oid(plan.get("snapshot_tree"))):
        return False
    return (
        _valid_oid(data["tree_fingerprint"])
        and (data.get("tree_stable") is None or type(data["tree_stable"]) is bool)
        and (status in _UNFINISHED or bool(commands))
        and all(_valid_command(command) for command in commands)
    )


def _name_second(name: str) -> datetime | None:
    if _RUN_NAME.fullmatch(name) is not None:
        try:
            return datetime.strptime(name[:15], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def _snapshot_tree(receipt: dict[str, Any]) -> str | None:
    return ((receipt.get("gating") or {}).get("plan") or {}).get("snapshot_tree")


def _move_out_command(path: Path) -> str:
    return f'mv -- {shlex.quote(str(path))} "$(mktemp -d)"'


class _Receipts:
    def __init__(self, target: Path, deadline: float) -> None:
        self.target = target
        self.root = target / ".brigade" / "work" / "verify-runs"
        self.deadline = deadline
        self._cap_error: _Indeterminate | None = None

    def _names(self) -> Iterator[str]:
        try:
            if not stat.S_ISDIR(self.root.lstat().st_mode):
                raise _Indeterminate(
                    f"Verification receipt store {str(self.root)!r} must be a directory. "
                    "Move the symbolic link or other file and restore a real receipt directory.",
                    remedy=_move_out_command(self.root),
                )
            with os.scandir(self.root) as dirs:
                for entry in dirs:
                    _remaining(self.deadline)
                    if entry.is_symlink():
                        raise self._entry_problem(entry.name, "is a symbolic link")
                    if entry.is_dir(follow_symlinks=False):
                        try:
                            (self.root / entry.name / "receipt.json").lstat()
                        except FileNotFoundError:
                            continue  # A run that never wrote a receipt contributes no evidence.
                        yield entry.name
        except FileNotFoundError:
            return

    def _entry_problem(self, name: str, description: str, *, receipt: bool = False) -> _Indeterminate:
        directory = self.root / name
        entry = directory / "receipt.json" if receipt else directory
        return _Indeterminate(
            f"Verification entry {str(entry)!r} {description}. Move the run out of the receipt store.",
            remedy=_move_out_command(directory),
        )

    def inventory(self, *, limit: int | None = None) -> tuple[list[tuple[str, datetime]], list[str]]:
        named: list[tuple[str, datetime]] = []
        stray: list[str] = []
        for name in self._names():
            second = _name_second(name)
            if second is None:
                stray.append(name)
            elif limit is None or len(named) < limit:
                named.append((name, second))
        return named, stray

    @cached_property
    def entries(self) -> list[tuple[str, datetime | None, dict[str, Any] | _Indeterminate | None]]:
        if self._cap_error is not None:
            raise self._cap_error
        named, stray = self.inventory(limit=_RECEIPT_LIMIT + 1)
        entries: list[tuple[str, datetime | None, dict[str, Any] | _Indeterminate | None]] = []
        ignored = 0
        first_ignored = ""
        for name in stray:
            receipt = self._load(name)
            if isinstance(receipt, _Indeterminate):
                ignored += 1
                first_ignored = first_ignored or name
            elif receipt is not None:
                # Only the binding is needed to reject a matching stray directory.
                entries.append(
                    (
                        name,
                        None,
                        {
                            "tree_fingerprint": receipt["tree_fingerprint"],
                            "gating": {"plan": {"snapshot_tree": _snapshot_tree(receipt)}},
                        },
                    )
                )
        if ignored:
            print(
                f"push gate: ignoring malformed receipt in a non-run directory {str(self.root / first_ignored)!r} "
                f"({ignored} entr{'y' if ignored == 1 else 'ies'}).",
                file=sys.stderr,
            )
        if len(named) > _RECEIPT_LIMIT:
            try:
                _, archive = config.resolve_verify_archive(self.target)
            except (OSError, ValueError):
                archive = self.target / config.DEFAULT_VERIFY_ARCHIVE_DIR
            self._cap_error = _Indeterminate(
                f"The local receipt scan reached its limit of {_RECEIPT_LIMIT} in {str(self.root)!r}. "
                f"Set verify_runs_keep in {str(self.target / config.CONFIG_REL_PATH)!r} to at most {_RECEIPT_LIMIT} "
                f"and check that the verify archive {str(archive)!r} is writable and has space.",
                remedy=f"brigade work verify push-gate prune --target {shlex.quote(str(self.target))}",
            )
            raise self._cap_error
        entries.extend((name, second, self._load(name)) for name, second in sorted(named, reverse=True))
        return entries

    def _load(self, name: str) -> dict[str, Any] | _Indeterminate | None:
        _remaining(self.deadline)
        path = self.root / name / "receipt.json"
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                return self._entry_problem(name, "is not a regular file", receipt=True)
            with path.open("rb") as stream:
                _remaining(self.deadline)
                raw = stream.read(_RECEIPT_BYTES + 1)
            _remaining(self.deadline)
            if len(raw) > _RECEIPT_BYTES:
                return self._entry_problem(name, "exceeds the size limit", receipt=True)
            data = json.loads(raw)
        except FileNotFoundError:
            return None
        except (ValueError, RecursionError):
            _remaining(self.deadline)
            return self._entry_problem(name, "is malformed", receipt=True)
        except OSError:
            return self._entry_problem(name, "could not be read", receipt=True)
        valid = _valid_receipt(data)
        _remaining(self.deadline)
        if not valid:
            return self._entry_problem(name, "is malformed", receipt=True)
        return {
            **{
                key: data[key]
                for key in ("status", "tree_stable", "tree_fingerprint", "started_at", "gating")
                if key in data
            },
            "commands": [
                {key: record.get(key) for key in ("argv", "status", "test_outcomes")} for record in data["commands"]
            ],
        }

    def matching(self, tree: str) -> Iterator[Iterator[dict[str, Any]]]:
        entries = self.entries
        now = datetime.now(UTC)
        ordered: list[tuple[datetime, dict[str, Any] | _Indeterminate]] = []
        for name, second, receipt in entries:
            _remaining(self.deadline)
            if receipt is None:
                continue
            if isinstance(receipt, _Indeterminate):
                if second is None or second > now:
                    raise receipt
                ordered.append((second, receipt))
                continue
            if receipt["tree_fingerprint"] != tree or _snapshot_tree(receipt) not in (None, tree):
                continue
            started = _receipt_started_at(receipt)
            if second is None or started is None or started.replace(microsecond=0) != second:
                raise self._entry_problem(name, "has an invalid or mismatched start timestamp")
            if started > now:
                raise self._entry_problem(name, "has a start timestamp in the future")
            if receipt["status"] not in _UNFINISHED:
                ordered.append((second, receipt))

        for _, group in groupby(ordered, key=lambda entry: entry[0]):
            yield self._group(group)

    @staticmethod
    def _group(group: Iterator[tuple[datetime, dict[str, Any] | _Indeterminate]]) -> Iterator[dict[str, Any]]:
        for _, receipt in group:
            if isinstance(receipt, _Indeterminate):
                raise receipt
            yield receipt


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
        for group in receipts.matching(tree):
            candidates = (_classify_receipt(policy, prefix, tree, receipt, deadline) for receipt in group)
            ordered = _order_candidates(candidates) if prefix else list(candidates)
            for candidate in ordered:
                if candidate.eligible:
                    if failure is None:
                        return _Verdict("ok")
                    return replace(
                        failure,
                        reason="The most recent run of the required check failed. An earlier run had passed.",
                        reason_code=_MASKED_PASS,
                    )
                if failure is None:
                    if candidate.failed:
                        # The remaining scan can only confirm whether an earlier pass existed.
                        failure = _rejected(
                            target,
                            policy,
                            candidate.receipt,
                            candidate.reason,
                            "The selected run of the required check",
                        )
                    rank = (candidate.ran_required, candidate.started_at or _EARLIEST)
                    if rejection is None or rank > rejection[0]:
                        rejection = rank, candidate.receipt, candidate.reason
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


def _classify_receipt(
    policy: verify_gate._GatePolicy, prefix: list[str], tree: str, receipt: dict[str, Any], deadline: float
) -> _Candidate:
    eligible, reason = verify_gate.evaluate_receipt(policy, receipt, tree, deadline=deadline)
    if reason == "policy_timeout":
        raise _Indeterminate(_TIMEOUT, "check_timeout")
    required = [
        command
        for command in receipt["commands"]
        if prefix and isinstance(command["argv"], list) and command["argv"][: len(prefix)] == prefix
    ]
    checked = [command for command in required if not policy.needs_arguments or len(command["argv"]) > len(prefix)]
    failed = _failed_required_check(receipt, checked)
    if eligible and failed:
        eligible, reason = False, "no_tests_passed"
    if eligible and prefix and not required:
        eligible, reason = False, "not_the_required_check"
    return _Candidate(receipt, _receipt_started_at(receipt), bool(required), eligible, failed, reason)


def _order_candidates(candidates: Iterator[_Candidate]) -> list[_Candidate]:
    """Order one second of runs without using run ID suffixes to break a pass/failure tie."""
    group = list(candidates)
    passes = {item.started_at for item in group if item.eligible}
    failures = {item.started_at for item in group if item.failed}
    if passes and failures and (None in passes or None in failures or passes & failures):
        raise _Indeterminate(
            "The push check could not order a pass and failure in the same second. "
            "Run verification for the pushed tree again."
        )
    return sorted(group, key=lambda item: item.started_at or _EARLIEST, reverse=True)


def _failed_required_check(receipt: dict[str, Any], commands: list[dict[str, Any]]) -> bool:
    """Recognize required command failure, timeout, or recorded test failures and errors."""
    for command in commands:
        outcomes = command.get("test_outcomes")
        status = command.get("status")
        if status is None:
            status = receipt.get("status")  # Older receipts may only have the overall status.
        if isinstance(outcomes, dict):
            for count in (outcomes.get("failed"), outcomes.get("errors")):
                if type(count) is int and count > 0:
                    return True
            if outcomes.get("no_tests_ran") is True and status in {"failed", "timed_out"}:
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
            key = (policy, tree)
            if key not in by_tree:
                by_tree[key] = _tree_verdict(target, policy, tree, receipts, deadline)
        except (subprocess.TimeoutExpired, _Indeterminate) as exc:
            reason = _TIMEOUT if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
            code = "check_timeout" if isinstance(exc, subprocess.TimeoutExpired) else exc.reason_code
            recovery = exc.remedy if isinstance(exc, _Indeterminate) else None
            return _Verdict("indeterminate", reason, recovery or _remedy(target, policy, {}, fresh=True), code, tree)
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
